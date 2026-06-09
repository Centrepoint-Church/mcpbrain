# Part 2 — Records Repo as Local Git in App-Dir — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the structured-records repo a per-user **local git repo created automatically inside the app-dir** (no remote, no `seed_joshbrain.py`, no "joshbrain" name in user-facing surfaces) — so any user's daemon can write decisions/continuity/memories without manual setup.

**Architecture:** A new `config.records_dir()` resolves the repo path (new `records_dir` key → legacy `joshbrain_dir` key for back-compat → `<home>/records` default). A new `mcpbrain/records.py` module `git init`s the repo and stamps the minimal scaffold the writers expect (the `decisions.md`/`hot.md` anchors, `MEMORY.md`, `memory/`, `context/voice.md`), idempotently and without clobbering. The daemon's drain path ensures the repo exists before every write, so it is created within one cycle of onboarding with zero user action. `draft.py` reads the voice file from `records_dir` instead of a hardcoded `~/joshbrain` sibling path.

**Tech Stack:** Python 3.12, pytest, local `git` (available in the dev/runtime environment). Tests use `tmp_path` as `home` and a real git repo (no network).

This is **Plan 2 of the productization series** (spec `docs/superpowers/specs/2026-06-09-mcpbrain-productization-design.md`, section **1.5**). The `agents.py` service-label rename (`church.centrepoint.joshbrain.*` → `com.mcpbrain.records.*`), the `agent_errs.py` glob, and the cross-platform cadence generators are **deferred to Plan 3 (platform, 1.6)** — they are coupled to the launchd/Task-Scheduler label work, not the records data layer.

**Scope decision (deliberate):** the Python module file `joshbrain_write.py` keeps its name — it is an internal import (`from mcpbrain import joshbrain_write`), not a user-facing surface, and renaming the file would churn imports for no user benefit. This plan renames the **directory, the config key, and the user-facing text**; the module file can be renamed in a later cleanup if desired.

---

## File Structure

- `mcpbrain/config.py` — add `records_dir()`; make `joshbrain_dir()` a back-compat alias.
- `mcpbrain/records.py` — **new**: `ensure_records_repo()` + the scaffold templates.
- `mcpbrain/drain.py` — add `_records_repo(home)` (resolve + ensure) and use it in the writer block (currently `config.joshbrain_dir` at line 447).
- `mcpbrain/draft.py` — `_load_voice_rules` reads `config.records_dir(home)/context/voice.md`.
- `mcpbrain/mcp_server.py` — user-facing tool-description text: "joshbrain" → "records".
- Tests: `tests/test_config_records.py`, `tests/test_records_repo.py`, `tests/test_drain_records_repo.py`, `tests/test_draft_voice.py` (all new).

**Dependency:** assumes Plan 1 is merged (it added `from mcpbrain import config` to `draft.py` and the neutralized `config.owner_*` helpers used for the git identity). Each task still guards its own imports.

---

## Task 1: `config.records_dir()` (+ back-compat alias)

**Files:**
- Modify: `mcpbrain/config.py:76-80`
- Test: `tests/test_config_records.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config_records.py
"""config.records_dir() — new key, legacy joshbrain_dir fallback, default."""
import json
from pathlib import Path

from mcpbrain import config


def _home(tmp_path: Path, data: dict) -> str:
    (tmp_path / "config.json").write_text(json.dumps(data))
    return str(tmp_path)


def test_default_is_records_under_home(tmp_path):
    assert config.records_dir(_home(tmp_path, {})) == str(tmp_path / "records")


def test_explicit_records_dir_key(tmp_path):
    assert config.records_dir(_home(tmp_path, {"records_dir": "/x/y"})) == "/x/y"


def test_legacy_joshbrain_dir_key_still_honored(tmp_path):
    assert config.records_dir(_home(tmp_path, {"joshbrain_dir": "/old/jb"})) == "/old/jb"


def test_records_dir_key_wins_over_legacy(tmp_path):
    home = _home(tmp_path, {"records_dir": "/new", "joshbrain_dir": "/old"})
    assert config.records_dir(home) == "/new"


def test_joshbrain_dir_alias_matches_records_dir(tmp_path):
    home = _home(tmp_path, {})
    assert config.joshbrain_dir(home) == config.records_dir(home)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config_records.py -v`
Expected: FAIL (`AttributeError: module 'mcpbrain.config' has no attribute 'records_dir'`).

- [ ] **Step 3: Write minimal implementation**

In `mcpbrain/config.py`, replace the existing `joshbrain_dir` function (lines 76-80) with:

```python
def records_dir(home) -> str:
    """Filesystem path to the per-user records repo the daemon writes into.

    A plain local git repo (no remote). Resolution: config 'records_dir' →
    legacy 'joshbrain_dir' key (back-compat for existing installs) →
    '<home>/records' default. The repo is created/scaffolded by
    records.ensure_records_repo at first write.
    """
    cfg = read_config(home)
    return (cfg.get("records_dir") or cfg.get("joshbrain_dir")
            or str(Path(home) / "records"))


def joshbrain_dir(home) -> str:
    """Deprecated alias for records_dir(); kept so older callers keep working."""
    return records_dir(home)
```

(`Path` is already imported at the top of `config.py`; the `import os` inside the old function is no longer needed here.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_config_records.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/config.py tests/test_config_records.py
git commit -m "feat(config): records_dir() with legacy joshbrain_dir fallback"
```

---

## Task 2: `mcpbrain/records.py` — ensure + scaffold the local repo

**Files:**
- Create: `mcpbrain/records.py`
- Test: `tests/test_records_repo.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_records_repo.py
"""ensure_records_repo: git-inits and scaffolds a local records repo, idempotently."""
from pathlib import Path

from mcpbrain import records
from mcpbrain.joshbrain_write import append_decision


def test_creates_repo_and_scaffold(tmp_path):
    repo = str(tmp_path / "records")
    out = records.ensure_records_repo(repo, git_name="t", git_email="t@t")
    assert out == repo
    assert (tmp_path / "records" / ".git").is_dir()
    dec = (tmp_path / "records" / "state" / "decisions.md").read_text()
    assert "Append new decisions at the top. One line per decision." in dec
    hot = (tmp_path / "records" / "state" / "hot.md").read_text()
    assert "## Just decided" in hot
    assert (tmp_path / "records" / "MEMORY.md").exists()
    assert (tmp_path / "records" / "memory").is_dir()
    assert (tmp_path / "records" / "context" / "voice.md").exists()


def test_idempotent_no_clobber_and_writer_appends(tmp_path):
    repo = str(tmp_path / "records")
    records.ensure_records_repo(repo, git_name="t", git_email="t@t")
    # Put custom content in decisions.md, then re-run ensure: must NOT clobber.
    dec_path = tmp_path / "records" / "state" / "decisions.md"
    custom = dec_path.read_text() + "\n| 2026-01-01 | Existing | - | Sam | Active | - |\n"
    dec_path.write_text(custom)
    records.ensure_records_repo(repo, git_name="t", git_email="t@t")
    assert "Existing" in dec_path.read_text()
    # The writer can append + commit against the scaffolded repo.
    assert append_decision(repo, text="Use X", owner="Sam") is True
    assert "| Sam |" in dec_path.read_text()


def test_existing_git_identity_is_not_overridden(tmp_path):
    import subprocess
    repo = str(tmp_path / "records")
    records.ensure_records_repo(repo, git_name="first", git_email="first@x")
    records.ensure_records_repo(repo, git_name="second", git_email="second@x")
    got = subprocess.run(["git", "-C", repo, "config", "user.name"],
                         capture_output=True, text=True).stdout.strip()
    assert got == "first"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_records_repo.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'mcpbrain.records'`).

- [ ] **Step 3: Write minimal implementation**

Create `mcpbrain/records.py`:

```python
"""Create and scaffold the per-user records repo (local git, no remote).

The daemon writes structured records (decisions, continuity, memories) into this
repo via joshbrain_write, committing by name. The repo is a plain local git repo
under the user's app dir. This module creates it and stamps the minimal scaffold
the writers expect (the decisions/hot anchors, MEMORY.md, memory/, voice.md),
idempotently — existing files are never clobbered and an existing repo's git
identity is left as-is.
"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

_DECISIONS_MD = """# Decisions

Decisions that supersede earlier behaviour. Newest first.

Append new decisions at the top. One line per decision.

| Date | Decision | Rationale | Owner | Status | Supersedes |
|------|----------|-----------|-------|--------|------------|
"""

_HOT_MD = """# Hot — active continuity

## Just decided

## Open
"""

_MEMORY_MD = "# Memory Index\n"

_VOICE_MD = "# Voice & style\n\n(Describe the owner's writing voice here.)\n"

# Relative path -> initial content. memory/ is created as a directory separately.
_SCAFFOLD = {
    "state/decisions.md": _DECISIONS_MD,
    "state/hot.md": _HOT_MD,
    "MEMORY.md": _MEMORY_MD,
    "context/voice.md": _VOICE_MD,
}


def _git(repo: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = {**os.environ, "LC_ALL": "C", "LANGUAGE": ""}
    return subprocess.run(["git", "-C", repo, *args], check=check,
                          capture_output=True, env=env)


def ensure_records_repo(repo_dir: str, *, git_name: str = "mcpbrain",
                        git_email: str = "mcpbrain@localhost") -> str:
    """Ensure repo_dir is a git repo with the scaffold the writers expect.

    git-inits the directory if absent, sets a local git identity only if none is
    configured (never overrides the user's), stamps any missing scaffold files
    (never clobbers existing ones), and commits the scaffold on first creation.
    Idempotent; safe to call every cycle. Returns repo_dir.
    """
    repo = Path(repo_dir)
    repo.mkdir(parents=True, exist_ok=True)
    fresh = not (repo / ".git").is_dir()
    if fresh:
        _git(repo_dir, "init")
    if _git(repo_dir, "config", "user.name", check=False).returncode != 0:
        _git(repo_dir, "config", "user.name", git_name)
    if _git(repo_dir, "config", "user.email", check=False).returncode != 0:
        _git(repo_dir, "config", "user.email", git_email)
    (repo / "memory").mkdir(exist_ok=True)
    wrote = False
    for rel, content in _SCAFFOLD.items():
        p = repo / rel
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
            wrote = True
    if fresh or wrote:
        _git(repo_dir, "add", "-A")
        staged = _git(repo_dir, "diff", "--cached", "--quiet",
                      check=False).returncode != 0
        if staged:
            _git(repo_dir, "commit", "-m", "scaffold: initialize records repo")
    return repo_dir
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_records_repo.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/records.py tests/test_records_repo.py
git commit -m "feat(records): ensure_records_repo — local git repo + scaffold"
```

---

## Task 3: drain writes to `records_dir`, ensuring the repo first

**Files:**
- Modify: `mcpbrain/drain.py` (add `_records_repo`; use it where `config.joshbrain_dir` is read, ~line 447)
- Test: `tests/test_drain_records_repo.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_drain_records_repo.py
"""drain._records_repo resolves records_dir and guarantees the repo exists."""
from mcpbrain.drain import _records_repo


def test_records_repo_resolves_and_creates(tmp_path):
    repo = _records_repo(str(tmp_path))
    assert repo == str(tmp_path / "records")
    assert (tmp_path / "records" / ".git").is_dir()
    assert (tmp_path / "records" / "state" / "decisions.md").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_drain_records_repo.py -v`
Expected: FAIL (`ImportError: cannot import name '_records_repo'`).

- [ ] **Step 3: Write minimal implementation**

In `mcpbrain/drain.py`, add this helper near the other module-level helpers (top of the file, after the imports):

```python
def _records_repo(home) -> str:
    """Resolve the records repo path and guarantee it exists (git + scaffold).

    The daemon is the single writer; ensuring here means a freshly-onboarded user
    gets a working repo on the first cycle with no manual seeding.
    """
    from mcpbrain import config, records
    repo = config.records_dir(str(home))
    name = config.owner_full_name(str(home)) or "mcpbrain"
    email = config.owner_email(str(home)) or "mcpbrain@localhost"
    records.ensure_records_repo(repo, git_name=name, git_email=email)
    return repo
```

Then change the writer block (currently at `drain.py:446-447`):

```python
                from mcpbrain import joshbrain_write as jw
                repo = config.joshbrain_dir(str(home_dir))
```

to:

```python
                from mcpbrain import joshbrain_write as jw
                repo = _records_repo(str(home_dir))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_drain_records_repo.py -v`
Expected: PASS (1 passed).

- [ ] **Step 5: Run the drain suite for regressions**

Run: `pytest tests/ -q -k drain`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/drain.py tests/test_drain_records_repo.py
git commit -m "feat(drain): write to records_dir, ensuring the local repo first"
```

---

## Task 4: `draft.py` voice file from `records_dir`

**Files:**
- Modify: `mcpbrain/draft.py:78-81` (`_load_voice_rules`)
- Test: `tests/test_draft_voice.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_draft_voice.py
"""draft._load_voice_rules reads the records repo's context/voice.md."""
import json

from mcpbrain import draft


def _home(tmp_path, data):
    (tmp_path / "config.json").write_text(json.dumps(data))
    return str(tmp_path)


def test_reads_voice_from_records_dir(tmp_path):
    home = _home(tmp_path, {"records_dir": str(tmp_path / "r")})
    (tmp_path / "r" / "context").mkdir(parents=True)
    (tmp_path / "r" / "context" / "voice.md").write_text("be warm and direct")
    assert draft._load_voice_rules(home) == "be warm and direct"


def test_missing_voice_returns_empty(tmp_path):
    home = _home(tmp_path, {})
    assert draft._load_voice_rules(home) == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_draft_voice.py -v`
Expected: FAIL (`test_reads_voice_from_records_dir` reads the old `~/joshbrain` sibling path, not `records_dir`).

- [ ] **Step 3: Write minimal implementation**

Replace `_load_voice_rules` in `mcpbrain/draft.py` (lines 78-81 and its body) with:

```python
def _load_voice_rules(home: str) -> str:
    """Read the records repo's context/voice.md. Returns '' if not found."""
    from mcpbrain import config
    p = Path(config.records_dir(home)) / "context" / "voice.md"
    try:
        return p.read_text()
    except OSError:
        return ""
```

(`Path` is already imported in `draft.py`; `config` was added in Plan 1 Task 7 — the local import here is harmless if it is also at module scope.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_draft_voice.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/draft.py tests/test_draft_voice.py
git commit -m "feat(draft): load voice.md from records_dir, not ~/joshbrain"
```

---

## Task 5: User-facing text "joshbrain" → "records"

The MCP tool descriptions tell the user where their captures go; they must not say "joshbrain". (Internal module/variable names are out of scope — see the header's scope decision.)

**Files:**
- Modify: `mcpbrain/mcp_server.py` (lines 231, 246, 261, 499, 518, 534 — the user-visible tool/queue descriptions)
- Modify: `mcpbrain/joshbrain_write.py:1` (module docstring) — cosmetic
- Test: none (text-only); verified by grep

- [ ] **Step 1: Replace the user-facing strings**

In `mcpbrain/mcp_server.py`, replace the word `joshbrain` with `records` in the six tool/queue description strings (lines ~231, 246, 261, 499, 518, 534). For example:

`"...appends a row to state/decisions.md in joshbrain ..."` → `"...appends a row to state/decisions.md in your records repo ..."`

Apply the same substitution to each of the six occurrences. In `mcpbrain/joshbrain_write.py:1`, change the docstring's "into the joshbrain repo" → "into the records repo".

- [ ] **Step 2: Verify no user-facing "joshbrain" text remains**

Run: `grep -rn "joshbrain" mcpbrain/mcp_server.py mcpbrain/joshbrain_write.py`
Expected: no hits in `mcp_server.py`; only the module *filename* reference (if any import line) remains — which is internal and intentional per the scope decision. (`agents.py`, `agent_errs.py` still contain `joshbrain` in labels — those are Plan 3.)

- [ ] **Step 3: Run the mcp_server tests for regressions**

Run: `pytest tests/ -q -k "mcp or server"`
Expected: PASS (any test asserting the old "joshbrain" wording must be updated to the new "records" wording).

- [ ] **Step 4: Commit**

```bash
git add mcpbrain/mcp_server.py mcpbrain/joshbrain_write.py
git commit -m "chore(records): user-facing text says 'records', not 'joshbrain'"
```

---

## Final: full suite + wrap-up

- [ ] **Step 1: Run the whole suite**

Run: `pytest -q`
Expected: PASS. Likely fallout: a test that configured `joshbrain_dir` and asserted a path — those still pass via the back-compat alias; a test asserting the old `~/joshbrain` voice path or the old "joshbrain" MCP wording must be updated.

- [ ] **Step 2: Lint**

Run: `ruff check mcpbrain/ tests/`
Expected: clean (remove the now-unused `import os` from `config.py` if it was only used by the old `joshbrain_dir`).

- [ ] **Step 3: Confirm the records repo auto-creates end-to-end**

Run: `python -c "import tempfile; from mcpbrain.drain import _records_repo; d=tempfile.mkdtemp(); print(_records_repo(d)); import os; print(sorted(os.listdir(d+'/records')))"`
Expected: prints `<tmp>/records` then a list including `.git`, `MEMORY.md`, `state`, `memory`, `context`.

---

## Self-Review

**Spec coverage (1.5):**
- Per-user local git repo in app-dir → Task 2 (`ensure_records_repo`, default `<home>/records`) + Task 3 (drain ensures it).
- `git init` + scaffold at onboarding → Task 2 (scaffold) + Task 3 (created on first cycle after onboarding, no manual seed).
- Rename `config.joshbrain_dir` → `records_dir` → Task 1 (with back-compat alias).
- Voice path off the hardcoded `~/joshbrain` → Task 4.
- "joshbrain" out of user-facing surfaces → Task 5. Module filename kept (documented scope decision). `agents.py`/`agent_errs.py` labels deferred to Plan 3 (documented).

**Placeholder scan:** every code step shows complete code; Task 5 is a concrete string substitution with a grep verification, not a vague instruction.

**Type consistency:** `records_dir(home)->str`, `joshbrain_dir(home)->str` (alias), `ensure_records_repo(repo_dir, *, git_name="mcpbrain", git_email="mcpbrain@localhost")->str`, `_records_repo(home)->str`, `_load_voice_rules(home)->str` — names/signatures consistent across tasks. The scaffold anchors written in Task 2 (`"Append new decisions at the top. One line per decision."`, `"## Just decided"`) exactly match the anchors `joshbrain_write.append_decision`/`append_continuity` search for.
