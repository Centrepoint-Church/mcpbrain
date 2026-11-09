"""Tests for the daemon/plugin stats payload behind the redesigned dashboard.

Covers dashboard.stats() (pure over a store + a status snapshot) and the
GET /api/dashboard/stats route wiring.
"""
import json
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from mcpbrain import dashboard
from mcpbrain.control_api import ControlServer


def _seed(path, *, chunks=(), entities=(), relations=(), payload_at=()):
    """Seed chunks, graph tables and enrich_payloads.

    chunks: (enrich_state, enriched) tuples. entities: (id, type) tuples.
    relations: (a, b) tuples. payload_at: `at` strings for enrich_payloads.
    """
    with sqlite3.connect(str(path)) as db:
        db.execute("CREATE TABLE chunks(rowid INTEGER PRIMARY KEY, enrich_state TEXT, enriched INTEGER)")
        db.execute("CREATE TABLE entities(id TEXT PRIMARY KEY, name TEXT, type TEXT)")
        db.execute("CREATE TABLE entity_relations(id INTEGER PRIMARY KEY, entity_a TEXT, "
                   "entity_b TEXT, invalidated_at TEXT)")
        db.execute("CREATE TABLE entity_observations(id INTEGER PRIMARY KEY, entity_id TEXT, "
                   "invalidated_at TEXT)")
        db.execute("CREATE TABLE sync_cursors(source TEXT PRIMARY KEY, cursor TEXT, updated_at TEXT)")
        db.execute("CREATE TABLE enrich_payloads(file_id TEXT PRIMARY KEY, at TEXT)")
        db.executemany("INSERT INTO chunks(enrich_state, enriched) VALUES(?,?)", list(chunks))
        db.executemany("INSERT INTO entities VALUES(?,?,?)", [(i, i, t) for i, t in entities])
        db.executemany("INSERT INTO entity_relations(entity_a, entity_b) VALUES(?,?)", list(relations))
        db.executemany("INSERT INTO enrich_payloads VALUES(?,?)",
                       [(f"f{i}", at) for i, at in enumerate(payload_at)])


def _fresh_cursors(path, hours_ago=0.2):
    when = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(str(path)) as db:
        for src in ("gmail", "drive", "calendar", "anarlog"):
            db.execute("INSERT INTO sync_cursors VALUES(?,?,?)", (src, "x", when))


class _Store:
    def __init__(self, path, communities=0, corrections=0, corrections_error=False):
        self._path = str(path)
        self._communities = communities
        self._corrections = corrections
        self._corrections_error = corrections_error

    def list_communities(self):
        return [{"community_id": i} for i in range(self._communities)]

    def pending_corrections(self, limit=50):
        if self._corrections_error:
            raise RuntimeError("graph_corrections table missing")
        return [{"id": i} for i in range(self._corrections)]


def _status(**over):
    base = {
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
        "open_findings": 0,
        "stalled": None,
        "watchdog_exits": 0,
        "watchdog_limit_reached": False,
        "reflow": None,
    }
    base.update(over)
    return base


# --- dashboard.stats() --------------------------------------------------------

def test_stats_regression_153_percent(tmp_path):
    """56 cold-but-enriched chunks used to be added to the numerator only,
    giving 153%. 56 cold/enriched + 44 cold + 96 done + 4 waiting is 96%."""
    p = tmp_path / "b.sqlite3"
    _seed(p, chunks=[("cold", 1)] * 56 + [("cold", 0)] * 44 + [("", 1)] * 96 + [("", 0)] * 4)
    out = dashboard.stats(_Store(p), str(tmp_path), _status())
    assert out["index"] == {"total": 200, "eligible": 100, "cold": 100, "duplicate": 0,
                            "enriched_eligible": 96, "waiting": 4, "pct": 96}


def test_stats_top_level_keys_exact(tmp_path):
    p = tmp_path / "b.sqlite3"
    _seed(p)
    out = dashboard.stats(_Store(p), str(tmp_path), _status())
    assert list(out) == ["as_of", "index", "enrich", "graph", "sources", "health",
                         "needs_you", "connections", "system"]
    assert "spool" not in out and "daemon" not in out and "reflow" not in out
    assert "links_per_entity" not in out["graph"]
    assert out["system"] == {"version": "0.7.77", "paused": False,
                             "watchdog_exits": 0, "reflow": None}


