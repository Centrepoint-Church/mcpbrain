import json
import urllib.error
import urllib.request

from mcpbrain.control_api import ControlServer
from mcpbrain.store import Store


class FakeDaemon:
    def __init__(self): self.calls = []
    def status(self): return {"google_connected": False, "granted_scopes": []}
    def bootstrap_baseline_once(self, services=None, *, force=False):
        self.calls.append(force)
        return {"status": "done", "cache_hits": 5, "done_drive_ids": ["D1"]}


def _post(port, token, path):
    data = b"{}"
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json",
                 "Content-Length": str(len(data)), "Host": "127.0.0.1"},
        method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_bootstrap_endpoint_runs_forced(tmp_path):
    store = Store(tmp_path / "brain.sqlite3", dim=4); store.init()
    dm = FakeDaemon()
    srv = ControlServer(dm, str(tmp_path), store=store)
    srv.start()
    try:
        status, body = _post(srv.port, srv.token, "/api/bootstrap-baseline")
        assert status == 200
        assert body["status"] == "done" and body["cache_hits"] == 5
        assert dm.calls == [True]           # forced
    finally:
        srv.stop()


def test_control_client_calls_endpoint(tmp_path, monkeypatch):
    from mcpbrain import control_client
    captured = {}

    def _fake_request(self, path, method="GET"):
        captured["path"] = path
        captured["method"] = method
        return {"status": "done"}
    monkeypatch.setattr(control_client.ControlClient, "_request", _fake_request)
    cc = control_client.ControlClient(str(tmp_path))
    assert cc.bootstrap_baseline() == {"status": "done"}
    assert captured == {"path": "/api/bootstrap-baseline", "method": "POST"}
