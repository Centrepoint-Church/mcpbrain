# Guided Onboarding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the bare setup form into a fully guided, nothing-assumed onboarding: pre-filled settings/profile, a timezone dropdown, three-state Claude-registration status, an auto-written enrichment skill, an enriched records-repo working project (full CLAUDE.md + context/reference), cross-session memory hooks, and per-step screenshots.

**Architecture:** Three new leaf modules (`timezones.py`, `cowork_tasks.py`, `hooks.py`) + two new CLI subcommands (`session-start`, `session-end`); the records-repo scaffold (`records.py`) gains a profile-interpolated full `CLAUDE.md` + context/reference templates shipped as package data; `probes.py` learns registration/enrichment/hooks states; `daemon.py`/`control_api.py` expose one-shot read endpoints (`/api/config`, `/api/timezones`) and action endpoints (`/api/records/scaffold`, `/api/hooks/install`, `/img/<name>`); `wizard/index.html` is rebuilt to pre-fill, use the dropdown, show a status-first configured view, and walk every step with expandable help + screenshots.

**Tech Stack:** Python 3.12 (stdlib `zoneinfo`, `http.server`), pytest, vanilla HTML/JS served by the daemon's loopback control API. No new third-party dependencies.

Spec: `docs/superpowers/specs/2026-06-10-settings-profile-and-status-design.md`.

---

## File structure

| File | Responsibility |
|---|---|
| `mcpbrain/timezones.py` (new) | Curated IANA zone list (≥1 per UTC offset) + GMT-offset labels |
| `mcpbrain/cowork_tasks.py` (new) | Resolve the Cowork scheduled-tasks dir; write the enrichment `SKILL.md`; detect it |
| `mcpbrain/cowork/enrichment.md` (new, package data) | Canonical enrichment skill body (single source of truth) |
| `mcpbrain/hooks.py` (new) | Install/uninstall/status of SessionStart+SessionEnd hooks in `~/.claude/settings.json` |
| `mcpbrain/session_hooks.py` (new) | `session-start` (prime) + `session-end` (capture) command bodies |
| `mcpbrain/records_templates/` (new, package data) | `CLAUDE.md` + context/reference templates (token-interpolated) |
| `mcpbrain/records.py` (modify) | `ensure_records_repo(profile=…)` stamps the new templates; `scaffold_records` |
| `mcpbrain/probes.py` (modify) | `probe_claude` registration-aware; `probe_enrichment`, `probe_memory_hooks`; `probe_records` CLAUDE.md-aware; `all_connections` |
| `mcpbrain/daemon.py` (modify) | `Daemon.config_profile()`; `apply_config` materialises skill + records scaffold |
| `mcpbrain/control_api.py` (modify) | `GET /api/config`, `GET /api/timezones`, `POST /api/records/scaffold`, `POST /api/hooks/install`, `GET /img/<name>` |
| `mcpbrain/cli.py` (modify) | Register `session-start` / `session-end` subcommands |
| `mcpbrain/wizard/index.html` (modify) | Prefill, dropdown, status-first settings, guided steps, checklist, buttons, screenshots |
| `mcpbrain/wizard/img/` (new, package data) | Onboarding screenshots (`.gitkeep` placeholder; PNGs added by maintainer) |
| `docs/onboarding/SCREENSHOTS.md` (new) | Screenshot manifest |
| `pyproject.toml` (modify) | Package-data globs for the new asset dirs |

Run the full suite with: `python -m pytest -q` (from repo root). Single test: `python -m pytest tests/test_x.py::test_y -v`.

---

## Task 1: `timezones.py` — curated zones with GMT-offset labels

**Files:**
- Create: `mcpbrain/timezones.py`
- Test: `tests/test_timezones.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_timezones.py
"""Curated timezone options carry a GMT-offset label and cover every UTC offset."""
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, available_timezones

from mcpbrain import timezones

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)  # fixed: deterministic offsets


def test_all_zones_are_valid_iana():
    avail = available_timezones()
    for z in timezones.CURATED_ZONES:
        assert z in avail, f"{z} is not a valid IANA zone"


def test_label_format():
    label = timezones.offset_label("Australia/Perth", now=NOW)
    assert re.match(r"^Australia/Perth \(GMT[+-]\d\d:\d\d\)$", label), label


def test_zone_options_shape_and_sorted():
    opts = timezones.zone_options(now=NOW)
    assert opts and all(set(o) == {"value", "label"} for o in opts)
    # sorted by offset then name
    offsets = [ZoneInfo(o["value"]).utcoffset(NOW) for o in opts]
    assert offsets == sorted(offsets)


def test_every_offset_minus12_to_plus14_present():
    opts = timezones.zone_options(now=NOW)
    have = {int(ZoneInfo(o["value"]).utcoffset(NOW).total_seconds() // 3600) for o in opts}
    for hour in range(-12, 15):  # -12 .. +14 inclusive
        assert hour in have, f"no curated zone at GMT{hour:+d}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_timezones.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mcpbrain.timezones'`

- [ ] **Step 3: Write the implementation**

```python
# mcpbrain/timezones.py
"""Curated IANA timezones with human GMT-offset labels for the setup dropdown.

A short, sorted list with at least one representative zone for every whole-hour
UTC offset from -12 to +14, so a user anywhere can pick a correct zone. Offsets
are computed at a caller-supplied `now` (DST-correct, deterministic in tests).
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

# One representative per whole-hour offset -12..+14 (plus a few common extras).
# Half-hour/45-min zones are intentionally omitted from the curated core; the
# label still renders their true offset if added later.
CURATED_ZONES: tuple[str, ...] = (
    "Etc/GMT+12",            # GMT-12
    "Pacific/Pago_Pago",     # GMT-11
    "Pacific/Honolulu",      # GMT-10
    "America/Anchorage",     # GMT-09
    "America/Los_Angeles",   # GMT-08
    "America/Denver",        # GMT-07
    "America/Chicago",       # GMT-06
    "America/New_York",      # GMT-05
    "America/Halifax",       # GMT-04
    "America/Sao_Paulo",     # GMT-03
    "Atlantic/South_Georgia",# GMT-02
    "Atlantic/Azores",       # GMT-01
    "Europe/London",         # GMT+00
    "Europe/Paris",          # GMT+01
    "Europe/Athens",         # GMT+02
    "Europe/Moscow",         # GMT+03
    "Asia/Dubai",            # GMT+04
    "Asia/Karachi",          # GMT+05
    "Asia/Dhaka",            # GMT+06
    "Asia/Bangkok",          # GMT+07
    "Asia/Singapore",        # GMT+08
    "Australia/Perth",       # GMT+08 (common; same offset, different name)
    "Asia/Tokyo",            # GMT+09
    "Australia/Sydney",      # GMT+10 (DST varies)
    "Australia/Brisbane",    # GMT+10 (no DST)
    "Pacific/Noumea",        # GMT+11
    "Pacific/Auckland",      # GMT+12 (DST varies)
    "Pacific/Tongatapu",     # GMT+13
    "Pacific/Kiritimati",    # GMT+14
)


def offset_label(zone: str, *, now: datetime) -> str:
    """Return '<zone> (GMT±HH:MM)' for `zone` at `now`."""
    off = ZoneInfo(zone).utcoffset(now)
    total = int(off.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{zone} (GMT{sign}{total // 3600:02d}:{(total % 3600) // 60:02d})"


def zone_options(*, now: datetime) -> list[dict]:
    """[{'value','label'}] for the curated set, sorted by offset then name.

    A zone that fails to resolve (bad tzdata) is skipped, never fatal.
    """
    out = []
    for z in CURATED_ZONES:
        try:
            off = ZoneInfo(z).utcoffset(now)
        except Exception:  # noqa: BLE001 — skip an unresolvable zone
            continue
        out.append((off, z))
    out.sort(key=lambda t: (t[0], t[1]))
    return [{"value": z, "label": offset_label(z, now=now)} for _, z in out]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_timezones.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/timezones.py tests/test_timezones.py
git commit -m "feat(timezones): curated IANA zones with GMT-offset labels"
```

---

## Task 2: `cowork_tasks.py` + `cowork/enrichment.md` — write the enrichment skill

**Files:**
- Create: `mcpbrain/cowork_tasks.py`
- Create: `mcpbrain/cowork/enrichment.md` (package data)
- Test: `tests/test_cowork_tasks.py`

- [ ] **Step 1: Create the canonical enrichment skill body**

Create `mcpbrain/cowork/enrichment.md` whose contents are the **exact text currently in the `<pre id="spec-task">` block of `mcpbrain/wizard/index.html`** (lines ~155–382), with HTML entities decoded (`&lt;`→`<`, `&gt;`→`>`, `&amp;`→`&`). Do **not** include YAML front-matter here — `write_enrichment_skill` adds it. This file becomes the single source of truth; Task 13 will make the wizard render it instead of the inline `<pre>`.

- [ ] **Step 2: Write the failing test**

