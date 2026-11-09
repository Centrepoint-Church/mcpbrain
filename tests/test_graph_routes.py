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
