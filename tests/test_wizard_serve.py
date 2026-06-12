import json
import urllib.request
import urllib.error
from pathlib import Path
import pytest
from mcpbrain import control_api
from mcpbrain.control_api import ControlServer

WIZ = Path("mcpbrain/wizard/index.html").read_text()

class FakeDaemon:
    def __init__(self):
        self.calls = []
    def status(self): return {"paused":False,"chunk_count":0,"google_connected":False,
                              "granted_scopes":[],"enrich_enabled":False}
    def start_auth(self): self.calls.append("start_auth")
    def start_enrich_backfill(self): self.calls.append("start_enrich_backfill")
    def cancel_enrich_backfill(self): self.calls.append("cancel_enrich_backfill")


def _authed_post(srv, path):
    req = urllib.request.Request(
        f"http://127.0.0.1:{srv.port}{path}",
        data=b"{}",
        headers={"Authorization": f"Bearer {srv.token}", "Content-Type": "application/json"},
        method="POST",
    )
    return urllib.request.urlopen(req)

def test_root_serves_wizard_with_token(tmp_path):
    srv = ControlServer(FakeDaemon(), home=str(tmp_path)); srv.start()
    try:
        html = urllib.request.urlopen(f"http://127.0.0.1:{srv.port}/").read().decode()
        assert "<html" in html.lower() and srv.token in html
        for a in ("step-google","step-projects","step-status"): assert a in html
    finally: srv.stop()

def test_home_has_status_center_elements(tmp_path):
    srv = ControlServer(FakeDaemon(), home=str(tmp_path)); srv.start()
    try:
        html = urllib.request.urlopen(f"http://127.0.0.1:{srv.port}/").read().decode()
        for el in ("home-status", "conn-cards", "backfill-progress",
                   "enrich-history-btn", "self-heal-banners", "privacy-note"):
            assert f'id="{el}"' in html, f"missing #{el}"
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


def test_reconnect_google_route(tmp_path):
    import time
    daemon = FakeDaemon()
    srv = ControlServer(daemon, home=str(tmp_path)); srv.start()
    try:
        resp = _authed_post(srv, "/api/auth/start")
        assert resp.status == 202
        body = json.loads(resp.read())
        assert body.get("started") is True
        time.sleep(0.05)  # allow daemon thread to run
        assert "start_auth" in daemon.calls
    finally: srv.stop()


def test_start_enrich_backfill_route(tmp_path):
    import time
    daemon = FakeDaemon()
    srv = ControlServer(daemon, home=str(tmp_path)); srv.start()
    try:
        resp = _authed_post(srv, "/api/enrich-backfill/start")
        assert resp.status == 202
        body = json.loads(resp.read())
        assert body.get("started") is True
        time.sleep(0.05)
        assert "start_enrich_backfill" in daemon.calls
    finally: srv.stop()


def test_cancel_enrich_backfill_route(tmp_path):
    daemon = FakeDaemon()
    srv = ControlServer(daemon, home=str(tmp_path)); srv.start()
    try:
        resp = _authed_post(srv, "/api/enrich-backfill/cancel")
        assert resp.status == 200
        body = json.loads(resp.read())
        assert body.get("cancelled") is True
        assert "cancel_enrich_backfill" in daemon.calls
    finally: srv.stop()


def test_wizard_has_timezone_field(tmp_path):
    srv = ControlServer(FakeDaemon(), home=str(tmp_path)); srv.start()
    try:
        html = urllib.request.urlopen(f"http://127.0.0.1:{srv.port}/").read().decode()
        assert 'id="timezone"' in html
    finally: srv.stop()


def test_timezone_is_a_select():
    assert '<select id="timezone"' in WIZ
    assert '<input id="timezone"' not in WIZ


def test_prefill_and_dropdown_bootstrap_present():
    assert "/api/config" in WIZ          # one-shot prefill fetch
    assert "/api/timezones" in WIZ       # dropdown population
    assert "leave blank to keep" in WIZ  # masked-token placeholder


def test_home_status_renders_before_main():
    # status-first: the home-status section must appear before the wizard <main>
    assert WIZ.index('id="home-status"') < WIZ.index("<main")


def test_connection_order_includes_new_cards():
    assert '"enrichment"' in WIZ and '"memory-hooks"' in WIZ


def test_guided_elements_present():
    assert "Settings → Apps → API Token" in WIZ
    assert "Copy link" in WIZ                       # List ID instructions
    assert 'id="step-projects"' in WIZ
    assert 'id="step-hooks"' in WIZ
    assert "/api/records/scaffold" in WIZ
    assert "/api/hooks/install" in WIZ
    assert "onerror" in WIZ                          # screenshots hide when absent
    assert "/img/clickup-apps-token.png" in WIZ


def test_workspace_step_is_from_scratch_and_setup_skill():
    assert "New from scratch" in WIZ
    assert "proj-instructions" in WIZ            # the pasteable instructions block
    assert "copyInstructions" in WIZ
    assert "/mcpbrain-setup" in WIZ              # the enrichment setup command
    assert 'id="step-enrich"' not in WIZ         # old redundant step removed
    assert "Use an existing folder" not in WIZ   # no longer point Cowork at folders


def test_prepare_workspace_verifies_scaffold_result():
    # The button must check the returned scaffolded list, not just HTTP ok,
    # because the route returns 200 even when scaffolding produced nothing.
    assert "j.scaffolded" in WIZ


def test_generic_hidden_css_rule_exists():
    # Without a generic `.hidden{display:none}` the home-status + step toggling
    # silently no-ops (only .badge.hidden was defined). This regressed once.
    assert ".hidden{display:none}" in WIZ.replace(" ", "")


def test_configured_view_keeps_actionable_steps_reachable():
    # renderHome must only hide the redundant google/status steps when configured,
    # so Register / Prepare-workspace / memory-hooks buttons stay reachable.
    assert 'HIDE_WHEN_CONFIGURED' in WIZ
    assert '"step-google"' in WIZ and '"step-status"' in WIZ


def test_step_badges_reflect_server_state_on_load():
    # ws-state/hooks-state/r-state must be re-derived from /api/status connections
    # each poll, so a click's badge survives a page reload (not just in-memory).
    assert "reflectStepBadges" in WIZ
    assert "reflectStepBadges(j.connections)" in WIZ
    for key in ('"records"', '"memory-hooks"', '"claude"'):
        assert key in WIZ
