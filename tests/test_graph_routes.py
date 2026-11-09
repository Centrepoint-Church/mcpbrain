import json
import sqlite3
import urllib.error
import urllib.request

from mcpbrain.control_api import ControlServer


class _Store:
    def __init__(self, path):
        self._path = str(path)


class _Daemon:
    def status(self):
        return {"paused": False}


def _seed(path, n_entities=3):
    with sqlite3.connect(str(path)) as db:
        db.execute("CREATE TABLE entities(id TEXT PRIMARY KEY, name TEXT, type TEXT, "
                   "org TEXT DEFAULT '', first_seen TEXT DEFAULT '', last_seen TEXT DEFAULT '', "
                   "email_count INTEGER DEFAULT 0, email_addr TEXT DEFAULT '', degree INTEGER DEFAULT 0)")
        db.execute("CREATE TABLE entity_relations(id INTEGER PRIMARY KEY, entity_a TEXT, "
                   "relation TEXT, entity_b TEXT, strength REAL DEFAULT 1)")
        db.execute("CREATE TABLE entity_suppressions(entity_id TEXT PRIMARY KEY, reason TEXT, suppressed_at TEXT)")
        db.execute("CREATE TABLE entity_communities(entity_id TEXT, community_id INTEGER, level INTEGER)")
        db.execute("CREATE TABLE community_summaries(community_id INTEGER, level INTEGER, title TEXT, "
                   "summary TEXT, member_count INTEGER, key_entities TEXT, updated TEXT)")
        for i in range(n_entities):
            db.execute("INSERT INTO entities(id,name,type,degree) VALUES(?,?,?,?)",
                       (f"e{i}", f"N{i}", "person", 9))


def _get(url, token):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, json.loads(r.read())


def test_canvas_route_returns_nodes(tmp_path):
    p = tmp_path / "b.sqlite3"; _seed(p)
    srv = ControlServer(_Daemon(), str(tmp_path), store=_Store(p)); srv.start()
    try:
        code, body = _get(f"http://127.0.0.1:{srv.port}/api/graph/canvas?min_conn=1", srv.token)
        assert code == 200
        assert len(body["nodes"]) == 3
        assert set(body) == {"nodes", "links", "communities"}
    finally:
        srv.stop()


def test_canvas_route_multi_type(tmp_path):
    """Repeated ?type= params union together — the multi-select type-chip contract."""
    p = tmp_path / "b.sqlite3"
    with sqlite3.connect(str(p)) as db:
        db.execute("CREATE TABLE entities(id TEXT PRIMARY KEY, name TEXT, type TEXT, "
                   "org TEXT DEFAULT '', first_seen TEXT DEFAULT '', last_seen TEXT DEFAULT '', "
                   "email_count INTEGER DEFAULT 0, email_addr TEXT DEFAULT '', degree INTEGER DEFAULT 0)")
        db.execute("CREATE TABLE entity_relations(id INTEGER PRIMARY KEY, entity_a TEXT, "
                   "relation TEXT, entity_b TEXT, strength REAL DEFAULT 1)")
        db.execute("CREATE TABLE entity_communities(entity_id TEXT, community_id INTEGER, level INTEGER)")
        db.execute("CREATE TABLE community_summaries(community_id INTEGER, level INTEGER, title TEXT, "
                   "summary TEXT, member_count INTEGER, key_entities TEXT, updated TEXT)")
        db.execute("INSERT INTO entities(id,name,type,degree) VALUES('p','Al','person',9)")
        db.execute("INSERT INTO entities(id,name,type,degree) VALUES('o','Acme','org',9)")
        db.execute("INSERT INTO entities(id,name,type,degree) VALUES('t','Budgets','topic',9)")
    srv = ControlServer(_Daemon(), str(tmp_path), store=_Store(p)); srv.start()
    try:
        code, body = _get(f"http://127.0.0.1:{srv.port}/api/graph/canvas?min_conn=1&type=person&type=org", srv.token)
        assert code == 200
        assert {n["id"] for n in body["nodes"]} == {"p", "o"}   # topic excluded, person+org unioned
    finally:
        srv.stop()


def test_canvas_route_413_when_too_large(tmp_path):
    p = tmp_path / "b.sqlite3"; _seed(p, n_entities=5001)
    srv = ControlServer(_Daemon(), str(tmp_path), store=_Store(p)); srv.start()
    try:
        try:
            _get(f"http://127.0.0.1:{srv.port}/api/graph/canvas?min_conn=1", srv.token)
            assert False, "expected 413"
        except urllib.error.HTTPError as e:
            assert e.code == 413
    finally:
        srv.stop()


def test_canvas_route_503_without_store(tmp_path):
    srv = ControlServer(_Daemon(), str(tmp_path), store=None); srv.start()
    try:
        try:
            _get(f"http://127.0.0.1:{srv.port}/api/graph/canvas", srv.token)
            assert False, "expected 503"
        except urllib.error.HTTPError as e:
            assert e.code == 503
    finally:
        srv.stop()


def test_canvas_route_requires_token(tmp_path):
    p = tmp_path / "b.sqlite3"; _seed(p)
    srv = ControlServer(_Daemon(), str(tmp_path), store=_Store(p)); srv.start()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{srv.port}/api/graph/canvas")
        try:
            urllib.request.urlopen(req, timeout=5)
            assert False, "expected 401"
        except urllib.error.HTTPError as e:
            assert e.code == 401
    finally:
        srv.stop()


