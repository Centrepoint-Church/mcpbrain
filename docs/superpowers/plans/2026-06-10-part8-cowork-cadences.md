# Part 8 — Gardener & Meeting-Packs Cross-Platform (cowork cadences) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the two `claude`-headless cadences (memory gardener, meeting packs) run on macOS/Linux/Windows for any user: ship their cowork prompts as genericized product scaffolding, port the shell wrappers into `mcpbrain records-gardener` / `mcpbrain meeting-packs` subcommands that shell to the local `claude` CLI, and add systemd/schtasks schedulers + the product install path.

**Architecture:** A new `mcpbrain/cowork.py` holds `run_cowork(prompt_name, *, tools, extra_context, …)` — it reads a **shipped** prompt from the `mcpbrain/cowork/` package dir (not the records repo), appends runtime context, and runs `claude -p` headless with the `ops-brain-search` MCP server config pointing at `mcpbrain mcp-server`, prompt piped via stdin (the prompts start with YAML `---`, which breaks `-p "<prompt>"`). Two thin subcommands wrap it. The `agents.py` generators are repointed to the subcommands and gain systemd/schtasks variants; `install_cadences` (Plan 3b) is extended to schedule them.

**Tech Stack:** Python 3.12, pytest. `claude` is shelled via subprocess (faked in tests). Prompts ship as package data.

This is **Plan 8 of the productization series** — spec **§1.6c**. It depends on **Plan 3b** (`install_cadences`, the subcommand/generator patterns) and reuses **Plan 7**'s `draft._find_claude`.

**Safety note (call out in the release):** the gardener runs `claude` with `--dangerously-skip-permissions` and `Bash,Read,Edit,Write` tools autonomously on a schedule, scoped to the user's records repo working dir. Meeting-packs runs with `Bash` (curl to the loopback control API). This is the same trust model the maintainer already runs; for other users it ships identically. Document it in the trust section.

**Grounding (verified — `~/joshbrain/bin/*.sh` + `~/joshbrain/cowork/*.md`):**
- `run_memory_gardener.sh`: `PROMPT=$(cat cowork/memory-gardener.md)` + appended runtime context (working dir, date, "commit by name") → `printf '%s' "$PROMPT" | claude -p --tools "Bash,Read,Edit,Write" --settings '{"disableAllHooks":true}' --strict-mcp-config --mcp-config '{"mcpServers":{"ops-brain-search":{"command":"<mcpbrain>","args":["mcp-server"],"env":{"MCPBRAIN_HOME":"…","MCPBRAIN_EMBEDDER":"bge-small"}}}}' --dangerously-skip-permissions` → log to `<home>/logs/memory_gardener.log`. Weekly Mon 08:00.
- `build_meeting_packs.sh`: reads `<home>/control_port` + `control_token`; **exits 0 early if either is missing** (daemon down); prompt = `cowork/meeting-packs.md` + control API base/token/date; same `claude -p` but `--tools "Bash"`; log to `meeting_packs.log`. Twice daily 07:45 + 12:00.
- Prompts (`cowork/memory-gardener.md` 4.9 KB, `cowork/meeting-packs.md` 3.2 KB) reference `~/joshbrain`, the connected `~/.mcpbrain`, and a couple of Josh/Centrepoint examples — must be genericized to the records repo + neutral examples.
- `config.EMBEDDER` exists; `config.records_dir(home)`, `config.app_dir()` exist. Plan 3b added `install_cadences(platform, *, mcpbrain_bin, home)` and the launchd/systemd/schtasks cadence patterns.

---

## File Structure

- `mcpbrain/cowork/memory-gardener.md`, `mcpbrain/cowork/meeting-packs.md` — **new** (genericized, shipped).
- `pyproject.toml` — add `mcpbrain/cowork/*` to package-data.
- `mcpbrain/cowork.py` — **new**: `run_cowork(...)`, `_mcpbrain_bin()`, `gardener_main`, `meeting_packs_main`.
- `mcpbrain/cli.py` — register `records-gardener` + `meeting-packs`.
- `mcpbrain/agents.py` — repoint `records_gardener_plist` + `meeting_packs_plist` to the subcommands; add systemd/schtasks variants; extend `install_cadences`.
- Tests: `tests/test_cowork.py`, `tests/test_agents_cowork_xplat.py` (new); extend `tests/test_cli.py`.

---

## Task 1: Ship genericized cowork prompts

