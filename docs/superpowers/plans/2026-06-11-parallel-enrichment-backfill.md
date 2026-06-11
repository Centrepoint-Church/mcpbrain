# Parallel Enrichment Backfill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Drain the ~62k-thread enrichment backlog faster by running N Claude extractor sessions in parallel while keeping every SQLite write on a single thread.

**Architecture:** A new `mcpbrain/parallel_backfill.py` runs a per-wave loop on the main thread (sole DB writer): pull one wave of unenriched threads, filter noise, partition into ≤`workers` disjoint sub-batches, fan the slow `claude --print` calls out across a thread pool (workers only write inbox files), then drain all inbox files serially. A thin `bin/fast_backfill.py` is the CLI. A small refactor extracts `prepare.build_pending()` (assemble the pending dict without writing the file) so the parallel path can build many in-memory batches.

**Tech Stack:** Python 3.11+, `concurrent.futures.ThreadPoolExecutor`, pytest, existing `mcpbrain` modules (`prepare`, `drain`, `thread_enrich`, `contract`, `graph_write`, `config`). Reuses `bin/drain_backlog.py` helpers (`extract_answer`, `parse_extractor_json`, `patch_extractions`, `atomic_write_inbox`, `quarantine`, `daemon_status`) by importing them.

**Spec:** `docs/superpowers/specs/2026-06-11-parallel-enrichment-backfill-design.md`

---

## File Structure

- **Modify** `mcpbrain/prepare.py` — extract `build_pending()` from `prepare()`; `prepare()` calls it then writes.
- **Create** `mcpbrain/parallel_backfill.py` — core wave loop, partitioning, worker, backoff, guard. Importable, testable; `run_claude`/`apply` injected.
- **Create** `bin/fast_backfill.py` — thin CLI wiring real `Store`/embedder/runner/apply into `run_parallel_backfill`.
- **Modify** `tests/test_prepare.py` — add `build_pending` cases.
- **Create** `tests/test_parallel_backfill.py` — wave loop, partitioning, backoff, guard, cancellation, drain barrier.

A note on importing from `bin/`: `bin/drain_backlog.py` is a script, not a package module. To reuse its helpers cleanly, **the helper functions stay in `bin/drain_backlog.py` and are imported by path** in `parallel_backfill.py` via a tiny loader (Task 2). This avoids copy-paste drift. If import-by-path feels fragile during implementation, the fallback is to lift those pure helpers into a new `mcpbrain/extractor_io.py` and have both `drain_backlog.py` and `parallel_backfill.py` import from there — but do NOT do that unless the path import is shown not to work; it widens scope.

---

## Task 1: Extract `prepare.build_pending()`

**Files:**
- Modify: `mcpbrain/prepare.py:366-409` (the `prepare` function)
- Test: `tests/test_prepare.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_prepare.py` (reuse whatever fake store / batch fixtures the existing tests in that file use; the existing `prepare` tests already construct a store and monkeypatch the `_group_unenriched_threads` / `_reassemble_thread` / context seams — mirror that setup):

```python
def test_build_pending_returns_dict_without_writing(tmp_path, monkeypatch):
    # build_pending must NOT touch the filesystem — no pending.json appears.
    import datetime
    from mcpbrain import prepare
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    monkeypatch.setattr(prepare, "_reassemble_thread",
                        lambda chunks: [{"message_id": "m1", "date": "2026-01-01",
                                         "sender": "a@x.org", "subject": "Hi", "text": "hello"}])
    monkeypatch.setattr(prepare, "_build_context", lambda store, tids: {"owner_name": "Sam"})

    class _Batch:
        thread_id = "t1"; doc_ids = ["d1"]; chunks = [{"doc_id": "d1"}]

    now = datetime.datetime(2026, 6, 11, 9, 0, 0, tzinfo=datetime.timezone.utc)
    data = prepare.build_pending(object(), [_Batch()], char_budget=200_000, now=now,
                                 batch_id="fastbf-0-0")
    assert data["batch_id"] == "fastbf-0-0"
    assert data["prepared_at"] == "2026-06-11T09:00:00Z"
    assert len(data["threads"]) == 1 and data["threads"][0]["thread_id"] == "t1"
    assert data["merge_review"] == []
    assert not (tmp_path / "enrich_queue" / "pending.json").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_prepare.py::test_build_pending_returns_dict_without_writing -v`
Expected: FAIL — `AttributeError: module 'mcpbrain.prepare' has no attribute 'build_pending'`.

- [ ] **Step 3: Implement `build_pending` and re-point `prepare`**

In `mcpbrain/prepare.py`, add `build_pending` above `prepare` (after `attach_extra_blocks`):

