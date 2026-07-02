"""Tests for the daemon/plugin stats payload behind the redesigned dashboard.

Covers dashboard.stats() (pure over a store + a status snapshot) and the
GET /api/dashboard/stats route wiring.
"""
import json
import sqlite3
import urllib.error
import urllib.request

from mcpbrain import dashboard
from mcpbrain.control_api import ControlServer


def _seed(path, *, entities=0, relations=0, observations=0, cold=0, warm=0):
    """Seed the graph tables + chunks so _graph_counts has rows to count."""
    with sqlite3.connect(str(path)) as db:
        db.execute("CREATE TABLE entities(id INTEGER PRIMARY KEY)")
        db.execute("CREATE TABLE entity_relations(id INTEGER PRIMARY KEY)")
        db.execute("CREATE TABLE entity_observations(id INTEGER PRIMARY KEY)")
        db.execute("CREATE TABLE chunks(rowid INTEGER PRIMARY KEY, enrich_state TEXT)")
        for _ in range(entities):
            db.execute("INSERT INTO entities DEFAULT VALUES")
        for _ in range(relations):
            db.execute("INSERT INTO entity_relations DEFAULT VALUES")
        for _ in range(observations):
            db.execute("INSERT INTO entity_observations DEFAULT VALUES")
        for _ in range(cold):
            db.execute("INSERT INTO chunks(enrich_state) VALUES('cold')")
        for _ in range(warm):
            db.execute("INSERT INTO chunks(enrich_state) VALUES('')")


class _Store:
    def __init__(self, path, communities=0):
        self._path = str(path)
        self._communities = communities

    def list_communities(self):
        return [{"community_id": i} for i in range(self._communities)]


def _status(**over):
    base = {
        "chunk_count": 100,
        "enriched_count": 61,
        "spool": {"pending": 1, "inbox": 3},
        "backfill": {
            "gmail": {"reached": "2019-03-01T00:00:00+00:00", "done": False},
            "drive": {"reached": None, "done": True},
            "calendar": {"reached": None, "done": True},
        },
        "connections": {
            "google": {"state": "ok", "detail": "Connected"},
            "claude": {"state": "not_started", "detail": "Plugin not connected"},
            "backup": {"state": "ok", "detail": "On"},
            "records": {"state": "ok", "detail": "Ready"},
            "enrichment": {"state": "ok", "detail": "Running"},
        },
        "paused": False,
        "version": "0.7.77",
        "enrich_enabled": True,
        "open_findings": 2,
    }
    base.update(over)
    return base


# --- dashboard.stats() --------------------------------------------------------

def test_stats_index_and_graph_counts(tmp_path):
    p = tmp_path / "b.sqlite3"
    _seed(p, entities=50, relations=125, observations=90, cold=12, warm=88)
    out = dashboard.stats(_Store(p, communities=7), str(tmp_path), _status())

    assert out["index"] == {"indexed": 100, "enriched": 61, "cold": 12, "enriched_pct": 61}
    assert out["graph"]["entities"] == 50
    assert out["graph"]["relations"] == 125
    assert out["graph"]["observations"] == 90
    assert out["graph"]["communities"] == 7
    assert out["graph"]["links_per_entity"] == 2.5  # 125 / 50


def test_stats_sources_from_backfill(tmp_path):
    p = tmp_path / "b.sqlite3"
    _seed(p)
    out = dashboard.stats(_Store(p), str(tmp_path), _status())
    srcs = {s["name"]: s for s in out["sources"]}
    assert [s["name"] for s in out["sources"]] == ["gmail", "drive", "calendar"]
    assert srcs["gmail"] == {"name": "gmail", "label": "Gmail",
                             "reached": "2019-03-01T00:00:00+00:00", "done": False}
    assert srcs["drive"]["done"] is True


def test_stats_connections_ordered_and_no_clickup(tmp_path):
    p = tmp_path / "b.sqlite3"
    _seed(p)
    out = dashboard.stats(_Store(p), str(tmp_path), _status())
    names = [c["name"] for c in out["connections"]]
    assert names == ["google", "claude", "backup", "records", "enrichment"]
    assert "clickup" not in names
    assert out["connections"][0] == {"name": "google", "label": "Google",
                                      "state": "ok", "detail": "Connected"}


def test_stats_daemon_and_spool_passthrough(tmp_path):
    p = tmp_path / "b.sqlite3"
    _seed(p)
    out = dashboard.stats(_Store(p), str(tmp_path), _status(paused=True, open_findings=5))
    assert out["daemon"] == {"paused": True, "version": "0.7.77",
                             "enrich_enabled": True, "open_findings": 5}
    assert out["spool"] == {"pending": 1, "inbox": 3}


def test_stats_degrades_on_empty_store(tmp_path):
    # No graph tables at all — every count degrades to 0, pct guards div-by-zero.
    p = tmp_path / "empty.sqlite3"
    with sqlite3.connect(str(p)):
        pass
    out = dashboard.stats(_Store(p), str(tmp_path),
                          _status(chunk_count=0, enriched_count=0))
    assert out["index"]["enriched_pct"] == 0
    assert out["graph"]["entities"] == 0
    assert out["graph"]["links_per_entity"] == 0.0


# --- GET /api/dashboard/stats route ------------------------------------------

class _RouteDaemon:
    def status(self):
        return _status()


def _get(url, token):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, json.loads(r.read())


def test_stats_route_returns_payload(tmp_path):
    p = tmp_path / "b.sqlite3"
    _seed(p, entities=10, relations=20, observations=5, cold=2, warm=8)
    srv = ControlServer(_RouteDaemon(), str(tmp_path), store=_Store(p, communities=3))
    srv.start()
    try:
        code, body = _get(f"http://127.0.0.1:{srv.port}/api/dashboard/stats", srv.token)
        assert code == 200
        assert set(body) >= {"index", "graph", "sources", "spool", "connections",
                             "daemon", "as_of"}
        assert body["graph"]["entities"] == 10
        assert body["graph"]["communities"] == 3
        assert [c["name"] for c in body["connections"]] == \
            ["google", "claude", "backup", "records", "enrichment"]
    finally:
        srv.stop()


def test_stats_route_503_without_store(tmp_path):
    srv = ControlServer(_RouteDaemon(), str(tmp_path), store=None)
    srv.start()
    try:
        try:
            _get(f"http://127.0.0.1:{srv.port}/api/dashboard/stats", srv.token)
            assert False, "expected HTTP 503"
        except urllib.error.HTTPError as e:
            assert e.code == 503
    finally:
        srv.stop()
