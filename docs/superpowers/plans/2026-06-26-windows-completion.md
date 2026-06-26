# Windows Completion — Implementation Plan (v2, complete)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. **Do not commit during planning; only at execution.**

**Goal:** Make a real Windows rollout work end-to-end. The Windows *machinery* exists and is unit-tested (schtasks generators, `install_cadences` win32 branch, `%APPDATA%` paths, msvcrt lock, doctor win32 repair, in-process auto-update), but it has **never run on a real box** and a careful audit found seven concrete defects — two latent `cmd.exe` quoting bugs, a missing `MCPBRAIN_HOME` in cadences, a stale `reg delete`, **a persistent/ flashing console window**, **no daemon log capture**, and **a brittle `uv` lookup in auto-update**. This plan fixes all seven with a coherent launch architecture and then runs the manual hard gate that is the true ship blocker.

**Architecture — why a hidden-console launcher shim:** The daemon spawns console subprocesses constantly — `git` on every records commit (`records.py`, `records_write.py`, `records_cadences.py`, `consolidation.py`), `uv` on auto-update, `schtasks` on restart. That single fact dictates the design:

- The **current** action `cmd /c "set … && mcpbrain daemon"` gives the daemon a *visible* console that stays open for its whole life (defect) — but its `git`/`uv` children reuse that console, so there are no extra flashes.
- A **windowless exe** (`mcpbrainw`/gui-scripts) would remove the window, but then every `git` child would pop its own console window all day. Rejected.
- A **hidden-console launcher** (a generated `.vbs` that runs `mcpbrain <sub>` with window style 0) gives the daemon a *hidden* console its children inherit → **no visible window and no child flashes**. This is the only option that satisfies both. Its one cost is process lifecycle: a detached hidden launch isn't killable by `schtasks /end`, so restart switches to `taskkill /IM mcpbrain.exe` then `schtasks /run` (Task 2).

So Task 1 **replaces** the fragile inline `cmd /c "set …"` string (and both quoting bugs) with a generated `.vbs` shim written into `app_dir()`; the shim sets `MCPBRAIN_HOME` in-process (handles custom homes, so no `setx`, so the `reg delete` is genuinely dead and removed in Task 5), quotes the binary correctly in VBScript, and is registered as `wscript.exe "<shim>"` (a short `/tr`, so no length limit). All five Windows tasks (daemon, tray, prune, health, beacon) route through one shim generator. Tasks 3–4 make output durable (daemon self-logs on Windows) and auto-update robust (resolve `uv` + `CREATE_NO_WINDOW`). Tasks 6–7 fix the install copy. Task 8 is the manual gate.

**Tech Stack:** Python 3.12, pytest, ruff. Shim/arg generators are pure and unit-tested; install/uninstall/restart bodies stay `# pragma: no cover`. Tests: `uv run pytest`; lint: `uv run ruff check mcpbrain/`.

**Worktree & Dependencies:** Create an isolated worktree via superpowers:using-git-worktrees at execution. OWNS and edits only: `mcpbrain/agents.py`, `mcpbrain/daemon.py`, `mcpbrain/update.py`, `plugin/commands/install.md`, `plugin/INSTALL.md`, `docs/RELEASE-RUNBOOK.md`, the test files `tests/test_agents_windows_xplat.py` / `tests/test_agents_cadence_xplat.py` / `tests/test_daemon_logging.py` (new) / `tests/test_update.py`, and the four version-bump files. Tasks are independent and land 1→8; Task 8 (manual gate) gates the *rollout*, not the wheel.

---

## Verified codebase facts (re-read before coding)

