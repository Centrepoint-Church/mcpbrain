from mcpbrain.agents import joshbrain_prune_plist, joshbrain_context_health_plist


def _prune(**kw):
    defaults = dict(python_bin="/usr/bin/python3",
                    joshbrain_dir="/Users/x/joshbrain",
                    mcpbrain_home="/Users/x/.mcpbrain")
    return joshbrain_prune_plist(**{**defaults, **kw})


def _health(**kw):
    defaults = dict(python_bin="/usr/bin/python3",
                    joshbrain_dir="/Users/x/joshbrain",
                    mcpbrain_home="/Users/x/.mcpbrain")
    return joshbrain_context_health_plist(**{**defaults, **kw})


def test_prune_label():
    assert "church.centrepoint.joshbrain.prune" in _prune()


def test_prune_uses_calendar_interval():
    plist = _prune()
    assert "StartCalendarInterval" in plist
    assert "<key>Hour</key>" in plist
    assert "<key>Hour</key>\n        <integer>6</integer>" in plist


def test_prune_no_keep_alive_no_run_at_load():
    plist = _prune()
    assert "KeepAlive" not in plist
    assert "RunAtLoad" not in plist


def test_prune_program_arguments():
    plist = _prune(python_bin="/opt/homebrew/bin/python3",
                   joshbrain_dir="/Users/josh/joshbrain")
    assert "/opt/homebrew/bin/python3" in plist
    assert "prune_hot_md.py" in plist
    assert "/Users/josh/joshbrain/bin/prune_hot_md.py" in plist


def test_prune_log_paths_under_mcpbrain_home():
    plist = _prune(mcpbrain_home="/Users/josh/.mcpbrain")
    assert "/Users/josh/.mcpbrain/church.centrepoint.joshbrain.prune.log" in plist
    assert "/Users/josh/.mcpbrain/church.centrepoint.joshbrain.prune.err" in plist


def test_health_label():
    assert "church.centrepoint.joshbrain.context-health" in _health()


def test_health_weekly_monday():
    plist = _health()
    assert "StartCalendarInterval" in plist
    assert "<key>Weekday</key>" in plist
    assert "<key>Weekday</key>\n        <integer>1</integer>" in plist
    assert "<key>Hour</key>\n        <integer>7</integer>" in plist


def test_health_no_keep_alive():
    assert "KeepAlive" not in _health()


def test_health_program_arguments():
    plist = _health(python_bin="/usr/bin/python3",
                    joshbrain_dir="/Users/josh/joshbrain")
    assert "context_health.py" in plist
    assert "/Users/josh/joshbrain/bin/context_health.py" in plist
