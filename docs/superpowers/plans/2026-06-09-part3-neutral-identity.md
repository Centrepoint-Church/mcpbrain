# Part 3 — Neutral Service Identity & Platform Hardening — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Take one person's and one org's name out of the service identity: rename every launchd/agent label from `church.centrepoint.{mcpbrain,joshbrain}.*` to `com.mcpbrain.*` / `com.mcpbrain.records.*`, move the `agent_errs` glob with it, and harden the `os.uname()` call in `config.app_dir()` so it can never raise on Windows.

**Architecture:** The labels in `agents.py` are module constants consumed by pure generator functions (`launchd_plist`, `_calendar_plist`, …) and by the install/restart helpers (via the derived path constants). Renaming the constants propagates everywhere. `agent_errs.GLOB` independently scans `<home>/<label>.err`, so it is renamed in lockstep. `config.app_dir()` already branches on `os.name == "nt"` first; the hardening swaps the macOS check from `os.uname().sysname` to `sys.platform`, removing the only call that has no Windows implementation.

**Tech Stack:** Python 3.12, pytest. Generators are pure and unit-tested; no OS calls in tests.

This is **Plan 3 of the productization series** — the first half of spec **1.6** (`docs/superpowers/specs/2026-06-09-mcpbrain-productization-design.md`), i.e. **1.6a: neutral identity + platform hardening**.

**Scope boundary — deferred to Plan 3b (1.6b, "Cross-platform cadence execution"):** the four cadences (prune, context-health, gardener, meeting-packs) are currently **launchd-only**, call scripts that live in the records repo (`{records}/bin/*.py|*.sh`), and are installed **only by `seed_joshbrain.py`** (never by the product). Making them run on Linux/Windows requires (a) porting that logic into `python -m mcpbrain <subcommand>`s, (b) systemd-timer + time-triggered `schtasks` generators, and (c) a product install path for cadences. That is a separate subsystem needing a discovery pass over the four scripts; Plan 3b owns it. This plan only renames the cadence **labels** (their identity) and leaves the generators otherwise intact for 3b to restructure.

---

## File Structure

- `mcpbrain/agents.py` — rename `_LABEL`, `_TRAY_LABEL` (line 24-25) and the four cadence labels (401-404).
- `mcpbrain/agent_errs.py` — rename `GLOB` (line 22) + docstring; `_agent_label` is unchanged (it strips `.err` regardless of prefix).
- `mcpbrain/config.py` — `app_dir()` (line 14-18): `os.uname().sysname == "Darwin"` → `sys.platform == "darwin"`; add `import sys`.
- `bin/seed_joshbrain.py` — update its hardcoded label dict keys (dev tool; keeps it consistent during transition).
- Tests: `tests/test_agents.py`, `tests/test_agents_calendar.py`, `tests/test_agent_errs.py`, `tests/test_daemon.py` (update existing assertions), `tests/test_config_appdir_platform.py` (new).

---

## Task 1: Rename daemon + tray labels → `com.mcpbrain` / `com.mcpbrain.tray`

**Files:**
- Modify: `mcpbrain/agents.py:24-25`
- Test: `tests/test_agents.py` (update existing assertions at lines 9, 33, 48)

- [ ] **Step 1: Update the tests to the new expected labels (they now fail)**

In `tests/test_agents.py`, replace the three label assertions:

- line 9: `assert "church.centrepoint.mcpbrain" in s` → `assert "com.mcpbrain" in s`
- line 33: `assert "church.centrepoint.mcpbrain.tray" in s` → `assert "com.mcpbrain.tray" in s`
- line 48: `assert "church.centrepoint.mcpbrain.tray" not in s` → `assert "com.mcpbrain.tray" not in s`

