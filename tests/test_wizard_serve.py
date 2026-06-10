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
        for a in ("step-google","step-enrich","step-register","step-status"): assert a in html
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
