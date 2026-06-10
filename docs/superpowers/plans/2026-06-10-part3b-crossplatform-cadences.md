# Part 3b — Cross-Platform Cadence Execution — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the deterministic records cadences (prune, context-health) run on **macOS, Linux, and Windows** by porting their logic into `python -m mcpbrain` subcommands (no shell, no records-repo scripts) and adding systemd-timer + Windows Task Scheduler generators alongside the launchd ones, with a real product install path so onboarding schedules them on every OS.

**Architecture:** Today the cadences are launchd-only `/bin/sh`/`/bin/bash` wrappers that call scripts living in the records repo (`{records}/bin/*.py|*.sh`) — scripts a freshly-scaffolded records repo doesn't even contain (Plan 2). This plan moves the logic *into the product*: a new `mcpbrain/records_cadences.py` holds the prune + health logic, exposed as `mcpbrain records-prune` and `mcpbrain records-health` CLI subcommands that operate on `config.records_dir(home)` and commit via the existing `records_write` helpers. The `agents.py` generators are repointed from records-repo shell scripts to these subcommands, and gain systemd `.timer` + `schtasks` time-triggered variants. A new `install_cadences()` writes/loads them per-OS and is called from `setup`.

**Tech Stack:** Python 3.12, pytest, local `git`. Generators are pure/tested; install bodies are `# pragma: no cover`.

This is **Plan 3b of the productization series** — spec **§1.6b**.

**Scope boundary — deferred (gardener + meeting-packs):** those two cadences shell to `claude` headless against cowork prompt files in the records repo (`cowork/memory-gardener.md`, `cowork/meeting-packs.md`) which a fresh records repo does not ship. Making them cross-platform also requires shipping that cowork-prompt content as product scaffolding and a `claude`-availability story — a separate effort. This plan covers the two **deterministic** cadences every user needs for records hygiene; gardener/meeting-packs stay launchd-only (and dev-seed-installed) until a follow-up (**§1.6c**, to be added to the spec).

**Grounding (verified):**
- CLI dispatch: `cli.py` builds subparsers from a tuple `("daemon","mcp-server","auth","setup","update","register","tray")` and a dispatch dict; add new names + handlers there. `tests/test_cli.py` monkeypatches each handler and asserts routing.
- Existing cadence generators: `agents.py:531-594` (`records_prune_plist`, `records_context_health_plist`) already take `records_dir` and call `<records_dir>/bin/<script>` via `/bin/sh`/python. `_calendar_plist(label, program_args, mcpbrain_home, hour, minute, weekday=None, run_at_load=True, env_vars=...)` is the launchd helper.
- Cross-platform pattern to mirror: `_systemd_unit(...)` (service unit) at `agents.py:103-117`; `_schtasks_args(...)` at `agents.py:132-143` (currently `/sc onlogon`).
- Commit helper: `records_write._commit_file(repo, relpath, message) -> bool` (stage-by-name, commit only if staged).
- Source to port from (on disk, readable): `~/joshbrain/bin/prune_hot_md.py` (234 lines; drops `**YYYY-MM-DD:**`-prefixed entries older than `--days` default 14 from `state/hot.md`; idempotent; exits 0; no git) and `~/joshbrain/bin/context_health.py` (74 lines; reads MEMORY.md/state/hot.md/`<mcpbrain_home>/context/memory.md`; prints WARN lines to stderr; exit 1 if any warning else 0; read-only).

---

## File Structure

- `mcpbrain/records_cadences.py` — **new**: `prune_hot_md(repo) -> int`, `context_health(repo, mcpbrain_home) -> list[str]`, and `main(argv)` for both subcommands.
- `mcpbrain/cli.py` — register `records-prune` + `records-health`.
- `mcpbrain/agents.py` — repoint the cadence generators to the subcommands; add `systemd_timer_units()` + `schtasks_cadence_args()`; add `install_cadences()`/`uninstall_cadences()`.
- `mcpbrain/setup.py` — call `install_cadences()` during onboarding (best-effort).
- Tests: `tests/test_records_cadences.py`, `tests/test_agents_cadence_xplat.py` (new); extend `tests/test_cli.py`.

