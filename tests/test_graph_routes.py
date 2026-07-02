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
