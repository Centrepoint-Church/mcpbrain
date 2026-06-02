from mcpbrain.agents import launchd_plist, systemd_unit, schtasks_args


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