```python
def build_pending(store, batches, *, char_budget: int, now,
                  batch_id: str | None = None, resolution_due: bool = False,
                  synthesis_requests: list | None = None,
                  extra_blocks: dict | None = None) -> dict:
    """Assemble the pending.json dict for already-grouped, noise-filtered batches.

    Pure assembly: builds thread blocks (splitting over-long threads), context,
    and the optional merge-review block, then returns the dict. Does NOT write
    any file and does NOT mark the store. `batch_id` defaults to a timestamped
    id when not supplied. Callers that need many concurrent batches pass their
    own unique batch_id.
    """
    threads = []
    for batch in batches:
        block = _thread_block(store, batch)
        threads.extend(_split_long_thread(block, char_budget))

    context = _build_context(store, [b.thread_id for b in batches])
    merge_review = _merge_review_block(store) if resolution_due else []

    if batch_id is None:
        batch_id = f"batch-{now:%Y%m%d-%H%M%S}"
    data = {
        "batch_id": batch_id,
        "prepared_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "context": context,
        "threads": threads,
        "merge_review": merge_review,
    }
    if synthesis_requests:
        from mcpbrain.synthesise_threads import attach_synthesis_block
        data = attach_synthesis_block(data, synthesis_requests)
    data = attach_extra_blocks(data, extra_blocks)
    return data
```

Then replace the body of `prepare` (from `threads = []` through the `return`) so it delegates. The new `prepare` body, starting after `if not kept: return {...}`:

```python
    data = build_pending(store, kept, char_budget=char_budget, now=now,
                         resolution_due=resolution_due,
                         synthesis_requests=synthesis_requests,
                         extra_blocks=extra_blocks)
    _write_pending(data)
    return {"batch_id": data["batch_id"], "threads": len(data["threads"]),
            "merge_pairs": len(data["merge_review"])}
```

