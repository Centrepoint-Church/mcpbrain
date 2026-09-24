"""Daemon-side entity resources (Task 10)."""
import concurrent.futures
import json
import urllib.error
import urllib.request

import pytest

from mcpbrain import entity_resource as er
from mcpbrain import graph_view
from mcpbrain import graph_write as gw
from mcpbrain.control_api import ControlServer
from mcpbrain.control_client import ControlClient
from mcpbrain.store import Store


def _store(tmp_path):
    s = Store(tmp_path / "g.sqlite3", dim=4)
    s.init()
    with s._connect(write=True) as db:
        rows = [("dana-okafor", "Dana Okafor", "person", "Northgate Trust", 30),
                ("northgate-trust", "Northgate Trust", "org", "", 50),
                ("topic-budget", "budget", "topic", "", 99),
                ("hidden", "Hidden Person", "person", "", 80),
                ("marcus-reyes", "Marcus Reyes", "person", "Northgate Trust", 5)]
        for eid, name, typ, org, deg in rows:
            db.execute("INSERT INTO entities(id,name,type,org,degree) VALUES(?,?,?,?,?)",
                       (eid, name, typ, org, deg))
        db.execute("INSERT INTO entity_suppressions(entity_id,reason) VALUES('hidden','junk')")
    gw.upsert_relation(s, "dana-okafor", "works_at", "northgate-trust", valid_from="2026-01-01")
    gw.upsert_relation(s, "marcus-reyes", "reports_to", "dana-okafor", valid_from="2026-01-01")
    return s


def test_top_entities_ranks_by_degree_and_filters(tmp_path):
    s = _store(tmp_path)
    ids = [e["id"] for e in er.top_entities(s, today="2026-09-24")]
    assert ids == ["northgate-trust", "dana-okafor", "marcus-reyes"]  # no topic, no suppressed


def test_top_entities_is_cached_per_day(tmp_path):
    s = _store(tmp_path)
    first = er.top_entities(s, today="2026-09-24")
    with s._connect(write=True) as db:
        db.execute("UPDATE entities SET degree=999 WHERE id='marcus-reyes'")
    assert er.top_entities(s, today="2026-09-24") == first
    assert er.top_entities(s, today="2026-09-25")[0]["id"] == "marcus-reyes"


def test_render_markdown_has_header_role_and_grouped_relations(tmp_path):
    s = _store(tmp_path)
    gw.write_role_observation(s, "dana-okafor", "Operations Lead", "manual", "2026-01-01", "high")
    out = er.render_markdown(s, "dana-okafor")
    md = out["markdown"]
    assert md.startswith("# Dana Okafor")
    assert "person, Northgate Trust" in md and "Operations Lead" in md
    assert "## works_at" in md and "Northgate Trust (northgate-trust)" in md
    assert "## reports_to" in md and "Marcus Reyes (marcus-reyes)" in md


def test_merged_away_id_resolves_to_survivor(tmp_path):
    s = _store(tmp_path)
    with s._connect(write=True) as db:
        db.execute("INSERT INTO entities(id,name,type) VALUES('d-okafor','D Okafor','person')")
    s.merge_entities("d-okafor", "dana-okafor")
    assert er.resolve_id(s, "d-okafor") == ("dana-okafor", "d-okafor")
    out = er.render_markdown(s, "d-okafor")
    assert out["id"] == "dana-okafor" and "Merged from d-okafor" in out["markdown"]


def test_unknown_and_suppressed_render_none(tmp_path):
    s = _store(tmp_path)
    assert er.render_markdown(s, "nobody") is None
    assert er.render_markdown(s, "hidden") is None


def test_merge_chain_ending_in_suppressed_survivor_renders_none(tmp_path):
    """X -> Y -> Z (two merge hops), Z itself suppressed: resolve_id must walk
    both hops to find the live survivor, THEN honour the suppression on it."""
    s = _store(tmp_path)
    with s._connect(write=True) as db:
        db.execute("INSERT INTO entities(id,name,type) VALUES('x','X','person')")
        db.execute("INSERT INTO entities(id,name,type) VALUES('y','Y','person')")
        db.execute("INSERT INTO entities(id,name,type) VALUES('z','Z','person')")
    s.merge_entities("x", "y")
    s.merge_entities("y", "z")
    with s._connect(write=True) as db:
        db.execute("INSERT INTO entity_suppressions(entity_id,reason) VALUES('z','junk')")
    assert er.resolve_id(s, "x") is None
    assert er.render_markdown(s, "x") is None


