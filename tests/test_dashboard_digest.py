"""Change digest: assemble() carries recent changes + open findings; the
control API can dismiss a finding."""
import json
import urllib.request
from unittest import mock

from mcpbrain import dashboard
from mcpbrain.control_api import ControlServer
from mcpbrain.store import Store


def _store(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    return s


def test_assemble_includes_changes_and_findings(tmp_path):
    s = _store(tmp_path)
    s.record_change("capture_ingest", ref_id="note-1", summary="Saved note")
    s.record_finding("org_unrecognised", ref_id="rotary club", summary="seen")
    with mock.patch("mcpbrain.dashboard.calendar_today", return_value=[]), \
         mock.patch("mcpbrain.dashboard.clickup_today", return_value=[]):
        out = dashboard.assemble(s, str(tmp_path))
    assert out["changes"][0]["summary"] == "Saved note"
    assert out["findings"][0]["ref_id"] == "rotary club"


class FakeDaemon:
    def status(self):
        return {"paused": False, "chunk_count": 0, "google_connected": False,
                "granted_scopes": [], "enrich_enabled": False}


def test_post_dismiss_finding(tmp_path):
    s = _store(tmp_path)
    s.record_finding("org_unrecognised", ref_id="x", summary="s")
    fid = s.open_findings()[0]["id"]
    srv = ControlServer(FakeDaemon(), home=str(tmp_path), store=s)
    srv.start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{srv.port}/api/dashboard/findings/{fid}/dismiss",
            data=b"{}", method="POST")
        req.add_header("Authorization", f"Bearer {srv.token}")
        out = json.loads(urllib.request.urlopen(req).read())
        assert out["dismissed"] is True
        assert s.open_findings_count() == 0
    finally:
        srv.stop()