```python
# tests/test_cowork_tasks.py
"""Resolve the Cowork scheduled-tasks dir and write the enrichment SKILL.md."""
from pathlib import Path

from mcpbrain import cowork_tasks


def test_scheduled_dir_prefers_documents_claude(tmp_path, monkeypatch):
    docs = tmp_path / "Documents" / "Claude"
    docs.mkdir(parents=True)  # parent exists -> Scheduled can be created under it
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert cowork_tasks.scheduled_dir() == docs / "Scheduled"


def test_scheduled_dir_none_when_no_parent(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))  # nothing exists
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert cowork_tasks.scheduled_dir() is None


def test_write_enrichment_skill_writes_frontmatter_and_body(tmp_path, monkeypatch):
    (tmp_path / "Documents" / "Claude").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    p = cowork_tasks.write_enrichment_skill(str(tmp_path / "home"))
    assert p is not None and p.name == "SKILL.md"
    text = p.read_text()
    assert text.startswith("---\nname: mcpbrain-enrichment\n")
    assert "enrich_queue/pending.json" in text  # body present
    assert cowork_tasks.enrichment_skill_present() is True
    # idempotent: second call returns the same path, no crash
    assert cowork_tasks.write_enrichment_skill(str(tmp_path / "home")) == p


def test_write_enrichment_skill_degrades_to_none(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))  # no Documents/Claude
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert cowork_tasks.write_enrichment_skill(str(tmp_path / "home")) is None
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python -m pytest tests/test_cowork_tasks.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mcpbrain.cowork_tasks'`

- [ ] **Step 4: Write the implementation**

```python
# mcpbrain/cowork_tasks.py
"""Author the Cowork enrichment scheduled-task SKILL.md (content only).

Claude Cowork stores a scheduled task's prompt as a plain SKILL.md under a
per-OS scheduled-tasks directory. The cadence/enabled state is app-managed and
NOT file-authorable, so this module only writes the prompt and detects it.
Every function degrades gracefully (returns None / False) rather than raising.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

ENRICHMENT_TASK = "mcpbrain-enrichment"
_DESCRIPTION = (
    "Reads a batch of email threads from ~/.mcpbrain/enrich_queue/pending.json, "
    "extracts structured knowledge (entities, actions, relations, org tags), and "
    "writes the result to ~/.mcpbrain/enrich_inbox/<batch_id>.json. No database "
    "access, no Gmail — two files in and out."
)


def _candidate_dirs() -> list[Path]:
    home = Path.home()
    cands = [home / "Documents" / "Claude" / "Scheduled"]
    cfg = os.getenv("CLAUDE_CONFIG_DIR")
    cands.append((Path(cfg) if cfg else home / ".claude") / "scheduled-tasks")
    return cands


def scheduled_dir() -> Path | None:
    """First candidate whose PARENT exists (so we may create the task subdir)."""
    for d in _candidate_dirs():
        try:
            if d.exists() or d.parent.exists():
                return d
        except OSError:
            continue
    return None


def _skill_body() -> str:
    return (Path(__file__).parent / "cowork" / "enrichment.md").read_text()


def write_enrichment_skill(home: str) -> Path | None:
    """Write <scheduled_dir>/mcpbrain-enrichment/SKILL.md. Returns path or None."""
    d = scheduled_dir()
    if d is None:
        return None
    try:
        task_dir = d / ENRICHMENT_TASK
        task_dir.mkdir(parents=True, exist_ok=True)
        front = f"---\nname: {ENRICHMENT_TASK}\ndescription: {_DESCRIPTION}\n---\n\n"
        out = task_dir / "SKILL.md"
        out.write_text(front + _skill_body())
        return out
    except OSError as exc:
        log.debug("write_enrichment_skill degraded: %s", exc)
        return None


def enrichment_skill_present() -> bool:
    d = scheduled_dir()
    return bool(d and (d / ENRICHMENT_TASK / "SKILL.md").exists())
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_cowork_tasks.py -v`
Expected: PASS (4 tests)

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/cowork_tasks.py mcpbrain/cowork/enrichment.md tests/test_cowork_tasks.py
git commit -m "feat(cowork): write enrichment SKILL.md from packaged source"
```

---

## Task 3: `hooks.py` — install memory hooks into `~/.claude/settings.json`

**Files:**
- Create: `mcpbrain/hooks.py`
- Test: `tests/test_hooks.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_hooks.py
"""Install SessionStart/SessionEnd hooks into ~/.claude/settings.json, mergefully."""
import json
import os
from pathlib import Path

import pytest

from mcpbrain import hooks


def _settings_path(tmp_path):
    return tmp_path / ".claude" / "settings.json"