(Lines 73-74, 85-86 reference `agents._LABEL` / `agents._TRAY_LABEL` by attribute, not literal, so they need no change.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_agents.py -v`
Expected: FAIL (the daemon plist still contains `church.centrepoint.mcpbrain`).

- [ ] **Step 3: Rename the constants**

In `mcpbrain/agents.py`, replace lines 24-25:

```python
# Bundle identifier used as the launchd label and scheduled-task name.
_LABEL = "com.mcpbrain"
_TRAY_LABEL = f"{_LABEL}.tray"
```

(The derived path constants `_LAUNCHD_PATH`/`_TRAY_LAUNCHD_PATH` and the restart targets are f-strings off `_LABEL`/`_TRAY_LABEL`, so they update automatically.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_agents.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/agents.py tests/test_agents.py
git commit -m "feat(agents): daemon/tray labels -> com.mcpbrain(.tray)"
```

---

## Task 2: Rename cadence labels → `com.mcpbrain.records.*`

**Files:**
- Modify: `mcpbrain/agents.py:401-404`
- Test: `tests/test_agents_calendar.py` (update assertions at lines 19, 79-80, 84, 151, 175-176)

- [ ] **Step 1: Update the tests to the new expected labels (they now fail)**

In `tests/test_agents_calendar.py`, replace each old label literal with the new one:

- `church.centrepoint.joshbrain.prune` → `com.mcpbrain.records.prune` (lines 19, 79, 80)
- `church.centrepoint.joshbrain.context-health` → `com.mcpbrain.records.context-health` (line 84)
- `church.centrepoint.joshbrain.gardener` → `com.mcpbrain.records.gardener` (lines 151, 175, 176)

For the `.log`/`.err` path assertions (79-80, 175-176), update the full string, e.g.:
`"/Users/josh/.mcpbrain/church.centrepoint.joshbrain.prune.log"` → `"/Users/josh/.mcpbrain/com.mcpbrain.records.prune.log"`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_agents_calendar.py -v`
Expected: FAIL (plists still contain the old labels).

- [ ] **Step 3: Rename the constants**

In `mcpbrain/agents.py`, replace lines 401-404:

```python
_PRUNE_LABEL = "com.mcpbrain.records.prune"
_HEALTH_LABEL = "com.mcpbrain.records.context-health"
_MEETING_PACKS_LABEL = "com.mcpbrain.records.meeting-packs"
_GARDENER_LABEL = "com.mcpbrain.records.gardener"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_agents_calendar.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/agents.py tests/test_agents_calendar.py
git commit -m "feat(agents): cadence labels -> com.mcpbrain.records.*"
```

---

## Task 3: Move the `agent_errs` glob to `com.mcpbrain.records.*.err`

The error-surfacing scanner globs `<home>/<label>.err`. It must follow the cadence rename — and, as a bonus, the new prefix no longer risks catching the daemon's own `com.mcpbrain.err` (the `records.` segment scopes it to the cadences).

**Files:**
- Modify: `mcpbrain/agent_errs.py:22` (+ docstring lines 1-10, 29)
- Test: `tests/test_agent_errs.py` (update the `.err` filenames), `tests/test_daemon.py:204`

- [ ] **Step 1: Update the tests to the new filenames (they now fail)**

In `tests/test_agent_errs.py`, replace every `church.centrepoint.joshbrain.` with `com.mcpbrain.records.` in the `.err` filenames and the cursor key (lines 28, 36, 44, 56, 78, 93, 108, 114, 122, 160). Example:
`_write(home, "church.centrepoint.joshbrain.prune.err", ...)` → `_write(home, "com.mcpbrain.records.prune.err", ...)`; and the cursor assertion at 114:
`s.get_cursor("agent_err:church.centrepoint.joshbrain.prune.err")` → `s.get_cursor("agent_err:com.mcpbrain.records.prune.err")`.

In `tests/test_daemon.py:204`, replace `church.centrepoint.joshbrain.prune.err` → `com.mcpbrain.records.prune.err`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_agent_errs.py tests/test_daemon.py -v -k "err or agent"`
Expected: FAIL (the glob still matches only the old prefix, so the new-named files aren't scanned).

- [ ] **Step 3: Update the glob + docstring**

In `mcpbrain/agent_errs.py`:

- line 22: `GLOB = "com.mcpbrain.records.*.err"`
- update the module docstring (lines 3-4) to read `... write stderr to ~/.mcpbrain/com.mcpbrain.records.*.err.`
- update the `_agent_label` docstring example (line 29) to `"com.mcpbrain.records.prune.err -> com.mcpbrain.records.prune"`.

(`_agent_label`'s body just strips a trailing `.err`, so no logic changes.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_agent_errs.py tests/test_daemon.py -v -k "err or agent"`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/agent_errs.py tests/test_agent_errs.py tests/test_daemon.py
git commit -m "feat(agent_errs): scan com.mcpbrain.records.*.err"
```

---

## Task 4: Harden `config.app_dir()` against `os.uname()` on Windows

**Files:**
- Modify: `mcpbrain/config.py` (add `import sys`; line 14-18)
- Test: `tests/test_config_appdir_platform.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config_appdir_platform.py
"""app_dir() picks the OS path via sys.platform and never calls os.uname()."""
from pathlib import Path

from mcpbrain import config


def test_darwin_branch_without_os_uname(tmp_path, monkeypatch):
    # Simulate macOS, and DELETE os.uname to prove app_dir() does not use it
    # (os.uname does not exist on Windows; relying on it is the bug we're fixing).
    monkeypatch.delenv("MCPBRAIN_HOME", raising=False)
    monkeypatch.setattr(config.os, "name", "posix")
    monkeypatch.setattr(config.sys, "platform", "darwin")
    monkeypatch.delattr(config.os, "uname", raising=False)
    monkeypatch.setattr(config.Path, "home", classmethod(lambda cls: tmp_path))
    d = config.app_dir()
    assert d == tmp_path / "Library" / "Application Support" / "mcpbrain"


def test_linux_branch(tmp_path, monkeypatch):
    monkeypatch.delenv("MCPBRAIN_HOME", raising=False)
    monkeypatch.setattr(config.os, "name", "posix")
    monkeypatch.setattr(config.sys, "platform", "linux")
    monkeypatch.setattr(config.Path, "home", classmethod(lambda cls: tmp_path))
    assert config.app_dir() == tmp_path / ".mcpbrain"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config_appdir_platform.py -v`
Expected: FAIL — `test_darwin_branch_without_os_uname` raises `AttributeError` (current code calls `os.uname()`, which was just deleted), and `config.sys` may not exist yet.

- [ ] **Step 3: Write minimal implementation**

In `mcpbrain/config.py`, add `import sys` to the imports (with the existing `import os`), and change the macOS check in `app_dir()` (the `else` branch, lines 16-18) from:

```python
        d = Path.home() / "Library" / "Application Support" / "mcpbrain" \
            if os.uname().sysname == "Darwin" else Path.home() / ".mcpbrain"
```

to:

```python
        d = Path.home() / "Library" / "Application Support" / "mcpbrain" \
            if sys.platform == "darwin" else Path.home() / ".mcpbrain"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_config_appdir_platform.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/config.py tests/test_config_appdir_platform.py
git commit -m "fix(config): app_dir uses sys.platform, never os.uname (Windows-safe)"
```

---

## Task 5: Update the dev seed tool's hardcoded labels

`bin/seed_joshbrain.py` writes plists keyed by the old label strings. It is a dev-only tool (being demoted in a later plan), but keep it internally consistent so it still works during the transition.

**Files:**
- Modify: `bin/seed_joshbrain.py` (the `plists` dict keys near line 278)
- Test: none (dev tool; no test harness covers it) — verified by grep

- [ ] **Step 1: Rename the four dict keys**

In `bin/seed_joshbrain.py`, in the `plists = { ... }` dict, replace the keys:

```python
    "com.mcpbrain": launchd_plist(mcpbrain_bin=mcpbrain_bin, home=mcpbrain_home),
    "com.mcpbrain.tray": launchd_tray_plist(mcpbrain_bin=mcpbrain_bin, home=mcpbrain_home),
    "com.mcpbrain.records.prune": joshbrain_prune_plist(
        python_bin=python_bin, joshbrain_dir=joshbrain_dir, mcpbrain_home=mcpbrain_home),
    "com.mcpbrain.records.context-health": joshbrain_context_health_plist(
        python_bin=python_bin, joshbrain_dir=joshbrain_dir, mcpbrain_home=mcpbrain_home),
```

(The generator function names and their `joshbrain_dir=` parameter are intentionally left unchanged — Plan 3b restructures these generators for cross-platform.)

- [ ] **Step 2: Verify no old labels remain in source**

Run: `grep -rn "church.centrepoint" mcpbrain/ bin/`
Expected: no hits.

- [ ] **Step 3: Commit**

```bash
git add bin/seed_joshbrain.py
git commit -m "chore(seed): use com.mcpbrain.* labels (dev tool consistency)"
```

---

## Final: full suite + migration note

- [ ] **Step 1: Run the whole suite**

Run: `pytest -q`
Expected: PASS. Any remaining failure is a test still asserting an old `church.centrepoint.*` label — update it to the `com.mcpbrain.*` / `com.mcpbrain.records.*` equivalent.

- [ ] **Step 2: Lint**

Run: `ruff check mcpbrain/ tests/`
Expected: clean (confirm the new `import sys` in `config.py` is used).

- [ ] **Step 3: Confirm the rename is total**

Run: `grep -rni "church.centrepoint" mcpbrain/ bin/ tests/`
Expected: no hits.

- [ ] **Step 4: Document the one-time migration for existing installs**

Existing installs (e.g. the maintainer's Mac) still have the OLD launchd agents loaded under `church.centrepoint.*`. After upgrading to this version, the old and new agents would both try to run the daemon — the single-writer lock prevents corruption, but the stale agents should be removed once. Add this note to the release/CHANGELOG and run it once per existing machine:

```bash
for label in church.centrepoint.mcpbrain church.centrepoint.mcpbrain.tray \
             church.centrepoint.joshbrain.prune church.centrepoint.joshbrain.context-health \
             church.centrepoint.joshbrain.meeting-packs church.centrepoint.joshbrain.gardener; do
  launchctl unload -w "$HOME/Library/LaunchAgents/$label.plist" 2>/dev/null
  rm -f "$HOME/Library/LaunchAgents/$label.plist"
done
```

New installs are unaffected (they never had the old labels).

---

## Self-Review

**Spec coverage (1.6a):**
- Org-/person-neutral service identity → Tasks 1 (daemon/tray) + 2 (cadences) + 3 (agent_errs glob) + 5 (seed tool).
- `os.uname()` hardening → Task 4.
- Deferred and clearly scoped: cross-platform cadence *execution* (systemd/schtasks generators, `python -m mcpbrain` subcommand port, product cadence-install path, generator repo-path/param renames) → **Plan 3b**, which needs a discovery read of `{records}/bin/{prune_hot_md.py,context_health.py,run_memory_gardener.sh,build_meeting_packs.sh}`.

**Placeholder scan:** every step is a concrete edit with the exact old→new string and the test to update; no vague instructions. The migration shell snippet is complete.

**Type consistency:** label constants are renamed in one place each and consumed by attribute elsewhere (`agents._LABEL`, `agents._TRAY_LABEL`) so no signature drift. `GLOB`/`_agent_label` keep their types. `app_dir()` return type unchanged. The new-label strings used in tests (`com.mcpbrain`, `com.mcpbrain.tray`, `com.mcpbrain.records.{prune,context-health,meeting-packs,gardener}`) match the constants set in Tasks 1-2 exactly.
