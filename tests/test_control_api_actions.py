"""POST scaffold/hooks endpoints + GET /img static serving (with traversal guard)."""
import json
import urllib.error
import urllib.request

import pytest

from mcpbrain.control_api import ControlServer
from mcpbrain import records, hooks


class _Daemon:
    def status(self): return {}


@pytest.fixture
def server(tmp_path):
    s = ControlServer(_Daemon(), str(tmp_path))
    s.start()
    yield s
    s.stop()


def _post(server, path):
    req = urllib.request.Request(f"http://127.0.0.1:{server.port}{path}", data=b"{}",
                                 method="POST",
                                 headers={"Authorization": f"Bearer {server.token}"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, json.loads(r.read())


def test_records_scaffold(server, monkeypatch):
    monkeypatch.setattr(records, "scaffold_records", lambda home: ["/x/CLAUDE.md"])
    code, body = _post(server, "/api/records/scaffold")
    assert code == 200 and body["scaffolded"] == ["/x/CLAUDE.md"]


def test_hooks_install(server, monkeypatch):
    from pathlib import Path
    monkeypatch.setattr(hooks, "install_session_hooks", lambda: Path("/x/settings.json"))
    code, body = _post(server, "/api/hooks/install")
    assert code == 200 and body["installed"] is True


def test_img_unknown_returns_404(server):
    # /img/ is served BEFORE the auth gate (browser <img> carries no token);
    # an unknown filename must 404 (not 401).
    req = urllib.request.Request(f"http://127.0.0.1:{server.port}/img/nope.png")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 404


def test_img_blocks_traversal(server):
    req = urllib.request.Request(f"http://127.0.0.1:{server.port}/img/..%2f..%2fconfig.json")
    with pytest.raises(urllib.error.HTTPError) as e2:
        urllib.request.urlopen(req, timeout=5)
    assert e2.value.code == 404