def test_install_creates_both_hooks(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    p = hooks.install_session_hooks()
    data = json.loads(p.read_text())
    cmds = [h["command"] for grp in data["hooks"].values() for blk in grp for h in blk["hooks"]]
    assert any("session-start" in c for c in cmds)
    assert any("session-end" in c for c in cmds)
    assert hooks.hooks_status()["installed"] is True
    assert (os.stat(p).st_mode & 0o777) == 0o600


def test_install_is_idempotent_and_preserves_existing(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    sp = _settings_path(tmp_path)
    sp.parent.mkdir(parents=True)
    sp.write_text(json.dumps({"env": {"FOO": "bar"},
                              "hooks": {"SessionStart": [{"hooks": [{"type": "command",
                                        "command": "/usr/local/bin/other"}]}]}}))
    hooks.install_session_hooks()
    hooks.install_session_hooks()  # twice -> no duplicate
    data = json.loads(sp.read_text())
    assert data["env"] == {"FOO": "bar"}                       # preserved
    starts = [h["command"] for blk in data["hooks"]["SessionStart"] for h in blk["hooks"]]
    assert "/usr/local/bin/other" in starts                    # preserved
    assert sum("session-start" in c for c in starts) == 1      # no duplicate


def test_install_refuses_malformed(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    sp = _settings_path(tmp_path)
    sp.parent.mkdir(parents=True)
    sp.write_text("{not json")
    with pytest.raises(ValueError):
        hooks.install_session_hooks()


def test_uninstall_removes_only_ours(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    sp = _settings_path(tmp_path)
    sp.parent.mkdir(parents=True)
    sp.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [{"type": "command",
                              "command": "/usr/local/bin/other"}]}]}}))
    hooks.install_session_hooks()
    hooks.uninstall_session_hooks()
    data = json.loads(sp.read_text())
    starts = [h["command"] for blk in data["hooks"].get("SessionStart", []) for h in blk["hooks"]]
    assert "/usr/local/bin/other" in starts
    assert not any("session-start" in c for c in starts)
    assert hooks.hooks_status()["installed"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_hooks.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mcpbrain.hooks'`

- [ ] **Step 3: Write the implementation**

```python
# mcpbrain/hooks.py
"""Install mcpbrain's SessionStart/SessionEnd hooks into ~/.claude/settings.json.

The hooks call the cross-platform `mcpbrain` console script (on PATH) so they
work on macOS/Windows/Linux without shell scripts. Installation is mergeful and
idempotent: existing keys and other hooks are preserved, a malformed settings
file is refused (never clobbered), and a re-run never duplicates our entry.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

# (event, command) pairs. The marker substring identifies OUR entries on re-run.
_HOOKS = (
    ("SessionStart", "session-start"),
    ("SessionEnd", "session-end"),
)


def settings_path() -> Path:
    base = os.getenv("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else Path.home() / ".claude") / "settings.json"


def _mcpbrain_bin() -> str:
    # Prefer the console script next to this interpreter (uv tool install); fall
    # back to the launching argv0, then PATH. Never returns a bare "mcpbrain"
    # that might not resolve under launchd.
    cand = Path(sys.executable).with_name("mcpbrain")
    if cand.exists():
        return str(cand)
    argv0 = Path(sys.argv[0])
    if argv0.name in ("mcpbrain", "mcpbrain.exe") and argv0.exists():
        return str(argv0)
    return shutil.which("mcpbrain") or "mcpbrain"


def _load(p: Path) -> dict:
    if not p.exists():
        return {}
    raw = p.read_text()
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{p} is not valid JSON; refusing to overwrite it: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{p} is not a JSON object; refusing to overwrite it.")
    return data


def _write(p: Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=p.name + ".", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(data, indent=2) + "\n")
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def install_session_hooks() -> Path:
    """Merge our two command hooks into settings.json. Idempotent. Returns the path."""
    p = settings_path()
    data = _load(p)
    bin_path = _mcpbrain_bin()
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
    for event, marker in _HOOKS:
        blocks = hooks.get(event)
        if not isinstance(blocks, list):
            blocks = []
        present = any(
            marker in (h.get("command") or "")
            for blk in blocks if isinstance(blk, dict)
            for h in blk.get("hooks", []) if isinstance(h, dict)
        )
        if not present:
            blocks.append({"hooks": [{"type": "command", "command": f"{bin_path} {marker}"}]})
        hooks[event] = blocks
    data["hooks"] = hooks
    _write(p, data)
    return p


def uninstall_session_hooks() -> Path:
    p = settings_path()
    data = _load(p)
    hooks = data.get("hooks")
    if isinstance(hooks, dict):
        for event, marker in _HOOKS:
            blocks = hooks.get(event)
            if not isinstance(blocks, list):
                continue
            kept = []
            for blk in blocks:
                if not isinstance(blk, dict):
                    kept.append(blk); continue
                inner = [h for h in blk.get("hooks", [])
                         if not (isinstance(h, dict) and marker in (h.get("command") or ""))]
                if inner:
                    kept.append({**blk, "hooks": inner})
            if kept:
                hooks[event] = kept
            else:
                hooks.pop(event, None)
        data["hooks"] = hooks
        _write(p, data)
    return p


def hooks_status() -> dict:
    """{'installed': bool} — true only when BOTH our hooks are present."""
    try:
        data = _load(settings_path())
    except ValueError:
        return {"installed": False}
    hooks = data.get("hooks") or {}
    def has(event, marker):
        return any(
            marker in (h.get("command") or "")
            for blk in (hooks.get(event) or []) if isinstance(blk, dict)
            for h in blk.get("hooks", []) if isinstance(h, dict)
        )
    return {"installed": all(has(e, m) for e, m in _HOOKS)}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_hooks.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/hooks.py tests/test_hooks.py
git commit -m "feat(hooks): mergeful install of SessionStart/SessionEnd memory hooks"
```

---

## Task 4: `session_hooks.py` + CLI `session-start` / `session-end`

**Files:**
- Create: `mcpbrain/session_hooks.py`
- Modify: `mcpbrain/cli.py:19-37`
- Test: `tests/test_session_hooks.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_session_hooks.py
"""session-start prints bounded priming; session-end captures a real session only."""
import io
import json
from pathlib import Path

from mcpbrain import session_hooks


def test_session_start_prints_hot_and_degrades(tmp_path, capsys, monkeypatch):
    repo = tmp_path / "records"
    (repo / "state").mkdir(parents=True)
    (repo / "state" / "hot.md").write_text(
        "# Hot\n\n## Just decided\n- **2026-06-10:** shipped the thing\n## Open\n")
    monkeypatch.setattr(session_hooks.config, "records_dir", lambda home: str(repo))
    # no control_port/token in home -> actions degrade, never crash
    session_hooks.session_start(str(tmp_path / "home"))
    out = capsys.readouterr().out
    assert "shipped the thing" in out
    assert "actions" in out.lower()  # heading present even when unavailable


def test_session_end_captures_substantial(tmp_path, monkeypatch):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("\n".join(json.dumps(x) for x in [
        {"type": "user", "message": {"role": "user", "content": "Plan the migration in detail"}},
        {"type": "assistant", "message": {"role": "assistant", "content": "Here is the plan ..."}},
        {"type": "user", "message": {"role": "user", "content": "Great, do step one"}},
    ]))
    hook = {"transcript_path": str(transcript), "session_id": "s1", "cwd": str(tmp_path)}
    captured = {}
    monkeypatch.setattr(session_hooks, "write_capture",
                        lambda home, env: captured.setdefault("env", env) or Path("x"))
    session_hooks.session_end(str(tmp_path / "home"), stdin=io.StringIO(json.dumps(hook)))
    assert captured["env"]["kind"] == "ingest"
    assert "migration" in captured["env"]["content"].lower()


def test_session_end_skips_trivial(tmp_path, monkeypatch):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps(
        {"type": "user", "message": {"role": "user", "content": "hi"}}))
    hook = {"transcript_path": str(transcript), "session_id": "s2", "cwd": str(tmp_path)}
    called = {"n": 0}
    monkeypatch.setattr(session_hooks, "write_capture",
                        lambda home, env: called.update(n=called["n"] + 1))
    session_hooks.session_end(str(tmp_path / "home"), stdin=io.StringIO(json.dumps(hook)))
    assert called["n"] == 0  # single trivial turn -> skipped
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_session_hooks.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mcpbrain.session_hooks'`

- [ ] **Step 3: Write the implementation**

```python
# mcpbrain/session_hooks.py
"""Cross-platform bodies for the `mcpbrain session-start` / `session-end` hooks.

session-start prints bounded priming context (recent hot.md + open actions) to
stdout; Claude Code injects it into the session. session-end reads the hook JSON
from stdin, parses the transcript, and queues a one-line session capture — but
only for substantial interactive sessions, so trivial/headless runs add no noise.
Neither ever hard-fails a session.
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from mcpbrain import config
from mcpbrain.capture import write_capture

_MAX_LINES = 8
_MIN_TURNS = 2          # at least this many user turns to count as "substantial"
_MIN_CHARS = 200        # ...or this much user text


def session_start(home: str, out=None) -> None:
    out = out or sys.stdout
    print("## Recent continuity (hot.md)", file=out)
    try:
        hot = Path(config.records_dir(home)) / "state" / "hot.md"
        lines = [ln for ln in hot.read_text().splitlines()
                 if ln.startswith("- **20")][:_MAX_LINES]
        print("\n".join(lines) if lines else "(none)", file=out)
    except OSError:
        print("(none)", file=out)
    print("\n## Open actions", file=out)
    print(_open_actions(home), file=out)


def _open_actions(home: str) -> str:
    try:
        port = (Path(home) / "control_port").read_text().strip()
        token = (Path(home) / "control_token").read_text().strip()
    except OSError:
        return "(actions unavailable)"
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/dashboard/today",
        headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=3) as r:  # noqa: S310 loopback only
            data = json.loads(r.read() or b"{}")
    except Exception:  # noqa: BLE001 — daemon down / no dashboard
        return "(actions unavailable)"
    actions = (data or {}).get("actions", {}) or {}
    rows = []
    for bucket in ("overdue", "due_today", "upcoming"):
        for x in (actions.get(bucket) or []):
            t = (x.get("text") or "").strip().replace("\n", " ")
            if t:
                rows.append(f"- [{bucket}] {t[:80]}")
    return "\n".join(rows[:_MAX_LINES]) if rows else "(no open actions)"


def session_end(home: str, stdin=None) -> None:
    stdin = stdin or sys.stdin
    try:
        hook = json.loads(stdin.read() or "{}")
    except (ValueError, OSError):
        return
    tpath = hook.get("transcript_path") or ""
    if not tpath:
        return
    try:
        raw = Path(tpath).read_text()
    except OSError:
        return
    user_texts = []
    for line in raw.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        msg = ev.get("message") or {}
        if msg.get("role") == "user":
            c = msg.get("content")
            if isinstance(c, str):
                user_texts.append(c)
            elif isinstance(c, list):
                user_texts.extend(b.get("text", "") for b in c if isinstance(b, dict))
    joined = " ".join(t.strip() for t in user_texts if t.strip())
    if len(user_texts) < _MIN_TURNS and len(joined) < _MIN_CHARS:
        return  # trivial / headless single-shot -> skip
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    envelope = {
        "kind": "ingest",
        "source": "session_end_hook",
        "captured_at": stamp,
        "title": f"Session {hook.get('session_id', 'unknown')[:8]} {stamp[:10]}",
        "content": joined[:2000],
        "tags": "session",
        "observation_type": "note",
    }
    try:
        write_capture(home, envelope)
    except (ValueError, OSError):
        return


def session_start_main(argv=None) -> int:
    session_start(str(config.app_dir()))
    return 0


def session_end_main(argv=None) -> int:
    session_end(str(config.app_dir()))
    return 0
```

- [ ] **Step 4: Wire the subcommands in `cli.py`**

In `mcpbrain/cli.py`, add the two names to the subparser loop (line 19-22) so the block reads:

```python
    for name in ("daemon","mcp-server","auth","setup","update","register","tray",
                 "enrich-backfill","records-prune","records-health",
                 "records-gardener","meeting-packs","session-start","session-end"):
        sub.add_parser(name, add_help=(name == "mcp-server"))
```

And add two entries to the dispatch dict (after the `"meeting-packs"` line, line 36):

```python
        "session-start": lambda: __import__("mcpbrain.session_hooks", fromlist=["session_start_main"]).session_start_main(rest),
        "session-end": lambda: __import__("mcpbrain.session_hooks", fromlist=["session_end_main"]).session_end_main(rest),
```

- [ ] **Step 5: Run tests + CLI smoke**

Run: `python -m pytest tests/test_session_hooks.py -v`
Expected: PASS (3 tests)
Run: `python -c "import sys; sys.argv=['mcpbrain','session-start']; from mcpbrain.cli import main; main()"`
Expected: prints the two headings (degrades to "(none)"/"(actions unavailable)" with no daemon)

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/session_hooks.py mcpbrain/cli.py tests/test_session_hooks.py
git commit -m "feat(hooks): session-start/session-end CLI commands"
```

---

## Task 5: `records_templates/` — full CLAUDE.md + context/reference templates

**Files:**
- Create: `mcpbrain/records_templates/CLAUDE.md`
- Create: `mcpbrain/records_templates/context_identity.md`
- Create: `mcpbrain/records_templates/context_preferences.md`
- Create: `mcpbrain/records_templates/reference_systems.md`
- Create: `mcpbrain/records_templates/reference_projects.md`

Interpolation tokens (replaced by `records.py` in Task 6): `{{OWNER_FULL_NAME}}`, `{{OWNER_ROLE}}`, `{{ORG_LIST}}` (comma-joined org names), `{{ORG_BLOCK}}` (generated bullet list). Markdown braces (`{ }` in JSON examples) are safe because replacement is literal token substitution, not `str.format`.

- [ ] **Step 1: Create `mcpbrain/records_templates/CLAUDE.md`**

```markdown
@context/identity.md
@context/voice.md
@context/preferences.md

---

<!-- GARDENER-PROTECTED-START: identity and core rules — gardener cannot modify this block -->

## Org tagging rules

<important if="this session involves people, organisations, or roles">
- Your organisations: {{ORG_LIST}}.
{{ORG_BLOCK}}
- Every person entity must include an org affiliation in its first observation.
- Every `brain_ingest` call names the org in the body text so searches surface org context.
- When memory results appear, check the org tag before using them. If ambiguous, flag it rather than assume.
</important>

## Role attribution rules

<important if="this session involves people, roles, or attribution">
- Never attribute a role/title to a person based on text you wrote about them.
- Only record a role if they stated it themselves, it is in their own email signature, or the owner confirms it.
- If a role is uncertain, omit it. Bad attribution is worse than none.
</important>

<!-- GARDENER-PROTECTED-END -->

---

## Memory Protocol

Read tools (mcpbrain MCP): `brain_search` (hybrid search → summaries), `brain_read` (full text of one chunk by doc_id), `brain_context` (profile an entity / list communities), `brain_actions` (open tasks/deadlines), `brain_graph` (entity relations), `brain_proactive` (surfaced findings), `brain_draft_reply` / `brain_draft_refine` (email drafts). Prefer these over loading whole files.

**Load on demand:**
1. Active continuity → `state/hot.md` (auto-pruned to 14 days).
2. Historical context, prior emails, document content → `brain_search` first (summaries); `brain_read` only the 2-3 most relevant doc_ids. Never load full sets.
3. Task involves a project → `reference/projects.md`.
4. Task involves tools, automation, or the mcpbrain stack → `reference/systems.md`.
5. Related prior decision → `state/decisions.md`.

**Extended thinking:** use for risk assessments, strategic recommendations, financial analysis, compliance reviews, multi-stakeholder planning.

---

## Where Things Go

Writes are **routed through MCP tools, not hand-edits.** Each write tool is **QUEUED**: the daemon owns the file write + commit and applies it on ~one daemon cycle (not instant). Do **not** call a write tool *and* also edit the underlying file — pick the tool for the routes below.

| Type | Route through | What the daemon does |
|---|---|---|
| Decision that supersedes earlier behaviour | `brain_decision(text, rationale, owner, supersedes, org)` | Appends a dated row to `state/decisions.md` + commits |
| Continuity / "just decided" note, active work | `brain_note(text)` | Prepends a dated entry to `state/hot.md` + commits |
| Durable memory (project/system/preference) | `brain_memory_write(slug, description, body, memory_type)` | Writes `memory/<slug>.md` + a `MEMORY.md` pointer + commits |
| Observational / entity fact, meeting outcome | `brain_ingest(title, content, tags, observation_type, org)` | Into the graph + memory index; searchable after the next sync (~5 min) |
| Rule that should always apply | Edit `CLAUDE.md` directly (or `reference/*` if conditional) | Hand-edit — this file is not daemon-owned |
| Project/system reference change | Edit `reference/projects.md` or `reference/systems.md` | Hand-edit |

**hot.md discipline:** entries are 2-4 lines max with a `**YYYY-MM-DD:**` prefix; anything older than 14 days is auto-pruned.

---

## Output File Convention

- Cross-cutting deliverables → `outputs/`
- Project-specific deliverables → `projects/<project-name>/outputs/`

## Quality Standard

`voice.md` and `preferences.md` are loaded at session start. Apply them to all output. Before presenting any draft, run the voice self-check and fix issues before presenting, not after.

---

## Planning Before Action

- Any request touching more than two files, or involving a new system/script: propose a numbered plan and wait for confirmation before writing anything.
- Don't add features that weren't explicitly requested. Build the best long-term solution within scope.

---

## Proactive Behaviours

- Run `brain_search` before answering historical questions ("what do we know about X", "have we dealt with X before").
- Use `brain_actions` for task/deadline questions.
- `brain_ingest` at natural capture points: after extended discussion with no explicit capture, or after producing a significant deliverable.
- Search discipline: for factual lookups allow up to 5 `brain_search` queries; if nothing relevant surfaces, say "not in the index" rather than answer from training data.

---

## Session Capture

At natural capture points, call the matching write tool (capture is QUEUED — applied on ~one daemon cycle, searchable after the next sync ~5 min):
- Decisions that persist across sessions → `brain_decision`
- Key facts about people, projects, or systems → `brain_ingest`
- A continuity note for the next session → `brain_note`

---

## Self-Evolution Protocol

Capture things when they occur — don't defer to end of session. Use the "Where Things Go" routes (a write tool for daemon-owned files, a direct edit for `CLAUDE.md` / `reference/*`). Propose; the owner approves.

---

## Platform Notes

Backed by the mcpbrain MCP server and this working tree. The `brain_*` write tools are QUEUED on every surface — the daemon owns the file write + commit.

- **Cowork** — the primary interactive surface. Use the `brain_*` tools for reads and writes; let the daemon apply file changes.
- **Claude Desktop** — reads context via the mcpbrain MCP server and uses the `brain_*` tools.
- **Claude Code** — for editing the working tree directly (`CLAUDE.md`, `reference/*`, code).

**Where the files live:** this records repo is the working tree (decisions, hot, memory, context, reference). `~/.mcpbrain` is the runtime (index, daemon, connected runtime state).
```

- [ ] **Step 2: Create `mcpbrain/records_templates/context_identity.md`**

```markdown
# Identity

**Name:** {{OWNER_FULL_NAME}}
**Role:** {{OWNER_ROLE}}
**Organisations:** {{ORG_LIST}}

<!-- Fill in the rest: your responsibilities, areas of expertise, and anything a
colleague would need to know to work with you. The richer this is, the better
Claude can act on your behalf. -->

## Responsibilities

(Describe your main responsibilities here.)

## Expertise

(List your areas of expertise here.)
```

- [ ] **Step 3: Create `mcpbrain/records_templates/context_preferences.md`**

```markdown
# Preferences

How you want Claude to work with you. Fill these in over time.

## Format defaults

- (e.g. bullet points over prose; Australian English; no em dashes.)

## Collaboration style

- (e.g. propose a plan before acting on anything non-trivial.)

## Hard rules

- (e.g. never send an email without showing me the draft first.)

## Search relevance

- Treat `brain_search` results below your relevance bar as "not found" rather than forcing an answer.
```

- [ ] **Step 4: Create `mcpbrain/records_templates/reference_systems.md`**

```markdown
# Systems

Tools, automations, and integrations you rely on. Fill in as you go.

## mcpbrain

The local daemon that indexes your mail/drive/calendar and serves it to Claude
over MCP. Runtime lives in `~/.mcpbrain`; this records repo is its working tree.

## (Add your other systems here)
```

- [ ] **Step 5: Create `mcpbrain/records_templates/reference_projects.md`**

```markdown
# Projects

Active bodies of work, with current status. Fill in as you go.

## (Add a project here)

- **Status:**
- **Next step:**
```

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/records_templates/
git commit -m "feat(records): full CLAUDE.md + context/reference templates"
```

---

## Task 6: `records.py` — interpolate templates into the scaffold

**Files:**
- Modify: `mcpbrain/records.py`
- Test: `tests/test_records.py` (extend; create if absent)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_records.py  (add these; keep any existing tests)
from pathlib import Path

from mcpbrain import records


def test_scaffold_stamps_claude_md_with_profile(tmp_path):
    repo = str(tmp_path / "records")
    profile = {"owner_full_name": "Dana Lee", "owner_role": "Ops Lead",
               "orgs": [{"name": "Acme"}, {"name": "Globex"}]}
    records._ENSURED.clear()
    records.ensure_records_repo(repo, profile=profile)
    claude = (Path(repo) / "CLAUDE.md").read_text()
    assert "Acme" in claude and "Globex" in claude
    ident = (Path(repo) / "context" / "identity.md").read_text()
    assert "Dana Lee" in ident and "Ops Lead" in ident
    assert "{{OWNER_FULL_NAME}}" not in ident  # token fully replaced


def test_scaffold_never_clobbers_user_edits(tmp_path):
    repo = str(tmp_path / "records")
    records._ENSURED.clear()
    records.ensure_records_repo(repo, profile={"owner_full_name": "A", "owner_role": "R", "orgs": []})
    edited = Path(repo) / "CLAUDE.md"
    edited.write_text("MY EDITS")
    records._ENSURED.clear()
    records.ensure_records_repo(repo, profile={"owner_full_name": "B", "owner_role": "R2", "orgs": []})
    assert edited.read_text() == "MY EDITS"  # write-if-absent


def test_scaffold_records_degrades(tmp_path, monkeypatch):
    # records_dir resolvable but git missing -> degrade to [] (no raise)
    monkeypatch.setattr(records, "ensure_records_repo",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("git missing")))
    assert records.scaffold_records(str(tmp_path / "home")) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_records.py -v`
Expected: FAIL (`ensure_records_repo() got an unexpected keyword argument 'profile'` / `scaffold_records` missing)

- [ ] **Step 3: Edit `records.py` — add template stamping + `scaffold_records`**

At the top of `records.py`, after the existing imports add:

```python
from mcpbrain import config

