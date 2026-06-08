from mcpbrain.agents import (
    launchd_plist, systemd_unit, schtasks_args,
    launchd_tray_plist, systemd_tray_unit, schtasks_tray_args,
)


def test_launchd_plist_runs_daemon():
    s = launchd_plist(mcpbrain_bin="/usr/local/bin/mcpbrain", home="/Users/j/.mcpbrain")
    assert "church.centrepoint.mcpbrain" in s and "/usr/local/bin/mcpbrain" in s and "daemon" in s
    assert "MCPBRAIN_HOME" in s and "<key>RunAtLoad</key>" in s


def test_systemd_unit_runs_daemon():
    s = systemd_unit(mcpbrain_bin="/home/j/.local/bin/mcpbrain", home="/home/j/.mcpbrain")
    assert "ExecStart=/home/j/.local/bin/mcpbrain daemon" in s
    assert "Environment=MCPBRAIN_HOME=/home/j/.mcpbrain" in s and "WantedBy=default.target" in s


def test_schtasks_args_at_logon():
    a = schtasks_args(mcpbrain_bin=r"C:\Users\j\mcpbrain.exe", home=r"C:\Users\j\.mcpbrain")
    assert "/sc" in a and "onlogon" in a and any("mcpbrain.exe daemon" in x for x in a)


def test_schtasks_args_quotes_path_with_spaces():
    a = schtasks_args(mcpbrain_bin=r"C:\Program Files\mcpbrain.exe", home=r"C:\Users\j\.mcpbrain")
    # The /tr value must wrap the spaced path in double-quotes.
    expected_tr = r'"C:\Program Files\mcpbrain.exe" daemon'
    assert expected_tr in a


def test_launchd_tray_plist_runs_tray_and_does_not_respawn():
    s = launchd_tray_plist(mcpbrain_bin="/usr/local/bin/mcpbrain", home="/Users/j/.mcpbrain")
    assert "church.centrepoint.mcpbrain.tray" in s
    assert "<string>tray</string>" in s and "<key>RunAtLoad</key>" in s
    # Quitting the icon must not respawn it.
    assert "<key>KeepAlive</key>\n    <false/>" in s


def test_daemon_launchd_plist_keeps_alive():
    # The daemon agent (unchanged) must still KeepAlive=true after the refactor.
    s = launchd_plist(mcpbrain_bin="/usr/local/bin/mcpbrain", home="/Users/j/.mcpbrain")
    assert "<key>KeepAlive</key>\n    <true/>" in s
    assert "church.centrepoint.mcpbrain.tray" not in s   # daemon label is distinct


def test_systemd_tray_unit_runs_tray():
    s = systemd_tray_unit(mcpbrain_bin="/home/j/.local/bin/mcpbrain", home="/home/j/.mcpbrain")
    assert "ExecStart=/home/j/.local/bin/mcpbrain tray" in s
    assert "Restart=no" in s and "WantedBy=default.target" in s


def test_schtasks_tray_args_at_logon():
    a = schtasks_tray_args(mcpbrain_bin=r"C:\Users\j\mcpbrain.exe", home=r"C:\Users\j\.mcpbrain")
    assert "mcpbrain-tray" in a and "onlogon" in a and any("mcpbrain.exe tray" in x for x in a)


def test_restart_agent_restarts_daemon_and_tray_darwin(monkeypatch, tmp_path):
    """restart_agent kicks BOTH the daemon and the tray (one system)."""
    import mcpbrain.agents as agents
    calls = []
    monkeypatch.setattr(agents.subprocess, "run",
                        lambda cmd, **k: calls.append(cmd) or None)
    # tray plist must appear to exist so the best-effort tray restart fires
    monkeypatch.setattr(agents, "_TRAY_LAUNCHD_PATH", tmp_path / "tray.plist")
    (tmp_path / "tray.plist").write_text("x")
    agents.restart_agent("darwin")
    kicked = [c for c in calls if "kickstart" in c]
    assert any(agents._LABEL in " ".join(c) for c in kicked), "daemon not restarted"
    assert any(agents._TRAY_LABEL in " ".join(c) for c in kicked), "tray not restarted"


def test_restart_agent_tray_absent_is_not_fatal(monkeypatch, tmp_path):
    """No tray registered -> daemon still restarts, no error."""
    import mcpbrain.agents as agents
    calls = []
    monkeypatch.setattr(agents.subprocess, "run",
                        lambda cmd, **k: calls.append(cmd) or None)
    monkeypatch.setattr(agents, "_TRAY_LAUNCHD_PATH", tmp_path / "absent.plist")
    agents.restart_agent("darwin")  # must not raise
    assert any("kickstart" in c and agents._LABEL in " ".join(c) for c in calls)
    assert not any(agents._TRAY_LABEL in " ".join(c) for c in calls)