def test_top_entities_thread_safe_under_concurrent_alternating_today(tmp_path):
    """Hammers top_entities from several threads with alternating `today`
    values (as the daemon's ThreadingHTTPServer would). The old check-then-act
    cache (`if key not in _cache: _cache.clear(); ...`) could KeyError when
    one thread's clear() lands between another thread's write and read."""
    s = _store(tmp_path)
    days = ["2026-09-24", "2026-09-25", "2026-09-26", "2026-09-27"]
    errors: list[Exception] = []

    def worker(i):
        try:
            for _ in range(50):
                er.top_entities(s, today=days[i % len(days)])
        except Exception as exc:
            errors.append(exc)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(worker, range(16)))
    assert errors == []


def test_relation_groups_are_bounded(tmp_path, monkeypatch):
    s = _store(tmp_path)
    monkeypatch.setattr(er, "MAX_RELATIONS", 1)
    md = er.render_markdown(s, "dana-okafor")["markdown"]
    assert "more relations not shown" in md


# ---- control-API routes (same style as Task 8/test_control_api_corrections.py) ----

class _FakeDaemon:
    def status(self):
        return {"paused": False, "chunk_count": 0, "google_connected": False,
                "granted_scopes": [], "enrich_enabled": False}


def _request(srv, path, *, authed=True):
    req = urllib.request.Request(f"http://127.0.0.1:{srv.port}{path}")
    if authed:
        req.add_header("Authorization", f"Bearer {srv.token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (json.loads(raw) if raw else {})


@pytest.fixture
def api_server(tmp_path):
    store = _store(tmp_path)
    srv = ControlServer(_FakeDaemon(), home=str(tmp_path), store=store)
    srv.start()
    try:
        yield srv
    finally:
        srv.stop()


def test_get_resources_entities_returns_top_list(api_server):
    status, body = _request(api_server, "/api/resources/entities")
    assert status == 200
    ids = [e["id"] for e in body["entities"]]
    assert ids == ["northgate-trust", "dana-okafor", "marcus-reyes"]


def test_get_resources_entity_returns_markdown(api_server):
    status, body = _request(api_server, "/api/resources/entity/dana-okafor")
    assert status == 200
    assert body["id"] == "dana-okafor"
    assert body["markdown"].startswith("# Dana Okafor")


def test_get_resources_entity_unknown_is_404(api_server):
    status, _ = _request(api_server, "/api/resources/entity/nobody")
    assert status == 404


def test_get_resources_reply_needed(api_server):
    status, body = _request(api_server, "/api/resources/reply-needed?q=")
    assert status == 200
    assert body["ids"] == []


def test_resources_routes_require_the_token(api_server):
    assert _request(api_server, "/api/resources/entities", authed=False)[0] == 401
    assert _request(api_server, "/api/resources/entity/dana-okafor", authed=False)[0] == 401
    assert _request(api_server, "/api/resources/reply-needed?q=", authed=False)[0] == 401


def test_resources_routes_wrap_store_errors_as_500(api_server, monkeypatch):
    """A raise inside entity_resource.* must not drop the connection -- same
    log.exception + h_json 500 contract as /api/dashboard/today and
    /api/graph/canvas."""
    def boom(*a, **kw):
        raise RuntimeError("store exploded")
    monkeypatch.setattr(er, "top_entities", boom)
    monkeypatch.setattr(er, "render_markdown", boom)
    monkeypatch.setattr(er, "reply_needed_ids", boom)

    status, body = _request(api_server, "/api/resources/entities")
    assert status == 500 and "store exploded" in body["error"]

    status, body = _request(api_server, "/api/resources/entity/dana-okafor")
    assert status == 500 and "store exploded" in body["error"]

    status, body = _request(api_server, "/api/resources/reply-needed?q=")
    assert status == 500 and "store exploded" in body["error"]


# ---- ControlClient methods (same style as test_control_client.py) ----

def test_control_client_entity_resources_round_trip(tmp_path):
    store = _store(tmp_path)
    srv = ControlServer(_FakeDaemon(), home=str(tmp_path), store=store)
    srv.start()
    try:
        c = ControlClient(home=str(tmp_path))
        ids = [e["id"] for e in c.entity_resources()]
        assert ids == ["northgate-trust", "dana-okafor", "marcus-reyes"]

        out = c.entity_resource("dana-okafor")
        assert out["id"] == "dana-okafor"
        assert out["markdown"].startswith("# Dana Okafor")

        assert c.entity_resource("nobody") is None
        assert c.reply_needed("") == []
        assert c.search_entities("Dana") == graph_view.search_entities(store, "Dana")
    finally:
        srv.stop()