_TEMPLATES = Path(__file__).parent / "records_templates"

# Relative target path in the repo -> template filename in records_templates/.
_TEMPLATE_FILES = {
    "CLAUDE.md": "CLAUDE.md",
    "context/identity.md": "context_identity.md",
    "context/preferences.md": "context_preferences.md",
    "reference/systems.md": "reference_systems.md",
    "reference/projects.md": "reference_projects.md",
}


def _render_template(name: str, profile: dict) -> str:
    text = (_TEMPLATES / name).read_text()
    orgs = [str(o.get("name") or "").strip() for o in (profile.get("orgs") or [])
            if isinstance(o, dict) and str(o.get("name") or "").strip()]
    org_list = ", ".join(orgs) if orgs else "(none configured yet)"
    org_block = "\n".join(f"- Items for {o} must be tagged clearly and kept separate." for o in orgs)
    repl = {
        "{{OWNER_FULL_NAME}}": profile.get("owner_full_name") or "(your name)",
        "{{OWNER_ROLE}}": profile.get("owner_role") or "(your role)",
        "{{ORG_LIST}}": org_list,
        "{{ORG_BLOCK}}": org_block,
    }
    for k, v in repl.items():
        text = text.replace(k, v)
    return text
```

Change the `ensure_records_repo` signature and add template stamping. Replace the signature line and the scaffold-writing loop:

```python
def ensure_records_repo(repo_dir: str, *, git_name: str = "mcpbrain",
                        git_email: str = "mcpbrain@localhost",
                        profile: dict | None = None) -> str:
```

Immediately after the existing `_SCAFFOLD` loop (after the `for rel, content in _SCAFFOLD.items(): ...` block that fills `newly_written`), add a second loop that stamps the interpolated templates:

```python
    if profile is not None:
        for rel, tmpl in _TEMPLATE_FILES.items():
            p = repo / rel
            if not p.exists():
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(_render_template(tmpl, profile))
                newly_written.append(rel)
```

> Note: this block must run BEFORE the `if fresh: git add -A ... elif newly_written: git add ...` commit block so the new files are committed in the same scaffold commit.

At the end of the module add:

```python
def scaffold_records(home: str) -> list[str]:
    """Ensure + stamp the records repo from the saved profile. Degrades to [].

    Best-effort: any failure (no git, unwritable dir) returns [] and never raises,
    so a settings POST is never failed by scaffolding.
    """
    try:
        repo = config.records_dir(home)
        profile = {
            "owner_full_name": config.owner_full_name(home),
            "owner_role": config.owner_role(home),
            "orgs": config.read_config(home).get("orgs") or [],
        }
        _ENSURED.discard(str(Path(repo).resolve()))  # force a re-stamp pass
        ensure_records_repo(repo, profile=profile)
        return [str(Path(repo) / rel) for rel in _TEMPLATE_FILES]
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.debug("scaffold_records degraded: %s", exc)
        return []
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_records.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/records.py tests/test_records.py
git commit -m "feat(records): stamp profile-interpolated CLAUDE.md + context/reference"
```

---

## Task 7: `probes.py` — registration / enrichment / hooks states

**Files:**
- Modify: `mcpbrain/probes.py`
- Test: `tests/test_probes.py` (extend)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_probes.py  (append)
import json
from pathlib import Path
from mcpbrain import probes


def _home(tmp_path, cfg):
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    return str(tmp_path)


def test_claude_not_registered(tmp_path, monkeypatch):
    monkeypatch.setattr(probes, "_claude_registered", lambda: False)
    r = probes.probe_claude(_home(tmp_path, {}))
    assert r["state"] == "not_started" and "register" in r["detail"].lower()


def test_claude_registered_awaiting_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(probes, "_claude_registered", lambda: True)
    r = probes.probe_claude(_home(tmp_path, {}))  # no heartbeat file
    assert r["state"] == "needs_action" and "reopen" in r["detail"].lower()


def test_enrichment_states(tmp_path, monkeypatch):
    home = _home(tmp_path, {})
    monkeypatch.setattr(probes.cowork_tasks, "enrichment_skill_present", lambda: False)
    assert probes.probe_enrichment(home)["state"] == "not_started"
    monkeypatch.setattr(probes.cowork_tasks, "enrichment_skill_present", lambda: True)
    # no recent inbox output -> needs_action
    assert probes.probe_enrichment(home)["state"] == "needs_action"
    inbox = tmp_path / "enrich_inbox"; inbox.mkdir()
    (inbox / "batch-1.json").write_text("{}")
    assert probes.probe_enrichment(home)["state"] == "ok"


def test_memory_hooks_probe(tmp_path, monkeypatch):
    monkeypatch.setattr(probes.hooks, "hooks_status", lambda: {"installed": True})
    assert probes.probe_memory_hooks(_home(tmp_path, {}))["state"] == "ok"
    monkeypatch.setattr(probes.hooks, "hooks_status", lambda: {"installed": False})
    assert probes.probe_memory_hooks(_home(tmp_path, {}))["state"] == "not_started"


def test_all_connections_has_new_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(probes, "_claude_registered", lambda: False)
    monkeypatch.setattr(probes.cowork_tasks, "enrichment_skill_present", lambda: False)
    monkeypatch.setattr(probes.hooks, "hooks_status", lambda: {"installed": False})
    conns = probes.all_connections(_home(tmp_path, {}))
    assert {"enrichment", "memory-hooks"} <= set(conns)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_probes.py -v`
Expected: FAIL (`_claude_registered` / `probe_enrichment` / `probe_memory_hooks` missing)

- [ ] **Step 3: Edit `probes.py`**

Add imports near the top (after `from mcpbrain import auth, config`):

```python
from mcpbrain import cowork_tasks, hooks
```

Add a registration helper and rewrite `probe_claude`:

```python
def _claude_registered() -> bool:
    """True when claude_desktop_config.json lists an mcpbrain server entry."""
    try:
        from mcpbrain.wizard.register import claude_desktop_config_path
        p = claude_desktop_config_path()
        data = json.loads(Path(p).read_text())
        servers = data.get("mcpServers") or {}
        return any("mcpbrain" in name for name in servers)
    except (OSError, ValueError, KeyError):
        return False


def probe_claude(home) -> dict:
    """Three states: not registered -> registered/awaiting restart -> connected."""
    p = Path(home) / "mcp_heartbeat.json"
    if not p.exists():
        if not _claude_registered():
            return _state("not_started", "Not registered yet — finish setup")
        return _state("needs_action", "Registered — quit & reopen Claude Desktop")
    try:
        last = json.loads(p.read_text()).get("last_seen")
        if last is None:
            raise ValueError("missing last_seen")
        last_dt = datetime.fromisoformat(last)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - last_dt > timedelta(days=_CLAUDE_STALE_DAYS):
            return _state("needs_action", "Not seen recently — open Claude Desktop", last_verified=last)
    except (OSError, ValueError):
        if not _claude_registered():
            return _state("not_started", "Not registered yet — finish setup")
        return _state("needs_action", "Registered — quit & reopen Claude Desktop")
    return _state("ok", "Connected", last_verified=last)
```

Add the two new probes (place after `probe_records`):

```python
import time as _time


def probe_enrichment(home) -> dict:
    """not_started (no SKILL.md) / ok (running) / needs_action (installed, idle)."""
    if not cowork_tasks.enrichment_skill_present():
        return _state("not_started", "Enrichment skill not installed yet")
    inbox = Path(home) / "enrich_inbox"
    try:
        recent = any(
            (_time.time() - p.stat().st_mtime) < 2 * 86400
            for p in inbox.glob("*.json")
        )
    except OSError:
        recent = False
    if recent:
        return _state("ok", "Running")
    return _state("needs_action", "Set up the schedule in Claude Desktop")


def probe_memory_hooks(home) -> dict:
    return (_state("ok", "On") if hooks.hooks_status().get("installed")
            else _state("not_started", "Off — turn on cross-session memory"))
```

Extend `probe_records` to report the CLAUDE.md state in its detail (replace the function body):

```python
def probe_records(home) -> dict:
    repo = Path(config.records_dir(home))
    if (repo / ".git").is_dir():
        detail = "Ready" if (repo / "CLAUDE.md").exists() else "Created (run Prepare working space)"
        return _state("ok", detail)
    return _state("not_started", "Records repo not created yet")
```

In `all_connections`, add the two probes to the `cheap` dict:

```python
    cheap = {
        "google": probe_google(home),
        "claude": probe_claude(home),
        "clickup": probe_clickup(home),
        "backup": probe_backup(home),
        "records": probe_records(home),
        "enrichment": probe_enrichment(home),
        "memory-hooks": probe_memory_hooks(home),
    }
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_probes.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/probes.py tests/test_probes.py
git commit -m "feat(probes): Claude registration, enrichment, and memory-hooks states"
```

---

## Task 8: `daemon.py` — `config_profile()` + materialise on save

