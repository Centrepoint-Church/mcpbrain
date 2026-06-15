from mcpbrain.agents import records_prune_plist, records_context_health_plist


def _prune(**kw):
    defaults = dict(mcpbrain_bin="/usr/local/bin/mcpbrain",
                    mcpbrain_home="/Users/x/.mcpbrain")
    return records_prune_plist(**{**defaults, **kw})


def _health(**kw):
    defaults = dict(mcpbrain_bin="/usr/local/bin/mcpbrain",
                    mcpbrain_home="/Users/x/.mcpbrain")
    return records_context_health_plist(**{**defaults, **kw})


def test_prune_label():
    assert "com.mcpbrain.records.prune" in _prune()


def test_prune_uses_calendar_interval():
    plist = _prune()
    assert "StartCalendarInterval" in plist
    assert "<key>Hour</key>" in plist
    assert "<key>Hour</key>\n        <integer>6</integer>" in plist


def test_prune_runs_at_load_no_keep_alive():
    plist = _prune()
    # No KeepAlive (one-shot job, not a resident service)...
    assert "KeepAlive" not in plist
    # ...but RunAtLoad so a run missed while powered off is caught up at the
    # next login/boot (idempotent: prune just re-trims hot.md).
    assert "<key>RunAtLoad</key>" in plist


def test_prune_program_arguments():
    plist = _prune(mcpbrain_bin="/opt/homebrew/bin/mcpbrain")
    assert "/opt/homebrew/bin/mcpbrain" in plist
    assert "records-prune" in plist
    assert "/bin/sh" not in plist
    assert "prune_hot_md.py" not in plist


def test_prune_plist_is_valid_xml():
    import xml.dom.minidom
    xml.dom.minidom.parseString(_prune())


def test_prune_log_paths_under_mcpbrain_home():
    plist = _prune(mcpbrain_home="/Users/user/.mcpbrain")
    assert "/Users/user/.mcpbrain/com.mcpbrain.records.prune.log" in plist
    assert "/Users/user/.mcpbrain/com.mcpbrain.records.prune.err" in plist


def test_health_label():
    assert "com.mcpbrain.records.context-health" in _health()


def test_health_weekly_monday():
    plist = _health()
    assert "StartCalendarInterval" in plist
    assert "<key>Weekday</key>" in plist
    assert "<key>Weekday</key>\n        <integer>1</integer>" in plist
    assert "<key>Hour</key>\n        <integer>7</integer>" in plist


def test_health_no_keep_alive():
    assert "KeepAlive" not in _health()


def test_health_program_arguments():
    plist = _health(mcpbrain_bin="/usr/bin/mcpbrain")
    assert "/usr/bin/mcpbrain" in plist
    assert "records-health" in plist
    assert "context_health.py" not in plist


def test_health_is_direct_invocation():
    plist = _health()
    assert "/bin/sh" not in plist
    assert "git commit" not in plist
    assert "&amp;&amp;" not in plist


def test_prune_still_runs_at_load():
    # Default run_at_load=True preserved for the cheap idempotent prune.
    assert "<key>RunAtLoad</key>" in _prune()


def test_context_health_still_runs_at_load():
    assert "<key>RunAtLoad</key>" in _health()


def test_prune_injects_mcpbrain_home():
    plist = _prune(mcpbrain_home="/Users/x/.mcpbrain")
    assert "MCPBRAIN_HOME" in plist
    assert "/Users/x/.mcpbrain" in plist


def test_health_injects_mcpbrain_home():
    plist = _health(mcpbrain_home="/Users/x/.mcpbrain")
    assert "MCPBRAIN_HOME" in plist
    assert "/Users/x/.mcpbrain" in plist