---

## Task 1: Port the prune cadence → `mcpbrain records-prune`

**Files:**
- Create: `mcpbrain/records_cadences.py`
- Test: `tests/test_records_cadences.py`

- [ ] **Step 1: Write the failing behavioral test** (this is the port's correctness oracle)

```python
# tests/test_records_cadences.py
"""Records cadences ported into the product: prune + context-health."""
import subprocess
from datetime import datetime, timedelta, timezone

from mcpbrain import records, records_cadences


def _repo(tmp_path):
    repo = str(tmp_path / "records")
    records.ensure_records_repo(repo, git_name="t", git_email="t@t")
    return repo


def _hot(repo):
    from pathlib import Path
    return Path(repo) / "state" / "hot.md"


def test_prune_drops_entries_older_than_14_days(tmp_path):
    repo = _repo(tmp_path)
    today = datetime.now(timezone.utc).date()
    old = (today - timedelta(days=40)).isoformat()
    recent = (today - timedelta(days=2)).isoformat()
    hot = _hot(repo)
    hot.write_text(
        "# Hot — active continuity\n\n## Just decided\n"
        f"- **{recent}:** keep me\n"
        f"- **{old}:** drop me\n"
    )
    removed = records_cadences.prune_hot_md(repo)
    body = hot.read_text()
    assert "keep me" in body
    assert "drop me" not in body
    assert removed >= 1


def test_prune_is_idempotent_no_op_on_fresh(tmp_path):
    repo = _repo(tmp_path)  # scaffold hot.md has no dated entries
    assert records_cadences.prune_hot_md(repo) == 0


def test_records_prune_subcommand_commits(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))  # records_dir default = <home>/records
    today = datetime.now(timezone.utc).date()
    old = (today - timedelta(days=40)).isoformat()
    _hot(repo).write_text(f"# Hot\n\n## Just decided\n- **{old}:** drop me\n")
    # commit the seeded edit so the prune commit is isolated
    subprocess.run(["git", "-C", repo, "commit", "-am", "seed"], check=True, capture_output=True)
    rc = records_cadences.main(["records-prune"])
    assert rc == 0
    log = subprocess.run(["git", "-C", repo, "log", "--oneline"],
                         capture_output=True, text=True).stdout
    assert "prune" in log.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_records_cadences.py -v -k prune`
Expected: FAIL (`ModuleNotFoundError: No module named 'mcpbrain.records_cadences'`).

- [ ] **Step 3: Implement — port the algorithm from the existing script**

Create `mcpbrain/records_cadences.py`. **Port the prune algorithm faithfully from `~/joshbrain/bin/prune_hot_md.py`** (read that file; preserve its `**YYYY-MM-DD:**`-entry parsing and the `--days` default of 14). Wrap it as:

```python
"""Records-hygiene cadences, ported into the product so they run on every OS.

`mcpbrain records-prune`  — drop hot.md entries older than N days, then commit.
`mcpbrain records-health` — read-only checks; exit 1 if any warning.

Both operate on config.records_dir(home); no shell, no records-repo scripts.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mcpbrain import config, records_write

_PRUNE_DAYS = 14
_DATE_RE = None  # set in prune_hot_md; entries look like "- **YYYY-MM-DD:** ..."


def prune_hot_md(repo: str, *, days: int = _PRUNE_DAYS, now=None) -> int:
    """Remove hot.md lines whose `**YYYY-MM-DD:**` date is older than `days`.

    Returns the number of entries removed. Idempotent; does not commit (the
    subcommand commits). Ported from joshbrain/bin/prune_hot_md.py — preserve its
    entry-matching (date-prefixed lines) and its keep-everything-else behavior.
    """
    import re
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=days)).date()
    p = Path(repo) / "state" / "hot.md"
    if not p.exists():
        return 0
    lines = p.read_text().splitlines(keepends=True)
    out, removed = [], 0
    rx = re.compile(r"^\s*-\s+\*\*(\d{4}-\d{2}-\d{2}):\*\*")
    for line in lines:
        m = rx.match(line)
        if m:
            try:
                d = datetime.strptime(m.group(1), "%Y-%m-%d").date()
            except ValueError:
                out.append(line); continue
            if d < cutoff:
                removed += 1
                continue
        out.append(line)
    if removed:
        p.write_text("".join(out))
    return removed


def context_health(repo: str, mcpbrain_home: str) -> list[str]:
    """Read-only health checks; return a list of WARN strings (empty == healthy).
    Ported from joshbrain/bin/context_health.py — preserve its three checks
    (MEMORY.md line-count, hot.md stale entries, <mcpbrain_home>/context/memory.md)."""
    warnings: list[str] = []
    # ... port the three checks here, appending WARN strings ...
    return warnings


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(prog="mcpbrain records-cadence")
    ap.add_argument("cmd", choices=["records-prune", "records-health"])
    ap.add_argument("--days", type=int, default=_PRUNE_DAYS)
    ns = ap.parse_args(argv)
    home = str(config.app_dir())
    repo = config.records_dir(home)
    if ns.cmd == "records-prune":
        n = prune_hot_md(repo, days=ns.days)
        if n:
            records_write._commit_file(repo, "state/hot.md", "prune: hot.md")
        print(f"pruned {n} entries")
        return 0
    warnings = context_health(repo, home)
    for w in warnings:
        print(w, file=sys.stderr)
    return 1 if warnings else 0
```

(The `context_health` body and the exact prune entry-format are ported from the two on-disk scripts; the behavioral tests above + Task 2's tests are the oracle.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_records_cadences.py -v -k prune`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/records_cadences.py tests/test_records_cadences.py
git commit -m "feat(cadences): port prune into mcpbrain records-prune (cross-platform)"
```

---

## Task 2: Port context-health + wire both subcommands into the CLI

**Files:**
- Modify: `mcpbrain/records_cadences.py` (`context_health` body)
- Modify: `mcpbrain/cli.py`
- Test: `tests/test_records_cadences.py` (health), `tests/test_cli.py`

- [ ] **Step 1: Write the failing tests**

```python
# add to tests/test_records_cadences.py
def test_context_health_clean_repo_no_warnings(tmp_path):
    repo = _repo(tmp_path)
    assert records_cadences.context_health(repo, str(tmp_path)) == []


def test_context_health_warns_on_stale_hot_entry(tmp_path):
    repo = _repo(tmp_path)
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc).date() - timedelta(days=40)).isoformat()
    _hot(repo).write_text(f"# Hot\n\n## Just decided\n- **{old}:** ancient\n")
    warnings = records_cadences.context_health(repo, str(tmp_path))
    assert any("hot.md" in w for w in warnings)
