import urllib.request
import urllib.error
import pytest
from mcpbrain import control_api
from mcpbrain.control_api import ControlServer

class FakeDaemon:
    def status(self): return {"paused":False,"chunk_count":0,"google_connected":False,
                              "granted_scopes":[],"enrich_enabled":False}

def test_root_serves_wizard_with_token(tmp_path):
    srv = ControlServer(FakeDaemon(), home=str(tmp_path)); srv.start()
    try:
        html = urllib.request.urlopen(f"http://127.0.0.1:{srv.port}/").read().decode()
        assert "<html" in html.lower() and srv.token in html
        for a in ("step-google","step-enrich","step-register","step-status"): assert a in html
    finally: srv.stop()

def test_root_500s_when_template_missing(tmp_path, monkeypatch):
    # Simulate a packaging error where wizard/index.html is absent, without
    # touching the real file. Path.exists is patched to False only for this test.
    monkeypatch.setattr(control_api.Path, "exists", lambda self: False)
    srv = ControlServer(FakeDaemon(), home=str(tmp_path)); srv.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(f"http://127.0.0.1:{srv.port}/")
        assert ei.value.code == 500
        assert b"not found" in ei.value.read()
    finally: srv.stop()