def test_stats_graph_counts_are_knowledge_not_bookkeeping(tmp_path):
    p = tmp_path / "b.sqlite3"
    _seed(p, entities=[("acme-a", "person"), ("acme-b", "org"), ("acme-t", "thread"),
                       ("acme-u", None)], relations=[("acme-a", "acme-b")])
    out = dashboard.stats(_Store(p, communities=7), str(tmp_path), _status())
    g = out["graph"]
    assert g["by_type"]["person"] == 1 and g["by_type"]["thread"] == 1
    assert g["by_type"]["unknown"] == 1
    assert g["relations_live"] == 1
    assert g["unlinked"] == 2
    assert g["circles"] == 7


def test_stats_meetings_is_the_fourth_source(tmp_path):
    p = tmp_path / "b.sqlite3"
    _seed(p)
    _fresh_cursors(p)
    out = dashboard.stats(_Store(p), str(tmp_path), _status())
    srcs = out["sources"]
    assert [s["name"] for s in srcs] == ["gmail", "drive", "calendar", "anarlog"]
    assert srcs[3]["label"] == "Meetings"
    assert srcs[0]["last_synced_at"] is not None
    assert srcs[0]["history"] == {"done": False, "reached": "2019-03-01T00:00:00+00:00"}
    assert srcs[1]["shared"] == {"total": 0, "stale": 0}
    assert "shared" not in srcs[0]
    assert [s["stale"] for s in srcs] == [False, False, False, False]


def test_stats_source_stale_flag(tmp_path):
    import json
    # Meetings (anarlog) is opt-in; a switched-off source is never stale.
    (tmp_path / "config.json").write_text(json.dumps({"anarlog": {"enabled": True}}))
    p = tmp_path / "b.sqlite3"
    _seed(p)
    _fresh_cursors(p, hours_ago=7.5)
    out = dashboard.stats(_Store(p), str(tmp_path), _status())
    assert all(s["stale"] is True and s["enabled"] is True for s in out["sources"])
    q = tmp_path / "c.sqlite3"
    _seed(q)
    out = dashboard.stats(_Store(q), str(tmp_path), _status())  # never synced
    assert all(s["stale"] is False and s["last_synced_at"] is None for s in out["sources"])


def test_stats_stale_shared_drive_does_not_touch_base_drive(tmp_path):
    p = tmp_path / "b.sqlite3"
    _seed(p)
    _fresh_cursors(p)
    old = (datetime.now(timezone.utc) - timedelta(days=120)).strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(str(p)) as db:
        db.execute("INSERT INTO sync_cursors VALUES('drive:0AFAKE','t',?)", (old,))
    out = dashboard.stats(_Store(p), str(tmp_path), _status())
    drive = out["sources"][1]
    assert out["health"]["state"] != "stalled"
    assert drive["last_synced_at"] is not None and drive["stale"] is False
    assert drive["shared"] == {"total": 1, "stale": 1}


def test_stats_degrades_when_store_is_unreadable(tmp_path):
    p = tmp_path / "junk.sqlite3"
    p.write_bytes(b"this is not a sqlite database" * 50)
    out = dashboard.stats(_Store(p), str(tmp_path), _status())
    assert list(out) == ["as_of", "index", "enrich", "graph", "sources", "health",
                         "needs_you", "connections", "system"]
    assert out["index"]["total"] == 0 and out["index"]["pct"] == 0
    assert len(out["graph"]["by_type"]) == 9 and out["graph"]["unlinked"] == 0
    assert all(s["last_synced_at"] is None for s in out["sources"])
    assert out["enrich"]["last_push_at"] is None and out["enrich"]["pushes_24h"] == 0
    missing = dashboard.stats(_Store(tmp_path / "nope.sqlite3"), str(tmp_path), _status())
    assert missing["index"]["total"] == 0


def test_stats_reports_a_starved_queue(tmp_path):
    p = tmp_path / "b.sqlite3"
    _seed(p, chunks=[("", 0)] * 5)
    out = dashboard.stats(_Store(p), str(tmp_path), _status(spool={"pending": 0, "inbox": 0}))
    assert out["enrich"]["queue_state"] == "starved"
    assert out["enrich"]["units_pending"] == 0
    assert out["index"]["waiting"] == 5
    assert out["health"]["state"] == "behind"