**Files:**
- Modify: `mcpbrain/daemon.py` (add `config_profile`; extend `apply_config`)
- Test: `tests/test_daemon_profile.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_daemon_profile.py
"""config_profile projects non-secret fields; apply_config materialises assets."""
import json
from pathlib import Path

from mcpbrain import daemon as daemon_mod


class _FakeStore:
    def chunk_count(self): return 0
    def enriched_count(self): return 0
    def open_findings_count(self): return 0


def test_config_profile_omits_secret(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(json.dumps(
        {"owner_full_name": "Dana", "owner_role": "Ops", "owner_email": "d@x.com",
         "orgs": [{"name": "Acme"}], "clickup_api_key": "pk_secret",
         "clickup_list_id": "L1", "timezone": "Australia/Perth"}))
    # config_profile() calls the `app_dir` name bound in daemon's namespace.
    monkeypatch.setattr(daemon_mod, "app_dir", lambda: tmp_path)
    d = daemon_mod.Daemon.__new__(daemon_mod.Daemon)  # bypass __init__ network work
    d._store = _FakeStore()
    prof = d.config_profile()
    assert prof["owner_full_name"] == "Dana"
    assert prof["clickup_api_key_set"] is True
    assert "clickup_api_key" not in prof
    assert prof["timezone"] == "Australia/Perth"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_daemon_profile.py -v`
Expected: FAIL (`Daemon` has no attribute `config_profile`)

- [ ] **Step 3: Add `config_profile` to the `Daemon` class**

In `mcpbrain/daemon.py`, just after the `status` method (around line 542, after its `return {...}`), add:

```python
    def config_profile(self) -> dict:
        """Saved profile for the settings form — never includes the ClickUp secret."""
        cfg = config.read_config(str(app_dir()))
        return {
            "owner_full_name": cfg.get("owner_full_name", "") or "",
            "owner_name": cfg.get("owner_name", "") or "",
            "owner_email": cfg.get("owner_email", "") or "",
            "owner_role": cfg.get("owner_role", "") or "",
            "orgs": cfg.get("orgs") or [],
            "clickup_list_id": cfg.get("clickup_list_id", "") or "",
            "clickup_api_key_set": bool(cfg.get("clickup_api_key")),
            "timezone": cfg.get("timezone", "") or "",
        }
```

- [ ] **Step 4: Extend `apply_config` to materialise assets**

In `apply_config` (line 594), at the very end of the method (after the `with self._config_lock:` block completes), add a best-effort materialise step:

```python
        # Best-effort: keep the enrichment skill + records-repo scaffold current
        # whenever settings are saved. Failures never fail the POST.
        try:
            from mcpbrain import cowork_tasks, records
            cowork_tasks.write_enrichment_skill(home)
            records.scaffold_records(home)
        except Exception as exc:  # noqa: BLE001
            log.debug("apply_config materialise degraded: %s", exc)
```

- [ ] **Step 5: Run tests (profile + existing daemon tests)**

Run: `python -m pytest tests/test_daemon_profile.py -v`
Expected: PASS
Run: `python -m pytest tests/ -k daemon -q`
Expected: PASS (no regressions)

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/daemon.py tests/test_daemon_profile.py
git commit -m "feat(daemon): config_profile (no secret) + materialise skill/records on save"
```

---

## Task 9: `control_api.py` — `GET /api/config` + `GET /api/timezones`

**Files:**
- Modify: `mcpbrain/control_api.py:71-103` (do_GET)
- Test: `tests/test_control_api_reads.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_control_api_reads.py
"""GET /api/config returns the profile (no secret); GET /api/timezones lists zones."""
import json
import urllib.request

import pytest

from mcpbrain.control_api import ControlServer


class _Daemon:
    def status(self): return {"google_connected": False, "granted_scopes": []}
    def config_profile(self):
        return {"owner_full_name": "Dana", "clickup_api_key_set": True, "timezone": "Asia/Tokyo"}


@pytest.fixture
def server(tmp_path):
    s = ControlServer(_Daemon(), str(tmp_path))
    s.start()
    yield s
    s.stop()