```

```python
# add to tests/test_cli.py (mirror the existing monkeypatch-routing pattern)
def test_dispatch_records_cadences(monkeypatch):
    import mcpbrain.cli as cli
    seen = {}
    monkeypatch.setattr("mcpbrain.records_cadences.main",
                        lambda argv: seen.setdefault("argv", argv) or 0)
    cli.main(["records-prune", "--days", "7"])
    assert seen["argv"][0] == "records-prune" and "--days" in seen["argv"]
    cli.main(["records-health"])
    assert seen["argv"][0] == "records-health"
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/test_records_cadences.py tests/test_cli.py -v -k "health or records"`
Expected: FAIL (context_health returns `[]` stub → stale test fails; cli has no `records-*`).

- [ ] **Step 3: Implement**

Port the three checks into `context_health` (from `~/joshbrain/bin/context_health.py`). Then in `mcpbrain/cli.py`:

- add `"records-prune"` and `"records-health"` to the subcommand-name tuple;
- add a handler and dispatch entries:

```python
def _records_cadence_main(argv):
    from mcpbrain.records_cadences import main as m
    return m(argv)
```

```python
        "records-prune": lambda: _records_cadence_main(["records-prune", *rest]),
        "records-health": lambda: _records_cadence_main(["records-health", *rest]),
