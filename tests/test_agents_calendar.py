from mcpbrain.agents import joshbrain_prune_plist, joshbrain_context_health_plist, joshbrain_gardener_plist


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


def test_prune_runs_at_load_no_keep_alive():
    plist = _prune()
    # No KeepAlive (one-shot job, not a resident service)...
    assert "KeepAlive" not in plist
    # ...but RunAtLoad so a run missed while powered off is caught up at the
    # next login/boot (idempotent: prune just re-trims hot.md).
    assert "<key>RunAtLoad</key>" in plist


def test_prune_program_arguments():
    plist = _prune(python_bin="/opt/homebrew/bin/python3",
                   joshbrain_dir="/Users/josh/joshbrain")
    # The job is now a single /bin/sh -c wrapper string, not bare arg vector.
    assert "/bin/sh" in plist
    assert "<string>-c</string>" in plist
    # python_bin and the prune script path appear inside the command string.
    assert "/opt/homebrew/bin/python3" in plist
    assert "prune_hot_md.py" in plist
    assert "/Users/josh/joshbrain/bin/prune_hot_md.py" in plist


def test_prune_commits_hot_md():
    plist = _prune(joshbrain_dir="/Users/josh/joshbrain")
    # The wrapper stages and conditionally commits state/hot.md.
    assert "git add state/hot.md" in plist
    assert "git diff --cached --quiet" in plist
    assert "git commit" in plist
    assert "cd /Users/josh/joshbrain" in plist


def test_prune_commit_scoped_to_hot_md():
    # Both the diff check and the commit carry a pathspec: files another
    # session left staged must neither trigger nor join the launchd commit.
    plist = _prune()
    assert "git diff --cached --quiet -- state/hot.md" in plist
    assert "git commit -m 'prune: hot.md (launchd)' -- state/hot.md" in plist


def test_prune_xml_escapes_shell_operators():
    # && must be XML-escaped as &amp;&amp; for the plist to be well-formed.
    plist = _prune()
    assert "&amp;&amp;" in plist
    assert "&& " not in plist  # no raw && in the generated XML
    # The generated XML parses cleanly.
    import xml.dom.minidom
    xml.dom.minidom.parseString(plist)


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


def test_health_is_direct_python_invocation():
    # The context-health job stays a direct python call — no shell wrapper,
    # no git commit (it only reads).
    plist = _health()
    assert "/bin/sh" not in plist
    assert "git commit" not in plist
    assert "&amp;&amp;" not in plist


class TestMeetingPacksPlist:
    def test_plist_has_calendar_intervals(self, tmp_path):
        from mcpbrain import agents
        plist = agents.meeting_packs_plist(str(tmp_path))
        assert "StartCalendarInterval" in plist
        # Should fire twice daily
        assert plist.count("<key>Minute</key>") >= 2

    def test_plist_includes_mcpbrain_home(self, tmp_path):
        from mcpbrain import agents
        plist = agents.meeting_packs_plist(str(tmp_path))
        assert str(tmp_path) in plist

    def test_plist_calls_meeting_packs_script(self, tmp_path):
        from mcpbrain import agents
        plist = agents.meeting_packs_plist(str(tmp_path))
        assert "meeting-packs" in plist.lower() or "meeting_packs" in plist.lower()

    def test_plist_fires_at_0745_and_1200(self, tmp_path):
        import plistlib
        from mcpbrain import agents
        plist = agents.meeting_packs_plist(str(tmp_path))
        parsed = plistlib.loads(plist.encode())
        intervals = parsed["StartCalendarInterval"]
        assert isinstance(intervals, list) and len(intervals) == 2
        times = {(i["Hour"], i["Minute"]) for i in intervals}
        assert times == {(7, 45), (12, 0)}


def _gardener(**kw):
    defaults = dict(joshbrain_dir="/Users/x/joshbrain",
                    mcpbrain_home="/Users/x/.mcpbrain")
    return joshbrain_gardener_plist(**{**defaults, **kw})


def test_gardener_label():
    assert "church.centrepoint.joshbrain.gardener" in _gardener()


def test_gardener_weekly_monday_0800():
    plist = _gardener()
    assert "StartCalendarInterval" in plist
    assert "<key>Weekday</key>\n        <integer>1</integer>" in plist
    assert "<key>Hour</key>\n        <integer>8</integer>" in plist
    assert "<key>Minute</key>\n        <integer>0</integer>" in plist


def test_gardener_no_keep_alive():
    assert "KeepAlive" not in _gardener()


def test_gardener_program_arguments():
    plist = _gardener(joshbrain_dir="/Users/josh/joshbrain")
    assert "/bin/bash" in plist
    assert "run_memory_gardener.sh" in plist
    assert "/Users/josh/joshbrain/bin/run_memory_gardener.sh" in plist


def test_gardener_log_paths_under_mcpbrain_home():
    plist = _gardener(mcpbrain_home="/Users/josh/.mcpbrain")
    assert "/Users/josh/.mcpbrain/church.centrepoint.joshbrain.gardener.log" in plist
    assert "/Users/josh/.mcpbrain/church.centrepoint.joshbrain.gardener.err" in plist


# --- FIX F: gardener must NOT RunAtLoad (expensive weekly headless session) --

def test_gardener_no_run_at_load():
    # The weekly gardener fires an expensive headless `claude` session; it must
    # NOT also run on every login/reboot.
    assert "RunAtLoad" not in _gardener()


def test_prune_still_runs_at_load():
    # Default run_at_load=True preserved for the cheap idempotent prune.
    assert "<key>RunAtLoad</key>" in _prune()


def test_context_health_still_runs_at_load():
    assert "<key>RunAtLoad</key>" in _health()