def _get(server, path):
    req = urllib.request.Request(f"http://127.0.0.1:{server.port}{path}",
                                 headers={"Authorization": f"Bearer {server.token}"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, json.loads(r.read())


def test_get_config_has_profile_no_secret(server):
    code, body = _get(server, "/api/config")
    assert code == 200 and body["owner_full_name"] == "Dana"
    assert body["clickup_api_key_set"] is True
    assert "clickup_api_key" not in body


def test_get_timezones(server):
    code, body = _get(server, "/api/timezones")
    assert code == 200 and body["zones"]
    assert all("GMT" in z["label"] for z in body["zones"])


def test_config_requires_token(server):
    req = urllib.request.Request(f"http://127.0.0.1:{server.port}/api/config")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 401
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_control_api_reads.py -v`
Expected: FAIL (404 on `/api/config`)

- [ ] **Step 3: Add the routes to `do_GET`**

In `mcpbrain/control_api.py`, inside `do_GET`, after the `if self.path == "/api/status": ...` line (line 77), add:

```python
                if self.path == "/api/config":
                    return h_json(self, 200, server.daemon.config_profile())
                if self.path == "/api/timezones":
                    from mcpbrain import timezones
                    return h_json(self, 200,
                                  {"zones": timezones.zone_options(now=datetime.now(timezone.utc))})
```

(`datetime`/`timezone` are already imported at the top of the file.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_control_api_reads.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/control_api.py tests/test_control_api_reads.py
git commit -m "feat(control-api): GET /api/config + GET /api/timezones"
```

---

## Task 10: `control_api.py` — scaffold / hooks / image routes

**Files:**
- Modify: `mcpbrain/control_api.py` (do_GET image route; do_POST handlers)
- Create: `mcpbrain/wizard/img/.gitkeep`
- Test: `tests/test_control_api_actions.py`

- [ ] **Step 1: Create the image directory placeholder**

```bash
mkdir -p mcpbrain/wizard/img && touch mcpbrain/wizard/img/.gitkeep
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_control_api_actions.py
"""POST scaffold/hooks endpoints + GET /img static serving (with traversal guard)."""
import json
import urllib.error
import urllib.request

import pytest

from mcpbrain.control_api import ControlServer
from mcpbrain import records, hooks


class _Daemon:
    def status(self): return {}


@pytest.fixture
def server(tmp_path):
    s = ControlServer(_Daemon(), str(tmp_path))
    s.start()
    yield s
    s.stop()


def _post(server, path):
    req = urllib.request.Request(f"http://127.0.0.1:{server.port}{path}", data=b"{}",
                                 method="POST",
                                 headers={"Authorization": f"Bearer {server.token}"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, json.loads(r.read())


def test_records_scaffold(server, monkeypatch):
    monkeypatch.setattr(records, "scaffold_records", lambda home: ["/x/CLAUDE.md"])
    code, body = _post(server, "/api/records/scaffold")
    assert code == 200 and body["scaffolded"] == ["/x/CLAUDE.md"]


def test_hooks_install(server, monkeypatch):
    from pathlib import Path
    monkeypatch.setattr(hooks, "install_session_hooks", lambda: Path("/x/settings.json"))
    code, body = _post(server, "/api/hooks/install")
    assert code == 200 and body["installed"] is True


def test_img_serves_and_blocks_traversal(server):
    (server.home)  # img dir is package data; serve a known shipped file or 404 cleanly
    # unknown name -> 404
    req = urllib.request.Request(f"http://127.0.0.1:{server.port}/img/nope.png")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 404
    # traversal attempt -> 404, never escapes the dir
    req = urllib.request.Request(f"http://127.0.0.1:{server.port}/img/..%2f..%2fconfig.json")
    with pytest.raises(urllib.error.HTTPError) as e2:
        urllib.request.urlopen(req, timeout=5)
    assert e2.value.code == 404
```

- [ ] **Step 3: Add the `/img/<name>` GET route**

In `do_GET`, before the final `self.send_response(404)` line (line 103), add:

```python
                m = re.match(r"^/img/([A-Za-z0-9._-]+\.png)$", self.path)
                if m:
                    return server._serve_image(m.group(1))
```

`/img/` is served after `_auth_ok()` in this position; that's fine for the wizard (it already has the token). The strict regex (`[A-Za-z0-9._-]+\.png`) rejects `/`, `%2f`, and `..` traversal by construction.

Add the `_serve_image` method to `ControlServer` (after `_serve_dashboard`). It takes the handler `h` as its first argument, matching the existing `_serve_wizard(self, h)` / `_serve_dashboard(self, h)` style:

```python
    def _serve_image(self, h, name):
        root = (Path(__file__).parent / "wizard" / "img").resolve()
        p = (root / name).resolve()
        if root not in p.parents or not p.is_file():
            h.send_response(404); h.end_headers(); return
        data = p.read_bytes()
        h.send_response(200); h.send_header("Content-Type", "image/png")
        h.send_header("Content-Length", str(len(data))); h.end_headers(); h.wfile.write(data)
```

and call it as `return server._serve_image(self, m.group(1))` in `do_GET`.

- [ ] **Step 4: Add the two POST handlers**

In `_handle_post`, after the `/api/register` line (line 180), add:

```python
            if h.path == "/api/records/scaffold":
                from mcpbrain import records
                return h_json(h, 200, {"scaffolded": records.scaffold_records(str(self.home))})
            if h.path == "/api/hooks/install":
                from mcpbrain import hooks
                hooks.install_session_hooks()
                return h_json(h, 200, {"installed": True})
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_control_api_actions.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/control_api.py mcpbrain/wizard/img/.gitkeep tests/test_control_api_actions.py
git commit -m "feat(control-api): records/scaffold, hooks/install, /img static route"
```

---

## Task 11: Wizard — timezone dropdown, prefill, masked token

**Files:**
- Modify: `mcpbrain/wizard/index.html`
- Test: `tests/test_wizard_serve.py` (extend or create)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_wizard_serve.py  (add)
from pathlib import Path

WIZ = Path("mcpbrain/wizard/index.html").read_text()


def test_timezone_is_a_select():
    assert '<select id="timezone"' in WIZ
    assert '<input id="timezone"' not in WIZ


def test_prefill_and_dropdown_bootstrap_present():
    assert "/api/config" in WIZ          # one-shot prefill fetch
    assert "/api/timezones" in WIZ       # dropdown population
    assert "leave blank to keep" in WIZ  # masked-token placeholder
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_wizard_serve.py -v`
Expected: FAIL (`<select id="timezone"` not found)

- [ ] **Step 3: Replace the timezone input with a select**

In `mcpbrain/wizard/index.html` replace the timezone input (line 124):

```html
        <input id="timezone" required placeholder="e.g. Australia/Perth" autocomplete="off" spellcheck="false">
```

with:

```html
        <select id="timezone" required></select>
```

- [ ] **Step 4: Add prefill + dropdown population JS**

In the `<script>` block, replace the existing browser-detected default (lines 643-645):

```javascript
// Default the timezone field to the browser's detected zone for convenience.
try { document.getElementById("timezone").value =
      Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch(e){}
```

with a populate-then-prefill bootstrap:

```javascript
// Populate the timezone dropdown, then prefill the whole form from saved config.
async function populateTimezones(selected){
  try{
    const j = await (await fetch("/api/timezones", H)).json();
    const sel = $("timezone");
    sel.innerHTML = "";
    let detected = "";
    try{ detected = Intl.DateTimeFormat().resolvedOptions().timeZone || ""; }catch(e){}
    (j.zones||[]).forEach(z => {
      const o = document.createElement("option");
      o.value = z.value; o.textContent = z.label;
      if(z.value === (selected || detected)) o.selected = true;
      sel.appendChild(o);
    });
  }catch(e){}
}

async function prefillFromConfig(){
  let c = {};
  try{ c = await (await fetch("/api/config", H)).json(); }catch(e){ c = {}; }
  if(c.owner_full_name) $("owner_name").value = c.owner_full_name;
  if(c.owner_email) $("owner_email").value = c.owner_email;
  if(c.owner_role) $("owner_role").value = c.owner_role;
  // Rebuild one org row per saved org (domains joined by ", ").
  const orgs = c.orgs || [];
  if(orgs.length){
    const rows = $("org-rows");
    rows.querySelectorAll(".org-row:not(:first-child)").forEach(r => r.remove());
    const tmpl = rows.querySelector(".org-row");
    orgs.forEach((o, i) => {
      const row = i === 0 ? tmpl : tmpl.cloneNode(true);
      row.querySelector(".org-name").value = o.name || "";
      row.querySelector(".org-domains").value = (o.domains || []).join(", ");
      if(i > 0) rows.appendChild(row);
    });
  }
  if(c.clickup_list_id) $("clickup_list_id").value = c.clickup_list_id;
  if(c.clickup_api_key_set){
    $("clickup_api_key").placeholder = "•••• configured — leave blank to keep";
  }
  await populateTimezones(c.timezone || "");
}
prefillFromConfig();
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_wizard_serve.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/wizard/index.html tests/test_wizard_serve.py
git commit -m "feat(wizard): timezone dropdown + prefill from saved config (masked token)"
```

---

## Task 12: Wizard — status-first configured view + new connection cards

**Files:**
- Modify: `mcpbrain/wizard/index.html`
- Test: `tests/test_wizard_serve.py` (extend)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_wizard_serve.py  (add)
def test_home_status_renders_before_main():
    # status-first: the home-status section must appear before the wizard <main>
    assert WIZ.index('id="home-status"') < WIZ.index("<main")


def test_connection_order_includes_new_cards():
    assert '"enrichment"' in WIZ and '"memory-hooks"' in WIZ
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_wizard_serve.py -k "home_status or connection_order" -v`
Expected: FAIL (home-status currently after `<main>`; new keys absent)

- [ ] **Step 3: Move `#home-status` above `<main>`**

Cut the entire `<section id="home-status" ...> ... </section>` block (lines 416-428) and paste it immediately BEFORE the `<main class="wrap">` opening tag (line 77). Leave its `class="hidden"` intact.

- [ ] **Step 4: Update `renderHome` to keep the settings form visible when configured**

Replace `renderHome` (lines 614-623):

```javascript
function renderHome(j){
  const configured = !!j.is_configured;
  $("home-status").classList.toggle("hidden", !configured);
  // Hide all wizard steps EXCEPT the profile form when configured (it doubles as settings).
  document.querySelectorAll("main > section.card").forEach(s => {
    if(s.id === "step-profile"){ s.classList.toggle("hidden", false); }
    else { s.classList.toggle("hidden", configured); }
  });
  document.querySelectorAll("main").forEach(s => s.classList.remove("hidden"));
  if(configured){
    const h2 = document.querySelector("#step-profile h2");
    if(h2) h2.innerHTML = "Your settings";
  }
  if(!configured) return;
  $("hs-version").textContent = j.version ? ("v" + j.version) : "";
  $("hs-health").textContent = j.paused ? "Paused" : "Running";
  renderConnections(j.connections || {});
  renderBackfill(j.backfill || {});
}
```

- [ ] **Step 5: Add the new cards to the connection order**

Replace the order array in `renderConnections` (line 590):

```javascript
  const order = ["google","claude","clickup","backup","records","enrichment","memory-hooks"];
```

- [ ] **Step 6: Run test to verify it passes**

Run: `python -m pytest tests/test_wizard_serve.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add mcpbrain/wizard/index.html tests/test_wizard_serve.py
git commit -m "feat(wizard): status-first configured view + enrichment/memory-hooks cards"
```

---

## Task 13: Wizard — guided steps, expanders, checklist, action buttons, screenshots

**Files:**
- Modify: `mcpbrain/wizard/index.html`
- Test: `tests/test_wizard_serve.py` (extend)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_wizard_serve.py  (add)
def test_guided_elements_present():
    # ClickUp anchored instructions, the two project paths step, hooks button,
    # and screenshot images that hide on error.
    assert "Settings → Apps → API Token" in WIZ
    assert "Copy link" in WIZ                       # List ID instructions
    assert 'id="step-projects"' in WIZ
    assert 'id="step-hooks"' in WIZ
    assert "/api/records/scaffold" in WIZ
    assert "/api/hooks/install" in WIZ
    assert "onerror" in WIZ                          # screenshots hide when absent
    assert "/img/clickup-apps-token.png" in WIZ


def test_enrichment_skill_no_longer_inline_spec_only_rendered():
    # The canonical body now lives in cowork/enrichment.md; the wizard may still
    # show it, but the new project step must reference the records repo path.
    assert "records" in WIZ.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_wizard_serve.py -k guided -v`
Expected: FAIL

- [ ] **Step 3: Add a reusable expander style + helper**

In the `<style>` section add:

```html
<style>
  details.howto{margin:8px 0;border:1px solid #e6e8ec;border-radius:8px;padding:6px 10px}
  details.howto summary{cursor:pointer;font-weight:600;color:#2563eb}
  details.howto img{max-width:100%;border:1px solid #e6e8ec;border-radius:6px;margin:8px 0}
  details.howto ol{margin:6px 0 6px 18px}
  .copybtn{font-size:12px;margin-left:6px}
</style>
```

- [ ] **Step 4: Add the ClickUp "Show me how" expander**

Inside `#step-profile`, immediately before the ClickUp inputs (before line 116 `<input id="clickup_api_key" ...>`), insert:

```html
    <details class="howto">
      <summary>Show me how to get my ClickUp token &amp; list ID</summary>
      <p><b>API token:</b></p>
      <ol>
        <li>In ClickUp, click your avatar (top-right) → <b>Settings</b>.</li>
        <li>In the sidebar, click <b>Apps</b>.</li>
        <li>Under <b>API Token</b>, click <b>Generate</b>, then <b>Copy</b> (it starts with <code>pk_</code>).</li>
      </ol>
      <img src="/img/clickup-apps-token.png" alt="ClickUp API Token screen" onerror="this.style.display='none'">
      <p><b>List ID:</b> right-click the List in the sidebar → <b>Copy link</b>. The number after <code>/li/</code> in the link is your List ID.</p>
      <img src="/img/clickup-list-copylink.png" alt="ClickUp Copy link" onerror="this.style.display='none'">
      <p><a href="https://app.clickup.com" target="_blank" rel="noopener">Open ClickUp</a></p>
    </details>
```

- [ ] **Step 5: Add the two-projects step and the memory-hooks step**

After the `</section>` that closes `#step-register` (around line 399), insert two new sections:

```html
  <section id="step-projects" class="card">
    <h2><span class="num">5</span>Your two Cowork projects</h2>
    <p class="desc">mcpbrain works through two Cowork projects. Click the button to create the working space, then point Cowork at the folders shown.</p>
    <div class="row">
      <button class="primary" type="button" onclick="prepareWorkspace()">Prepare my working space</button>
      <span id="ws-state" class="badge idle hidden"></span>
    </div>
    <details class="howto">
      <summary>1. mcpbrain Enrichment — the engine room</summary>
      <p>A scheduled task that turns your mail into structured memory every hour.</p>
      <ol>
        <li>In Cowork: <b>Projects → + → Use an existing folder</b>.</li>
        <li>Choose the mcpbrain home folder: <code id="home-path">~/.mcpbrain</code> <button class="copybtn" type="button" onclick="copyText('home-path')">Copy</button></li>
        <li>Open <b>Scheduled → New → Local</b>, set the schedule to <b>Hourly</b>, and point it at the <code>mcpbrain-enrichment</code> skill (already written for you).</li>
      </ol>
      <img src="/img/cowork-scheduled-fields.png" alt="Cowork scheduled task" onerror="this.style.display='none'">
    </details>
    <details class="howto">
      <summary>2. My Brain — where you work day to day</summary>
      <p>Like Claude Code's memory, in Cowork. Its <code>CLAUDE.md</code> is already written with your identity, voice, and the memory tools.</p>
      <ol>
        <li>In Cowork: <b>Projects → + → Use an existing folder</b>.</li>
        <li>Choose your records repo: <code id="records-path">~/.mcpbrain/records</code> <button class="copybtn" type="button" onclick="copyText('records-path')">Copy</button></li>
        <li>Connect <code>~/.mcpbrain</code> as a read folder and make sure the mcpbrain tools are attached.</li>
      </ol>
      <img src="/img/cowork-use-existing-folder.png" alt="Cowork use existing folder" onerror="this.style.display='none'">
    </details>
  </section>

  <section id="step-hooks" class="card">
    <h2><span class="num">6</span>Turn on cross-session memory</h2>
    <p class="desc">This lets Claude remember across sessions automatically — it primes each new session with your recent context and captures each one at the end (the same way Claude Code does). It edits <code>~/.claude/settings.json</code> and keeps anything already there. Applies to both Claude Code and Cowork.</p>
    <div class="row">
      <button class="primary" type="button" onclick="installHooks()">Turn on memory hooks</button>
      <span id="hooks-state" class="badge idle hidden"></span>
    </div>
  </section>
```

- [ ] **Step 6: Add the button JS**

In the `<script>` block, add:

```javascript
function copyText(id){
  const t = $(id).textContent;
  navigator.clipboard.writeText(t).catch(()=>{});
}
async function prepareWorkspace(){
  $("ws-state").classList.remove("hidden");
  badge("ws-state", "Preparing…", "wait");
  try{
    const r = await P("/api/records/scaffold");
    const j = await r.json().catch(()=>({}));
    badge("ws-state", (j.scaffolded && j.scaffolded.length) ? "Ready" : "Ready", "ok");
  }catch(e){ badge("ws-state", "Could not prepare", "wait"); }
}
async function installHooks(){
  $("hooks-state").classList.remove("hidden");
  badge("hooks-state", "Turning on…", "wait");
  try{ await P("/api/hooks/install"); badge("hooks-state", "On", "ok"); }
  catch(e){ badge("hooks-state", "Could not turn on", "wait"); }
}
```

- [ ] **Step 7: Renumber the existing Status step**

The old Status step `<section id="step-status">` keeps its content; update its heading number from whatever it is to `<span class="num">7</span>`.

- [ ] **Step 8: Run test to verify it passes**

Run: `python -m pytest tests/test_wizard_serve.py -v`
Expected: PASS

- [ ] **Step 9: Manual smoke (optional but recommended)**

Run the daemon locally, open the wizard, confirm the dropdown lists zones, fields prefill, the expanders open, and the two buttons return without error.

- [ ] **Step 10: Commit**

```bash
git add mcpbrain/wizard/index.html tests/test_wizard_serve.py
git commit -m "feat(wizard): guided steps, ClickUp/Cowork how-to, projects + memory-hooks steps"
```

---

## Task 14: Screenshot manifest

**Files:**
- Create: `docs/onboarding/SCREENSHOTS.md`

- [ ] **Step 1: Write the manifest**

```markdown
# Onboarding screenshots

PNGs live in `mcpbrain/wizard/img/` (shipped in the wheel, served at `/img/<name>`).
Capture at ~1200px wide, light mode, and **redact any personal data** (email,
workspace names, real list names) — these ship publicly. Until a file exists, its
`<img>` hides itself (`onerror`), so the wizard ships text-only meanwhile.

| Filename | What it must show |
|----------|-------------------|
| `google-unverified-advanced.png` | Google consent "hasn't verified this app" → Advanced → Continue |
| `clickup-settings.png` | ClickUp avatar menu, Settings highlighted |
| `clickup-apps-token.png` | Settings → Apps → API Token (Generate/Copy) |
| `clickup-list-copylink.png` | Right-click List → Copy link |
| `clickup-list-id-url.png` | Copied URL with the `/li/<id>` portion highlighted |
| `claude-quit-reopen.png` | macOS menu bar Claude → Quit |
| `cowork-projects-plus.png` | Cowork Projects → + (the 3 options) |
| `cowork-use-existing-folder.png` | "Use an existing folder" picker |
| `cowork-project-create.png` | Naming the project + Create |
| `cowork-scheduled-new.png` | Scheduled → New → Local |
| `cowork-scheduled-fields.png` | Routine form: name, folder, Schedule = Hourly |
| `cowork-run-now-allow.png` | Run now + "Always allow" prompt |
```

- [ ] **Step 2: Commit**

```bash
git add docs/onboarding/SCREENSHOTS.md
git commit -m "docs(onboarding): screenshot capture manifest"
```

---

## Task 15: Packaging + full-suite verification

**Files:**
- Modify: `pyproject.toml`
- Verify: full test suite

- [ ] **Step 1: Confirm package-data globs**

Open `pyproject.toml` and find the package-data / `[tool.setuptools.package-data]` (or hatch `force-include` / `[tool.setuptools.packages.find]`) section that already ships `mcpbrain/wizard/*.html` and `mcpbrain/cowork/*.md`. Ensure these globs are present (add any missing):

```toml
[tool.setuptools.package-data]
mcpbrain = [
    "wizard/*.html",
    "wizard/img/*",
    "cowork/*.md",
    "records_templates/*",
    "records_templates/**/*",
    "google_oauth_client.json",
]
```

(Match the existing build backend's syntax — if the project uses hatchling, add the dirs to `[tool.hatch.build.targets.wheel.force-include]` or the `include` list instead. Do not change the backend.)

- [ ] **Step 2: Build the wheel and confirm assets are included**

Run:
```bash
python -m build --wheel 2>/dev/null || uv build --wheel
python - <<'PY'
import glob, zipfile
whl = sorted(glob.glob("dist/mcpbrain-*.whl"))[-1]
names = zipfile.ZipFile(whl).namelist()
need = ["mcpbrain/cowork/enrichment.md",
        "mcpbrain/records_templates/CLAUDE.md",
        "mcpbrain/wizard/index.html"]
for n in need:
    assert n in names, f"MISSING from wheel: {n}"
print("wheel includes all new assets")
PY
```
Expected: `wheel includes all new assets`

- [ ] **Step 3: Run the full test suite**

Run: `python -m pytest -q`
Expected: all green (no failures, no errors). Investigate and fix any regression before continuing.

- [ ] **Step 4: Run ruff**

Run: `ruff check mcpbrain tests`
Expected: clean (fix any new findings).

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml
git commit -m "build: ship records_templates + wizard/img + cowork package data"
```

---

## Self-review checklist (run before declaring done)

- [ ] Every spec requirement maps to a task: prefill (T9/T11), timezone dropdown (T1/T9/T11), Claude registration status (T7), enrichment skill + status (T2/T7/T8), full records CLAUDE.md + context/reference (T5/T6), memory hooks build + step (T3/T4/T7/T10/T13), guided onboarding + screenshots (T13/T14), status-first settings view (T12), packaging (T15).
- [ ] No `clickup_api_key` ever leaves `config_profile` / `GET /api/config` (T8/T9 assert this).
- [ ] All new probes + writers degrade, never raise (T2/T6/T7).
- [ ] Method/route names match across tasks: `config_profile`, `scaffold_records`, `install_session_hooks`, `hooks_status`, `enrichment_skill_present`, `zone_options`, `/api/config`, `/api/timezones`, `/api/records/scaffold`, `/api/hooks/install`, `/img/<name>`.
- [ ] `mcpbrain session-start` / `session-end` run without a daemon (degrade) and are registered in `cli.py`.