def test_stats_queue_working_and_idle(tmp_path):
    p = tmp_path / "b.sqlite3"
    _seed(p, chunks=[("", 0)])
    out = dashboard.stats(_Store(p), str(tmp_path), _status())
    assert out["enrich"]["queue_state"] == "working"
    p2 = tmp_path / "c.sqlite3"
    _seed(p2, chunks=[("", 1)])
    out = dashboard.stats(_Store(p2), str(tmp_path), _status(spool={"pending": 0}))
    assert out["enrich"]["queue_state"] == "idle"


def test_stats_enrich_activity(tmp_path):
    p = tmp_path / "b.sqlite3"
    recent = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    _seed(p, payload_at=[recent, "2020-01-01 00:00:00"])
    out = dashboard.stats(_Store(p), str(tmp_path), _status())
    assert out["enrich"]["pushes_24h"] == 1
    assert out["enrich"]["last_push_at"].startswith(recent[:10])


def test_stats_pending_corrections_failure_degrades_to_zero(tmp_path):
    p = tmp_path / "b.sqlite3"
    _seed(p)
    out = dashboard.stats(_Store(p, corrections_error=True), str(tmp_path), _status())
    assert out["needs_you"]["reviews"] == 0


def test_stats_needs_you_and_health(tmp_path):
    p = tmp_path / "b.sqlite3"
    _seed(p)
    _fresh_cursors(p)
    status = _status(open_findings=2)
    status["connections"]["google"] = {"state": "needs_action", "detail": "Reconnect"}
    out = dashboard.stats(_Store(p, corrections=3), str(tmp_path), status)
    ny = out["needs_you"]
    assert ny["findings"] == 2 and ny["reviews"] == 3
    assert [c["name"] for c in ny["connections"]] == ["google"]
    assert out["health"]["state"] == "needs_you"


def test_stats_stale_source_is_stalled(tmp_path):
    p = tmp_path / "b.sqlite3"
    _seed(p)
    _fresh_cursors(p, hours_ago=7.5)
    out = dashboard.stats(_Store(p), str(tmp_path), _status())
    assert out["health"]["state"] == "stalled"
    assert "Gmail has not synced for 7 hours" in out["health"]["reasons"]


def test_stats_reflow_only_while_active(tmp_path):
    p = tmp_path / "b.sqlite3"
    _seed(p)
    done = {"halted": None, "queued": 0, "remaining": 0, "owners_done": 9}
    out = dashboard.stats(_Store(p), str(tmp_path), _status(reflow=done))
    assert out["system"]["reflow"] is None
    halted = dict(done, halted="integrity")
    out = dashboard.stats(_Store(p), str(tmp_path), _status(reflow=halted))
    assert out["system"]["reflow"] == halted


def test_stats_connections_ordered_and_no_clickup(tmp_path):
    p = tmp_path / "b.sqlite3"
    _seed(p)
    out = dashboard.stats(_Store(p), str(tmp_path), _status())
    names = [c["name"] for c in out["connections"]]
    assert names == ["google", "claude", "backup", "records", "enrichment"]
    assert "clickup" not in names
    assert out["connections"][0] == {"name": "google", "label": "Google",
                                      "state": "ok", "detail": "Connected"}


def test_stats_degrades_on_empty_store(tmp_path):
    p = tmp_path / "empty.sqlite3"
    with sqlite3.connect(str(p)):
        pass
    out = dashboard.stats(_Store(p), str(tmp_path), _status())
    assert out["index"]["pct"] == 0
    assert out["graph"]["by_type"]["person"] == 0
    assert out["health"]["state"] == "healthy"


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
    _seed(p, entities=[(f"acme-{i}", "person") for i in range(10)])
    srv = ControlServer(_RouteDaemon(), str(tmp_path), store=_Store(p, communities=3))
    srv.start()
    try:
        code, body = _get(f"http://127.0.0.1:{srv.port}/api/dashboard/stats", srv.token)
        assert code == 200
        assert list(body) == ["as_of", "index", "enrich", "graph", "sources", "health",
                              "needs_you", "connections", "system"]
        assert body["graph"]["by_type"]["person"] == 10
        assert body["graph"]["circles"] == 3
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
