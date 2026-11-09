"""System controls (console Settings > System): restart, start at login,
update check/apply, in-process diagnostics, log tails and the support report.

Nothing here runs launchctl/schtasks, restarts the real daemon, touches
~/Library/LaunchAgents or hits the network: runners, paths and fetchers are
injected or monkeypatched.
"""

import json
import threading
import urllib.error
import urllib.request

import pytest

from mcpbrain import agents, config, system_ops
from mcpbrain.control_api import ControlServer


# -- fixtures ------------------------------------------------------------------

class FakeDaemon:
    def __init__(self):
        self.calls = []
        self._pending_update = None
        self._wake = threading.Event()

    def status(self):
        return {"paused": False, "chunk_count": 12, "enriched_count": 4,
                "google_connected": True, "google_account": "dana@example.org",
                "google_name": "Dana Okafor", "is_configured": True,
                "open_findings": 0, "watchdog_exits": 1,
                "watchdog_limit_reached": False, "version": "9.9.9",
                "connections": {"gmail": {"state": "ok", "detail": "Synced"}}}

    def stop(self):
        self.calls.append("stop")

    def model_status(self):
        return {"cached": True, "downloading": False, "error": None}

    def _recent_watchdog_exits(self):
        return [1.0, 2.0]


def _req(method, url, token, body=None, timeout=5):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    return urllib.request.urlopen(req, timeout=timeout)


@pytest.fixture
def server(tmp_path):
    d = FakeDaemon()
    srv = ControlServer(d, home=str(tmp_path))
    srv.start()
    try:
        yield srv, d, f"http://127.0.0.1:{srv.port}"
    finally:
        srv.stop()


_ON_PLIST = agents.launchd_plist(mcpbrain_bin="/opt/mcpbrain/bin/mcpbrain",
                                 home="/opt/mcpbrain-home")


# -- restart -------------------------------------------------------------------

def test_request_restart_stops_then_restarts_without_watchdog_exit():
    d = FakeDaemon()
    d._restart_process = lambda *a, **k: pytest.fail("must not spend the watchdog budget")
    order = []
    d.stop = lambda: order.append("stop")
    t = system_ops.request_restart(d, platform="darwin",
                                   restart=lambda p: order.append(("restart", p)))
    t.join(5)
    assert order == ["stop", ("restart", "darwin")]


def test_restart_route_replies_202_before_restart_agent(server, monkeypatch):
    srv, d, base = server
    released = threading.Event()
    done = threading.Event()
    order = []

    def fake_restart(platform):
        released.wait(5)
        order.append("restart")
        done.set()

    monkeypatch.setattr(agents, "restart_agent", fake_restart)
    resp = _req("POST", base + "/api/restart", srv.token, {}, timeout=3)
    order.append("replied")
    assert resp.status == 202
    assert json.loads(resp.read()) == {"restarting": True}
    released.set()
    assert done.wait(5)
    assert order == ["replied", "restart"]
    assert "stop" in d.calls


# -- start at login ------------------------------------------------------------

def test_start_at_login_defaults_true(tmp_path):
    assert config.start_at_login(str(tmp_path)) is True
    config.write_config(str(tmp_path), {"start_at_login": "yes"})
    assert config.start_at_login(str(tmp_path)) is True
    config.write_config(str(tmp_path), {"start_at_login": False})
    assert config.start_at_login(str(tmp_path)) is False


def test_daemon_plist_off_does_not_run_at_load():
    s = agents.launchd_plist(mcpbrain_bin="/opt/m", home="/opt/h", start_at_login=False)
    assert "<key>RunAtLoad</key>\n    <false/>" in s
    # Any KeepAlive other than <false/> implies RunAtLoad (launchd.plist(5)), so
    # "off" must not keep the job alive or it would still start at login.
    assert "<key>KeepAlive</key>\n    <false/>" in s
    on = agents.launchd_plist(mcpbrain_bin="/opt/m", home="/opt/h")
    assert "<key>RunAtLoad</key>\n    <true/>" in on
    assert "<key>KeepAlive</key>\n    <true/>" in on