(Leave the grouping / `_filter_noise` / `kept = kept[:thread_cap]` / empty-check lines at the top of `prepare` exactly as they are.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_prepare.py -v`
Expected: PASS — the new test plus all existing `prepare` tests (behaviour of `prepare()` is unchanged).

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/prepare.py tests/test_prepare.py
git commit -m "refactor(prepare): extract build_pending() (assemble dict, no file write)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Helper loader + module skeleton with the config gate

**Files:**
- Create: `mcpbrain/parallel_backfill.py`
- Test: `tests/test_parallel_backfill.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_parallel_backfill.py`:

```python
import json
from mcpbrain import parallel_backfill


def test_run_parallel_backfill_refuses_when_unconfigured(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text("{}")
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    res = parallel_backfill.run_parallel_backfill(
        store=object(), embedder=object(), home=str(tmp_path),
        run_claude=lambda *a, **k: "{}", apply=lambda *a, **k: {})
    assert res["status"] == "not_configured"
    assert res["waves"] == 0


def test_helpers_are_importable():
    # The drain_backlog helpers must be reachable from the module.
    for name in ("extract_answer", "parse_extractor_json", "patch_extractions",
                 "atomic_write_inbox", "quarantine", "daemon_status"):
        assert hasattr(parallel_backfill, name), name
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_parallel_backfill.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcpbrain.parallel_backfill'`.

- [ ] **Step 3: Implement the skeleton + helper loader**

Create `mcpbrain/parallel_backfill.py`:

```python
"""Parallel enrichment backfill: fan the slow claude extractor calls out across
a thread pool, drain the results serially on the main thread.

Tactical one-shot drainer for a large un-enriched backlog. Standalone — opens
the store read-write and runs prepare/drain itself, so the daemon must be paused
or stopped (the CLI guards this). The main thread is the SOLE SQLite writer;
worker threads only run `claude --print` subprocesses and write inbox files.
Ongoing steady-state enrichment stays on the daemon/cowork path.
"""
from __future__ import annotations

import importlib.util
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from mcpbrain import config, prepare, drain as drain_mod
from mcpbrain.contract import validate_batch_file
from mcpbrain.thread_enrich import group_unenriched_threads
from mcpbrain.draft import _find_claude

log = logging.getLogger("mcpbrain.parallel_backfill")

_CHAR_BUDGET = 200_000


def _load_drain_backlog():
    """Import bin/drain_backlog.py by path to reuse its pure helpers without
    duplicating them. bin/ is not a package, so load it as a standalone module."""
    script = Path(__file__).resolve().parents[1] / "bin" / "drain_backlog.py"
    spec = importlib.util.spec_from_file_location("_drain_backlog", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_db = _load_drain_backlog()
extract_answer = _db.extract_answer
parse_extractor_json = _db.parse_extractor_json
patch_extractions = _db.patch_extractions
atomic_write_inbox = _db.atomic_write_inbox
quarantine = _db.quarantine
daemon_status = _db.daemon_status
_PREAMBLE = _db._PREAMBLE
_PENDING_DELIM = _db._PENDING_DELIM


def run_parallel_backfill(*, store, embedder, home=None, model="sonnet",
                          workers=8, batch_size=20, char_budget=_CHAR_BUDGET,
                          timeout=600, max_waves=None, run_claude=None,
                          apply=None, cancel_event=None) -> dict:
    """Drain the backlog wave-by-wave with `workers` parallel claude sessions.

    Gated on config.is_configured. Returns a summary dict with keys:
    status ("done"|"max_waves"|"cancelled"|"not_configured"), waves, threads,
    quarantined.
    """
    home = home or str(config.app_dir())
    if not config.is_configured(home):
        return {"status": "not_configured", "waves": 0, "threads": 0,
                "quarantined": 0}
    # Full loop arrives in Task 5; skeleton returns a configured no-op for now.
    return {"status": "done", "waves": 0, "threads": 0, "quarantined": 0}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_parallel_backfill.py -v`
Expected: PASS — both tests.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/parallel_backfill.py tests/test_parallel_backfill.py
git commit -m "feat(parallel_backfill): module skeleton + drain_backlog helper reuse + config gate

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Partition a wave into disjoint sub-batches

**Files:**
- Modify: `mcpbrain/parallel_backfill.py`
- Test: `tests/test_parallel_backfill.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_parallel_backfill.py`:

```python
def test_partition_splits_into_disjoint_sub_batches():
    from mcpbrain import parallel_backfill
    items = list(range(45))           # 45 threads
    parts = parallel_backfill._partition(items, batch_size=20)
    assert [len(p) for p in parts] == [20, 20, 5]
    # disjoint + complete
    flat = [x for p in parts for x in p]
    assert flat == items and len(set(flat)) == 45


def test_partition_empty_returns_empty():
    from mcpbrain import parallel_backfill
    assert parallel_backfill._partition([], batch_size=20) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_parallel_backfill.py::test_partition_splits_into_disjoint_sub_batches -v`
Expected: FAIL — `AttributeError: ... has no attribute '_partition'`.

- [ ] **Step 3: Implement `_partition`**

Add to `mcpbrain/parallel_backfill.py`:

```python
def _partition(items, *, batch_size):
    """Split a list into consecutive disjoint chunks of at most batch_size.

    Consecutive slicing guarantees no item appears in two chunks, so two
    workers never extract the same thread."""
    return [items[i:i + batch_size] for i in range(0, len(items), batch_size)]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_parallel_backfill.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/parallel_backfill.py tests/test_parallel_backfill.py
git commit -m "feat(parallel_backfill): disjoint wave partitioning

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Worker — prompt build, parse/patch/validate, backoff, quarantine

**Files:**
- Modify: `mcpbrain/parallel_backfill.py`
- Test: `tests/test_parallel_backfill.py`

The worker is the only place a `claude` subprocess runs. It takes a pre-built
pending dict (from `prepare.build_pending`) and a `run_claude` callable, retries
on transient rate-limit/overload, and writes either an inbox file (success) or a
quarantine file (terminal failure). It does NOT touch the store.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_parallel_backfill.py`:

```python
import subprocess


def _pending(batch_id="fastbf-0-0"):
    return {"batch_id": batch_id, "prepared_at": "2026-06-11T09:00:00Z",
            "context": {}, "threads": [{"thread_id": "t1",
            "messages": [{"message_id": "m1", "date": "2026-01-01",
                          "sender": "a@x.org", "subject": "Hi", "text": "hello"}]}],
            "merge_review": []}


def test_worker_writes_inbox_on_valid_answer(tmp_path):
    from mcpbrain import parallel_backfill
    answer = {"batch_id": "fastbf-0-0", "extractions": [
        {"thread_id": "t1", "content_type": "fyi",
         "messages": [{"message_id": "m1", "date": "2026-01-01"}],
         "entities": [], "relations": [], "actions": []}]}
    ok, reason = parallel_backfill._process_batch_worker(
        home=tmp_path, pending=_pending(), prompt_prefix="EXTRACT",
        run_claude=lambda prompt, **k: json.dumps(answer),
        model="sonnet", timeout=600, max_retries=3, backoff_base=0.0)
    assert ok is True
    assert (tmp_path / "enrich_inbox" / "fastbf-0-0.json").exists()


def test_worker_retries_then_succeeds_on_rate_limit(tmp_path):
    from mcpbrain import parallel_backfill
    answer = {"batch_id": "fastbf-0-0", "extractions": []}
    calls = {"n": 0}
    def flaky(prompt, **k):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise subprocess.CalledProcessError(1, "claude", stderr="overloaded_error: 429")
        return json.dumps(answer)
    ok, reason = parallel_backfill._process_batch_worker(
        home=tmp_path, pending=_pending(), prompt_prefix="EXTRACT",
        run_claude=flaky, model="sonnet", timeout=600,
        max_retries=5, backoff_base=0.0)
    assert ok is True and calls["n"] == 3


def test_worker_quarantines_on_persistent_timeout(tmp_path):
    from mcpbrain import parallel_backfill
    def always_timeout(prompt, **k):
        raise subprocess.TimeoutExpired("claude", 600)
    ok, reason = parallel_backfill._process_batch_worker(
        home=tmp_path, pending=_pending(), prompt_prefix="EXTRACT",
        run_claude=always_timeout, model="sonnet", timeout=600,
        max_retries=2, backoff_base=0.0)
    assert ok is False
    assert list((tmp_path / "enrich_inbox" / "bad").glob("*.txt"))


def test_worker_quarantines_unparseable_answer(tmp_path):
    from mcpbrain import parallel_backfill
    ok, reason = parallel_backfill._process_batch_worker(
        home=tmp_path, pending=_pending(), prompt_prefix="EXTRACT",
        run_claude=lambda prompt, **k: "not json at all",
        model="sonnet", timeout=600, max_retries=2, backoff_base=0.0)
    assert ok is False
    assert list((tmp_path / "enrich_inbox" / "bad").glob("*.txt"))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_parallel_backfill.py -k worker -v`
Expected: FAIL — `AttributeError: ... has no attribute '_process_batch_worker'`.

- [ ] **Step 3: Implement the worker + rate-limit detection**

Add to `mcpbrain/parallel_backfill.py`:

```python
import json
import subprocess

# stderr substrings the claude CLI surfaces for transient back-pressure.
_RATE_LIMIT_MARKERS = ("overloaded", "rate limit", "rate_limit", "429",
                       "usage limit", "too many requests", "529")
_MAX_RETRIES = 5
_BACKOFF_BASE = 5.0          # seconds; doubles each retry, capped
_BACKOFF_CAP = 40.0


def _is_rate_limited(exc: subprocess.CalledProcessError) -> bool:
    text = (exc.stderr or "").lower()
    return any(m in text for m in _RATE_LIMIT_MARKERS)


def _run_with_backoff(run_claude, prompt, *, model, timeout, max_retries,
                      backoff_base):
    """Call run_claude, retrying transient rate-limit/overload with exponential
    backoff + jitter. A timeout or a non-rate-limit error raises immediately."""
    attempt = 0
    while True:
        try:
            return run_claude(prompt, model=model, timeout=timeout)
        except subprocess.CalledProcessError as exc:
            if not _is_rate_limited(exc) or attempt >= max_retries:
                raise
            delay = min(backoff_base * (2 ** attempt), _BACKOFF_CAP)
            # Deterministic-ish jitter from attempt count (no Math.random need):
            delay += (attempt % 3) * 0.5
            log.warning("rate-limited (attempt %d/%d); backing off %.1fs",
                        attempt + 1, max_retries, delay)
            if delay:
                time.sleep(delay)
            attempt += 1


def _process_batch_worker(*, home, pending, prompt_prefix, run_claude, model,
                          timeout, max_retries=_MAX_RETRIES,
                          backoff_base=_BACKOFF_BASE):
    """Run one batch end-to-end on a worker thread. Returns (ok, reason).

    On success writes enrich_inbox/<batch_id>.json and returns (True, "").
    On terminal failure quarantines the raw output/cause and returns
    (False, reason). NEVER touches the store.
    """
    home = Path(home)
    batch_id = pending["batch_id"]
    full_prompt = (_PREAMBLE + prompt_prefix + _PENDING_DELIM +
                   json.dumps(pending, ensure_ascii=False))
    try:
        raw = _run_with_backoff(run_claude, full_prompt, model=model,
                                timeout=timeout, max_retries=max_retries,
                                backoff_base=backoff_base)
    except subprocess.TimeoutExpired:
        quarantine(home, batch_id, "", f"claude timed out after {timeout}s")
        return False, "timeout"
    except subprocess.CalledProcessError as exc:
        tail = (exc.stderr or "").strip().splitlines()[-3:]
        quarantine(home, batch_id, exc.stderr or "", f"claude exited {exc.returncode}")
        return False, f"claude exited {exc.returncode}: {' | '.join(tail)}"

    answer = extract_answer(raw)
    try:
        out = parse_extractor_json(answer)
    except json.JSONDecodeError as exc:
        quarantine(home, batch_id, raw, f"json decode: {exc}")
        return False, f"unparseable: {exc}"

    if out.get("batch_id") != batch_id:
        quarantine(home, batch_id, raw,
                   f"batch_id mismatch: {batch_id} vs {out.get('batch_id')!r}")
        return False, "batch_id mismatch"
    if not isinstance(out.get("extractions"), list):
        quarantine(home, batch_id, raw, "answer missing 'extractions' list")
        return False, "missing extractions"

    patch_extractions(pending, out)
    problems = validate_batch_file(out)
    if problems:
        quarantine(home, batch_id, raw,
                   f"contract errors after patch ({len(problems)}): {problems[0]}")
        return False, f"contract: {problems[0]}"

    atomic_write_inbox(home, batch_id, out)
    return True, ""
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_parallel_backfill.py -k worker -v`
Expected: PASS — all four worker tests.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/parallel_backfill.py tests/test_parallel_backfill.py
git commit -m "feat(parallel_backfill): batch worker with rate-limit backoff + quarantine

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: The wave loop with parallel fan-out and serial drain barrier

**Files:**
- Modify: `mcpbrain/parallel_backfill.py` (replace the `run_parallel_backfill` no-op body)
- Test: `tests/test_parallel_backfill.py`

The loop must: (1) pull a wave via `group_unenriched_threads`, (2) noise-filter on
the main thread, (3) partition + `build_pending` per sub-batch, (4) fan out workers,
(5) **after the barrier** call `drain.drain` once, (6) repeat until dry / max_waves /
cancel. The DB-touching calls (`_filter_noise`, `drain`, plus `group_unenriched_threads`'s
reads) all run on the main thread.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_parallel_backfill.py`. The fake store yields one wave then
goes dry; we monkeypatch the seams so no real claude/DB is needed:

```python
def _configure(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(json.dumps(
        {"owner_name": "Sam", "owner_email": "s@x.org", "orgs": [{"name": "Org"}]}))
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))


class _Batch:
    def __init__(self, tid):
        self.thread_id = tid; self.doc_ids = [f"d-{tid}"]; self.chunks = []


def test_wave_loop_runs_until_backlog_dry(tmp_path, monkeypatch):
    from mcpbrain import parallel_backfill as pb
    _configure(tmp_path, monkeypatch)
    # Wave 1: 3 threads; wave 2: empty (dry).
    waves = iter([[_Batch("t1"), _Batch("t2"), _Batch("t3")], []])
    monkeypatch.setattr(pb, "group_unenriched_threads", lambda store, **k: next(waves))
    monkeypatch.setattr(pb.prepare, "_filter_noise", lambda store, batches: batches)
    monkeypatch.setattr(pb.prepare, "build_pending",
                        lambda store, batches, **k: {"batch_id": k["batch_id"],
                            "threads": [{"thread_id": b.thread_id} for b in batches]})
    workered = []
    monkeypatch.setattr(pb, "_process_batch_worker",
                        lambda **kw: (workered.append(kw["pending"]["batch_id"]), (True, ""))[1])
    drained = {"n": 0}
    res = pb.run_parallel_backfill(
        store=object(), embedder=object(), home=str(tmp_path),
        workers=8, batch_size=2,
        run_claude=lambda *a, **k: "{}",
        apply=lambda *a, **k: {},
        drain_fn=lambda **k: drained.__setitem__("n", drained["n"] + 1) or {})
    assert res["status"] == "done"
    assert res["waves"] == 1                 # one productive wave, then dry
    assert res["threads"] == 3
    # 3 threads / batch_size 2 => 2 sub-batches => 2 worker calls
    assert len(workered) == 2
    assert drained["n"] == 1                 # drain barrier ran once for the wave


def test_wave_loop_honours_max_waves(tmp_path, monkeypatch):
    from mcpbrain import parallel_backfill as pb
    _configure(tmp_path, monkeypatch)
    monkeypatch.setattr(pb, "group_unenriched_threads",
                        lambda store, **k: [_Batch("t1")])   # never goes dry
    monkeypatch.setattr(pb.prepare, "_filter_noise", lambda store, batches: batches)
    monkeypatch.setattr(pb.prepare, "build_pending",
                        lambda store, batches, **k: {"batch_id": k["batch_id"], "threads": []})
    monkeypatch.setattr(pb, "_process_batch_worker", lambda **kw: (True, ""))
    res = pb.run_parallel_backfill(
        store=object(), embedder=object(), home=str(tmp_path),
        workers=1, batch_size=20, max_waves=3,
        run_claude=lambda *a, **k: "{}", apply=lambda *a, **k: {},
        drain_fn=lambda **k: {})
    assert res["status"] == "max_waves" and res["waves"] == 3


def test_wave_loop_cancels_after_current_wave(tmp_path, monkeypatch):
    from mcpbrain import parallel_backfill as pb
    import threading
    _configure(tmp_path, monkeypatch)
    monkeypatch.setattr(pb, "group_unenriched_threads",
                        lambda store, **k: [_Batch("t1")])
    monkeypatch.setattr(pb.prepare, "_filter_noise", lambda store, batches: batches)
    monkeypatch.setattr(pb.prepare, "build_pending",
                        lambda store, batches, **k: {"batch_id": k["batch_id"], "threads": []})
    monkeypatch.setattr(pb, "_process_batch_worker", lambda **kw: (True, ""))
    cancel = threading.Event()
    drained = {"n": 0}
    def drain_then_cancel(**k):
        drained["n"] += 1
        cancel.set()                          # cancel during the first wave's drain
        return {}
    res = pb.run_parallel_backfill(
        store=object(), embedder=object(), home=str(tmp_path),
        workers=1, batch_size=20, cancel_event=cancel,
        run_claude=lambda *a, **k: "{}", apply=lambda *a, **k: {},
        drain_fn=drain_then_cancel)
    assert res["status"] == "cancelled"
    assert drained["n"] == 1                  # the in-flight wave's drain completed
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_parallel_backfill.py -k wave_loop -v`
Expected: FAIL — `run_parallel_backfill` returns the skeleton `waves: 0` / has no `drain_fn` param.

- [ ] **Step 3: Implement the wave loop**

Replace the body of `run_parallel_backfill` (everything after the `not_configured`
gate) in `mcpbrain/parallel_backfill.py`. Also add `drain_fn` to the signature
(default `None`, resolved to `drain_mod.drain`) so the drain barrier is injectable
for tests:

```python
def run_parallel_backfill(*, store, embedder, home=None, model="sonnet",
                          workers=8, batch_size=20, char_budget=_CHAR_BUDGET,
                          timeout=600, max_waves=None, run_claude=None,
                          apply=None, cancel_event=None, drain_fn=None) -> dict:
    home = home or str(config.app_dir())
    if not config.is_configured(home):
        return {"status": "not_configured", "waves": 0, "threads": 0,
                "quarantined": 0}
    if run_claude is None:
        run_claude = local_claude_runner
    if apply is None:
        from mcpbrain.graph_write import apply as apply   # noqa: PLC0414 — mirror enrich_backfill
    if drain_fn is None:
        drain_fn = drain_mod.drain

    import datetime
    home_path = Path(home)
    prompt_prefix = (Path(__file__).resolve().parents[1] / "mcpbrain"
                     / "enrich_prompt.md").read_text()

    def _cancelled():
        return cancel_event is not None and cancel_event.is_set()

    waves = 0
    threads_done = 0
    quarantined = 0
    while True:
        if _cancelled():
            return {"status": "cancelled", "waves": waves,
                    "threads": threads_done, "quarantined": quarantined}
        if max_waves is not None and waves >= max_waves:
            return {"status": "max_waves", "waves": waves,
                    "threads": threads_done, "quarantined": quarantined}

        batches = group_unenriched_threads(store, thread_cap=workers * batch_size)
        if not batches:
            return {"status": "done", "waves": waves,
                    "threads": threads_done, "quarantined": quarantined}

        kept = prepare._filter_noise(store, batches)   # DB write, main thread
        if not kept:
            continue                                   # all noise; pull next wave

        now = datetime.datetime.now(datetime.timezone.utc)
        sub_batches = _partition(kept, batch_size=batch_size)
        pendings = []
        for i, chunk in enumerate(sub_batches):
            batch_id = f"fastbf-{waves}-{i}-{now:%H%M%S}"
            pendings.append(prepare.build_pending(
                store, chunk, char_budget=char_budget, now=now, batch_id=batch_id))

        log.info("wave %d: %d threads -> %d sub-batches x %d workers (%s)",
                 waves, len(kept), len(pendings), workers, model)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(
                lambda p: _process_batch_worker(
                    home=home_path, pending=p, prompt_prefix=prompt_prefix,
                    run_claude=run_claude, model=model, timeout=timeout),
                pendings))
        quarantined += sum(1 for ok, _ in results if not ok)

        # Serial drain barrier — the only place the wave's results hit the store.
        drain_fn(store=store, home=home, apply=apply, embedder=embedder)

        waves += 1
        threads_done += len(kept)