```

(Pass the subcommand name through so `records_cadences.main` sees it as `argv[0]`, matching its `choices`.)

- [ ] **Step 4: Run to verify they pass**

Run: `pytest tests/test_records_cadences.py tests/test_cli.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/records_cadences.py mcpbrain/cli.py tests/test_records_cadences.py tests/test_cli.py
git commit -m "feat(cadences): port context-health; wire records-prune/health into CLI"
```

---

## Task 3: Cross-platform cadence generators (repoint launchd; add systemd timer + schtasks)

**Files:**
- Modify: `mcpbrain/agents.py` (repoint `records_prune_plist`/`records_context_health_plist` to the subcommands; add `prune_timer_units()`, `health_timer_units()`, `prune_schtasks_args()`, `health_schtasks_args()`)
- Test: `tests/test_agents_cadence_xplat.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agents_cadence_xplat.py
"""Cross-platform cadence generators call `mcpbrain <subcommand>`, not shell scripts."""
from mcpbrain import agents


def test_launchd_prune_calls_subcommand_not_shell(tmp_path):
    plist = agents.records_prune_plist(
        mcpbrain_bin="/usr/local/bin/mcpbrain", mcpbrain_home="/h")
    assert "records-prune" in plist
    assert "/bin/sh" not in plist and "prune_hot_md.py" not in plist


def test_systemd_prune_timer_daily_0600():
    service, timer = agents.prune_timer_units(mcpbrain_bin="/usr/local/bin/mcpbrain", home="/h")
    assert "mcpbrain records-prune" in service
    assert "OnCalendar=*-*-* 06:00" in timer


def test_systemd_health_timer_weekly_monday():
    service, timer = agents.health_timer_units(mcpbrain_bin="/usr/local/bin/mcpbrain", home="/h")
    assert "records-health" in service
    assert "OnCalendar=Mon" in timer


def test_schtasks_prune_daily():
    a = agents.prune_schtasks_args(mcpbrain_bin=r"C:\mcpbrain.exe")
    assert "/sc" in a and "daily" in a and "/st" in a and "06:00" in a
    assert any("records-prune" in x for x in a)


def test_schtasks_health_weekly_monday():
    a = agents.health_schtasks_args(mcpbrain_bin=r"C:\mcpbrain.exe")
    assert "weekly" in a and "MON" in a and any("records-health" in x for x in a)
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_agents_cadence_xplat.py -v`
Expected: FAIL (new generators don't exist; launchd plist still references the shell script).

- [ ] **Step 3: Implement**

In `mcpbrain/agents.py`:

1. Repoint the launchd generators to the subcommand. Change `records_prune_plist` to take `mcpbrain_bin` (drop `python_bin`/`records_dir`/the shell command) and emit:

```python
def records_prune_plist(*, mcpbrain_bin: str, mcpbrain_home: str) -> str:
    """launchd plist: `mcpbrain records-prune` daily at 06:00."""
    return _calendar_plist(
        label=_PRUNE_LABEL,
        program_args=[mcpbrain_bin, "records-prune"],
        mcpbrain_home=mcpbrain_home, hour=6, minute=0,
        env_vars={"MCPBRAIN_HOME": mcpbrain_home},
    )

def records_context_health_plist(*, mcpbrain_bin: str, mcpbrain_home: str) -> str:
    """launchd plist: `mcpbrain records-health` weekly Monday 07:00."""
    return _calendar_plist(
        label=_HEALTH_LABEL,
        program_args=[mcpbrain_bin, "records-health"],
        mcpbrain_home=mcpbrain_home, hour=7, minute=0, weekday=1,
        env_vars={"MCPBRAIN_HOME": mcpbrain_home},
    )