def _req(url, token, method, body=None):
    import json, urllib.request
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Authorization": f"Bearer {token}",
                                        "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=5) as resp:
        return resp.status, json.loads(resp.read() or b"{}")

def _rw(tmp_path):
    from mcpbrain.store import Store
    s = Store(tmp_path / "rw.sqlite3", dim=4); s.init()
    with s._connect() as db:
        db.executemany("INSERT INTO entities(id,name,type,org,email_addr) VALUES(?,?,?,?,?)",
                       [("alice","Alice","person","Acme","a@x.com"),
                        ("al","Alice A.","person","","a@x.com")])
    return s

def test_entity_route(tmp_path):
    s = _rw(tmp_path); srv = ControlServer(_Daemon(), str(tmp_path), store=s); srv.start()
    try:
        code, body = _get(f"http://127.0.0.1:{srv.port}/api/graph/entity/alice", srv.token)
        assert code == 200 and body["name"] == "Alice"
        try:
            _get(f"http://127.0.0.1:{srv.port}/api/graph/entity/nope", srv.token); assert False
        except urllib.error.HTTPError as e: assert e.code == 404
    finally: srv.stop()

def test_search_route(tmp_path):
    s = _rw(tmp_path); srv = ControlServer(_Daemon(), str(tmp_path), store=s); srv.start()
    try:
        code, body = _get(f"http://127.0.0.1:{srv.port}/api/graph/search?q=alice", srv.token)
        assert code == 200 and any(r["id"] == "alice" for r in body)
    finally: srv.stop()

def test_update_route(tmp_path):
    s = _rw(tmp_path); srv = ControlServer(_Daemon(), str(tmp_path), store=s); srv.start()
    try:
        code, body = _req(f"http://127.0.0.1:{srv.port}/api/graph/entity/alice", srv.token,
                          "POST", {"org": "Beta"})
        assert code == 200 and body["org"] == "Beta"
    finally: srv.stop()

def test_merge_route_409_role_or_self(tmp_path):
    s = _rw(tmp_path); srv = ControlServer(_Daemon(), str(tmp_path), store=s); srv.start()
    try:
        try:
            _req(f"http://127.0.0.1:{srv.port}/api/graph/merge", srv.token, "POST",
                 {"loser_id": "alice", "winner_id": "alice"}); assert False
        except urllib.error.HTTPError as e: assert e.code == 409
    finally: srv.stop()

def test_delete_route(tmp_path):
    s = _rw(tmp_path); srv = ControlServer(_Daemon(), str(tmp_path), store=s); srv.start()
    try:
        code, body = _req(f"http://127.0.0.1:{srv.port}/api/graph/entity/al", srv.token, "DELETE")
        assert code == 200 and body["ok"] is True
    finally: srv.stop()

def test_merge_preview_route(tmp_path):
    s = _rw(tmp_path); srv = ControlServer(_Daemon(), str(tmp_path), store=s); srv.start()
    try:
        code, body = _get(f"http://127.0.0.1:{srv.port}/api/graph/merge/preview?loser=al&winner=alice", srv.token)
        assert code == 200 and body["ok"] and "result" in body
        assert s.get_entity("al") is not None   # preview didn't mutate
        try:
            _get(f"http://127.0.0.1:{srv.port}/api/graph/merge/preview?loser=alice&winner=alice", srv.token); assert False
        except urllib.error.HTTPError as e: assert e.code == 409   # self-merge guard
    finally: srv.stop()


def test_merge_route_role_inbox_409(tmp_path):
    from mcpbrain.store import Store
    s = Store(tmp_path / "ri.sqlite3", dim=4); s.init()
    with s._connect() as db:
        db.execute("INSERT INTO entities(id,name,type,email_addr) VALUES('office','Office','person','office@x.com')")
        db.execute("INSERT INTO entities(id,name,type,email_addr) VALUES('front','Front','person','office@x.com')")
    srv = ControlServer(_Daemon(), str(tmp_path), store=s); srv.start()
    try:
        try:
            _req(f"http://127.0.0.1:{srv.port}/api/graph/merge", srv.token, "POST",
                 {"loser_id": "front", "winner_id": "office"}); assert False
        except urllib.error.HTTPError as e: assert e.code == 409
    finally: srv.stop()


def test_ego_route(tmp_path):
    p = tmp_path / "b.sqlite3"
    with sqlite3.connect(str(p)) as db:
        db.execute("CREATE TABLE entities(id TEXT PRIMARY KEY, name TEXT, type TEXT, "
                   "org TEXT DEFAULT '', first_seen TEXT DEFAULT '', last_seen TEXT DEFAULT '', "
                   "email_count INTEGER DEFAULT 0, email_addr TEXT DEFAULT '', degree INTEGER DEFAULT 0)")
        db.execute("CREATE TABLE entity_relations(id INTEGER PRIMARY KEY, entity_a TEXT, "
                   "relation TEXT, entity_b TEXT, strength REAL DEFAULT 1)")
        db.execute("CREATE TABLE entity_communities(entity_id TEXT, community_id INTEGER, level INTEGER)")
        db.execute("CREATE TABLE community_summaries(community_id INTEGER, level INTEGER, title TEXT, "
                   "summary TEXT, member_count INTEGER, key_entities TEXT, updated TEXT)")
        db.execute("INSERT INTO entities(id,name,type,degree) VALUES('e1','Alice','person',1)")
        db.execute("INSERT INTO entities(id,name,type,degree) VALUES('e2','Bob','person',1)")
        db.execute("INSERT INTO entity_relations(id,entity_a,relation,entity_b,strength) "
                   "VALUES(0,'e1','knows','e2',4)")
    srv = ControlServer(_Daemon(), str(tmp_path), store=_Store(p)); srv.start()
    try:
        code, body = _get(f"http://127.0.0.1:{srv.port}/api/graph/ego?id=e1&hops=1", srv.token)
        assert code == 200
        assert {n["id"] for n in body["nodes"]} == {"e1", "e2"}   # degree 1 — canvas' default min_conn=7 would drop both
        try:
            _get(f"http://127.0.0.1:{srv.port}/api/graph/ego?id=nope", srv.token); assert False
        except urllib.error.HTTPError as e: assert e.code == 404
        try:
            _get(f"http://127.0.0.1:{srv.port}/api/graph/ego", srv.token); assert False
        except urllib.error.HTTPError as e: assert e.code == 400
    finally: srv.stop()