def test_login_off_rewrites_plist_without_launchctl(tmp_path):
    plist = tmp_path / "com.mcpbrain.plist"
    plist.write_text(_ON_PLIST)
    calls = []
    out = system_ops.set_start_at_login(str(tmp_path), False, platform="darwin",
                                        plist_path=plist, run=lambda *a, **k: calls.append(a))
    assert out == {"ok": True, "applies": "next_login"}
    s = plist.read_text()
    assert "<key>RunAtLoad</key>\n    <false/>" in s
    assert "<key>KeepAlive</key>\n    <false/>" in s
    # The rewrite keeps the installed binary and home.
    assert "<string>/opt/mcpbrain/bin/mcpbrain</string>" in s
    assert "<string>/opt/mcpbrain-home</string>" in s
    assert calls == []
    assert config.read_config(str(tmp_path))["start_at_login"] is False


def test_login_on_restores_keepalive(tmp_path):
    plist = tmp_path / "com.mcpbrain.plist"
    plist.write_text(agents.launchd_plist(mcpbrain_bin="/opt/m", home="/opt/h",
                                          start_at_login=False))
    system_ops.set_start_at_login(str(tmp_path), True, platform="darwin",
                                  plist_path=plist, run=lambda *a, **k: None)
    s = plist.read_text()
    assert "<key>RunAtLoad</key>\n    <true/>" in s
    assert "<key>KeepAlive</key>\n    <true/>" in s
    assert config.read_config(str(tmp_path))["start_at_login"] is True


def test_login_toggle_without_installed_agent_only_writes_config(tmp_path):
    plist = tmp_path / "absent.plist"
    out = system_ops.set_start_at_login(str(tmp_path), False, platform="darwin",
                                        plist_path=plist, run=lambda *a, **k: None)
    assert out["ok"] is True and not plist.exists()
    assert config.start_at_login(str(tmp_path)) is False


def test_login_toggle_windows_uses_schtasks_change(tmp_path):
    calls = []

    class R:
        returncode = 0

    def run(cmd, **kw):
        calls.append(cmd)
        return R()

    system_ops.set_start_at_login(str(tmp_path), False, platform="win32", run=run)
    system_ops.set_start_at_login(str(tmp_path), True, platform="win32", run=run)
    assert calls == [["schtasks", "/change", "/tn", "mcpbrain", "/disable"],
                     ["schtasks", "/change", "/tn", "mcpbrain", "/enable"]]


def test_install_launchd_keeps_owner_login_choice(tmp_path, monkeypatch):
    config.write_config(str(tmp_path), {"start_at_login": False})
    plist = tmp_path / "LaunchAgents" / "com.mcpbrain.plist"
    monkeypatch.setattr(agents, "_LAUNCHD_PATH", plist)
    ran = []
    monkeypatch.setattr(agents.subprocess, "run", lambda cmd, **kw: ran.append(cmd))
    agents._install_launchd(mcpbrain_bin="/opt/m", home=str(tmp_path))
    assert "<key>RunAtLoad</key>\n    <false/>" in plist.read_text()
    assert ran and ran[0][:2] == ["launchctl", "load"]


def test_login_route_and_system_route(server, monkeypatch):
    srv, d, base = server
    seen = []
    monkeypatch.setattr(system_ops, "set_start_at_login",
                        lambda home, on: seen.append(on) or {"ok": True, "applies": "next_login"})
    r = _req("POST", base + "/api/system/login", srv.token, {"on": False})
    assert json.loads(r.read()) == {"ok": True, "applies": "next_login"}
    assert seen == [False]
    with pytest.raises(urllib.error.HTTPError) as e:
        _req("POST", base + "/api/system/login", srv.token, {"on": "no"})
    assert e.value.code == 400
    body = json.loads(_req("GET", base + "/api/system", srv.token).read())
    assert body["start_at_login"] is True
    assert body["model"] == {"cached": True, "downloading": False, "error": None}
    assert body["watchdog_exits"] == 2
    assert isinstance(body["version"], str) and body["version"]


def test_settings_api_still_refuses_start_at_login(server):
    srv, d, base = server
    try:
        _req("POST", base + "/api/settings", srv.token, {"start_at_login": False})
    except urllib.error.HTTPError:
        pass
    assert "start_at_login" not in config.read_config(str(srv.home))


# -- updates -------------------------------------------------------------------

