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


# ---------------------------------------------------------------------------
# Task 1 — hidden-console .vbs shim replaces cmd /c inline action
# ---------------------------------------------------------------------------

def test_win_shim_content_runs_subcommand_hidden_and_sets_home():
    vbs = agents._win_shim_content(
        mcpbrain_bin=r"C:\Program Files\mcpbrain\mcpbrain.exe",
        home=r"C:\Users\Jo Smith\mcpbrain", subcommand="daemon")
    # Env exported in-process (handles custom + spaced home) — no registry, no `set "VAR="`.
    assert r'"MCPBRAIN_HOME") = "C:\Users\Jo Smith\mcpbrain"' in vbs
    # Window style 0 = hidden; the daemon's git/uv children inherit the hidden console.
    assert ", 0, " in vbs or ", 0," in vbs
    # Absolute installed launcher, quoted as one token via VBScript doubled-quotes;
    # subcommand present. No pythonw module form.
    assert '""C:\\Program Files\\mcpbrain\\mcpbrain.exe""' in vbs
    assert '""C:\\Program Files\\mcpbrain\\mcpbrain.exe"" daemon' in vbs
    assert "-m mcpbrain" not in vbs


def test_daemon_schtasks_runs_shim_via_wscript():
    a = agents.schtasks_args(mcpbrain_bin=r"C:\T\mcpbrain.exe", home=r"C:\Users\jo\mcpbrain")
    assert a[0] == "schtasks" and _flag_value(a, "/sc") == "onlogon"
    tr = _flag_value(a, "/tr")
    assert tr.lower().startswith("wscript")          # launched windowless via wscript
    assert tr.endswith('.vbs"') and "mcpbrain" in tr  # points at the generated shim
    # The two old bugs are gone by construction: no inline cmd, no `set VAR=`.
    assert "cmd /c" not in tr and "set MCPBRAIN_HOME=" not in tr


def test_cadence_and_beacon_shims_carry_home():
    for fn in (agents.prune_schtasks_args, agents.health_schtasks_args,
               agents.fleet_beacon_schtasks_args):
        a = fn(mcpbrain_bin=r"C:\T\mcpbrain.exe", home=r"C:\Users\jo\mcpbrain")
        assert _flag_value(a, "/tr").lower().startswith("wscript")
    # The shim content for a cadence carries MCPBRAIN_HOME + the right subcommand.
    vbs = agents._win_shim_content(mcpbrain_bin=r"C:\T\mcpbrain.exe",
                                   home=r"C:\Users\jo\mcpbrain", subcommand="records-prune")
    assert "MCPBRAIN_HOME" in vbs and "records-prune" in vbs


def test_tray_schtasks_args_well_formed():
    a = agents.schtasks_tray_args(mcpbrain_bin=r"C:\Tools\mcpbrain.exe", home=r"C:\Users\jo\mcpbrain")
    assert a[0] == "schtasks"
    assert _flag_value(a, "/tn") == "mcpbrain-tray"
    assert _flag_value(a, "/sc") == "onlogon"
    assert "tray" in _flag_value(a, "/tr")


def test_shim_path_embedded_in_schtasks_tr():
    # Spaces in home must be included inside the wscript "<path>" so Task Scheduler
    # finds the shim file correctly.
    a = agents.schtasks_args(
        mcpbrain_bin=r"C:\Program Files\mcpbrain\mcpbrain.exe",
        home=r"C:\Users\Jo Smith\mcpbrain")
    tr = _flag_value(a, "/tr")
    assert r"C:\Users\Jo Smith\mcpbrain" in tr


def test_prune_schtasks_args_well_formed():
    a = agents.prune_schtasks_args(mcpbrain_bin=r"C:\mcpbrain.exe", home=r"C:\Users\jo\mcpbrain")
    assert a[0] == "schtasks"
    assert _flag_value(a, "/tn") == "mcpbrain-records-prune"
    assert _flag_value(a, "/sc") == "daily"
    assert _flag_value(a, "/st") == "06:00"
    assert "records-prune" in _flag_value(a, "/tr")  # task name appears in shim path
    assert "/f" in a


def test_health_schtasks_args_well_formed():
    a = agents.health_schtasks_args(mcpbrain_bin=r"C:\mcpbrain.exe", home=r"C:\Users\jo\mcpbrain")
    assert a[0] == "schtasks"
    assert _flag_value(a, "/tn") == "mcpbrain-records-health"
    assert _flag_value(a, "/sc") == "weekly"
    assert _flag_value(a, "/d") == "MON"
    assert _flag_value(a, "/st") == "07:00"
    assert "records-health" in _flag_value(a, "/tr")  # task name appears in shim path


def test_shim_content_quotes_binary_with_spaces():
    # Installed launcher path with spaces is handled inside the shim via VBScript
    # double-quote doubling.
    vbs = agents._win_shim_content(
        mcpbrain_bin=r"C:\Program Files\mcpbrain\mcpbrain.exe",
        home=r"C:\Users\jo\mcpbrain", subcommand="records-prune")
    assert '""C:\\Program Files\\mcpbrain\\mcpbrain.exe""' in vbs


# ---------------------------------------------------------------------------
# Task 2 — restart uses taskkill + schtasks /run (not /end)
# ---------------------------------------------------------------------------

def test_restart_schtasks_taskkills_then_runs(monkeypatch):
    calls = []
    monkeypatch.setattr(agents.subprocess, "run",
        lambda args, **k: calls.append(list(map(str, args))) or
        __import__("types").SimpleNamespace(returncode=0))
    agents._restart_schtasks()
    flat = [" ".join(c) for c in calls]
    assert any("taskkill" in c and "mcpbrain" in c for c in flat)   # kill detached daemon
    assert any("schtasks" in c and "/run" in c for c in flat)        # relaunch via shim
    # /end can't reach a detached process; taskkill must come before /run.
    assert flat.index(next(c for c in flat if "taskkill" in c)) < \
           flat.index(next(c for c in flat if "/run" in c))


# ---------------------------------------------------------------------------
# Task 5 — uninstall removes shim file; no registry write
# ---------------------------------------------------------------------------

def test_uninstall_removes_task_and_shim_not_registry(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(agents.subprocess, "run",
        lambda args, **k: calls.append(" ".join(map(str, args))) or
        __import__("types").SimpleNamespace(returncode=0))
    shim = agents._win_shim_path(str(tmp_path), agents._TASK_NAME)
    shim.parent.mkdir(parents=True); shim.write_text("x")
    agents._uninstall_schtasks(home=str(tmp_path))
    assert any("schtasks" in c and "/delete" in c for c in calls)
    assert "reg" not in " ".join(calls) and "MCPBRAIN_HOME" not in " ".join(calls)
    assert not shim.exists()
