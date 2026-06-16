# In-Context Failure Recovery Implementation Plan

## Session summary — 2026-06-16

**Status:** Complete. Merged to `main` and pushed to `Centrepoint-Church/mcpbrain` (`77dd4f0`).

**Executed by:** Claude (subagent-driven-development, 7 tasks × implementer + spec-reviewer + code-quality-reviewer)

**What was built:**
- `_REMEDIES` dict and `_REMEDY_PRIORITY`/`_MAX_ACTIONS` constants added to `mcpbrain/session_hooks.py`
- `_action_needed(home) -> str` helper: calls `probes.all_connections`, filters `needs_action` probes, sorts by priority (google → claude → records → backup → clickup → enrichment), caps at 3, returns formatted block or `""`
- `session_start` wired to print the block after the open-actions section (only when non-empty)
- Exception safety: any error from `all_connections` is silently swallowed; session hook never hard-fails
- 10 new tests added; pre-existing degradation test hardened with a probe monkeypatch (hermeticity fix caught by final reviewer)

**Files changed:** `mcpbrain/session_hooks.py` (+56 lines), `tests/test_session_hooks.py` (+157 lines, 14 tests total)

**Constraints honoured:** `mcpbrain.doctor` never imported or called; remedy strings plain text only; `probes.py` and `cli.py` untouched.

---

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface broken connections inside the user's Cowork/Claude session at session-start by appending a bounded "Action needed" block (problem + one-step remedy) to the existing `mcpbrain session-start` hook output.

**Architecture:** Add a module-level `_REMEDIES` dict and an `_action_needed(home) -> str` helper to `mcpbrain/session_hooks.py`. The helper calls the existing no-network `probes.all_connections(home, store=None)`, keeps only `needs_action` probes, maps each to a copy-pasteable remedy string, sorts by a fixed priority, caps at 3, and returns a formatted block (or `""`). `session_start()` prints this block after the open-actions block, wrapped so any exception from the probes is swallowed and never hard-fails the session.

**Tech Stack:** Python 3.12, pytest, ruff. Tests: `uv run pytest`; lint: `uv run ruff check mcpbrain/`.

**Worktree & Dependencies:** This worktree owns ONLY `mcpbrain/session_hooks.py` and `tests/test_session_hooks.py` — zero file collisions with any other spec, mergeable in any order. It depends on NO other spec's new code. The remedy strings name `mcpbrain doctor` (Spec 3) and `/mcpbrain-fix` (Cowork skill) as **plain text only**: this worktree must NOT import `mcpbrain.doctor`, must NOT call it, and must NOT assert in any test that `mcpbrain doctor` exists or is runnable. `mcpbrain auth` already exists in 0.0.6 `cli.py` and is safe to name (still text only — no import/call). Shared read-only dependency: `probes.all_connections` (never modified here). Create an isolated worktree via superpowers:using-git-worktrees at execution.

---

## Reference: exact remedy strings (from spec remedy table)

These are the canonical strings. Use them verbatim in `_REMEDIES` and in test assertions.

| Probe key | Remedy line |
|---|---|
| `google` | `Google sign-in expired → run: mcpbrain auth` |
| `claude` | `Daemon/plugin not seen recently → run: mcpbrain doctor` |
| `clickup` | `ClickUp key invalid → re-enter it in the mcpbrain wizard` |
| `backup` | `Backup overdue → run: mcpbrain doctor` |
| `records` | `Records repo problem → run: mcpbrain doctor` |
| `enrichment` | `Enrichment stalled → open Claude so the hourly task can run, or run /mcpbrain-fix` |

**Block format** (printed only when at least one remedy fires):

```
## ⚠️ Action needed
- <remedy 1>
- <remedy 2>
- <remedy 3>
```

**Priority order** (spec: "google, claude, daemon/records, then the rest"). Concretely, the ordering tuple is:

```
google, claude, records, backup, clickup, enrichment
```

Rationale: `google` and `claude` are explicit in the spec; "daemon/records" maps to the `records` probe (the daemon owns the records repo) and the doctor-class `backup`; the remaining probes (`clickup`, `enrichment`) are "the rest". Probes not present in this order list (none expected) sort last in insertion order. Cap at 3 after sorting.

---

## Task 1 — `_REMEDIES` dict exists with exact strings