def test_check_update_newer(monkeypatch):
    from mcpbrain import update
    monkeypatch.setattr(update, "_index_url", lambda: "https://index.example.org/simple")
    monkeypatch.setattr(update, "_installed_version", lambda: "1.2.3")
    monkeypatch.setattr(update, "_fetch", lambda url: (
        '<a href="x">mcpbrain-1.2.3-py3-none-any.whl</a>'
        '<a href="y">mcpbrain-1.10.0-py3-none-any.whl</a>'))
    assert system_ops.check_update() == {"installed": "1.2.3", "latest": "1.10.0",
                                         "newer": True}


def test_check_update_network_failure(monkeypatch):
    from mcpbrain import update
    monkeypatch.setattr(update, "_index_url", lambda: "https://index.example.org/simple")
    monkeypatch.setattr(update, "_installed_version", lambda: "1.2.3")

    def boom(url):
        raise OSError("offline")

    monkeypatch.setattr(update, "_fetch", boom)
    assert system_ops.check_update() == {"installed": "1.2.3", "latest": None,
                                         "newer": False}


def test_check_update_no_channel(monkeypatch):
    from mcpbrain import update
    monkeypatch.setattr(update, "_index_url", lambda: None)
    monkeypatch.setattr(update, "_installed_version", lambda: "1.2.3")
    assert system_ops.check_update()["newer"] is False


def test_apply_update_sets_pending_and_wakes():
    d = FakeDaemon()
    system_ops.apply_update(d, check=lambda: {"installed": "1", "latest": "2", "newer": True})
    assert d._pending_update == "2" and d._wake.is_set()
    d2 = FakeDaemon()
    with pytest.raises(system_ops.UpToDate):
        system_ops.apply_update(d2, check=lambda: {"installed": "2", "latest": "2",
                                                   "newer": False})
    assert d2._pending_update is None


def test_update_routes(server, monkeypatch):
    srv, d, base = server
    info = {"installed": "1", "latest": "2", "newer": True}
    monkeypatch.setattr(system_ops, "check_update", lambda: dict(info))
    assert json.loads(_req("GET", base + "/api/update/check", srv.token).read()) == info
    r = _req("POST", base + "/api/update/apply", srv.token, {})
    assert r.status == 202 and json.loads(r.read()) == {"updating": True, "version": "2"}
    assert d._pending_update == "2"
    info["newer"] = False
    with pytest.raises(urllib.error.HTTPError) as e:
        _req("POST", base + "/api/update/apply", srv.token, {})
    assert e.value.code == 409


# -- diagnostics ---------------------------------------------------------------

_DOCTOR_OUT = ("mcpbrain doctor\n\n"
               "✅ Gmail            Synced\n"
               "⚠️  Drive            Needs consent → run mcpbrain setup\n"
               "❌ Claude           not connected\n"
               "➖ Baseline         not checked\n")


def test_diagnostics_runs_doctor_without_repairs(tmp_path):
    seen = {}

    def fake_doctor(home, **kw):
        seen["home"] = home
        seen.update(kw)
        return 1, _DOCTOR_OUT

    diag = system_ops.Diagnostics(run_doctor=fake_doctor)
    assert diag.result() == {"running": False, "ran_at": None, "code": None, "lines": []}
    status = {"watchdog_exits": 0}
    assert diag.start(str(tmp_path), status) is True
    diag.join(5)
    assert seen["repairs"] == {} and seen["daemon_status"] is status
    res = diag.result()
    assert res["running"] is False and res["code"] == 1 and res["ran_at"]
    assert [ln["status"] for ln in res["lines"]] == ["info", "ok", "warn", "bad", "info"]
    assert res["lines"][2]["text"].startswith("⚠️")


def test_diagnostics_single_run_at_a_time(tmp_path):
    gate = threading.Event()

    def slow(home, **kw):
        gate.wait(5)
        return 0, "✅ ok\n"

    diag = system_ops.Diagnostics(run_doctor=slow)
    assert diag.start(str(tmp_path), {}) is True
    assert diag.result()["running"] is True
    assert diag.start(str(tmp_path), {}) is False
    gate.set()
    diag.join(5)
    assert diag.result()["code"] == 0


def test_diagnostics_doctor_failure_is_reported(tmp_path):
    def broken(home, **kw):
        raise RuntimeError("store locked")

    diag = system_ops.Diagnostics(run_doctor=broken)
    diag.start(str(tmp_path), {})
    diag.join(5)
    res = diag.result()
    assert res["running"] is False and res["code"] is None
    assert res["lines"][0]["status"] == "bad" and "store locked" in res["lines"][0]["text"]