```

Add `local_claude_runner` near the top of the module (reused shape from
`enrich_backfill.py`, adapted to raise `CalledProcessError` so backoff can detect
rate limits via stderr):

```python
def local_claude_runner(prompt: str, *, model: str = "sonnet", timeout: int = 600) -> str:
    """Shell to the local claude CLI in headless print mode; prompt via stdin.
    Raises subprocess.CalledProcessError (carrying stderr) on non-zero exit so
    the backoff layer can classify rate-limit/overload responses."""
    claude = _find_claude()
    return subprocess.run(
        [claude, "--print", "--model", model, "--output-format", "json",
         "--settings", '{"disableAllHooks":true}'],
        input=prompt, capture_output=True, text=True, timeout=timeout,
        check=True,
    ).stdout
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_parallel_backfill.py -v`
Expected: PASS — all tests in the file (gate, helpers, partition, worker, wave loop).

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/parallel_backfill.py tests/test_parallel_backfill.py
git commit -m "feat(parallel_backfill): wave loop with parallel fan-out + serial drain barrier

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: Daemon guard

**Files:**
- Modify: `mcpbrain/parallel_backfill.py`
- Test: `tests/test_parallel_backfill.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_parallel_backfill.py`:

```python
def test_guard_refuses_when_daemon_running_and_not_paused():
    from mcpbrain import parallel_backfill as pb
    ok, msg = pb.check_daemon_guard(status={"paused": False}, force=False)
    assert ok is False and "pause" in msg.lower()


