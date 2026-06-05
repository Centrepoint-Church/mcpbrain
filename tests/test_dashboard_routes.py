"""Tests for dashboard routes added to ControlServer (Task 4)."""
import json
import sqlite3
import urllib.error
import urllib.request

from mcpbrain.control_api import ControlServer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_store_path(tmp_path):
    p = tmp_path / "b.sqlite3"
    with sqlite3.connect(str(p)) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS actions (
            id INTEGER PRIMARY KEY,
            text TEXT,
            deadline TEXT,
            org TEXT,
            project_id TEXT,
            action_type TEXT,
            waiting_on TEXT,
            source TEXT,
            status TEXT DEFAULT 'open',
            resolved_by TEXT,
            resolved_at TEXT,
            updated_at TEXT
        )""")
    return p


class FakeStore:
    def __init__(self, path):
        self._path = path

    def snooze_action(self, action_id, until_iso):
        # No matching open row in this empty store: nothing to snooze.
        return False


class FakeDaemon:
    def status(self):
        return {"paused": False, "chunk_count": 0, "google_connected": False,
                "granted_scopes": [], "enrich_enabled": False}

    def pause(self): pass
    def resume(self): pass
    def apply_config(self, body): pass
    def register(self): return "/tmp/cfg.json"
    def start_auth(self): pass


def _get(url, token=None):
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    return urllib.request.urlopen(req)


def _post(url, token, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    return urllib.request.urlopen(req)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_get_dashboard_serves_html(tmp_path, monkeypatch):
    """GET /dashboard returns 200 with the injected token in the response."""
    # Create a minimal dashboard.html that contains the placeholder
    dashboard_html = tmp_path / "wizard" / "dashboard.html"
    dashboard_html.parent.mkdir(parents=True)
    dashboard_html.write_text("<html><body>mcpbrain dashboard __MCPBRAIN_TOKEN__</body></html>")

    # Patch Path(__file__).parent so _serve_dashboard finds our tmp wizard dir
    import mcpbrain.control_api as mod
    monkeypatch.setattr(mod, "__file__", str(tmp_path / "control_api.py"))

    d = FakeDaemon()
    srv = ControlServer(d, home=str(tmp_path))
    srv.start()
    try:
        r = _get(f"http://127.0.0.1:{srv.port}/dashboard")
        assert r.status == 200
        body = r.read().decode()
        assert "mcpbrain" in body
        # Token must be injected — placeholder should be gone
        assert "__MCPBRAIN_TOKEN__" not in body
        assert srv.token in body
    finally:
        srv.stop()


def test_get_dashboard_missing_html_returns_500(tmp_path, monkeypatch):
    """GET /dashboard when dashboard.html doesn't exist returns 500."""
    import mcpbrain.control_api as mod

    # Point __file__ at a location where wizard/dashboard.html won't exist
    monkeypatch.setattr(mod, "__file__", str(tmp_path / "control_api.py"))

    d = FakeDaemon()
    srv = ControlServer(d, home=str(tmp_path))
    srv.start()
    try:
        try:
            _get(f"http://127.0.0.1:{srv.port}/dashboard")
            assert False, "expected HTTP 500"
        except urllib.error.HTTPError as e:
            assert e.code == 500
    finally:
        srv.stop()


def test_get_dashboard_today_no_store_returns_503(tmp_path):
    """GET /api/dashboard/today with store=None returns 503."""
    d = FakeDaemon()
    srv = ControlServer(d, home=str(tmp_path), store=None)
    srv.start()
    try:
        try:
            _get(f"http://127.0.0.1:{srv.port}/api/dashboard/today", token=srv.token)
            assert False, "expected HTTP 503"
        except urllib.error.HTTPError as e:
            assert e.code == 503
            payload = json.loads(e.read())
            assert "dashboard not available" in payload["error"]
    finally:
        srv.stop()


def test_get_dashboard_today_with_store(tmp_path):
    """GET /api/dashboard/today with a real store returns 200 with expected keys."""
    path = _make_store_path(tmp_path)
    store = FakeStore(path)

    d = FakeDaemon()
    srv = ControlServer(d, home=str(tmp_path), store=store)
    srv.start()
    try:
        r = _get(f"http://127.0.0.1:{srv.port}/api/dashboard/today", token=srv.token)
        assert r.status == 200
        payload = json.loads(r.read())
        assert set(payload.keys()) >= {"actions", "calendar", "clickup", "as_of"}
        # actions should have the four buckets
        assert set(payload["actions"].keys()) >= {"overdue", "due_today", "upcoming", "blocked"}
        # calendar and clickup degrade to [] without credentials
        assert isinstance(payload["calendar"], list)
        assert isinstance(payload["clickup"], list)
    finally:
        srv.stop()


def test_post_mark_done(tmp_path):
    """POST /api/dashboard/actions/1/done returns 200 {"done": false} (empty store)."""
    from mcpbrain.store import Store
    store = Store(tmp_path / "real.sqlite3", dim=4)
    store.init()

    d = FakeDaemon()
    srv = ControlServer(d, home=str(tmp_path), store=store)
    srv.start()
    try:
        r = _post(f"http://127.0.0.1:{srv.port}/api/dashboard/actions/1/done", srv.token)
        assert r.status == 200
        payload = json.loads(r.read())
        # No action with id=1 in the empty store, so done should be False
        assert payload == {"done": False}
    finally:
        srv.stop()


def test_post_snooze_unknown_action_404(tmp_path):
    """POST snooze when snooze_action returns False (no open row) maps to 404.

    Covers the FakeStore/route wiring layer: when the store reports nothing was
    snoozed, the route mirrors the dismiss path and returns 404.
    """
    path = _make_store_path(tmp_path)
    store = FakeStore(path)

    d = FakeDaemon()
    srv = ControlServer(d, home=str(tmp_path), store=store)
    srv.start()
    try:
        _post(
            f"http://127.0.0.1:{srv.port}/api/dashboard/actions/1/snooze",
            srv.token,
            {"until": "2026-06-10"},
        )
        assert False, "expected HTTP 404"
    except urllib.error.HTTPError as e:
        assert e.code == 404
    finally:
        srv.stop()


def test_post_dashboard_no_store_returns_503(tmp_path):
    """POST to dashboard action routes with store=None returns 503."""
    d = FakeDaemon()
    srv = ControlServer(d, home=str(tmp_path), store=None)
    srv.start()
    try:
        for path in ["/api/dashboard/actions/1/done", "/api/dashboard/actions/1/snooze"]:
            try:
                _post(f"http://127.0.0.1:{srv.port}{path}", srv.token)
                assert False, f"expected HTTP 503 for {path}"
            except urllib.error.HTTPError as e:
                assert e.code == 503, f"got {e.code} for {path}"
                payload = json.loads(e.read())
                assert "dashboard not available" in payload["error"]
    finally:
        srv.stop()