- `mcpbrain/agents.py`:
  - `_schtasks_args(*, task_name, subcommand, mcpbrain_bin, home)` (L103) builds `action = f'cmd /c "set MCPBRAIN_HOME={quoted_home} && {quoted_bin} {subcommand}"'`. **BUG 1:** `set MCPBRAIN_HOME="C:\…"` bakes quotes into the value (correct cmd idiom is `set "VAR=value"`). **BUG 2:** outer `cmd /c "…"` + inner quotes triggers cmd's strip-first-and-last rule. **DEFECT (console):** `cmd /c` keeps a visible console open for the daemon's life. Both used by `schtasks_args` (daemon, L112) and `schtasks_tray_args` (tray, L117).
  - `_cadence_schtasks_args(*, task_name, subcommand, mcpbrain_bin, schedule)` (L440) → `"/tr", f"{quoted} {subcommand}"`, **no `MCPBRAIN_HOME`** (GAP 3). Callers `prune_schtasks_args` (L447), `health_schtasks_args` (L454). `fleet_beacon_schtasks_args` (L461) likewise omits it.
  - `_install_schtasks`/`_install_schtasks_tray`/`_install_cadences_schtasks` (L206/L273/L519) run the arg lists; `_uninstall_schtasks` (L214) runs `schtasks /delete` **and** a stale `reg delete HKCU\Environment /v MCPBRAIN_HOME /f` (L222) — nothing sets that var any more (GAP 4 / dead code).
  - `_restart_schtasks`/`_restart_schtasks_tray` (L228/L293) do `schtasks /end` then `/run`. With a *detached hidden* launch (Task 1) `/end` can't reach the daemon → restart must `taskkill` first (Task 2).
  - Dispatchers `install_agent`/`uninstall_agent`/`restart_agent`/`install_cadences` have working `win32` branches; `linux` raises `ValueError` (guarded by `tests/test_agents_no_linux.py` — leave intact).
- `mcpbrain/daemon.py` (L2082-2090): `logging.basicConfig()` to stdout/stderr only. macOS launchd captures these to `{home}/com.mcpbrain.log/.err` ([agents.py:52-79]); **a hidden-console (or any) schtasks launch captures nothing** → daemon crashes are invisible on Windows (DEFECT, logging). The daemon's `_emit`/enrich log writes to `logs/enrich.log` already, proving the app-dir `logs/` pattern.
- `mcpbrain/update.py` (L83-93): `update_from_index` runs `subprocess.run(["uv", "tool", "install", …])` via `_run` (L71). Bare `uv` may not be on the task's PATH (uv lives in `%USERPROFILE%\.local\bin`); no `CREATE_NO_WINDOW`, so a flash during update under the hidden console’s child. (DEFECT, auto-update.) `_restart_agent` uses `sys.platform` (→ `win32`, fine).
- `mcpbrain/config.py`: `app_dir()` → `%APPDATA%\mcpbrain` on Windows, honouring `MCPBRAIN_HOME` env first. So a shim that exports `MCPBRAIN_HOME` fully controls home with no persistent registry write.
- `pyproject.toml`: `[project.scripts] mcpbrain = "mcpbrain.cli:main"` — **console** entry point only; no gui-scripts (intentional — see Architecture; we keep the console exe and hide its console via the shim).
- `mcpbrain/setup.py`, `backup.py`/restore, `tray.py`, `doctor.py`, `_desktop_config_path`: confirmed cross-platform — **no change needed**.
- Version is `0.7.69` across the four sources of truth → this plan releases `0.7.70`.

---

## Task 1 — Replace the inline `cmd /c` action with a hidden-console `.vbs` launcher (fixes BUG 1, BUG 2, GAP 3, console-window; covers daemon/tray/prune/health/beacon)

**Files:** `mcpbrain/agents.py`; tests `tests/test_agents_windows_xplat.py`, `tests/test_agents_cadence_xplat.py`.

- [ ] **Write failing tests.** Rewrite the Windows action assertions around the shim. In `tests/test_agents_windows_xplat.py`:

```python
def test_win_shim_content_runs_subcommand_hidden_and_sets_home():
    vbs = agents._win_shim_content(
        mcpbrain_bin=r"C:\Program Files\mcpbrain\mcpbrain.exe",
        home=r"C:\Users\Jo Smith\mcpbrain", subcommand="daemon")
    # Env exported in-process (handles custom + spaced home) — no registry, no `set "VAR="`.
    assert r'"MCPBRAIN_HOME") = "C:\Users\Jo Smith\mcpbrain"' in vbs
    # Window style 0 = hidden; the daemon's git/uv children inherit the hidden console.
    assert ", 0, " in vbs or ", 0," in vbs
    # Binary quoted as one token via VBScript doubled-quotes; subcommand present.
    assert '""C:\\Program Files\\mcpbrain\\mcpbrain.exe""' in vbs
    assert "daemon" in vbs


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
    # And the shim content for a cadence carries MCPBRAIN_HOME + the right subcommand.
    vbs = agents._win_shim_content(mcpbrain_bin=r"C:\T\mcpbrain.exe",
                                   home=r"C:\Users\jo\mcpbrain", subcommand="records-prune")
    assert "MCPBRAIN_HOME" in vbs and "records-prune" in vbs
```

  In `tests/test_agents_cadence_xplat.py`, update `test_schtasks_prune_daily`/`test_schtasks_health_weekly_monday` to assert the `/sc daily`/`/sc weekly /d MON` flags **and** that `/tr` is a `wscript … .vbs` shim (the subcommand now lives in the shim, not the `/tr` string). Note `prune_schtasks_args`/`health_schtasks_args`/`fleet_beacon_schtasks_args` **gain a required `home=` kwarg**.