def test_guard_proceeds_when_daemon_unreachable():
    from mcpbrain import parallel_backfill as pb
    ok, msg = pb.check_daemon_guard(status=None, force=False)
    assert ok is True


def test_guard_proceeds_when_daemon_paused():
    from mcpbrain import parallel_backfill as pb
    ok, msg = pb.check_daemon_guard(status={"paused": True}, force=False)
    assert ok is True


def test_guard_force_overrides():
    from mcpbrain import parallel_backfill as pb
    ok, msg = pb.check_daemon_guard(status={"paused": False}, force=True)
    assert ok is True and "force" in msg.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_parallel_backfill.py -k guard -v`
Expected: FAIL — `AttributeError: ... has no attribute 'check_daemon_guard'`.

- [ ] **Step 3: Implement the guard**

Add to `mcpbrain/parallel_backfill.py`:

```python
def check_daemon_guard(*, status, force) -> tuple[bool, str]:
    """Decide whether it's safe to run as the sole writer.

    status is daemon_status(home)'s return (None when unreachable). Proceed when
    the daemon is unreachable (stopped) or reports paused. Refuse when it is
    reachable and actively enriching, unless force is set."""
    if force:
        return True, "force: bypassing daemon guard (advanced; ensure no other writer)"
    if status is None:
        return True, "daemon unreachable — proceeding as sole writer"
    if status.get("paused"):
        return True, "daemon paused — proceeding"
    return (False,
            "daemon is running and not paused — pause or stop it first "
            "(mcpbrain pause / launchctl unload the agent), then re-run. "
            "Use --force to override.")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_parallel_backfill.py -k guard -v`
Expected: PASS — all four guard tests.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/parallel_backfill.py tests/test_parallel_backfill.py
git commit -m "feat(parallel_backfill): daemon-paused guard with --force override

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: CLI `bin/fast_backfill.py`

**Files:**
- Create: `bin/fast_backfill.py`
- Test: `tests/test_fast_backfill_cli.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_fast_backfill_cli.py`:

```python
import importlib.util
from pathlib import Path