def test_graph_routes_503_without_store(tmp_path):
    srv = ControlServer(_Daemon(), str(tmp_path), store=None); srv.start()
    calls = [("GET", f"http://127.0.0.1:{srv.port}/api/graph/entity/x", None),
             ("GET", f"http://127.0.0.1:{srv.port}/api/graph/search?q=x", None),
             ("GET", f"http://127.0.0.1:{srv.port}/api/graph/ego?id=x", None),
             ("POST", f"http://127.0.0.1:{srv.port}/api/graph/entity/x", {"org": "Y"}),
             ("POST", f"http://127.0.0.1:{srv.port}/api/graph/merge", {"loser_id": "a", "winner_id": "b"}),
             ("DELETE", f"http://127.0.0.1:{srv.port}/api/graph/entity/x", None)]
    try:
        for m, url, body in calls:
            try:
                _get(url, srv.token) if m == "GET" else _req(url, srv.token, m, body)
                assert False, f"expected 503 for {m} {url}"
            except urllib.error.HTTPError as e:
                assert e.code == 503, f"{m} {url} -> {e.code}"
    finally: srv.stop()


# --- hidden entities: list and restore (Settings > Hidden entities) -------------

def _real_store(tmp_path):
    from mcpbrain.store import Store
    s = Store(tmp_path / "brain.sqlite3", dim=4)
    s.init()
    return s