### 1.1 Write the failing test

- [ ] Add this test to `tests/test_session_hooks.py` (append at end of file):

```python
def test_remedies_map_has_exact_strings():
    r = session_hooks._REMEDIES
    assert r["google"] == "Google sign-in expired → run: mcpbrain auth"
    assert r["claude"] == "Daemon/plugin not seen recently → run: mcpbrain doctor"
    assert r["clickup"] == "ClickUp key invalid → re-enter it in the mcpbrain wizard"
    assert r["backup"] == "Backup overdue → run: mcpbrain doctor"
    assert r["records"] == "Records repo problem → run: mcpbrain doctor"
    assert r["enrichment"] == (
        "Enrichment stalled → open Claude so the hourly task can run, or run /mcpbrain-fix"
    )
```

### 1.2 Run — expect FAIL

- [ ] `uv run pytest tests/test_session_hooks.py::test_remedies_map_has_exact_strings -q`
- [ ] Expected: `AttributeError` / `KeyError` — `_REMEDIES` does not exist yet.

### 1.3 Minimal implementation

- [ ] In `mcpbrain/session_hooks.py`, add the module-level constants below the existing `_MIN_CHARS = 200` line:

```python
# In-context recovery: each needs_action probe maps to one copy-pasteable remedy.
# Strings are kept here (single source) so they stay consistent with monitor.py.
# NOTE: `mcpbrain doctor` and `/mcpbrain-fix` are named as text only — this module
# must never import or call them. `mcpbrain auth` already exists in cli.py.
_REMEDIES: dict[str, str] = {
    "google": "Google sign-in expired → run: mcpbrain auth",
    "claude": "Daemon/plugin not seen recently → run: mcpbrain doctor",
    "clickup": "ClickUp key invalid → re-enter it in the mcpbrain wizard",
    "backup": "Backup overdue → run: mcpbrain doctor",
    "records": "Records repo problem → run: mcpbrain doctor",
    "enrichment": (
        "Enrichment stalled → open Claude so the hourly task can run, or run /mcpbrain-fix"
    ),
}

# Priority for the action-needed block: google, claude, then daemon/records, then the rest.
_REMEDY_PRIORITY: tuple[str, ...] = (
    "google",
    "claude",
    "records",
    "backup",
    "clickup",
    "enrichment",
)

_MAX_ACTIONS = 3
```

### 1.4 Run — expect PASS

- [ ] `uv run pytest tests/test_session_hooks.py::test_remedies_map_has_exact_strings -q` → PASS
- [ ] `uv run ruff check mcpbrain/` → clean

### 1.5 Commit

- [ ] `git add mcpbrain/session_hooks.py tests/test_session_hooks.py && git commit -m "feat(session-hooks): add _REMEDIES map for in-context recovery"`

---

## Task 2 — `_action_needed`: single needs_action → one-line block

### 2.1 Write the failing test

- [ ] Append to `tests/test_session_hooks.py`:

```python
def test_action_needed_single_google(monkeypatch):
    monkeypatch.setattr(session_hooks.probes, "all_connections", lambda home, store=None: {
        "google": {"state": "needs_action", "detail": "", "last_verified": None},
        "claude": {"state": "ok", "detail": "", "last_verified": None},
        "clickup": {"state": "ok", "detail": "", "last_verified": None},
        "backup": {"state": "ok", "detail": "", "last_verified": None},
        "records": {"state": "ok", "detail": "", "last_verified": None},
        "enrichment": {"state": "ok", "detail": "", "last_verified": None},
    })
    block = session_hooks._action_needed("/some/home")
    assert "## ⚠️ Action needed" in block
    assert "Google sign-in expired → run: mcpbrain auth" in block
    # only one remedy line
    assert block.count("\n- ") == 1
```

### 2.2 Run — expect FAIL

- [ ] `uv run pytest tests/test_session_hooks.py::test_action_needed_single_google -q`
- [ ] Expected: `AttributeError` — `_action_needed` does not exist, and `session_hooks.probes` is not imported.

### 2.3 Minimal implementation

- [ ] Add `probes` to the top-level imports in `mcpbrain/session_hooks.py`. Change the existing import block:

```python
from mcpbrain import config
from mcpbrain.capture import write_capture
```