- [ ] **Run (expect FAIL):** `uv run pytest tests/test_agents_windows_xplat.py tests/test_agents_cadence_xplat.py -q`.

- [ ] **Implement** in `mcpbrain/agents.py`:
  1. Pure shim-content + path generators:

```python
_WIN_SHIM_DIR = "agents"  # under app_dir()/agents/<task>.vbs

def _win_shim_content(*, mcpbrain_bin: str, home: str, subcommand: str) -> str:
    """A .vbs that runs `mcpbrain <subcommand>` with a HIDDEN console.

    Window style 0 hides the console; the daemon's child git/uv processes inherit
    that hidden console, so nothing flashes. MCPBRAIN_HOME is set in-process so a
    custom/spaced home works without touching the registry. VBScript escapes a
    double-quote by doubling it.
    """
    bin_q = '""' + mcpbrain_bin + '""'          # ""C:\path\mcpbrain.exe""
    home_esc = home.replace('"', '""')
    return (
        'Set sh = CreateObject("WScript.Shell")\r\n'
        f'sh.Environment("PROCESS")("MCPBRAIN_HOME") = "{home_esc}"\r\n'
        f'sh.Run "{bin_q} {subcommand}", 0, False\r\n'
    )

def _win_shim_path(home: str, task_name: str) -> Path:
    return Path(home) / _WIN_SHIM_DIR / f"{task_name}.vbs"

def _schtasks_tr_for_shim(shim_path: Path) -> str:
    return f'wscript "{shim_path}"'
```

  2. Repoint `_schtasks_args` and the cadence/beacon builders to register `wscript "<shim>"` (they no longer embed the action; they return the arg list that points at the shim path). Add `home=` to the cadence/beacon signatures. Keep `/sc onlogon` for daemon/tray and the existing `/sc daily|weekly|hourly` schedules for the rest.
  3. In the install bodies (`_install_schtasks`, `_install_schtasks_tray`, `_install_cadences_schtasks` — all `# pragma: no cover`): **write the shim file** (`_win_shim_path(home, task).parent.mkdir(parents=True, exist_ok=True)`, then `write_text(_win_shim_content(...))`) **before** `subprocess.run(schtasks …)`. Pass `home=home` into the cadence/beacon arg builders.

- [ ] **Run (expect PASS):** `uv run pytest tests/test_agents_windows_xplat.py tests/test_agents_cadence_xplat.py tests/test_agents_no_linux.py -q`.
- [ ] **Lint:** `uv run ruff check mcpbrain/agents.py`
- [ ] **Commit:** `fix(agents): hidden-console .vbs launcher for Windows tasks (no window, no child flashes, env-correct)`

## Task 2 — Restart by `taskkill` + `/run` (detached hidden launch isn't killable by `/end`)

**Files:** `mcpbrain/agents.py`; test `tests/test_agents_windows_xplat.py`.

- [ ] **Write failing test** (mock `subprocess.run`, assert the new restart sequence):

```python
def test_restart_schtasks_taskkills_then_runs(monkeypatch):
    calls = []
    monkeypatch.setattr(agents.subprocess, "run",
        lambda args, **k: calls.append(list(map(str, args))) or
        __import__("types").SimpleNamespace(returncode=0))
    agents._restart_schtasks()
    flat = [" ".join(c) for c in calls]
    assert any("taskkill" in c and "mcpbrain" in c for c in flat)   # kill detached daemon
    assert any("schtasks" in c and "/run" in c for c in flat)        # relaunch via shim
    # /end can't reach a detached process; we must not rely on it alone.
    assert flat.index(next(c for c in flat if "taskkill" in c)) < \
           flat.index(next(c for c in flat if "/run" in c))
```