def _post(url, token, body=None):
    req = urllib.request.Request(url, data=json.dumps(body or {}).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {token}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_list_suppressed_names_each_hidden_entity(tmp_path):
    from mcpbrain import graph_view
    s = _real_store(tmp_path)
    s.upsert_entity("p-dana", "Dana Okafor", "person")
    s.upsert_entity("o-harbour", "Harbour Trust", "org")
    s.upsert_entity("p-sam", "Sam Ito", "person")
    s.suppress_entity("p-dana", reason="graph-ui")
    s.suppress_entity("o-harbour", reason="No links to anything")
    out = graph_view.list_suppressed(s)
    assert {r["id"] for r in out} == {"p-dana", "o-harbour"}
    row = next(r for r in out if r["id"] == "p-dana")
    assert row["name"] == "Dana Okafor" and row["type"] == "person"
    assert row["reason"] == "graph-ui" and row["suppressed_at"]
    assert set(row) == {"id", "name", "type", "suppressed_at", "reason"}
    # Newest first.
    assert [r["suppressed_at"] for r in out] == sorted((r["suppressed_at"] for r in out),
                                                       reverse=True)


def test_suppressed_and_restore_routes(tmp_path):
    from mcpbrain import change_undo
    s = _real_store(tmp_path)
    s.upsert_entity("p-dana", "Dana Okafor", "person")
    s.suppress_entity("p-dana", reason="graph-ui")
    srv = ControlServer(_Daemon(), str(tmp_path), store=s); srv.start()
    base = f"http://127.0.0.1:{srv.port}"
    try:
        code, body = _get(base + "/api/graph/suppressed", srv.token)
        assert code == 200 and [i["id"] for i in body["items"]] == ["p-dana"]

        code, body = _post(base + "/api/graph/entity/p-dana/restore", srv.token)
        assert code == 200 and body["ok"] is True and body["change_id"]
        code, body2 = _get(base + "/api/graph/suppressed", srv.token)
        assert body2["items"] == []
        with s._connect() as db:
            row = dict(db.execute("SELECT * FROM change_log WHERE id=?",
                                  (body["change_id"],)).fetchone())
        assert row["change_type"] == "entity_unsuppressed" and row["ref_id"] == "p-dana"
        assert "Dana Okafor" in row["summary"]
        assert change_undo.undo(s, str(tmp_path), row["id"])["ok"] is True
        code, body3 = _get(base + "/api/graph/suppressed", srv.token)
        assert [i["id"] for i in body3["items"]] == ["p-dana"]

        # Not hidden any more / never existed.
        code, _ = _post(base + "/api/graph/entity/p-dana/restore", srv.token)
        assert code == 200
        code, body = _post(base + "/api/graph/entity/p-dana/restore", srv.token)
        assert code == 409 and body["ok"] is False and body["reason"]
        code, _ = _post(base + "/api/graph/entity/nobody/restore", srv.token)
        assert code == 404
    finally:
        srv.stop()


def test_suppressed_route_without_store(tmp_path):
    srv = ControlServer(_Daemon(), str(tmp_path)); srv.start()
    try:
        with __import__("pytest").raises(urllib.error.HTTPError) as e:
            _get(f"http://127.0.0.1:{srv.port}/api/graph/suppressed", srv.token)
        assert e.value.code == 503
    finally:
        srv.stop()


def test_list_suppressed_skips_a_suppression_whose_entity_is_gone(tmp_path):
    from mcpbrain import graph_view
    s = _real_store(tmp_path)
    s.upsert_entity("p-dana", "Dana Okafor", "person")
    s.upsert_entity("o-gone", "Gone Org", "org")
    s.suppress_entity("p-dana", reason="user")
    s.suppress_entity("o-gone", reason="graph-ui")
    with s._connect(write=True) as db:   # what an org import's entity removal leaves behind
        db.execute("DELETE FROM entities WHERE id='o-gone'")  # admin-delete-ok
    assert [r["id"] for r in graph_view.list_suppressed(s)] == ["p-dana"]


# --- Task 8: graph page filters, kinds, facts ----------------------------------

def _graph_store(tmp_path):
    """A real store: Dana works at Harbour Trust (live link); Sam had a link that
    was invalidated; Lena and an unclassified stub have none."""
    from mcpbrain import graph_write as gw
    s = _real_store(tmp_path)
    s.upsert_entity("p-dana", "Dana Okafor", "person")
    s.upsert_entity("o-harbour", "Harbour Trust", "org")
    s.upsert_entity("p-sam", "Sam Ito", "person")
    s.upsert_entity("o-north", "Northgate Tools", "org")
    s.upsert_entity("p-lena", "Lena Whitfield", "person")
    s.upsert_entity("x-stub", "Loose end", "unknown")
    s.upsert_entity("d-quote", "Fence quote", "document")
    s.upsert_entity("f-shed", "Tool shed", "facility")
    gw.upsert_relation(s, "p-dana", "works_at", "o-harbour", valid_from="2026-01-01")
    gw.upsert_relation(s, "p-dana", "collaborates_with", "p-lena", valid_from="2026-01-01")
    gw.upsert_relation(s, "d-quote", "mentioned_with", "o-north", valid_from="2026-01-01")
    rid = gw.upsert_relation(s, "p-sam", "works_at", "o-north", valid_from="2026-01-01")
    with s._connect(write=True) as db:
        db.execute("UPDATE entity_relations SET invalidated_at='2026-02-01' WHERE id=?", (rid,))
        db.execute("UPDATE entities SET degree=9")
    return s


def test_canvas_unlinked_returns_only_entities_with_no_live_link(tmp_path):
    s = _graph_store(tmp_path)
    srv = ControlServer(_Daemon(), str(tmp_path), store=s); srv.start()
    try:
        # min_conn is ignored: degree means nothing for an entity with no links.
        code, body = _get(f"http://127.0.0.1:{srv.port}/api/graph/canvas?unlinked=1&min_conn=50",
                          srv.token)
        assert code == 200
        assert {n["id"] for n in body["nodes"]} == {"p-sam", "x-stub", "f-shed"}
        assert body["links"] == []
        assert body["total"] == 3 and body["truncated"] is False
        code, body = _get(f"http://127.0.0.1:{srv.port}/api/graph/canvas?unlinked=1&type=person",
                          srv.token)
        assert {n["id"] for n in body["nodes"]} == {"p-sam"}
    finally:
        srv.stop()


def test_canvas_unlinked_caps_to_the_newest_and_says_so(tmp_path, monkeypatch):
    from mcpbrain import graph_view
    s = _graph_store(tmp_path)
    with s._connect(write=True) as db:
        db.execute("UPDATE entities SET last_seen='2026-03-01' WHERE id='x-stub'")
        db.execute("UPDATE entities SET last_seen='2026-01-01' WHERE id IN ('p-sam','f-shed')")
    monkeypatch.setattr(graph_view, "_UNLINKED_CAP", 1)
    out = graph_view.graph_canvas(s, unlinked=True)
    assert [n["id"] for n in out["nodes"]] == ["x-stub"]
    assert out["total"] == 3 and out["truncated"] is True


def test_canvas_since_hours_returns_only_recently_added(tmp_path):
    from datetime import datetime, timedelta, timezone

    from mcpbrain import change_undo, graph_view
    s = _graph_store(tmp_path)
    now = datetime.now(timezone.utc)
    fresh = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    stale = (now - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with s._connect(write=True) as db:
        rid = db.execute("SELECT id FROM entity_relations WHERE entity_a='p-dana' "
                         "AND entity_b='o-harbour'").fetchone()[0]
        db.execute("INSERT INTO relation_sources(relation_id, source_key, source_class, "
                   "first_seen) VALUES(?,?,?,?)", (rid, "msg-1", "email", fresh))
        old = db.execute("SELECT id FROM entity_relations WHERE entity_a='d-quote'").fetchone()[0]
        db.execute("INSERT INTO relation_sources(relation_id, source_key, source_class, "
                   "first_seen) VALUES(?,?,?,?)", (old, "msg-2", "email", stale))
        oid = db.execute("INSERT INTO entity_observations(entity_id, attribute, value, source, "
                         "valid_from) VALUES('p-lena','role','Site lead','x','2026-01-01')"
                         ).lastrowid
        db.execute("INSERT INTO observation_sources(observation_id, source_key, source_class, "
                   "first_seen) VALUES(?,?,?,?)", (oid, "msg-3", "email", fresh))
    out = graph_view.graph_canvas(s, min_conn=0, since_hours=24)
    ids = {n["id"] for n in out["nodes"]}
    assert ids == {"p-dana", "o-harbour", "p-lena"}
    assert ids == {r["entity_id"] for r in change_undo.added_since(s, hours=24)}
    srv = ControlServer(_Daemon(), str(tmp_path), store=s); srv.start()
    try:
        code, body = _get(f"http://127.0.0.1:{srv.port}/api/graph/canvas?since_hours=24&min_conn=0",
                          srv.token)
        assert code == 200 and {n["id"] for n in body["nodes"]} == ids
    finally:
        srv.stop()


def test_graph_types_match_graph_counts(tmp_path):
    from mcpbrain import console_stats, contract, graph_view
    from mcpbrain.dashboard import _open_ro
    s = _graph_store(tmp_path)
    types = graph_view.graph_types(s)
    assert all(set(t) == {"type", "count"} for t in types)
    assert {t["type"] for t in types} == {"person", "org", "unknown", "document", "facility"}
    db = _open_ro(s.path)
    try:
        by_type = console_stats.graph_counts(db)["by_type"]
    finally:
        db.close()
    folded = {}
    for t in types:
        key = t["type"] if t["type"] in by_type else "unknown"
        folded[key] = folded.get(key, 0) + t["count"]
    assert folded == {k: v for k, v in by_type.items() if v}
    srv = ControlServer(_Daemon(), str(tmp_path), store=s); srv.start()
    try:
        code, body = _get(f"http://127.0.0.1:{srv.port}/api/graph/types", srv.token)
        assert code == 200 and body["types"] == types
        assert body["classified"] == sorted(contract.ENTITY_TYPES)
    finally:
        srv.stop()


def test_ego_follows_one_relation(tmp_path):
    from mcpbrain import graph_view
    s = _graph_store(tmp_path)
    out = graph_view.graph_ego(s, "p-dana", hops=1, relation="works_at")
    assert {n["id"] for n in out["nodes"]} == {"p-dana", "o-harbour"}
    assert {lk["relation"] for lk in out["links"]} == {"works_at"}
    srv = ControlServer(_Daemon(), str(tmp_path), store=s); srv.start()
    try:
        code, body = _get(f"http://127.0.0.1:{srv.port}/api/graph/ego?id=p-dana&hops=1"
                          "&relation=collaborates_with", srv.token)
        assert code == 200 and {n["id"] for n in body["nodes"]} == {"p-dana", "p-lena"}
    finally:
        srv.stop()


def test_add_a_fact_route_validates_and_is_undoable(tmp_path):
    from mcpbrain import change_undo, graph_view
    s = _graph_store(tmp_path)
    srv = ControlServer(_Daemon(), str(tmp_path), store=s); srv.start()
    base = f"http://127.0.0.1:{srv.port}/api/graph/entity"
    try:
        code, body = _post(base + "/p-dana/facts", srv.token,
                           {"attribute": "role", "value": "x" * 81})
        assert code == 400 and body["reason"]
        code, body = _post(base + "/p-dana/facts", srv.token, {"attribute": "role", "value": " "})
        assert code == 400
        code, body = _post(base + "/p-dana/facts", srv.token, {"attribute": "", "value": "Lead"})
        assert code == 400
        code, body = _post(base + "/nobody/facts", srv.token, {"attribute": "role", "value": "Lead"})
        assert code == 404

        code, body = _post(base + "/p-dana/facts", srv.token,
                           {"attribute": "role", "value": "Site lead"})
        assert code == 200 and body["ok"] is True and body["change_id"]
        obs = graph_view.entity_detail(s, "p-dana")["observations"]
        assert any(o["attribute"] == "role" and o["value"] == "Site lead"
                   and o["source"] == "manual" for o in obs)
        assert any(o["value"] == "Site lead" for o in body["entity"]["observations"])
        with s._connect() as db:
            row = dict(db.execute("SELECT * FROM change_log WHERE id=?",
                                  (body["change_id"],)).fetchone())
            conf = db.execute("SELECT confidence_source FROM entity_observations "
                              "WHERE value='Site lead'").fetchone()[0]
        assert conf == "high"
        assert row["change_type"] == "fact_added" and row["ref_id"] == "p-dana"
        assert "Dana Okafor" in row["summary"] and row["revert_ref"]

        assert change_undo.undo(s, str(tmp_path), row["id"])["ok"] is True
        obs = graph_view.entity_detail(s, "p-dana")["observations"]
        assert not any(o["value"] == "Site lead" for o in obs)
        # A second undo is refused, not repeated.
        assert change_undo.undo(s, str(tmp_path), row["id"])["ok"] is False
    finally:
        srv.stop()


def test_fact_undo_refuses_once_the_fact_has_changed(tmp_path):
    from mcpbrain import change_undo, graph_view
    s = _graph_store(tmp_path)
    out = graph_view.add_fact(s, "p-dana", "role", "Site lead")
    with s._connect(write=True) as db:
        db.execute("UPDATE entity_observations SET valid_to='2026-11-01' WHERE value='Site lead'")
    res = change_undo.undo(s, str(tmp_path), out["change_id"])
    assert res["ok"] is False and res["reason"] == change_undo.CHANGED_SINCE


def test_hide_from_the_graph_returns_its_change_for_undo(tmp_path):
    from mcpbrain import change_undo
    s = _graph_store(tmp_path)
    srv = ControlServer(_Daemon(), str(tmp_path), store=s); srv.start()
    try:
        code, body = _req(f"http://127.0.0.1:{srv.port}/api/graph/entity/p-lena",
                          srv.token, "DELETE", None)
        assert code == 200 and body["ok"] is True and body["change_id"]
        assert change_undo.undo(s, str(tmp_path), body["change_id"])["ok"] is True
        code, body = _get(f"http://127.0.0.1:{srv.port}/api/graph/suppressed", srv.token)
        assert body["items"] == []
    finally:
        srv.stop()


def test_graph_page_serves_with_a_query_string(tmp_path):
    srv = ControlServer(_Daemon(), str(tmp_path), store=None); srv.start()
    try:
        for q in ("?type=person", "?show=unlinked", "?entity=p-dana", "?view=circles",
                  "?since=24h", "?type=document&type=thread"):
            with urllib.request.urlopen(f"http://127.0.0.1:{srv.port}/graph{q}", timeout=5) as r:
                assert r.status == 200 and srv.token in r.read().decode()
    finally:
        srv.stop()


# --- Task 8 fix round 1 ----------------------------------------------------------

def test_relation_views_and_canvas_draw_live_links_only(tmp_path):
    from mcpbrain import graph_view
    from mcpbrain import graph_write as gw
    s = _graph_store(tmp_path)
    gw.upsert_relation(s, "p-sam", "works_at", "o-harbour", valid_from="2026-03-01")
    out = graph_view.graph_ego(s, "p-sam", hops=1, relation="works_at")
    assert {n["id"] for n in out["nodes"]} == {"p-sam", "o-harbour"}   # not the old employer
    assert {(lk["source"], lk["target"]) for lk in out["links"]} == {("p-sam", "o-harbour")}
    canvas = graph_view.graph_canvas(s, min_conn=0)
    pairs = {(lk["source"], lk["target"]) for lk in canvas["links"]}
    assert ("p-sam", "o-north") not in pairs and ("p-sam", "o-harbour") in pairs
    # The drawer's count and the relation view agree.
    detail = graph_view.entity_detail(s, "p-sam")
    assert [r["other_id"] for r in detail["relations"] if r["relation"] == "works_at"] == ["o-harbour"]


def test_since_and_recency_filters_together(tmp_path):
    from datetime import datetime, timedelta, timezone

    from mcpbrain import graph_view
    s = _graph_store(tmp_path)
    now = datetime.now(timezone.utc)
    with s._connect(write=True) as db:
        db.execute("UPDATE entities SET last_seen=?", (now.strftime("%Y-%m-%d"),))
        fresh = db.execute("SELECT id FROM entity_relations WHERE entity_a='p-dana' "
                           "AND entity_b='o-harbour'").fetchone()[0]
        old = db.execute("SELECT id FROM entity_relations WHERE entity_a='d-quote'").fetchone()[0]
        db.execute("INSERT INTO relation_sources VALUES(?, 'm1', 'email', ?)",
                   (fresh, now.strftime("%Y-%m-%dT%H:%M:%SZ")))
        db.execute("INSERT INTO relation_sources VALUES(?, 'm2', 'email', ?)",
                   (old, (now - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")))
    out = graph_view.graph_canvas(s, min_conn=0, since_hours=24, recency_days=30)
    assert {n["id"] for n in out["nodes"]} == {"p-dana", "o-harbour"}


def test_a_new_fact_supersedes_the_last_and_undo_puts_it_back(tmp_path):
    from mcpbrain import change_undo, graph_view
    s = _graph_store(tmp_path)
    first = graph_view.add_fact(s, "p-dana", "role", "Site lead")
    second = graph_view.add_fact(s, "p-dana", "role", "Operations lead")
    with s._connect() as db:
        rows = {r["value"]: dict(r) for r in db.execute(
            "SELECT value, valid_to FROM entity_observations WHERE entity_id='p-dana' "
            "AND attribute='role' AND source='manual'")}
    assert rows["Site lead"]["valid_to"] and rows["Operations lead"]["valid_to"] is None
    assert change_undo.undo(s, str(tmp_path), second["change_id"])["ok"] is True
    with s._connect() as db:
        rows = {r["value"]: r["valid_to"] for r in db.execute(
            "SELECT value, valid_to FROM entity_observations WHERE entity_id='p-dana' "
            "AND attribute='role' AND source='manual'")}
    assert rows == {"Site lead": None}
    # Another attribute is left alone.
    graph_view.add_fact(s, "p-dana", "phone", "0400 000 000")
    assert first["ok"]


def test_fact_undo_refuses_when_the_closed_fact_changed(tmp_path):
    from mcpbrain import change_undo, graph_view
    s = _graph_store(tmp_path)
    graph_view.add_fact(s, "p-dana", "role", "Site lead")
    second = graph_view.add_fact(s, "p-dana", "role", "Operations lead")
    with s._connect(write=True) as db:
        db.execute("UPDATE entity_observations SET valid_to='2026-01-02' WHERE value='Site lead'")
    res = change_undo.undo(s, str(tmp_path), second["change_id"])
    assert res["ok"] is False and res["reason"] == change_undo.CHANGED_SINCE
    with s._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM entity_observations "
                          "WHERE value='Operations lead'").fetchone()[0] == 1


def test_no_fact_on_a_hidden_entity(tmp_path):
    s = _graph_store(tmp_path)
    s.suppress_entity("p-lena", reason="graph-ui")
    srv = ControlServer(_Daemon(), str(tmp_path), store=s); srv.start()
    try:
        code, body = _post(f"http://127.0.0.1:{srv.port}/api/graph/entity/p-lena/facts",
                           srv.token, {"attribute": "role", "value": "Site lead"})
        assert code == 409 and "hidden" in body["reason"]
        with s._connect() as db:
            assert db.execute("SELECT COUNT(*) FROM entity_observations "
                              "WHERE entity_id='p-lena'").fetchone()[0] == 0
        code, body = _get(f"http://127.0.0.1:{srv.port}/api/graph/types", srv.token)
        assert body["hidden"] == 1
    finally:
        srv.stop()


def test_unnamed_relations_and_unknown_type_are_reachable(tmp_path):
    from mcpbrain import graph_view
    s = _graph_store(tmp_path)
    with s._connect(write=True) as db:
        db.execute("INSERT INTO entity_relations(entity_a, relation, entity_b, strength) "
                   "VALUES('p-lena', '', 'o-north', 1)")
        db.execute("UPDATE entities SET type='' WHERE id='f-shed'")
    out = graph_view.graph_ego(s, "p-lena", hops=1, relation=graph_view.EMPTY_RELATION)
    assert {n["id"] for n in out["nodes"]} == {"p-lena", "o-north"}
    assert {lk["relation"] for lk in out["links"]} == {""}
    assert {t["type"]: t["count"] for t in graph_view.graph_types(s)}["unknown"] == 2
    canvas = graph_view.graph_canvas(s, min_conn=0, types=["unknown"])
    assert {n["id"] for n in canvas["nodes"]} == {"x-stub", "f-shed"}
    srv = ControlServer(_Daemon(), str(tmp_path), store=s); srv.start()
    try:
        code, body = _get(f"http://127.0.0.1:{srv.port}/api/graph/ego?id=p-lena&hops=1"
                          "&relation=_none", srv.token)
        assert code == 200 and {n["id"] for n in body["nodes"]} == {"p-lena", "o-north"}
    finally:
        srv.stop()


# --- Follow-up Task 2: unlinked entities as a reviewable list --------------------

def _unlinked_store(tmp_path):
    """_graph_store plus twelve more unlinked people with known last_seen
    dates, so paging has more than one page at the smallest page size."""
    s = _graph_store(tmp_path)
    with s._connect(write=True) as db:
        db.execute("UPDATE entities SET last_seen='2026-01-01', org='Acme', mentions=2 "
                   "WHERE id IN ('p-sam','x-stub','f-shed')")
        for i in range(12):
            db.execute("INSERT INTO entities(id, name, type, org, last_seen, mentions) "
                       "VALUES(?,?,?,?,?,?)",
                       (f"p-u{i:02d}", f"Pat Nguyen {i:02d}", "person", "example.org",
                        f"2026-02-{i + 1:02d}", i))
    return s


def test_list_unlinked_pages_newest_first(tmp_path):
    from mcpbrain import graph_view
    s = _unlinked_store(tmp_path)
    first = graph_view.list_unlinked(s, page=1, page_size=10)
    assert first["total"] == 15 and first["page"] == 1 and first["page_size"] == 10
    ids = [r["id"] for r in first["items"]]
    assert ids == [f"p-u{i:02d}" for i in range(11, 1, -1)]
    second = graph_view.list_unlinked(s, page=2, page_size=10)
    # Ties on last_seen fall back to id, so paging is stable.
    assert [r["id"] for r in second["items"]] == ["p-u01", "p-u00", "f-shed", "p-sam", "x-stub"]
    row = first["items"][0]
    assert set(row) == {"id", "name", "type", "org", "first_seen", "last_seen", "mentions"}
    assert (row["name"], row["type"], row["org"], row["last_seen"], row["mentions"]) == (
        "Pat Nguyen 11", "person", "example.org", "2026-02-12", 11)
    # Past the last page: no rows, the same total.
    past = graph_view.list_unlinked(s, page=3, page_size=10)
    assert past["items"] == [] and past["total"] == 15


def test_list_unlinked_clamps_page_size(tmp_path):
    from mcpbrain import graph_view
    s = _unlinked_store(tmp_path)
    assert graph_view.list_unlinked(s, page_size=1)["page_size"] == 10
    assert graph_view.list_unlinked(s, page_size=5000)["page_size"] == 200
    assert len(graph_view.list_unlinked(s, page_size=5000)["items"]) == 15


def test_list_unlinked_leaves_out_linked_and_hidden(tmp_path):
    from mcpbrain import graph_view
    s = _unlinked_store(tmp_path)
    all_ids = {r["id"] for r in graph_view.list_unlinked(s, page_size=200)["items"]}
    # Dana, Lena, Harbour Trust, Northgate and the quote have live links;
    # Sam's only link was invalidated, so he counts as unlinked.
    assert {"p-dana", "p-lena", "o-harbour", "o-north", "d-quote"}.isdisjoint(all_ids)
    assert "p-sam" in all_ids
    s.suppress_entity("p-sam", reason="graph-ui")
    out = graph_view.list_unlinked(s, page_size=200)
    assert "p-sam" not in {r["id"] for r in out["items"]} and out["total"] == 14


def test_list_unlinked_kind_filter_matches_the_canvas(tmp_path):
    from mcpbrain import graph_view
    s = _unlinked_store(tmp_path)
    with s._connect(write=True) as db:
        db.execute("INSERT INTO entities(id, name, type) VALUES('x-blank', 'Blank kind', '')")
    people = graph_view.list_unlinked(s, kinds=("person",), page_size=200)
    assert people["total"] == 13
    assert {r["type"] for r in people["items"]} == {"person"}
    # 'unknown' takes in empty and unknown together, as the canvas does.
    loose = graph_view.list_unlinked(s, kinds=("unknown",), page_size=200)
    assert {r["id"] for r in loose["items"]} == {"x-stub", "x-blank"}
    assert {r["type"] for r in loose["items"]} == {"unknown"}
    canvas = graph_view.graph_canvas(s, unlinked=True, types=["unknown"])
    assert {n["id"] for n in canvas["nodes"]} == {"x-stub", "x-blank"}
    both = graph_view.list_unlinked(s, kinds=("facility", "unknown"), page_size=200)
    assert {r["id"] for r in both["items"]} == {"x-stub", "x-blank", "f-shed"}


def test_list_unlinked_name_filter(tmp_path):
    from mcpbrain import graph_view
    s = _unlinked_store(tmp_path)
    with s._connect(write=True) as db:
        db.execute("INSERT INTO entities(id, name, type) VALUES('t-pct', '100% Acme', 'topic')")
        db.execute("INSERT INTO entities(id, name, type) VALUES('t-und', 'Acme_ops', 'topic')")
    out = graph_view.list_unlinked(s, q="pat nguyen 0", page_size=200)
    assert out["total"] == 10
    assert graph_view.list_unlinked(s, q="  SAM ", page_size=200)["total"] == 1
    # LIKE wildcards in what the owner typed are plain characters.
    assert [r["id"] for r in graph_view.list_unlinked(s, q="%", page_size=200)["items"]] == ["t-pct"]
    assert [r["id"] for r in graph_view.list_unlinked(s, q="_", page_size=200)["items"]] == ["t-und"]
    assert graph_view.list_unlinked(s, q="acme", kinds=("topic",), page_size=200)["total"] == 2


def test_list_unlinked_twelve_thousand_in_under_a_second(tmp_path):
    import time

    from mcpbrain import graph_view
    s = _real_store(tmp_path)
    with s._connect(write=True) as db:
        db.executemany(
            "INSERT INTO entities(id, name, type, org, last_seen) VALUES(?,?,?,?,?)",
            [(f"p-{i:05d}", f"Dana Okafor {i}", "person", "Acme",
              f"2026-{1 + i % 12:02d}-{1 + i % 28:02d}") for i in range(12000)])
        db.execute("INSERT INTO entities(id, name, type) VALUES('o-acme', 'Acme', 'org')")
        db.execute("INSERT INTO entities(id, name, type) VALUES('o-ex', 'Example', 'org')")
        db.execute("INSERT INTO entity_relations(entity_a, relation, entity_b, strength) "
                   "VALUES('o-acme', 'partner_of', 'o-ex', 1)")
    t0 = time.perf_counter()
    out = graph_view.list_unlinked(s, page=1, page_size=50)
    took = time.perf_counter() - t0
    assert out["total"] == 12000 and len(out["items"]) == 50
    assert took < 1.0, f"took {took:.3f}s"


def test_unlinked_route_pages_and_filters(tmp_path):
    s = _unlinked_store(tmp_path)
    srv = ControlServer(_Daemon(), str(tmp_path), store=s); srv.start()
    try:
        base = f"http://127.0.0.1:{srv.port}/api/graph/unlinked"
        code, body = _get(base + "?page=2&page_size=10", srv.token)
        assert code == 200 and body["total"] == 15 and body["page"] == 2 and len(body["items"]) == 5
        code, body = _get(base, srv.token)
        assert code == 200 and body["page"] == 1 and body["page_size"] == 50
        code, body = _get(base + "?type=facility&type=unknown", srv.token)
        assert {r["id"] for r in body["items"]} == {"x-stub", "f-shed"}
        code, body = _get(base + "?q=Nguyen%2010", srv.token)
        assert [r["id"] for r in body["items"]] == ["p-u10"]
    finally:
        srv.stop()


def test_unlinked_route_refuses_a_bad_page_in_words(tmp_path):
    s = _unlinked_store(tmp_path)
    srv = ControlServer(_Daemon(), str(tmp_path), store=s); srv.start()
    try:
        base = f"http://127.0.0.1:{srv.port}/api/graph/unlinked"
        for q in ("?page=0", "?page=-1", "?page=two", "?page_size=lots", "?page=1.5"):
            try:
                _get(base + q, srv.token)
                assert False, f"expected 400 for {q}"
            except urllib.error.HTTPError as e:
                assert e.code == 400, q
                body = json.loads(e.read())
                assert body["worded"] is True and body["ok"] is False, q
                assert body["reason"].endswith("."), q
    finally:
        srv.stop()


def test_graph_page_serves_the_list_and_map_views(tmp_path):
    srv = ControlServer(_Daemon(), str(tmp_path), store=None); srv.start()
    try:
        for q in ("?show=unlinked&as=map", "?show=unlinked&page=3&q=Dana"):
            with urllib.request.urlopen(f"http://127.0.0.1:{srv.port}/graph{q}", timeout=5) as r:
                assert r.status == 200
    finally:
        srv.stop()