**Files:**
- Create: `mcpbrain/cowork/memory-gardener.md`, `mcpbrain/cowork/meeting-packs.md`
- Modify: `pyproject.toml` (package-data)
- Test: `tests/test_cowork.py` (prompt ships + is generic)

- [ ] **Step 1: Failing test**

```python
# tests/test_cowork.py
from pathlib import Path
import mcpbrain


def _cowork_dir():
    return Path(mcpbrain.__file__).parent / "cowork"


def test_prompts_are_shipped():
    for name in ("memory-gardener.md", "meeting-packs.md"):
        assert (_cowork_dir() / name).exists()


def test_prompts_are_generic():
    for name in ("memory-gardener.md", "meeting-packs.md"):
        text = (_cowork_dir() / name).read_text().lower()
        assert "joshbrain" not in text
        assert "centrepoint" not in text
        assert "josh" not in text
```

- [ ] **Step 2: Run → fail.** `pytest tests/test_cowork.py -v`

- [ ] **Step 3: Implement** — copy `~/joshbrain/cowork/memory-gardener.md` and `~/joshbrain/cowork/meeting-packs.md` into `mcpbrain/cowork/`, then **genericize**: replace `~/joshbrain` with "the records repo (your records folder)", replace `~/.mcpbrain` references with "the app data folder", drop the Josh/Centrepoint example lines (e.g. "Courageous Church items not mixed with Centrepoint" → "org tags are internally consistent"), and remove "ask Josh" phrasing. Preserve the hygiene rules, the read-first list, the protected-files list, and the commit-by-name discipline. In `pyproject.toml` add to `[tool.setuptools.package-data]`:

```toml
"mcpbrain.cowork" = ["*.md"]
```

(and ensure `mcpbrain.cowork` is a package — add an empty `mcpbrain/cowork/__init__.py`).

- [ ] **Step 4: Run → pass.** `pytest tests/test_cowork.py -v`

- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(cowork): ship genericized gardener + meeting-packs prompts"`

---

## Task 2: `mcpbrain/cowork.py` — the headless-claude runner

**Files:**
- Create: `mcpbrain/cowork.py`
- Test: `tests/test_cowork.py`

- [ ] **Step 1: Failing test**

```python
# add to tests/test_cowork.py
from mcpbrain import cowork


def test_run_cowork_builds_claude_command(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    monkeypatch.setattr(cowork, "_find_claude", lambda: "/usr/bin/claude")
    monkeypatch.setattr(cowork, "_mcpbrain_bin", lambda: "/usr/bin/mcpbrain")
    seen = {}
    class _R: returncode = 0; stdout = ""; stderr = ""
    def fake_run(cmd, *, input=None, capture_output=None, text=None, timeout=None, cwd=None):
        seen.update(cmd=cmd, input=input, cwd=cwd, timeout=timeout); return _R()
    monkeypatch.setattr(cowork.subprocess, "run", fake_run)
    rc = cowork.run_cowork("memory-gardener.md", tools="Bash,Read,Edit,Write",
                           extra_context="CTX", log_name="memory_gardener.log",
                           cwd=str(tmp_path / "records"), timeout=120)
    assert rc == 0
    assert seen["cmd"][0] == "/usr/bin/claude" and "-p" in seen["cmd"]
    assert "Bash,Read,Edit,Write" in seen["cmd"]
    assert "--dangerously-skip-permissions" in seen["cmd"]
    # mcp-config JSON carries the mcpbrain binary + mcp-server
    assert any("/usr/bin/mcpbrain" in c and "mcp-server" in c for c in seen["cmd"])
    # prompt = shipped file + extra context, piped via stdin
    assert "CTX" in seen["input"] and seen["input"].startswith("#")  # markdown heading
    assert seen["cwd"] == str(tmp_path / "records")
    assert (tmp_path / "logs" / "memory_gardener.log").exists()
```

- [ ] **Step 2: Run → fail.** `pytest tests/test_cowork.py -v -k run_cowork`

- [ ] **Step 3: Implement** — create `mcpbrain/cowork.py`:

```python
"""Headless-claude cowork cadences (gardener, meeting-packs), cross-platform.

Reads a shipped prompt from mcpbrain/cowork/, appends runtime context, and runs
`claude -p` with the ops-brain-search MCP server pointing at `mcpbrain mcp-server`.
Prompt is piped via stdin (the prompts start with YAML `---`, which breaks -p)."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from mcpbrain import config
from mcpbrain.draft import _find_claude

_PROMPT_DIR = Path(__file__).parent / "cowork"


def _mcpbrain_bin() -> str:
    return (shutil.which("mcpbrain")
            or str(Path(sys.executable).with_name("mcpbrain")))


def _mcp_config(home: str) -> str:
    return json.dumps({"mcpServers": {"ops-brain-search": {
        "command": _mcpbrain_bin(), "args": ["mcp-server"],
        "env": {"MCPBRAIN_HOME": home, "MCPBRAIN_EMBEDDER": config.EMBEDDER}}}})


def run_cowork(prompt_name: str, *, tools: str, extra_context: str,
               log_name: str, cwd: str | None = None, timeout: int = 1800) -> int:
    """Run a shipped cowork prompt via headless claude. Returns the claude rc."""
    home = str(config.app_dir())
    prompt = (_PROMPT_DIR / prompt_name).read_text() + "\n\n" + extra_context
    cmd = [_find_claude(), "-p", "--tools", tools,
           "--settings", '{"disableAllHooks":true}',
           "--strict-mcp-config", "--mcp-config", _mcp_config(home),
           "--dangerously-skip-permissions"]
    logs = Path(home) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                            timeout=timeout, cwd=cwd)
    stamp = datetime.now(timezone.utc).isoformat()
    with (logs / log_name).open("a") as f:
        f.write(f"[{stamp}] rc={result.returncode}\n{result.stdout}\n{result.stderr}\n")
    return result.returncode


def gardener_main(argv=None) -> int:
    home = str(config.app_dir())
    repo = config.records_dir(home)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ctx = (f"Weekly hygiene run. Working directory: {repo}\nToday's date: {today}\n"
           "When committing, use git add <specific-path> (never -A) and commit by name.")
    return run_cowork("memory-gardener.md", tools="Bash,Read,Edit,Write",
                      extra_context=ctx, log_name="memory_gardener.log", cwd=repo)


def meeting_packs_main(argv=None) -> int:
    home = str(config.app_dir())
    port = _read(Path(home) / "control_port")
    token = _read(Path(home) / "control_token")
    if not port or not token:
        return 0  # daemon not running — nothing to do (matches the shell script)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ctx = (f"Control API base URL: http://127.0.0.1:{port}\nAuth token: {token}\n"
           f"Today's date: {today}\nRun now: check calendar, find events needing packs, build them.")
    return run_cowork("meeting-packs.md", tools="Bash", extra_context=ctx,
                      log_name="meeting_packs.log")


def _read(p: Path) -> str:
    try:
        return p.read_text().strip()
    except OSError:
        return ""
```

- [ ] **Step 4: Run → pass.** `pytest tests/test_cowork.py -v`

- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(cowork): headless-claude runner + gardener/meeting-packs entrypoints"`

---

## Task 3: Meeting-packs skip-when-down + CLI wiring for both

**Files:**
- Modify: `mcpbrain/cli.py`
- Test: `tests/test_cowork.py`, `tests/test_cli.py`

- [ ] **Step 1: Failing tests**

```python
# add to tests/test_cowork.py
def test_meeting_packs_skips_when_daemon_down(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))  # no control_port/token
    called = {"n": 0}
    monkeypatch.setattr(cowork, "run_cowork", lambda *a, **k: called.__setitem__("n", 1))
    assert cowork.meeting_packs_main([]) == 0
    assert called["n"] == 0  # never invoked claude
```

```python
# add to tests/test_cli.py
def test_dispatch_cowork_cadences(monkeypatch):
    import mcpbrain.cli as cli
    seen = {}
    monkeypatch.setattr("mcpbrain.cowork.gardener_main", lambda a: seen.setdefault("g", True) or 0)
    monkeypatch.setattr("mcpbrain.cowork.meeting_packs_main", lambda a: seen.setdefault("m", True) or 0)
    cli.main(["records-gardener"]); assert seen.get("g")
    cli.main(["meeting-packs"]); assert seen.get("m")
```

- [ ] **Step 2: Run → fail.** `pytest tests/test_cowork.py tests/test_cli.py -v -k "meeting or cowork or gardener"`

- [ ] **Step 3: Implement** — in `mcpbrain/cli.py` add `"records-gardener"` and `"meeting-packs"` to the subcommand tuple, plus dispatch entries:

```python
        "records-gardener": lambda: __import__("mcpbrain.cowork", fromlist=["gardener_main"]).gardener_main(rest),
        "meeting-packs": lambda: __import__("mcpbrain.cowork", fromlist=["meeting_packs_main"]).meeting_packs_main(rest),
```

(The skip-when-down logic is already in `meeting_packs_main` from Task 2; the test just pins it.)

- [ ] **Step 4: Run → pass.** `pytest tests/test_cowork.py tests/test_cli.py -v`

- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(cli): records-gardener + meeting-packs subcommands"`

---

## Task 4: Cross-platform schedulers + extend install_cadences

**Files:**
- Modify: `mcpbrain/agents.py`
- Test: `tests/test_agents_cowork_xplat.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_agents_cowork_xplat.py
from mcpbrain import agents


def test_launchd_gardener_calls_subcommand():
    plist = agents.records_gardener_plist(mcpbrain_bin="/usr/local/bin/mcpbrain", mcpbrain_home="/h")
    assert "records-gardener" in plist
    assert "/bin/bash" not in plist and "run_memory_gardener.sh" not in plist
    assert "RunAtLoad" not in plist  # weekly-only, expensive


def test_launchd_meeting_packs_twice_daily_subcommand():
    plist = agents.meeting_packs_plist(home="/Users/x/.mcpbrain", mcpbrain_bin="/usr/local/bin/mcpbrain")
    assert "meeting-packs" in plist and "build_meeting_packs.sh" not in plist
    assert "<integer>45</integer>" in plist and "<integer>12</integer>" in plist  # 07:45 + 12:00


def test_systemd_gardener_timer_weekly_monday_0800():
    service, timer = agents.gardener_timer_units(mcpbrain_bin="/m", home="/h")
    assert "records-gardener" in service and "OnCalendar=Mon *-*-* 08:00" in timer


def test_systemd_meeting_packs_timer_has_two_times():
    service, timer = agents.meeting_packs_timer_units(mcpbrain_bin="/m", home="/h")
    assert "meeting-packs" in service
    assert "07:45" in timer and "12:00" in timer  # two OnCalendar lines


def test_schtasks_gardener_weekly():
    a = agents.gardener_schtasks_args(mcpbrain_bin=r"C:\mcpbrain.exe")
    assert "weekly" in a and "MON" in a and any("records-gardener" in x for x in a)


def test_schtasks_meeting_packs_returns_two_tasks():
    tasks = agents.meeting_packs_schtasks_args(mcpbrain_bin=r"C:\mcpbrain.exe")
    assert len(tasks) == 2  # am + pm (schtasks has one /st per task)
    assert any("07:45" in " ".join(t) for t in tasks)
    assert any("12:00" in " ".join(t) for t in tasks)
```

- [ ] **Step 2: Run → fail.** `pytest tests/test_agents_cowork_xplat.py -v`

- [ ] **Step 3: Implement** — in `mcpbrain/agents.py`:

1. Repoint `records_gardener_plist` to the subcommand (keep `run_at_load=False`):

```python
def records_gardener_plist(*, mcpbrain_bin: str, mcpbrain_home: str) -> str:
    """launchd plist: `mcpbrain records-gardener` weekly Monday 08:00 (no RunAtLoad)."""
    return _calendar_plist(
        label=_GARDENER_LABEL, program_args=[mcpbrain_bin, "records-gardener"],
        mcpbrain_home=mcpbrain_home, hour=8, minute=0, weekday=1,
        run_at_load=False, env_vars={"MCPBRAIN_HOME": mcpbrain_home})
```

2. Repoint `meeting_packs_plist` to `[mcpbrain_bin, "meeting-packs"]` — change its signature to `meeting_packs_plist(*, home: str, mcpbrain_bin: str)` and replace the `/bin/bash`+script `ProgramArguments` with the subcommand, keeping the existing two-interval (07:45, 12:00) `StartCalendarInterval` array.

