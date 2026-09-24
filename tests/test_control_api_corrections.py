"""Dashboard approval for pending graph corrections: GET /api/corrections/pending
lists them, POST .../apply and .../decline resolve them, and the routes sit
behind the same bearer-token gate as every other control-API route."""
import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from mcpbrain import graph_corrections as gc
from mcpbrain.control_api import ControlServer
from mcpbrain.store import Store


class FakeDaemon:
    def status(self):
        return {"paused": False, "chunk_count": 0, "google_connected": False,
                "granted_scopes": [], "enrich_enabled": False}


def _request(srv, path, *, method="GET", body=None, authed=True):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{srv.port}{path}", data=data, method=method)
    if authed:
        req.add_header("Authorization", f"Bearer {srv.token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (json.loads(raw) if raw else {})


class _Api:
    """Thin request helper standing in for the brief's placeholder api.get/api.post,
    built on the real ControlServer fixture/pattern used by test_control_api_post.py
    and test_dashboard_digest.py."""

    def __init__(self, srv, *, authed=True):
        self.srv = srv
        self.authed = authed

    def get(self, path):
        return _request(self.srv, path, authed=self.authed)

    def post(self, path, body):
        return _request(self.srv, path, method="POST", body=body, authed=self.authed)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    s.upsert_entity("x", "Dana Okafor", "person")
    return s


@pytest.fixture
def api(store, tmp_path):
    srv = ControlServer(FakeDaemon(), home=str(tmp_path), store=store)
    srv.start()
    try:
        yield _Api(srv)
    finally:
        srv.stop()


@pytest.fixture
def api_unauthenticated(store, tmp_path):
    srv = ControlServer(FakeDaemon(), home=str(tmp_path), store=store)
    srv.start()
    try:
        yield _Api(srv, authed=False)
    finally:
        srv.stop()


def test_pending_lists_and_apply_applies(api, store):
    cid = gc.submit(store, {"op": "hide", "basis": "inferred", "reason": "r",
                            "entity_id": "x"})["correction_id"]
    status, body = api.get("/api/corrections/pending")
    assert status == 200 and body["pending"][0]["id"] == cid
    assert body["pending"][0]["summary"] == "Hide x from the graph"
    status, body = api.post(f"/api/corrections/{cid}/apply", {})
    assert status == 200 and body["status"] == "applied"
    assert store.get_correction(cid)["confirmed_via"] == "dashboard"


def test_decline_and_unknown(api, store):
    cid = gc.submit(store, {"op": "hide", "basis": "inferred", "reason": "r",
                            "entity_id": "x"})["correction_id"]
    assert api.post(f"/api/corrections/{cid}/decline", {})[0] == 200
    assert api.post(f"/api/corrections/{cid}/decline", {})[0] == 404
    assert api.post("/api/corrections/999/apply", {})[0] == 409


def test_routes_require_the_token(api_unauthenticated):
    assert api_unauthenticated.get("/api/corrections/pending")[0] == 401


# ---- dashboard.html: card + script (Step 5's manual click-through stands in for
# this -- see the task report for why an automated browser check isn't run here) ----

DASH = Path("mcpbrain/wizard/dashboard.html").read_text()


def test_html_has_corrections_card():
    assert 'id="corrections-card"' in DASH
    assert 'id="corrections-body"' in DASH


def test_html_wires_up_the_two_routes():
    assert "/api/corrections/pending" in DASH
    assert "/api/corrections/${c.id}/${verb}" in DASH


def test_corrections_script_never_uses_innerhtml():
    """The summary text is model-supplied (0.7.87 graph stored-XSS precedent):
    the new loadCorrections() code must build the DOM with textContent/
    createElement only, never innerHTML."""
    start = DASH.index("function loadCorrections")
    end = DASH.index("refresh();", start)
    snippet = DASH[start:end]
    assert "loadCorrections" in snippet  # sanity: the slice found the function
    assert "innerHTML" not in snippet