to:

```python
from mcpbrain import config, probes
from mcpbrain.capture import write_capture
```

- [ ] Add the `_action_needed` helper (place it directly after `_open_actions`):

```python
def _action_needed(home: str) -> str:
    """Build the in-context recovery block: one remedy per needs_action probe.

    Returns the formatted block, or "" when nothing needs action. Never raises:
    if all_connections blows up, the caller still gets "" and the session is fine.
    not_started is deliberately ignored (mid-onboarding, not a regression).
    """
    try:
        conns = probes.all_connections(home, store=None) or {}
    except Exception:  # noqa: BLE001 — surfacing must never hard-fail the session
        return ""
    broken = [name for name, c in conns.items()
              if isinstance(c, dict) and c.get("state") == "needs_action"
              and name in _REMEDIES]

    def _rank(name: str) -> int:
        return _REMEDY_PRIORITY.index(name) if name in _REMEDY_PRIORITY else len(_REMEDY_PRIORITY)

    broken.sort(key=_rank)
    lines = [f"- {_REMEDIES[name]}" for name in broken[:_MAX_ACTIONS]]
    if not lines:
        return ""
    return "## ⚠️ Action needed\n" + "\n".join(lines)
```

### 2.4 Run — expect PASS

- [ ] `uv run pytest tests/test_session_hooks.py::test_action_needed_single_google -q` → PASS
- [ ] `uv run ruff check mcpbrain/` → clean

### 2.5 Commit

- [ ] `git add mcpbrain/session_hooks.py tests/test_session_hooks.py && git commit -m "feat(session-hooks): add _action_needed helper"`

---

## Task 3 — `not_started` is suppressed; `ok` everywhere → empty

### 3.1 Write the failing tests

- [ ] Append to `tests/test_session_hooks.py`:

```python
def test_action_needed_ignores_not_started(monkeypatch):
    monkeypatch.setattr(session_hooks.probes, "all_connections", lambda home, store=None: {
        "google": {"state": "ok", "detail": "", "last_verified": None},
        "claude": {"state": "ok", "detail": "", "last_verified": None},
        # never configured -> must NOT produce a line
        "clickup": {"state": "not_started", "detail": "", "last_verified": None},
        "backup": {"state": "not_started", "detail": "", "last_verified": None},
        "records": {"state": "ok", "detail": "", "last_verified": None},
        "enrichment": {"state": "ok", "detail": "", "last_verified": None},
    })
    assert session_hooks._action_needed("/some/home") == ""


def test_action_needed_empty_when_all_ok(monkeypatch):
    monkeypatch.setattr(session_hooks.probes, "all_connections", lambda home, store=None: {
        name: {"state": "ok", "detail": "", "last_verified": None}
        for name in ("google", "claude", "clickup", "backup", "records", "enrichment")
    })
    assert session_hooks._action_needed("/some/home") == ""
```

### 3.2 Run — expect PASS (behaviour already implemented in Task 2)

- [ ] `uv run pytest tests/test_session_hooks.py::test_action_needed_ignores_not_started tests/test_session_hooks.py::test_action_needed_empty_when_all_ok -q`
- [ ] Expected: PASS. (These tests lock in the spec's `not_started`-suppression and no-noise contracts; Task 2's filter already enforces them. If either fails, fix `_action_needed` before proceeding.)

### 3.3 Commit

- [ ] `git add tests/test_session_hooks.py && git commit -m "test(session-hooks): lock not_started suppression + no-noise-when-ok"`

---

## Task 4 — priority ordering + cap-at-3

### 4.1 Write the failing test

- [ ] Append to `tests/test_session_hooks.py`:

```python
def test_action_needed_caps_at_three_in_priority_order(monkeypatch):
    # All six broken -> only the top 3 by priority survive: google, claude, records.
    monkeypatch.setattr(session_hooks.probes, "all_connections", lambda home, store=None: {
        name: {"state": "needs_action", "detail": "", "last_verified": None}
        for name in ("google", "claude", "clickup", "backup", "records", "enrichment")
    })
    block = session_hooks._action_needed("/some/home")
    lines = block.splitlines()
    assert lines[0] == "## ⚠️ Action needed"
    body = lines[1:]
    assert len(body) == 3  # capped
    assert body[0] == "- Google sign-in expired → run: mcpbrain auth"
    assert body[1] == "- Daemon/plugin not seen recently → run: mcpbrain doctor"
    assert body[2] == "- Records repo problem → run: mcpbrain doctor"
    # lower-priority remedies dropped
    assert "ClickUp key invalid" not in block
    assert "Enrichment stalled" not in block


def test_action_needed_orders_subset(monkeypatch):
    # Only enrichment + claude broken -> claude first (higher priority), enrichment second.
    monkeypatch.setattr(session_hooks.probes, "all_connections", lambda home, store=None: {
        "google": {"state": "ok", "detail": "", "last_verified": None},
        "claude": {"state": "needs_action", "detail": "", "last_verified": None},
        "clickup": {"state": "ok", "detail": "", "last_verified": None},
        "backup": {"state": "ok", "detail": "", "last_verified": None},
        "records": {"state": "ok", "detail": "", "last_verified": None},
        "enrichment": {"state": "needs_action", "detail": "", "last_verified": None},
    })
    body = session_hooks._action_needed("/some/home").splitlines()[1:]
    assert body == [
        "- Daemon/plugin not seen recently → run: mcpbrain doctor",
        "- Enrichment stalled → open Claude so the hourly task can run, or run /mcpbrain-fix",
    ]
```

### 4.2 Run — expect PASS (behaviour already implemented in Task 2)

- [ ] `uv run pytest tests/test_session_hooks.py::test_action_needed_caps_at_three_in_priority_order tests/test_session_hooks.py::test_action_needed_orders_subset -q`
- [ ] Expected: PASS. Task 2's `_rank` sort + `[:_MAX_ACTIONS]` slice satisfy these. If they fail, fix the sort/cap in `_action_needed`.

### 4.3 Commit

- [ ] `git add tests/test_session_hooks.py && git commit -m "test(session-hooks): lock priority ordering + cap-at-3"`

---

## Task 5 — `session_start` integration (prints block after actions)

### 5.1 Write the failing test

- [ ] Append to `tests/test_session_hooks.py`:

```python
def test_session_start_appends_action_block_after_actions(tmp_path, monkeypatch):
    repo = tmp_path / "records"
    (repo / "state").mkdir(parents=True)
    (repo / "state" / "hot.md").write_text(
        "# Hot\n- **2026-06-10:** shipped the thing\n")
    monkeypatch.setattr(session_hooks.config, "records_dir", lambda home: str(repo))
    monkeypatch.setattr(session_hooks.probes, "all_connections", lambda home, store=None: {
        "google": {"state": "needs_action", "detail": "", "last_verified": None},
        "claude": {"state": "ok", "detail": "", "last_verified": None},
        "clickup": {"state": "ok", "detail": "", "last_verified": None},
        "backup": {"state": "ok", "detail": "", "last_verified": None},
        "records": {"state": "ok", "detail": "", "last_verified": None},
        "enrichment": {"state": "ok", "detail": "", "last_verified": None},
    })
    out = io.StringIO()
    session_hooks.session_start(str(tmp_path / "home"), out=out)
    text = out.getvalue()
    assert "## Open actions" in text
    assert "## ⚠️ Action needed" in text
    assert "Google sign-in expired → run: mcpbrain auth" in text
    # ordering: the action-needed block comes AFTER the open-actions heading
    assert text.index("## Open actions") < text.index("## ⚠️ Action needed")


def test_session_start_no_action_block_when_all_ok(tmp_path, monkeypatch):
    repo = tmp_path / "records"
    (repo / "state").mkdir(parents=True)
    (repo / "state" / "hot.md").write_text("# Hot\n")
    monkeypatch.setattr(session_hooks.config, "records_dir", lambda home: str(repo))
    monkeypatch.setattr(session_hooks.probes, "all_connections", lambda home, store=None: {
        name: {"state": "ok", "detail": "", "last_verified": None}
        for name in ("google", "claude", "clickup", "backup", "records", "enrichment")
    })
    out = io.StringIO()
    session_hooks.session_start(str(tmp_path / "home"), out=out)
    assert "Action needed" not in out.getvalue()
```

### 5.2 Run — expect FAIL

- [ ] `uv run pytest tests/test_session_hooks.py::test_session_start_appends_action_block_after_actions -q`
- [ ] Expected: FAIL — `session_start` does not yet print the block.