- [ ] **Run (expect FAIL):** `uv run pytest tests/test_agents_windows_xplat.py -k restart -q`.
- [ ] **Implement:** rewrite `_restart_schtasks` to `subprocess.run(["taskkill", "/F", "/IM", "mcpbrain.exe"], check=False)` then `subprocess.run(["schtasks", "/run", "/tn", _TASK_NAME], check=True)`. Do the same for `_restart_schtasks_tray` (best-effort, `check=False`). Drop the now-ineffective `schtasks /end`.
- [ ] **Run (expect PASS):** `uv run pytest tests/test_agents_windows_xplat.py -q`
- [ ] **Lint + Commit:** `fix(agents): Windows restart taskkills the daemon then re-runs the task`

## Task 3 — Daemon self-logs to a file on Windows (fixes invisible-output defect)

**Files:** `mcpbrain/daemon.py`; new `tests/test_daemon_logging.py`.

- [ ] **Write failing test.** Extract the logging setup into a testable helper and assert a Windows file handler:

```python
import logging
from mcpbrain import daemon

def test_windows_logging_attaches_file_handler(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon.sys, "platform", "win32")
    monkeypatch.setattr(daemon.config, "app_dir", lambda: tmp_path)
    root = logging.getLogger("mcpbrain.test-isolated")
    daemon._configure_logging(root)
    paths = [getattr(h, "baseFilename", "") for h in root.handlers]
    assert any(str(tmp_path / "com.mcpbrain.log") == p for p in paths)

def test_non_windows_logging_no_file_handler(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon.sys, "platform", "darwin")
    root = logging.getLogger("mcpbrain.test-isolated2")
    daemon._configure_logging(root)
    assert not any(getattr(h, "baseFilename", None) for h in root.handlers)
```

- [ ] **Run (expect FAIL):** `uv run pytest tests/test_daemon_logging.py -q`.
- [ ] **Implement:** add `_configure_logging(root=None)` that always sets the existing format/level, and **on `sys.platform == "win32"`** additionally attaches a `logging.handlers.RotatingFileHandler(config.app_dir()/"com.mcpbrain.log", maxBytes=1_000_000, backupCount=3)`. Replace the inline `logging.basicConfig(...)` at L2087 with a call to it. (macOS keeps launchd redirection; Windows now has a durable log even though the hidden console captures nothing.)
- [ ] **Run (expect PASS):** `uv run pytest tests/test_daemon_logging.py -q`
- [ ] **Lint + Commit:** `feat(daemon): durable rotating log file on Windows (schtasks captures no stdout)`

## Task 4 — Robust `uv` resolution + windowless subprocess in auto-update (fixes B)

**Files:** `mcpbrain/update.py`; test `tests/test_update.py`.

- [ ] **Write failing test:**

```python
def test_resolve_uv_prefers_path_then_local_bin(monkeypatch, tmp_path):
    from mcpbrain import update
    monkeypatch.setattr(update.shutil, "which", lambda n: None)
    fake = tmp_path / ".local" / "bin" / ("uv.exe" if update.os.name == "nt" else "uv")
    fake.parent.mkdir(parents=True); fake.write_text("")
    monkeypatch.setattr(update.Path, "home", classmethod(lambda cls: tmp_path))
    assert update._resolve_uv() == str(fake)

def test_resolve_uv_falls_back_to_bare_name(monkeypatch):
    from mcpbrain import update
    monkeypatch.setattr(update.shutil, "which", lambda n: None)
    monkeypatch.setattr(update.Path, "home", classmethod(lambda cls: __import__("pathlib").Path("/nonexistent")))
    assert update._resolve_uv() == "uv"
```

- [ ] **Run (expect FAIL):** `uv run pytest tests/test_update.py -k resolve_uv -q`.
- [ ] **Implement:** add `import shutil, os` and `from pathlib import Path`; add `_resolve_uv()` → `shutil.which("uv")` else `~/.local/bin/uv[.exe]` if it exists else `"uv"`. Use it in `update_from_index` (first arg of the install command). In `_run`, on Windows pass `creationflags=subprocess.CREATE_NO_WINDOW` so the `uv` child doesn't flash under the daemon's hidden console.
- [ ] **Run (expect PASS):** `uv run pytest tests/test_update.py -q`
- [ ] **Lint + Commit:** `fix(update): resolve uv via PATH+~/.local/bin; CREATE_NO_WINDOW on Windows`

## Task 5 — Remove the now-truly-dead `reg delete` from uninstall (GAP 4)

**Files:** `mcpbrain/agents.py`; test `tests/test_agents_windows_xplat.py`.

- [ ] **Write failing test:** assert `_uninstall_schtasks` does `schtasks /delete`, **deletes the shim file**, and never touches the registry:

```python
def test_uninstall_removes_task_and_shim_not_registry(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(agents.subprocess, "run",
        lambda args, **k: calls.append(" ".join(map(str, args))) or
        __import__("types").SimpleNamespace(returncode=0))
    # Shim present → uninstall should remove it.
    shim = agents._win_shim_path(str(tmp_path), agents._TASK_NAME)
    shim.parent.mkdir(parents=True); shim.write_text("x")
    agents._uninstall_schtasks(home=str(tmp_path))
    assert any("schtasks" in c and "/delete" in c for c in calls)
    assert "reg" not in " ".join(calls) and "MCPBRAIN_HOME" not in " ".join(calls)
    assert not shim.exists()
```

- [ ] **Run (expect FAIL):** `uv run pytest tests/test_agents_windows_xplat.py -k uninstall -q`.
- [ ] **Implement:** delete the `reg delete` block; add `home` param to `_uninstall_schtasks` and unlink `_win_shim_path(home, _TASK_NAME)` (missing_ok=True). Thread `home` through `uninstall_agent("win32", home=…)` — update the dispatcher signature and its one call site in `setup`/`doctor` if present (grep `uninstall_agent(`); default `home=str(config.app_dir())` to keep callers simple.
- [ ] **Run (expect PASS):** `uv run pytest tests/test_agents_windows_xplat.py -q`
- [ ] **Lint + Commit:** `fix(agents): uninstall removes the shim; drop stale HKCU reg-delete`

## Task 6 — Windows branch in the install command (`/mcpbrain:install`)

**Files:** `plugin/commands/install.md`. (`tests/test_plugin_assets.py` only checks existence — keep the file.)

- [ ] **Edit** to branch per-OS, keeping the macOS steps and adding Windows equivalents for the parts that differ:
  - **Install (Windows):** `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`, then the same `uv tool install --python 3.12 --index "mcpbrain=…" mcpbrain --force` and `mcpbrain setup`.
  - **Connect (Windows):** replace `osascript`/`open -a` with `taskkill /IM Claude.exe /F` (best-effort) → `mcpbrain connect` → relaunch Claude from the Start menu (or `start "" "%LOCALAPPDATA%\Programs\Claude\Claude.exe"`). Note `mcpbrain connect` itself is cross-platform.
  - **Verify (Windows):** `schtasks /query /tn mcpbrain` and `schtasks /query /tn mcpbrain-tray` in place of `launchctl list`. Note the daemon runs **without a visible window** (hidden-console shim) — absence of a window is expected, not a failure; check the task state and `%APPDATA%\mcpbrain\com.mcpbrain.log`.
  - **Run on startup / four Local tasks:** identical (OS-independent; reach mcpbrain via MCP tools).
  - Update the front-matter `description:` so it isn't "on this Mac".
- [ ] **Verify** front-matter intact; `uv run pytest tests/test_plugin_assets.py -q` green.
- [ ] **Commit:** `docs(install): add Windows branch to /mcpbrain:install`

## Task 7 — Windows section in `plugin/INSTALL.md`

**Files:** `plugin/INSTALL.md`.

- [ ] **Edit** the opener so it isn't Mac-only and add a "## Windows" subsection mirroring the macOS flow (marketplace → `/mcpbrain:install` → wizard → four Local tasks → Run on startup), cross-referencing `schtasks` verification and the `com.mcpbrain.log` location. Keep the "Local tasks, not Cloud routines" warning.
- [ ] **Commit:** `docs(install): document Windows install path in INSTALL.md`

## Task 8 — Run the Windows clean-machine HARD GATE (the real ship blocker)

**Files:** `docs/RELEASE-RUNBOOK.md` (record results in §5). Cannot be automated from this host.

> Do this on a clean Windows box with a **non-author** `@centrepoint.church` Google account, after Tasks 1–7 ship in `0.7.70`. The fixes are designed to pass it, but only a live cmd.exe/Task Scheduler proves the shim, hidden launch, restart, logging, and update paths.