```

2. Add systemd timer generators (a `.service` + `.timer` pair):

```python
def _timer_units(*, label_desc: str, subcommand: str, mcpbrain_bin: str, home: str,
                 on_calendar: str) -> tuple[str, str]:
    service = (f"[Unit]\nDescription={label_desc}\n\n[Service]\nType=oneshot\n"
               f"ExecStart={mcpbrain_bin} {subcommand}\nEnvironment=MCPBRAIN_HOME={home}\n")
    timer = (f"[Unit]\nDescription={label_desc} timer\n\n[Timer]\n"
             f"OnCalendar={on_calendar}\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n")
    return service, timer

def prune_timer_units(*, mcpbrain_bin: str, home: str) -> tuple[str, str]:
    return _timer_units(label_desc="mcpbrain records prune", subcommand="records-prune",
                        mcpbrain_bin=mcpbrain_bin, home=home, on_calendar="*-*-* 06:00:00")

def health_timer_units(*, mcpbrain_bin: str, home: str) -> tuple[str, str]:
    return _timer_units(label_desc="mcpbrain records health", subcommand="records-health",
                        mcpbrain_bin=mcpbrain_bin, home=home, on_calendar="Mon *-*-* 07:00:00")
```

3. Add schtasks time-triggered args (mirror `_schtasks_args` quoting):

```python
def _cadence_schtasks_args(*, task_name: str, subcommand: str, mcpbrain_bin: str,
                           schedule: list[str]) -> list[str]:
    quoted = f'"{mcpbrain_bin}"' if any(c.isspace() for c in mcpbrain_bin) else mcpbrain_bin
    return ["schtasks", "/create", "/tn", task_name, *schedule,
            "/tr", f"{quoted} {subcommand}", "/f"]

def prune_schtasks_args(*, mcpbrain_bin: str) -> list[str]:
    return _cadence_schtasks_args(task_name="mcpbrain-records-prune", subcommand="records-prune",
                                  mcpbrain_bin=mcpbrain_bin, schedule=["/sc", "daily", "/st", "06:00"])

def health_schtasks_args(*, mcpbrain_bin: str) -> list[str]:
    return _cadence_schtasks_args(task_name="mcpbrain-records-health", subcommand="records-health",
                                  mcpbrain_bin=mcpbrain_bin,
                                  schedule=["/sc", "weekly", "/d", "MON", "/st", "07:00"])
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_agents_cadence_xplat.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Update the launchd-caller fallout**

Run: `grep -rn "records_prune_plist\|records_context_health_plist" mcpbrain/ bin/ tests/`
Update `bin/seed_joshbrain.py` and `tests/test_agents_calendar.py` to the new signatures (`mcpbrain_bin=...` instead of `python_bin=/records_dir=`). Run `pytest tests/test_agents_calendar.py -q`.

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/agents.py bin/seed_joshbrain.py tests/
git commit -m "feat(agents): cadence generators call mcpbrain subcommands; systemd+schtasks variants"
```

---

## Task 4: Product install path for cadences + wire into setup

**Files:**
- Modify: `mcpbrain/agents.py` (`install_cadences`/`uninstall_cadences`, per-OS, `# pragma: no cover` bodies + a tiny tested dispatcher)
- Modify: `mcpbrain/setup.py` (call `install_cadences` best-effort, like the tray)
- Test: `tests/test_agents_cadence_xplat.py` (dispatcher routing)

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_agents_cadence_xplat.py
def test_install_cadences_dispatches_by_platform(monkeypatch):
    from mcpbrain import agents
    calls = []
    monkeypatch.setattr(agents, "_install_cadences_launchd", lambda **k: calls.append("darwin"))
    monkeypatch.setattr(agents, "_install_cadences_systemd", lambda **k: calls.append("linux"))
    monkeypatch.setattr(agents, "_install_cadences_schtasks", lambda **k: calls.append("win32"))
    agents.install_cadences("darwin", mcpbrain_bin="/x", home="/h"); assert calls == ["darwin"]
    agents.install_cadences("linux", mcpbrain_bin="/x", home="/h"); assert calls[-1] == "linux"
    agents.install_cadences("win32", mcpbrain_bin="/x", home="/h"); assert calls[-1] == "win32"
    import pytest
    with pytest.raises(ValueError):
        agents.install_cadences("plan9", mcpbrain_bin="/x", home="/h")
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_agents_cadence_xplat.py -v -k install_cadences`
Expected: FAIL (`AttributeError: install_cadences`).

- [ ] **Step 3: Implement**

In `mcpbrain/agents.py`, add the dispatcher + per-OS bodies (mirror `install_agent`):

```python
def install_cadences(platform: str, *, mcpbrain_bin: str, home: str) -> None:
    """Schedule the deterministic records cadences (prune daily, health weekly)."""
    if platform == "darwin":
        _install_cadences_launchd(mcpbrain_bin=mcpbrain_bin, home=home)
    elif platform == "linux":
        _install_cadences_systemd(mcpbrain_bin=mcpbrain_bin, home=home)
    elif platform == "win32":
        _install_cadences_schtasks(mcpbrain_bin=mcpbrain_bin, home=home)
    else:
        raise ValueError(f"Unsupported platform: {platform!r}")