3. Add systemd timer generators (reuse Plan 3b's `_timer_units`; meeting-packs needs two `OnCalendar` lines — extend `_timer_units` to accept a list, or inline):

```python
def gardener_timer_units(*, mcpbrain_bin, home):
    return _timer_units(label_desc="mcpbrain records gardener", subcommand="records-gardener",
                        mcpbrain_bin=mcpbrain_bin, home=home, on_calendar="Mon *-*-* 08:00:00")

def meeting_packs_timer_units(*, mcpbrain_bin, home):
    service = (f"[Unit]\nDescription=mcpbrain meeting packs\n\n[Service]\nType=oneshot\n"
               f"ExecStart={mcpbrain_bin} meeting-packs\nEnvironment=MCPBRAIN_HOME={home}\n")
    timer = ("[Unit]\nDescription=mcpbrain meeting packs timer\n\n[Timer]\n"
             "OnCalendar=*-*-* 07:45:00\nOnCalendar=*-*-* 12:00:00\nPersistent=true\n\n"
             "[Install]\nWantedBy=timers.target\n")
    return service, timer
```

4. Add schtasks args (gardener weekly; meeting-packs returns **two** task arg-lists):

```python
def gardener_schtasks_args(*, mcpbrain_bin):
    return _cadence_schtasks_args(task_name="mcpbrain-records-gardener", subcommand="records-gardener",
                                  mcpbrain_bin=mcpbrain_bin, schedule=["/sc","weekly","/d","MON","/st","08:00"])

def meeting_packs_schtasks_args(*, mcpbrain_bin):
    return [
        _cadence_schtasks_args(task_name="mcpbrain-meeting-packs-am", subcommand="meeting-packs",
                               mcpbrain_bin=mcpbrain_bin, schedule=["/sc","daily","/st","07:45"]),
        _cadence_schtasks_args(task_name="mcpbrain-meeting-packs-pm", subcommand="meeting-packs",
                               mcpbrain_bin=mcpbrain_bin, schedule=["/sc","daily","/st","12:00"]),
    ]
```

5. Extend `install_cadences` (Plan 3b) so each per-OS body also installs the gardener + meeting-packs (launchd: write the two plists + load; systemd: write `gardener.{service,timer}` + `meeting-packs.{service,timer}` + enable the timers; schtasks: run the gardener args + both meeting-packs arg-lists). Mirror the prune/health install style added in Plan 3b.

- [ ] **Step 4: Run → pass.** `pytest tests/test_agents_cowork_xplat.py -v`

- [ ] **Step 5: Fallout** — `grep -rn "records_gardener_plist\|meeting_packs_plist" mcpbrain/ bin/ tests/`; update `bin/seed_joshbrain.py` (if it calls them) and `tests/test_agents_calendar.py` to the new signatures. `pytest tests/ -q -k "agents or calendar"`.

- [ ] **Step 6: Commit** — `git add -A && git commit -m "feat(agents): gardener+meeting-packs call subcommands; xplat schedulers; install_cadences extended"`

---

## Final: suite + spec

- [ ] **Step 1:** `pytest -q` green; `ruff check mcpbrain/ tests/` clean.
- [ ] **Step 2:** Update the spec: mark **§1.6c done** (these cadences now ship cross-platform via subcommands + shipped prompts); add the gardener autonomy/safety note to the trust section. Commit.

---

## Self-Review

**Spec coverage (§1.6c):** both cowork cadences run cross-platform via `mcpbrain records-gardener` / `mcpbrain meeting-packs` (Tasks 2–3), reading shipped genericized prompts (Task 1), scheduled on launchd/systemd/schtasks and installed by `install_cadences` (Task 4). No `/bin/bash`, no records-repo scripts, no "joshbrain"/"Josh" in shipped content.

**Placeholder honesty:** runner + entrypoints + generators + CLI are full code with TDD. Task 1 is a content-port of two on-disk prompts with a genericity test (no "josh"/"centrepoint"/"joshbrain") as the oracle. The `claude` invocation is faked in tests (no real headless run in CI).

**Type consistency:** `run_cowork(prompt_name, *, tools, extra_context, log_name, cwd=None, timeout=1800)->int`; `gardener_main(argv)->int`, `meeting_packs_main(argv)->int`; generators `records_gardener_plist(*, mcpbrain_bin, mcpbrain_home)`, `meeting_packs_plist(*, home, mcpbrain_bin)`, `gardener_timer_units/meeting_packs_timer_units(*, mcpbrain_bin, home)->(service,timer)`, `gardener_schtasks_args(*, mcpbrain_bin)->list`, `meeting_packs_schtasks_args(*, mcpbrain_bin)->list[list]`. Subcommand names `records-gardener`/`meeting-packs` match across CLI, generators, and entrypoints. Reuses Plan 3b's `_timer_units`/`_cadence_schtasks_args` and Plan 7's `draft._find_claude`.