- [ ] Work the 10-step §5 checklist, with emphasis on what this plan changed:
  - **PATH/uv:** `mcpbrain --version` in a fresh PowerShell after `irm …install.ps1 | iex`.
  - **Tasks registered + actually start:** `schtasks /query | findstr mcpbrain`; confirm the daemon task reaches **Running** and that **no console window appears** at logon (proves Task 1). Confirm `git` activity (a records commit) produces **no console flashes**.
  - **Custom home:** install with a custom `MCPBRAIN_HOME`, trigger `schtasks /run /tn mcpbrain-records-prune`, confirm prune touches *that* repo (proves the shim env export).
  - **Logging:** kill/restart and confirm `%APPDATA%\mcpbrain\com.mcpbrain.log` (or the custom home) fills (proves Task 3).
  - **Restart:** `mcpbrain doctor` / `mcpbrain update` restart leaves exactly **one** daemon (no lock-conflict second instance) — proves Task 2’s taskkill path.
  - **Auto-update:** force a behind-version state; confirm the in-process update runs `uv` with no visible window and succeeds (proves Task 4).
  - **Connector:** `%APPDATA%\Claude\claude_desktop_config.json` has the `mcpbrain` entry; `brain_*` tools appear after restart.
- [ ] **Record results inline in §5** (pass/fail per step, machine + date). Any gap → fix in `agents.py`/`daemon.py`/`update.py` and add a regression assertion before re-running.
- [ ] **Commit (if edits):** `docs(runbook): record Windows clean-machine gate results`

---

## Release (after Tasks 1–7 green; Task 8 gates rollout, not the wheel)

Follow `docs/RELEASE-RUNBOOK.md` §1 exactly. Bump `0.7.69 → 0.7.70` in the FOUR sources of truth (`pyproject.toml`, `mcpbrain/__init__.py`, `plugin/.claude-plugin/plugin.json`, `plugin/.claude-plugin/marketplace.json`); `uv run pytest -q` + `uv run ruff check mcpbrain/`; push `mcpbrain`; publish the wheel to `mcpbrain-dist` (purge the stale wheel); sync `plugin/` into `mcpbrain-plugin` via `git archive HEAD:plugin`. **Do not broaden the org marketplace to Windows users until Task 8 passes.**

---

## Final verification (before finishing the branch)

- [ ] **Full suite green:** `uv run pytest -q` (existing suite + updated `test_agents_windows_xplat.py`/`test_agents_cadence_xplat.py` + new `test_daemon_logging.py`/`test_update.py`; `test_agents_no_linux.py` unaffected).
- [ ] **Lint clean:** `uv run ruff check mcpbrain/ tests/`
- [ ] **No out-of-scope edits:** `git status --porcelain` shows only owned files.
- [ ] **Sweep iCloud conflict-copies** per the runbook env hazard before committing.
- [ ] Use superpowers:finishing-a-development-branch to decide merge/PR.

---

## Self-Review — does this address every bug and gap?

| # | Defect (file:line) | Fixed by |
|---|---|---|
| BUG 1 | `set VAR="…"` quote-in-value (`agents.py:108`) | Task 1 — inline `cmd /c` removed; env exported in the shim |
| BUG 2 | nested `cmd /c "…"` quote-strip (`agents.py:108`) | Task 1 — no inline cmd string at all |
| GAP 3 | cadences/beacon omit `MCPBRAIN_HOME` (`agents.py:440,461`) | Task 1 — all five tasks route through the env-setting shim |
| DEFECT | persistent/flashing console window | Task 1 — hidden-console shim; children inherit hidden console |
| DEFECT | `/end` can't kill detached daemon → restart spawns a 2nd | Task 2 — `taskkill` then `/run` |
| DEFECT | no daemon log capture on Windows (`daemon.py:2087`) | Task 3 — RotatingFileHandler on win32 |
| DEFECT | bare `uv` PATH + child flash in auto-update (`update.py:84`) | Task 4 — `_resolve_uv` + `CREATE_NO_WINDOW` |
| GAP 4 | stale `reg delete` (`agents.py:222`) | Task 5 — removed; shim file removed instead |
| GAP | macOS-only install command | Task 6 |
| GAP | macOS-only INSTALL.md | Task 7 |
| BLOCKER | never validated on real Windows | Task 8 — manual hard gate |

**Design coherence:** every architectural choice falls out of one fact — the daemon spawns console children all day, so the launcher must give it a *hidden* console (not no console). That choice makes BUG 1/2/GAP 3/console all disappear together (one shim generator), forces the restart change (Task 2) and the self-logging (Task 3), and makes the registry write genuinely dead (Task 5). Tasks 4/6/7 are independent robustness/doc fixes. **Confirmed out of scope (audited clean):** Linux/systemd (removed §9F, guarded), `setup.py`, `backup.py`/restore, `tray.py`, `doctor.py`, `_desktop_config_path`, the in-process update cadence.