### 5.3 Minimal implementation

- [ ] In `session_start`, after the existing open-actions print, append the block. Change the tail of `session_start` from:

```python
    print("\n## Open actions", file=out)
    print(_open_actions(home), file=out)
```

to:

```python
    print("\n## Open actions", file=out)
    print(_open_actions(home), file=out)
    block = _action_needed(home)
    if block:
        print("\n" + block, file=out)
```

### 5.4 Run — expect PASS

- [ ] `uv run pytest tests/test_session_hooks.py::test_session_start_appends_action_block_after_actions tests/test_session_hooks.py::test_session_start_no_action_block_when_all_ok -q` → PASS
- [ ] `uv run ruff check mcpbrain/` → clean

### 5.5 Commit

- [ ] `git add mcpbrain/session_hooks.py tests/test_session_hooks.py && git commit -m "feat(session-hooks): print action-needed block at session start"`

---

## Task 6 — exception safety (all_connections raising must not break session_start)

### 6.1 Write the failing test

- [ ] Append to `tests/test_session_hooks.py`:

```python
def test_session_start_survives_probe_exception(tmp_path, monkeypatch):
    repo = tmp_path / "records"
    (repo / "state").mkdir(parents=True)
    (repo / "state" / "hot.md").write_text(
        "# Hot\n- **2026-06-10:** shipped the thing\n")
    monkeypatch.setattr(session_hooks.config, "records_dir", lambda home: str(repo))

    def boom(home, store=None):
        raise RuntimeError("probe blew up")

    monkeypatch.setattr(session_hooks.probes, "all_connections", boom)
    out = io.StringIO()
    # must NOT raise
    session_hooks.session_start(str(tmp_path / "home"), out=out)
    text = out.getvalue()
    # continuity + actions still printed
    assert "shipped the thing" in text
    assert "## Open actions" in text
    # no action block emitted
    assert "Action needed" not in text


def test_action_needed_returns_empty_on_exception(monkeypatch):
    def boom(home, store=None):
        raise RuntimeError("nope")

    monkeypatch.setattr(session_hooks.probes, "all_connections", boom)
    assert session_hooks._action_needed("/some/home") == ""
```

### 6.2 Run — expect PASS (try/except already in Task 2)

- [ ] `uv run pytest tests/test_session_hooks.py::test_session_start_survives_probe_exception tests/test_session_hooks.py::test_action_needed_returns_empty_on_exception -q`
- [ ] Expected: PASS — the `try/except Exception` in `_action_needed` already guarantees this. These tests lock the spec's hard "hook never hard-fails a session" contract. If either fails, ensure the `except` clause in `_action_needed` is broad (`except Exception`) and returns `""`.

### 6.3 Commit

- [ ] `git add tests/test_session_hooks.py && git commit -m "test(session-hooks): lock exception safety of action-needed surfacing"`

---

## Task 7 — full suite + lint green; finish

- [ ] `uv run pytest tests/test_session_hooks.py -q` → all PASS
- [ ] `uv run pytest -q` → no regressions in the broader suite
- [ ] `uv run ruff check mcpbrain/` → clean
- [ ] Confirm the four pre-existing `session_end` / `session_start` tests still pass unchanged.
- [ ] Use superpowers:finishing-a-development-branch to integrate. Do NOT commit beyond the per-task commits above unless directed; do NOT merge automatically.

---

## Final self-check (verify before claiming done)

- [ ] `mcpbrain/session_hooks.py` does NOT contain `import` of `mcpbrain.doctor`, nor any call to a `doctor` function. The strings `mcpbrain doctor` / `/mcpbrain-fix` appear only inside `_REMEDIES` literals.
- [ ] No test asserts `mcpbrain doctor` exists or is runnable; all probe behaviour is monkeypatched.
- [ ] Only `mcpbrain/session_hooks.py` and `tests/test_session_hooks.py` were modified. `probes.py` and `cli.py` untouched.
- [ ] `_REMEDIES` strings match the spec table byte-for-byte (including the `→` arrow and the `⚠️` in the heading).
- [ ] `not_started` never emits a line; `ok`-everywhere prints nothing; cap is 3 in priority order; probe exceptions are swallowed.
