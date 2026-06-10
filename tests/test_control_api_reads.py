"""GET /api/config returns the profile (no secret); GET /api/timezones lists zones."""
import json
import urllib.error
import urllib.request

import pytest

from mcpbrain.control_api import ControlServer


class _Daemon:
    def status(self): return {"google_connected": False, "granted_scopes": []}
    def config_profile(self):
        return {"owner_full_name": "Dana", "clickup_api_key_set": True, "timezone": "Asia/Tokyo"}


@pytest.fixture
def server(tmp_path):
    s = ControlServer(_Daemon(), str(tmp_path))
    s.start()
    yield s
    s.stop()


def _get(server, path):
    req = urllib.request.Request(f"http://127.0.0.1:{server.port}{path}",
                                 headers={"Authorization": f"Bearer {server.token}"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, json.loads(r.read())


def test_get_config_has_profile_no_secret(server):
    code, body = _get(server, "/api/config")
    assert code == 200 and body["owner_full_name"] == "Dana"
    assert body["clickup_api_key_set"] is True
    assert "clickup_api_key" not in body


def test_get_timezones(server):
    code, body = _get(server, "/api/timezones")
    assert code == 200 and body["zones"]
    assert all("GMT" in z["label"] for z in body["zones"])


def test_config_requires_token(server):
    req = urllib.request.Request(f"http://127.0.0.1:{server.port}/api/config")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 401