def _load_cli():
    script = Path(__file__).resolve().parents[1] / "bin" / "fast_backfill.py"
    spec = importlib.util.spec_from_file_location("_fast_backfill_cli", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cli_parses_workers_and_model():
    cli = _load_cli()
    args = cli.parse_args(["--workers", "12", "--model", "haiku",
                           "--batch-size", "10", "--max-waves", "2"])
    assert args.workers == 12
    assert args.model == "haiku"
    assert args.batch_size == 10
    assert args.max_waves == 2
    assert args.force is False


def test_cli_force_flag():
    cli = _load_cli()
    args = cli.parse_args(["--force"])
    assert args.force is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_fast_backfill_cli.py -v`
Expected: FAIL — `FileNotFoundError` / module load error (script absent).

- [ ] **Step 3: Implement the CLI**

Create `bin/fast_backfill.py`:

```python
#!/usr/bin/env python3
"""Parallel enrichment backfill CLI.

Tactical one-shot drainer that fans the slow `claude --print` extractor calls out
across N worker threads while keeping every SQLite write on the main thread. Run
with the daemon paused or stopped (the script guards this). Steady-state
enrichment stays on the daemon/cowork path.

Run with:
  python bin/fast_backfill.py                  # ~/.mcpbrain, sonnet, 8 workers
  python bin/fast_backfill.py --workers 12 --model haiku
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running in-place from a checkout without `uv tool install`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcpbrain import config, parallel_backfill   # noqa: E402

DEFAULT_HOME = Path.home() / ".mcpbrain"


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--home", type=Path, default=DEFAULT_HOME)
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--batch-size", type=int, default=20)
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--max-waves", type=int, default=None)
    ap.add_argument("--force", action="store_true",
                    help="bypass the daemon-paused guard (advanced)")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    import logging
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s",
                        datefmt="%H:%M:%S")
    args = parse_args(argv)
    home = str(args.home.expanduser().resolve())

    status = parallel_backfill.daemon_status(Path(home))
    ok, msg = parallel_backfill.check_daemon_guard(status=status, force=args.force)
    print(msg)
    if not ok:
        return 2
    if status:
        total = status.get("chunk_count", 0)
        enr = status.get("enriched_count", 0)
        print(f"backlog: {enr:,}/{total:,} enriched ({total - enr:,} to go)")

    from mcpbrain.store import Store
    from mcpbrain.embed import get_embedder
    emb = get_embedder(config.EMBEDDER)
    store = Store(config.store_path(), dim=emb.dim, read_only=False)

    res = parallel_backfill.run_parallel_backfill(
        store=store, embedder=emb, home=home, model=args.model,
        workers=args.workers, batch_size=args.batch_size, timeout=args.timeout,
        max_waves=args.max_waves)
    print(f"fast-backfill: {res['status']} after {res['waves']} waves "
          f"({res['threads']:,} threads, {res['quarantined']} quarantined)")
    return 0 if res["status"] in ("done", "cancelled", "max_waves") else 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/test_fast_backfill_cli.py -v`
Expected: PASS — both arg-parsing tests.

- [ ] **Step 5: Commit**

```bash
git add bin/fast_backfill.py tests/test_fast_backfill_cli.py
git commit -m "feat(cli): bin/fast_backfill.py — parallel enrichment backfill entry point

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 8: SIGINT/SIGTERM wiring + full-suite green

**Files:**
- Modify: `bin/fast_backfill.py` (install signal handlers, pass a `cancel_event`)
- Test: full suite

- [ ] **Step 1: Wire cancellation into the CLI**

In `bin/fast_backfill.py`, inside `main()` before the `run_parallel_backfill`
call, create a `threading.Event` and install handlers so Ctrl-C requests a clean
stop (the loop finishes the current wave's drain, then returns `cancelled`):

```python
    import signal
    import threading
    cancel = threading.Event()
    def _on_signal(_sig, _frame):
        print("\ncancellation requested — finishing current wave, then stopping")
        cancel.set()
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
```

Add `cancel_event=cancel` to the `run_parallel_backfill(...)` call.

- [ ] **Step 2: Manual smoke (no network) — confirm the guard + dry-run path**

Run (expects the daemon NOT paused → guard refuses, exit 2, no store writes):

```bash
python bin/fast_backfill.py --max-waves 0
```

Expected: prints either "daemon unreachable — proceeding…" or the refuse message;
exits without touching enrichment. (`--max-waves 0` returns `max_waves` immediately
after the gate, so this is safe even if it proceeds.)

- [ ] **Step 3: Run the full test suite**

Run: `pytest -q`
Expected: PASS — entire suite green (new tests + all existing). Ruff clean:
`ruff check mcpbrain bin tests`.

- [ ] **Step 4: Commit**

```bash
git add bin/fast_backfill.py
git commit -m "feat(cli): clean SIGINT/SIGTERM cancellation for fast_backfill

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review notes (addressed)

- **Spec coverage:** standalone/daemon-paused (Task 6 guard + CLI), sonnet default
  (Task 7), `--workers` default 8 (Task 7), `build_pending` refactor (Task 1),
  parallel fan-out + serial drain barrier (Task 5), single-writer (drain/filter on
  main thread only — Task 5), no double-processing (`_partition` disjoint — Task 3),
  backoff (Task 4), quarantine (Task 4), guard + `--force` (Task 6), cancellation
  (Task 5 loop + Task 8 wiring), testing seams (`run_claude`/`apply`/`drain_fn`
  injected). All present.
- **Type/name consistency:** `_process_batch_worker` signature, `_partition`,
  `check_daemon_guard`, `run_parallel_backfill` params, and `build_pending`
  signature match across tasks and tests.
- **Apply seam note:** Task 5 uses `from mcpbrain.graph_write import apply as apply`
  to mirror `enrich_backfill.run_backfill`; confirm the symbol is `graph_write.apply`
  during Task 5 (it is, per `drain.py`'s docstring) — if the import name differs,
  adjust the import line only.
```
