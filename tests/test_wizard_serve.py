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

def test_backup_auto_pending_without_account(tmp_path):
    # Before Google sign-in there's no owner_email, so the automatic restore-or-backup
    # route reports "pending" (and the wizard will retry) rather than erroring.
    srv = ControlServer(FakeDaemon(), home=str(tmp_path)); srv.start()
    try:
        resp = _authed_post(srv, "/api/backup/auto")
        body = json.loads(resp.read())
        assert resp.status == 200
        assert body.get("action") == "pending"
    finally: srv.stop()


def test_backup_auto_restores_when_available_and_store_empty(tmp_path, monkeypatch):
    # With an account signed in, a restorable backup, and an empty local store, the
    # route restores automatically (no user choice).
    from mcpbrain import config, restore, auth
    monkeypatch.setattr(config, "owner_email", lambda home: "me@example.com")
    monkeypatch.setattr(config, "store_path", lambda: str(tmp_path / "missing.sqlite3"))
    monkeypatch.setattr(auth, "load_credentials", lambda: object())
    monkeypatch.setattr(auth, "build_service", lambda *a, **k: object())
    monkeypatch.setattr(restore, "detect_restorable",
                        lambda home, svc: {"available": True, "snapshot_id": "snap1"})
    called = {}
    monkeypatch.setattr(restore, "run_restore_auto",
                        lambda home, **k: called.setdefault("restored", True))
    srv = ControlServer(FakeDaemon(), home=str(tmp_path)); srv.start()
    try:
        resp = _authed_post(srv, "/api/backup/auto")
        body = json.loads(resp.read())
        assert body.get("action") == "restored"
        assert called.get("restored") is True
    finally: srv.stop()


def test_backup_auto_restores_over_schema_only_store(tmp_path, monkeypatch):
    # Regression (issue #3): the daemon initializes brain.sqlite3 (schema, no
    # rows) BEFORE the wizard runs, so the store file exists and is non-empty.
    # The old file-size check then never restored. With the content check, a
    # schema-only store is still treated as empty and restore runs (force=True
    # because the file already exists).
    from mcpbrain import config, restore, auth
    from mcpbrain.store import Store
    store_p = tmp_path / "brain.sqlite3"
    Store(str(store_p), dim=384).init()              # schema only, no chunks
    assert store_p.stat().st_size > 0                # file exists + non-empty
    monkeypatch.setattr(config, "owner_email", lambda home: "me@example.com")
    monkeypatch.setattr(config, "store_path", lambda: str(store_p))
    monkeypatch.setattr(auth, "load_credentials", lambda: object())
    monkeypatch.setattr(auth, "build_service", lambda *a, **k: object())
    monkeypatch.setattr(restore, "detect_restorable",
                        lambda home, svc: {"available": True, "snapshot_id": "snap1"})
    seen = {}
    monkeypatch.setattr(restore, "run_restore_auto",
                        lambda home, **k: seen.update(k) or "ok")
    srv = ControlServer(FakeDaemon(), home=str(tmp_path)); srv.start()
    try:
        resp = _authed_post(srv, "/api/backup/auto")
        body = json.loads(resp.read())
        assert body.get("action") == "restored"
        assert seen.get("force") is True             # bypasses run_restore_auto's size guard
    finally: srv.stop()


def test_backup_auto_enables_when_no_backup(tmp_path, monkeypatch):
    # No restorable backup → turn on encrypted backup instead (still no user choice).
    from mcpbrain import config, restore, auth, backup_setup
    monkeypatch.setattr(config, "owner_email", lambda home: "me@example.com")
    monkeypatch.setattr(config, "store_path", lambda: str(tmp_path / "missing.sqlite3"))
    monkeypatch.setattr(config, "read_config", lambda home: {})  # no escrow_key yet
    monkeypatch.setattr(auth, "load_credentials", lambda: object())
    monkeypatch.setattr(auth, "build_service", lambda *a, **k: object())
    monkeypatch.setattr(restore, "detect_restorable",
                        lambda home, svc: {"available": False})
    called = {}
    monkeypatch.setattr(backup_setup, "enable_backup",
                        lambda home, **k: called.setdefault("enabled", True) or {})
    srv = ControlServer(FakeDaemon(), home=str(tmp_path)); srv.start()
    try:
        resp = _authed_post(srv, "/api/backup/auto")
        body = json.loads(resp.read())
        assert body.get("action") == "backup_enabled"
        assert called.get("enabled") is True
    finally: srv.stop()


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
                   "backup-status", "self-heal-banners", "privacy-note"):
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
    assert '"enrichment"' in WIZ


def test_guided_elements_present():
    assert "Settings → Apps → API Token" in WIZ
    assert "Copy link" in WIZ                       # List ID instructions
    assert 'id="step-projects"' in WIZ
    assert "/api/records/scaffold" in WIZ
    assert "onerror" in WIZ                          # screenshots hide when absent
    assert "/img/clickup-apps-token.png" in WIZ


def test_final_step_defers_scheduling_to_claude_code():
    # Tasks are created in the Claude Code install prompt (Local scheduled tasks),
    # NOT here — the wizard must not duplicate or conflict with that. The final step
    # carries no project setup at all: the MCP server hands every session its standing
    # instructions automatically, so there's nothing to paste or wire up here.
    assert "proj-instructions" not in WIZ        # no instructions to paste anymore
    assert "copyInstructions" not in WIZ
    assert 'id="brain-home"' not in WIZ          # no manual workspace step
    assert "run the mcpbrain-cowork-setup skill" not in WIZ  # skill removed
    assert "/mcpbrain-setup" not in WIZ          # old slash command removed
    assert "Local" in WIZ                        # tasks are Local scheduled tasks
    assert "Use an existing folder" not in WIZ


def test_backup_is_automatic_no_button():
    # Backup/restore is automatic (no user choice): the wizard calls /api/backup/auto
    # once Google connects, and there is no "Enable backup" button.
    assert "/api/backup/auto" in WIZ
    assert "autoBackup" in WIZ
    assert "enable-backup-btn" not in WIZ        # the manual button is gone
    assert "scaffold" in WIZ                     # records still scaffold (now automatic)


def test_generic_hidden_css_rule_exists():
    # Without a generic `.hidden{display:none}` the home-status + step toggling
    # silently no-ops (only .badge.hidden was defined). This regressed once.
    assert ".hidden{display:none}" in WIZ.replace(" ", "")


def test_configured_view_keeps_actionable_steps_reachable():
    # renderHome must only hide the redundant google/status steps when configured,
    # so Register / Prepare-workspace buttons stay reachable.
    assert 'HIDE_WHEN_CONFIGURED' in WIZ
    assert '"step-google"' in WIZ and '"step-status"' in WIZ


def test_step_badges_reflect_server_state_on_load():
    # ws-state/r-state must be re-derived from /api/status connections
    # each poll, so a click's badge survives a page reload (not just in-memory).
    assert "reflectStepBadges" in WIZ
    assert "reflectStepBadges(j.connections)" in WIZ
    for key in ('"records"', '"claude"'):
        assert key in WIZ
