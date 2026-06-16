"""Cross-platform Windows schtasks generators produce well-formed arg lists.

Lives in its own file (NOT test_agents_cadence_xplat.py) so this worktree and
Spec 1 never collide. The live Windows validation is the manual runbook gate in
docs/RELEASE-RUNBOOK.md; these assertions exercise the pure generators in CI.
"""
from mcpbrain import agents


def _flag_value(args, flag):
    """Return the token immediately after `flag` in the arg list, or None."""
    for i, tok in enumerate(args):
        if tok == flag and i + 1 < len(args):
            return args[i + 1]
    return None


def test_daemon_schtasks_args_well_formed():
    a = agents.schtasks_args(mcpbrain_bin=r"C:\Tools\mcpbrain.exe", home=r"C:\Users\jo\mcpbrain")
    assert a[0] == "schtasks"
    assert "/create" in a and "/f" in a
    assert _flag_value(a, "/tn") == "mcpbrain"
    assert _flag_value(a, "/sc") == "onlogon"
    action = _flag_value(a, "/tr")
    assert action is not None
    # The daemon subcommand and the embedded home both appear in the action.
    assert "daemon" in action
    assert r"C:\Users\jo\mcpbrain" in action
    assert "MCPBRAIN_HOME" in action


def test_tray_schtasks_args_well_formed():
    a = agents.schtasks_tray_args(mcpbrain_bin=r"C:\Tools\mcpbrain.exe", home=r"C:\Users\jo\mcpbrain")
    assert a[0] == "schtasks"
    assert _flag_value(a, "/tn") == "mcpbrain-tray"
    assert _flag_value(a, "/sc") == "onlogon"
    assert "tray" in _flag_value(a, "/tr")


def test_schtasks_args_quote_paths_with_spaces():
    a = agents.schtasks_args(
        mcpbrain_bin=r"C:\Program Files\mcpbrain\mcpbrain.exe",
        home=r"C:\Users\Jo Smith\mcpbrain")
    action = _flag_value(a, "/tr")
    # Both the binary and the home (each containing a space) are quoted in the
    # embedded cmd action so schtasks parses them as single tokens.
    assert r'"C:\Program Files\mcpbrain\mcpbrain.exe"' in action
    assert r'"C:\Users\Jo Smith\mcpbrain"' in action


def test_prune_schtasks_args_well_formed():
    a = agents.prune_schtasks_args(mcpbrain_bin=r"C:\mcpbrain.exe")
    assert a[0] == "schtasks"
    assert _flag_value(a, "/tn") == "mcpbrain-records-prune"
    assert _flag_value(a, "/sc") == "daily"
    assert _flag_value(a, "/st") == "06:00"
    assert "records-prune" in _flag_value(a, "/tr")
    assert "/f" in a


def test_health_schtasks_args_well_formed():
    a = agents.health_schtasks_args(mcpbrain_bin=r"C:\mcpbrain.exe")
    assert a[0] == "schtasks"
    assert _flag_value(a, "/tn") == "mcpbrain-records-health"
    assert _flag_value(a, "/sc") == "weekly"
    assert _flag_value(a, "/d") == "MON"
    assert _flag_value(a, "/st") == "07:00"
    assert "records-health" in _flag_value(a, "/tr")


def test_health_and_prune_args_quote_binary_with_spaces():
    a = agents.prune_schtasks_args(mcpbrain_bin=r"C:\Program Files\mcpbrain\mcpbrain.exe")
    action = _flag_value(a, "/tr")
    assert action.startswith(r'"C:\Program Files\mcpbrain\mcpbrain.exe"')
