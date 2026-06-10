# Part 7 — Backfill (indexing progress + one-shot Claude Code enrichment) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** (A) Surface the *existing* newest-first indexing backfill as progress in `status()`. (B) Add a one-shot **"enrich history with Claude Code"** action that drains the enrichment spool newest-first using the locally-installed `claude` CLI — catching up the backlog without a Gemini key or Nexus.

**Architecture:** The indexing backfill already works (`progressive_backfill_step`, newest→oldest, per-source floor cursors). Part A adds a pure `backfill_progress(store)` reader and exposes it under `status()["backfill"]`. Part B adds `mcpbrain/enrich_backfill.py`: a `local_claude_runner` (reusing `draft._find_claude`) that satisfies `extractor_driver.run_extractor`'s injected `run_claude`, and a one-shot loop `run_backfill()` that repeats prepare→extract→drain until the spool is dry. It is **gated** on `config.is_configured`, ordered **newest-first** (Task 2 makes `unenriched_chunks` recency-ordered), cancellable (a flag file), and exposed as `mcpbrain enrich-backfill`. Ongoing enrichment stays on the existing spool/cowork path — this is a catch-up, not a mode.

**Tech Stack:** Python 3.12, pytest. The `claude` CLI is shelled via subprocess (faked in tests).

This is **Plan 7 of the productization series** — spec **Part 4**. The UI cards that show backfill progress + the "Enrich history" button are in Plan 6 (this plan exposes the data + the action they call).

**Grounding (verified):**
- `sync/__init__.progressive_backfill_step` sets cursors `gmail_backfill_until` / `gmail_backfill_empty` (+ `drive_*`, `calendar_*`); `*_done` is a result-dict flag (derived from the empty counter reaching `_STOP_AFTER_EMPTY_WINDOWS = 4`). Read via `store.get_cursor(key)`.
- `extractor_driver.run_extractor(*, home=None, model="sonnet", timeout=600, run_claude=None) -> str|None`; default `run_claude` lazily imports the Nexus-only `claude_pool`. Injecting `run_claude=<callable>` overrides it. `run_claude(prompt, model=model, timeout=timeout) -> str` (raw text; driver json.loads it).
- `draft._find_claude() -> str` (CLAUDE_BIN → `shutil.which("claude")` → `~/.local/bin/claude`).
- `prepare.prepare(store, *, thread_cap, char_budget, resolution_due, now=None, ...)` selects via `group_unenriched_threads` → `store.unenriched_chunks()`, currently **rowid-ascending (oldest-first)** — must become newest-first.
- `drain.drain(store, *, home=None, apply=None, embedder=None) -> dict` (summary has `applied`, `marked`, …). Daemon's spool branch uses `apply=_graph_apply()`.
- `config.is_configured(home) -> bool`; `config.enrich_mode(home)` (spool|gemini|off) is unchanged by this plan.
- Test fakes: `run_claude=lambda prompt, **kw: json.dumps(batch)`; `RecordingApply()` in `tests/test_drain.py`.

---

## File Structure

