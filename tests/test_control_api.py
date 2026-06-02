import json, urllib.request, urllib.error
from mcpbrain.control_api import ControlServer

class FakeDaemon:
    def status(self): return {"paused": False, "chunk_count": 3, "google_connected": False,
                              "granted_scopes": [], "enrich_enabled": False}
    def pause(self): pass
    def resume(self): pass

def _get(url, token=None):
    req = urllib.request.Request(url)
    if token: req.add_header("Authorization", f"Bearer {token}")
    return urllib.request.urlopen(req)

def test_status_requires_token_and_binds_loopback(tmp_path):
    srv = ControlServer(FakeDaemon(), home=str(tmp_path)); srv.start()
    try:
        base = f"http://127.0.0.1:{srv.port}"
        assert (tmp_path/"control_token").read_text().strip() == srv.token
        try: _get(base + "/api/status"); assert False, "expected 401"
        except urllib.error.HTTPError as e: assert e.code == 401
        body = json.loads(_get(base + "/api/status", srv.token).read())
        assert body["chunk_count"] == 3
    finally: srv.stop()

def test_auth_status_maps_daemon_status(tmp_path):
    srv = ControlServer(FakeDaemon(), home=str(tmp_path)); srv.start()
    try:
        base = f"http://127.0.0.1:{srv.port}"
        body = json.loads(_get(base + "/api/auth/status", srv.token).read())
        assert body == {"connected": False, "granted_scopes": []}
    finally: srv.stop()