```

Add `_install_cadences_launchd/_systemd/_schtasks` (and matching `uninstall_cadences`) as `# pragma: no cover` bodies that write the generated definitions to the canonical per-OS paths and load them (launchd: write the two plists to `~/Library/LaunchAgents` + `launchctl load -w`; systemd: write `<prune|health>.service`+`.timer` to `~/.config/systemd/user` + `systemctl --user enable --now …timer`; schtasks: run the two arg lists). Mirror the existing `_install_launchd`/`_install_systemd`/`_install_schtasks` style exactly.

Then in `mcpbrain/setup.py`, after the tray install, add a best-effort call (failure must not block onboarding):

```python
    try:
        from mcpbrain import agents
        agents.install_cadences(_platform(), mcpbrain_bin=_mcpbrain_bin(), home=home)
        print("Records cadences scheduled (prune daily, health weekly).")
    except Exception as exc:  # noqa: BLE001 — optional; never block onboarding
        print(f"Skipped scheduling records cadences ({exc}).", file=sys.stderr)
```

- [ ] **Step 4: Run to verify it passes**

Run: `pytest tests/test_agents_cadence_xplat.py -v -k install_cadences`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/agents.py mcpbrain/setup.py tests/test_agents_cadence_xplat.py
git commit -m "feat(agents): install_cadences per-OS; setup schedules prune+health"
```

---

## Final: full suite + spec follow-up

- [ ] **Step 1:** `pytest -q` green; `ruff check mcpbrain/ tests/` clean.
- [ ] **Step 2:** Add a one-line note to the spec (§1.6) that gardener + meeting-packs cross-platform are a remaining **§1.6c** follow-up (they need the cowork prompt content shipped as product scaffolding + a `claude`-availability story). Commit the spec edit.

---

## Self-Review

**Spec coverage (§1.6b):** the two deterministic cadences run on all three OSes via subcommands (Tasks 1–2), with launchd/systemd/schtasks generators (Task 3) and a product install path wired into onboarding (Task 4). No shell, no records-repo scripts. Gardener/meeting-packs explicitly deferred to §1.6c (documented).

**Placeholder honesty:** the prune/health *algorithms* are ported from two on-disk scripts (`~/joshbrain/bin/{prune_hot_md,context_health}.py`) — the plan gives the function skeletons, the CLI/commit/scheduler wiring as complete code, and **behavioral tests as the correctness oracle** for the ported logic. This is a faithful-port task, not a vague "implement later."

**Type consistency:** `prune_hot_md(repo, *, days=14, now=None) -> int`; `context_health(repo, mcpbrain_home) -> list[str]`; `records_cadences.main(argv) -> int`; generators `records_prune_plist(*, mcpbrain_bin, mcpbrain_home)`, `prune_timer_units(*, mcpbrain_bin, home) -> (service, timer)`, `prune_schtasks_args(*, mcpbrain_bin) -> list[str]`; `install_cadences(platform, *, mcpbrain_bin, home)`. Subcommand names `records-prune`/`records-health` match between CLI, generators, and `main`'s `choices`.