- `mcpbrain/sync/__init__.py` — add `backfill_progress(store) -> dict`.
- `mcpbrain/daemon.py` — `status()` adds a `backfill` key.
- `mcpbrain/store.py` — `unenriched_chunks` ordered newest-first.
- `mcpbrain/enrich_backfill.py` — **new**: `local_claude_runner`, `run_backfill`, `main`, cancel-flag helpers.
- `mcpbrain/cli.py` — register `enrich-backfill`.
- `mcpbrain/control_api.py` — POST `/api/enrich-backfill/start` + `/cancel` (so Plan 6's button has an endpoint).
- Tests: `tests/test_backfill_progress.py`, `tests/test_unenriched_order.py`, `tests/test_enrich_backfill.py` (new); extend `tests/test_cli.py`.

---

## Task 1: Surface indexing-backfill progress in `status()`

**Files:**
- Modify: `mcpbrain/sync/__init__.py` (add `backfill_progress`)
- Modify: `mcpbrain/daemon.py` (`status()` adds `backfill`)
- Test: `tests/test_backfill_progress.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_backfill_progress.py
from mcpbrain.store import Store
from mcpbrain.sync import backfill_progress, _STOP_AFTER_EMPTY_WINDOWS


def _store(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4, read_only=False); s.init(); return s


def test_progress_defaults_when_no_cursors(tmp_path):
    p = backfill_progress(_store(tmp_path))
    assert set(p) == {"gmail", "drive", "calendar"}
    assert p["gmail"] == {"reached": None, "done": False}


def test_progress_reads_floor_and_done(tmp_path):
    s = _store(tmp_path)
    s.set_cursor("gmail_backfill_until", "2019-03-01T00:00:00+00:00")
    s.set_cursor("gmail_backfill_empty", str(_STOP_AFTER_EMPTY_WINDOWS))
    p = backfill_progress(s)
    assert p["gmail"]["reached"].startswith("2019-03-01")
    assert p["gmail"]["done"] is True
```

- [ ] **Step 2: Run → fail.** `pytest tests/test_backfill_progress.py -v`

- [ ] **Step 3: Implement** — in `mcpbrain/sync/__init__.py`:

```python
def backfill_progress(store) -> dict:
    """Per-source indexing-backfill progress for the status UI.

    `reached` is the floor cursor (how far back this source has indexed; None if
    not started). `done` is True once the empty-window counter hit the stop
    threshold (the source has walked past its earliest item)."""
    out = {}
    for src in ("gmail", "drive", "calendar"):
        reached = store.get_cursor(f"{src}_backfill_until")
        try:
            empty = int(store.get_cursor(f"{src}_backfill_empty") or 0)
        except ValueError:
            empty = 0
        out[src] = {"reached": reached, "done": empty >= _STOP_AFTER_EMPTY_WINDOWS}
    return out
```

In `mcpbrain/daemon.py` `status()`, before the return, add `backfill = run_sync_backfill_progress(self._store)` — i.e. import and call it — and add `"backfill": backfill` to the dict:

```python
        from mcpbrain.sync import backfill_progress
        backfill = backfill_progress(self._store)
```
and in the returned dict: `"backfill": backfill,`

- [ ] **Step 4: Run → pass** (+ `pytest tests/ -q -k "daemon and status"`).

- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(backfill): expose indexing progress in status()"`

---

## Task 2: Order un-enriched chunks newest-first

**Files:**
- Modify: `mcpbrain/store.py` (`unenriched_chunks`)
- Test: `tests/test_unenriched_order.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_unenriched_order.py
"""unenriched_chunks returns newest-synced first so backfill enriches recent history first."""
from mcpbrain.store import Store


def test_unenriched_chunks_newest_first(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4, read_only=False); s.init()
    # upsert three chunks in order c1, c2, c3 (ascending rowid = sync order)
    for cid in ("c1", "c2", "c3"):
        s.upsert_chunk(doc_id=f"gmail-{cid}-body-0", text=f"text {cid}",
                       metadata={"thread_id": cid})
    ids = [c["doc_id"] for c in s.unenriched_chunks()]
    # newest-synced (c3) must come first
    assert ids.index("gmail-c3-body-0") < ids.index("gmail-c1-body-0")
```

(Adapt `upsert_chunk` kwargs to the real signature — check `store.py`; the assertion on ordering is the oracle.)

- [ ] **Step 2: Run → fail** (current order is oldest-first). `pytest tests/test_unenriched_order.py -v`

- [ ] **Step 3: Implement** — in `mcpbrain/store.py`, find `unenriched_chunks` and add `ORDER BY rowid DESC` to its SELECT (newest-synced first). Preserve the existing `limit` handling.

- [ ] **Step 4: Run → pass.** Then `pytest tests/ -q -k "enrich or prepare or drain or backfill"` to confirm no ordering-dependent test broke (fix any that assumed oldest-first).

- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(store): unenriched_chunks newest-first (recent history enriches first)"`

---

## Task 3: Local Claude Code runner

**Files:**
- Create: `mcpbrain/enrich_backfill.py`
- Test: `tests/test_enrich_backfill.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_enrich_backfill.py
from mcpbrain import enrich_backfill


def test_local_claude_runner_invokes_cli(monkeypatch):
    seen = {}
    class _Result:
        stdout = '{"batch_id": "b1"}'
        returncode = 0
    def fake_run(cmd, *, input=None, capture_output=None, text=None, timeout=None):
        seen["cmd"] = cmd; seen["input"] = input; seen["timeout"] = timeout
        return _Result()
    monkeypatch.setattr(enrich_backfill, "_find_claude", lambda: "/usr/bin/claude")
    monkeypatch.setattr(enrich_backfill.subprocess, "run", fake_run)
    out = enrich_backfill.local_claude_runner("PROMPT", model="sonnet", timeout=120)
    assert out == '{"batch_id": "b1"}'
    assert seen["cmd"][0] == "/usr/bin/claude"
    assert "PROMPT" == seen["input"]            # prompt piped via stdin
    assert seen["timeout"] == 120
```

- [ ] **Step 2: Run → fail.** `pytest tests/test_enrich_backfill.py -v -k runner`

- [ ] **Step 3: Implement** — create `mcpbrain/enrich_backfill.py` (start with the runner; the loop is Task 4):

```python
"""One-shot 'enrich history with Claude Code': drain the spool newest-first
using the locally-installed claude CLI. Catch-up only — ongoing enrichment
stays on the spool/cowork path."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from mcpbrain import config
from mcpbrain.draft import _find_claude


def local_claude_runner(prompt: str, *, model: str = "sonnet", timeout: int = 600) -> str:
    """run_claude implementation for extractor_driver: shell to the local claude
    CLI in headless print mode, prompt piped via stdin, return stdout (the model's
    text — the extractor json.loads it)."""
    claude = _find_claude()
    result = subprocess.run(
        [claude, "-p", "--model", model, "--settings", '{"disableAllHooks":true}'],
        input=prompt, capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude CLI failed (rc={result.returncode}): {result.stderr[:500]}")
    return result.stdout


def _cancel_path(home) -> Path:
    return Path(home) / "enrich_backfill.cancel"


def request_cancel(home) -> None:
    _cancel_path(home).write_text("1")


def _cancelled(home) -> bool:
    return _cancel_path(home).exists()
```

- [ ] **Step 4: Run → pass.** `pytest tests/test_enrich_backfill.py -v -k runner`

- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(enrich-backfill): local Claude Code runner"`

---

## Task 4: One-shot backfill loop + CLI

**Files:**
- Modify: `mcpbrain/enrich_backfill.py` (`run_backfill`, `main`)
- Modify: `mcpbrain/cli.py` (register `enrich-backfill`)
- Test: `tests/test_enrich_backfill.py`, `tests/test_cli.py`

- [ ] **Step 1: Failing tests**

```python
# add to tests/test_enrich_backfill.py
import json


def test_run_backfill_refuses_when_unconfigured(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text("{}")
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    res = enrich_backfill.run_backfill(store=object(), embedder=object())
    assert res["status"] == "not_configured" and res["batches"] == 0


def test_run_backfill_loops_until_spool_dry(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(json.dumps({
        "owner_name": "Sam", "owner_email": "s@x.org", "orgs": [{"name": "Org"}]}))
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    # prepare returns 2 threads, then 1, then 0 (dry) — drives 2 loop iterations
    seq = iter([{"threads": [1, 2]}, {"threads": [1]}, {"threads": []}])
    monkeypatch.setattr(enrich_backfill.prepare, "prepare", lambda *a, **k: next(seq))
    extracted = {"n": 0}
    monkeypatch.setattr(enrich_backfill.extractor_driver, "run_extractor",
                        lambda **k: extracted.__setitem__("n", extracted["n"] + 1) or "inbox.json")
    drained = {"n": 0}
    monkeypatch.setattr(enrich_backfill.drain, "drain",
                        lambda *a, **k: drained.__setitem__("n", drained["n"] + 1) or {"applied": 1})
    res = enrich_backfill.run_backfill(store=object(), embedder=object())
    assert res["status"] == "done"
    assert res["batches"] == 2 and extracted["n"] == 2 and drained["n"] == 2
    # the extractor was given the LOCAL runner
    # (verified indirectly: run_extractor was called with run_claude=local_claude_runner)
```

```python
# add to tests/test_cli.py
def test_dispatch_enrich_backfill(monkeypatch):
    import mcpbrain.cli as cli
    seen = {}
    monkeypatch.setattr("mcpbrain.enrich_backfill.main", lambda argv: seen.setdefault("hit", True) or 0)
    cli.main(["enrich-backfill"])
    assert seen.get("hit") is True
```

- [ ] **Step 2: Run → fail.** `pytest tests/test_enrich_backfill.py tests/test_cli.py -v -k "backfill or enrich"`

- [ ] **Step 3: Implement** — add to `mcpbrain/enrich_backfill.py`:

```python
from mcpbrain import prepare, drain, extractor_driver

_THREAD_CAP = 20
_CHAR_BUDGET = 200_000


def run_backfill(*, store, embedder, home=None, model="sonnet", max_batches=10_000) -> dict:
    """Drain the enrichment spool newest-first via the local claude CLI until dry.

    Gated on config.is_configured (enrichment writes identity/org into the graph).
    Each iteration: prepare (writes pending.json from newest unenriched threads) →
    run_extractor (local runner → inbox) → drain (apply + mark). Stops when prepare
    yields no threads, on cancel, or at max_batches."""
    home = home or str(config.app_dir())
    if not config.is_configured(home):
        return {"status": "not_configured", "batches": 0}
    from mcpbrain.graph_write import apply as graph_apply  # same apply the daemon uses
    batches = 0
    while batches < max_batches:
        if _cancelled(home):
            _cancel_path(home).unlink(missing_ok=True)
            return {"status": "cancelled", "batches": batches}
        prep = prepare.prepare(store, thread_cap=_THREAD_CAP, char_budget=_CHAR_BUDGET,
                               resolution_due=False)
        if not prep.get("threads"):
            return {"status": "done", "batches": batches}
        path = extractor_driver.run_extractor(home=home, model=model,
                                              run_claude=local_claude_runner)
        if path is None:
            return {"status": "done", "batches": batches}
        drain.drain(store, home=home, apply=graph_apply, embedder=embedder)
        batches += 1
    return {"status": "max_batches", "batches": batches}


def main(argv=None) -> int:
    from mcpbrain.store import Store
    from mcpbrain.embed import get_embedder
    home = str(config.app_dir())
    emb = get_embedder(config.EMBEDDER)
    store = Store(config.store_path(), dim=emb.dim, read_only=False)
    res = run_backfill(store=store, embedder=emb, home=home)
    print(f"enrich-backfill: {res['status']} after {res['batches']} batches")
    return 0 if res["status"] in ("done", "cancelled") else 1
```

Wire into `mcpbrain/cli.py`: add `"enrich-backfill"` to the subcommand tuple and a dispatch entry:

```python
        "enrich-backfill": lambda: __import__("mcpbrain.enrich_backfill", fromlist=["main"]).main(rest),
```

(Match the import style used by the other handlers in `cli.py`.)

- [ ] **Step 4: Run → pass.** `pytest tests/test_enrich_backfill.py tests/test_cli.py -v`

- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(enrich-backfill): one-shot gated newest-first spool drain + CLI"`

---

## Task 5: Control-API endpoints for the UI button (Plan 6 calls these)

**Files:**
- Modify: `mcpbrain/control_api.py` (POST `/api/enrich-backfill/start`, `/api/enrich-backfill/cancel`)
- Test: extend `tests/test_control_api*.py`

- [ ] **Step 1: Failing test** (mirror the existing `FakeDaemon` + `ControlServer` round-trip pattern; assert the start route spawns the backfill on a thread and returns 202, cancel writes the flag and returns 200). Add a `start_enrich_backfill`/`cancel_enrich_backfill` method to the `FakeDaemon`-style test or to the real `Daemon`.

- [ ] **Step 2–4: Implement** — add to the daemon a thin `start_enrich_backfill()` (spawns `enrich_backfill.run_backfill` on a daemon thread using its own store/embedder, single-flight like `start_auth`) and `cancel_enrich_backfill()` (`enrich_backfill.request_cancel(home)`). Add the two POST routes in `_handle_post` mirroring `/api/auth/start`:

```python
        if h.path == "/api/enrich-backfill/start":
            threading.Thread(target=d.start_enrich_backfill, daemon=True).start()
            return h_json(h, 202, {"started": True})
        if h.path == "/api/enrich-backfill/cancel":
            d.cancel_enrich_backfill(); return h_json(h, 200, {"cancelled": True})
```

Run the control-api suite; commit.

```bash
git add -A && git commit -m "feat(control-api): enrich-backfill start/cancel endpoints"
```

---

## Final: full suite

- [ ] `pytest -q` green; `ruff check mcpbrain/ tests/` clean.
- [ ] Manual smoke: `mcpbrain enrich-backfill` on a configured install prints `done after N batches` (or `not_configured` on a blank one).

---

## Self-Review

**Spec coverage (Part 4):** indexing-backfill progress surfaced (Task 1); one-shot Claude-Code enrichment that is gated (Task 4 refuses unless `is_configured`), newest-first (Task 2 + the loop), cancellable/resumable (cancel flag; the spool persists so a re-run continues), via the local `claude` CLI (Task 3); plus the endpoints Plan 6's button calls (Task 5). Ongoing enrichment untouched.

**Placeholder honesty:** code is concrete. Task 2 changes a SQL `ORDER BY` whose exact query lives in `store.py` (the implementer locates it; the ordering test is the oracle). Task 5's test mirrors the existing control-api round-trip pattern rather than re-quoting it.

**Type consistency:** `backfill_progress(store)->dict` (`{src: {reached, done}}`); `local_claude_runner(prompt, *, model, timeout)->str` (matches `run_extractor`'s `run_claude` contract); `run_backfill(*, store, embedder, home=None, model="sonnet", max_batches=...)->dict` (`status`∈{not_configured,done,cancelled,max_batches}, `batches`); `request_cancel(home)`. The injected `run_claude=local_claude_runner` is what makes the extractor use the local CLI.