def test_diagnostics_routes(server, monkeypatch):
    srv, d, base = server
    srv.diagnostics = system_ops.Diagnostics(run_doctor=lambda home, **kw: (0, "✅ fine\n"))
    r = _req("POST", base + "/api/diagnostics/run", srv.token, {})
    assert r.status == 202
    srv.diagnostics.join(5)
    body = json.loads(_req("GET", base + "/api/diagnostics", srv.token).read())
    assert body["code"] == 0 and body["lines"] == [{"status": "ok", "text": "✅ fine"}]


# -- logs and support report ---------------------------------------------------

def test_tail_log_returns_last_bytes_on_line_boundary(tmp_path):
    lines = [f"line {i}" for i in range(2000)]
    (tmp_path / "com.mcpbrain.err").write_text("\n".join(lines) + "\n")
    out = system_ops.tail_log(str(tmp_path), "daemon", max_bytes=100)
    assert len(out.encode()) <= 100
    assert out.endswith("line 1999\n")
    assert out.splitlines()[0].startswith("line ")
    assert system_ops.tail_log(str(tmp_path), "tray") == ""
    with pytest.raises(ValueError):
        system_ops.tail_log(str(tmp_path), "../config.json")


def test_tail_log_tray_files(tmp_path):
    (tmp_path / "com.mcpbrain.tray.log").write_text("tray up\n")
    assert "tray up" in system_ops.tail_log(str(tmp_path), "tray")


def test_logs_route_is_text_plain(server):
    srv, d, base = server
    (srv.home / "com.mcpbrain.err").write_text("hello log\n")
    r = _req("GET", base + "/api/logs/daemon", srv.token)
    assert r.headers["Content-Type"].startswith("text/plain")
    assert "hello log" in r.read().decode()
    with pytest.raises(urllib.error.HTTPError) as e:
        _req("GET", base + "/api/logs/secrets", srv.token)
    assert e.value.code == 404


def _owner_home(tmp_path):
    config.write_config(str(tmp_path), {
        "owner_email": "dana.okafor@example.org",
        "owner_full_name": "Dana Okafor",
        "owner_name": "Dana",
        "owner_aliases": ["DK", "Danny O"],
    })
    (tmp_path / "google_account").write_text("dana@acme.example.org\n")
    (tmp_path / "com.mcpbrain.err").write_text(
        "INFO sync: synced 4 threads for dana.okafor@example.org\n"
        "INFO graph: Dana Okafor met Sam Chen (DK notes)\n"
        "INFO graph: danny o replied; dana said yes\n"
        "INFO auth: account dana@acme.example.org ok\n"
        f"ERROR store: /Users/dana/Library/x.sqlite3 locked; home {tmp_path}/brain\n"
        "ERROR win: C:\\Users\\dana\\AppData\\mcpbrain failed\n")
    return str(tmp_path)


def test_support_report_redacts_owner_and_home_paths(tmp_path):
    home = _owner_home(tmp_path)
    diag = system_ops.Diagnostics(run_doctor=lambda h, **kw: (
        1, "⚠️  Gmail            dana.okafor@example.org needs consent\n"))
    diag.start(home, {})
    diag.join(5)
    report = system_ops.support_report(home, FakeDaemon().status(), diagnostics=diag)
    low = report.lower()
    for needle in ("dana.okafor@example.org", "dana@acme.example.org", "dana okafor",
                   "okafor", "dana", "danny o", "dk notes", "/users/", "c:\\users\\",
                   str(tmp_path).lower()):
        assert needle not in low, needle
    # The useful parts survive.
    assert "Sam Chen" in report
    assert "synced 4 threads" in report
    assert "9.9.9" in report or system_ops._version() in report
    assert "Diagnostics" in report and "needs consent" in report


def test_support_report_keeps_last_200_log_lines(tmp_path):
    (tmp_path / "com.mcpbrain.err").write_text(
        "".join(f"entry {i}\n" for i in range(500)))
    report = system_ops.support_report(str(tmp_path), {})
    assert "entry 499" in report and "entry 300" in report
    assert "entry 299\n" not in report


def test_support_report_route(server):
    srv, d, base = server
    _owner_home(srv.home)
    r = _req("GET", base + "/api/support/report", srv.token)
    assert r.headers["Content-Type"].startswith("text/plain")
    text = r.read().decode()
    assert "dana" not in text.lower() and "/Users/" not in text
