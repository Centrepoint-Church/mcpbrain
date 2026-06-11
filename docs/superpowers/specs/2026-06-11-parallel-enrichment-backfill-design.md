# Parallel enrichment backfill — design

**Date:** 2026-06-11
**Status:** Approved (brainstorming), pending spec review

## Problem

The corpus has **76,752 chunks indexed but only 14,013 enriched** — a backlog of
~62,700 un-enriched threads. Both existing drainers process **one Claude session
at a time, sequentially**:

- `mcpbrain/enrich_backfill.py` (`run_backfill`) — clean in-process loop
  (`prepare` → `extractor_driver.run_extractor` → `drain`), owns the store, ~20
  threads per batch, one `claude` call per batch, fully serial.
- `bin/drain_backlog.py` — defers all DB writes to the daemon (writes inbox
  files only) and **waits for the daemon to drain each batch before starting the
  next**, so it is serial *and* throttled to the daemon's cycle.

At ~20 threads/batch that is **3,100+ sequential Claude calls**. At ~45s each
that is well over a day of wall-clock. The bottleneck is the `claude --print`
subprocess; the DB write (`drain`/`apply`) is fast.

## Goal

Drain the enrichment backlog substantially faster by **running N Claude sessions
in parallel** while keeping all SQLite writes on a single thread.

Non-goals: changing the extractor contract, the enrichment prompt, the daemon's
steady-state cadence, or the serial drainers (kept as-is).

## Decisions (locked during brainstorming)

| Decision | Choice | Rationale |
|---|---|---|
| DB ownership | **Standalone, daemon paused** | Script opens the store read-write and runs the whole loop itself. No second writer, no SQLite contention, full control over parallelism. |
| Model | **sonnet** | Extraction quality of the enriched graph matters more than raw throughput; matches the current default. |
| Concurrency | **Configurable, default 8** (`--workers`) | Aggressive default; tune down if rate-limited. |

## Architecture

Two new units plus one small refactor:

### 1. `prepare.build_pending()` (refactor of `prepare.py`)

Extract the pending-dict assembly out of `prepare.prepare()` into a new public
function that **returns the dict without writing the file**:

```python
def build_pending(store, batches, *, char_budget, now, resolution_due=False) -> dict:
    """Assemble the pending.json dict for a list of ThreadBatch (no file write).
    Returns {batch_id, prepared_at, context, threads, merge_review}."""
```

`prepare.prepare()` keeps its current behaviour by calling `build_pending()` then
`_write_pending()`. This gives the parallel path a way to build many in-memory
batches with distinct `batch_id`s. The `batch_id` is parameterised (caller passes
a unique suffix) so concurrent batches never collide.

Noise filtering (`_filter_noise`, a DB write) stays the caller's responsibility —
it is invoked once per wave on the main thread, before `build_pending`.

Covered by existing `tests/test_prepare.py` (behaviour of `prepare()` unchanged)
plus new tests for `build_pending()` directly.

### 2. `mcpbrain/parallel_backfill.py` (new — core logic, testable)

`run_parallel_backfill(store, embedder, *, home, model="sonnet", workers=8,`
`batch_size=20, char_budget=200_000, max_waves=None, run_claude=..., apply=...)`.

`run_claude` and `apply` are **injectable seams** (default to the real
`local_claude_runner` / `graph_write.apply`) so the wave loop, partitioning,
drain barrier, and backoff are unit-testable with a fake runner — no real Claude
calls in tests. Mirrors the `run_claude` seam already in `enrich_backfill.py`.

**Gate:** returns `{"status": "not_configured"}` if `not config.is_configured`
(enrichment writes identity/org into the graph) — matching `run_backfill`.

**Per-wave loop (main thread is the sole writer):**

1. `batches = group_unenriched_threads(store, thread_cap=workers * batch_size)`
   — pull one wave's worth (default 8 × 20 = 160 threads), rowid order.
2. If empty → `status: "done"`.
3. `kept = prepare._filter_noise(store, batches)` — marks noise enriched (DB
   write, main thread). If everything was noise, loop again (more backlog may
   remain).
4. Partition `kept` into chunks of `batch_size` → up to `workers` disjoint
   sub-batches. Each gets a unique `batch_id` (`f"fastbf-{wave}-{i}"`).
5. For each chunk, `prepare.build_pending(store, chunk, ...)` → in-memory pending
   dict.
6. `ThreadPoolExecutor(max_workers=workers)`: each pending dict →
   `_process_batch_worker(...)`:
   - build prompt = `_PREAMBLE + enrich_prompt.md + delimiter + json(pending)`
     (reused verbatim from `drain_backlog.py`),
   - `run_claude(prompt, model=model, timeout=timeout)` **with retry +
     exponential backoff on rate-limit / overload** (see below),
   - `extract_answer` → `parse_extractor_json` → `patch_extractions` →
     contract `validate_batch_file`,
   - `atomic_write_inbox(home, batch_id, out)` on success; `quarantine(...)` on
     failure. Workers do **no** store access — pure subprocess + file write.
7. **Barrier:** after all workers in the wave return, call
   `drain.drain(store, home=home, apply=apply, embedder=embedder)` **once,
   serially** — applies every inbox file written this wave, marks chunks
   enriched, quarantines residual-bad files.
8. Repeat until `group_unenriched_threads` returns empty, `max_waves` reached,
   or cancellation.

Returns `{"status": "done"|"max_waves"|"cancelled"|"not_configured", "waves": n,`
`"threads": total, "quarantined": q}`.

### 3. `bin/fast_backfill.py` (new — thin CLI)

Mirrors `bin/drain_backlog.py`'s arg-parsing / logging / ETA style. Wires up the
real `Store`, embedder, `local_claude_runner`, and `graph_write.apply`, then calls
`run_parallel_backfill`.

```
bin/fast_backfill.py [--workers 8] [--model sonnet] [--batch-size 20]
                     [--timeout 600] [--max-waves N] [--home ~/.mcpbrain]
```

Per-wave progress line: threads done this wave, cumulative, backlog (from
`store.chunk_count() - store.enriched_count()`), rolling avg wave time, ETA.

## Rate-limit handling (essential, not optional)

Eight parallel sonnet sessions on a subscription **will** hit 429 / overload /
"usage limit" responses. The worker wraps `run_claude` in a retry loop:

- Detect rate-limit/overload from the `CalledProcessError` exit code + stderr
  text (the `claude` CLI surfaces these on stderr).
- Exponential backoff with jitter: ~5s, 10s, 20s, 40s (cap), max ~5 retries.
- A `timeout` (per-call wall-clock) is a hard failure → quarantine the batch and
  move on (its threads stay `enriched=0` and re-queue next wave).
- Distinguish transient (retry) from terminal (quarantine) so a genuinely bad
  batch doesn't burn all retries.

## Safety properties

- **Single writer:** only the main thread calls `mark_enriched`, `_filter_noise`,
  and `drain`/`apply`. Workers only run subprocesses and write inbox files
  (`atomic_write_inbox` = temp + `os.replace`, already concurrency-safe).
- **No double-processing:** one `group_unenriched_threads` result is partitioned
  into **disjoint** sub-batches, so no two workers see the same thread. The next
  wave only sees threads the drain barrier hasn't yet marked `enriched=1`.
- **Daemon paused:** no second writer. The CLI refuses to run if the daemon's
  control API is reachable **and** not paused (see guard).
- **Crash-safe:** `drain` applies before `mark_enriched` (existing guarantee), so
  a crash mid-wave re-queues rather than silently skipping. Bad answers
  quarantine to `enrich_inbox/bad/`.
- **Idempotent:** `apply()`'s upsert/dedup means re-applying a batch (e.g. after a
  crash between write-inbox and drain) is safe.

## Daemon guard

Before the first wave, `daemon_status(home)` (reused from `drain_backlog.py`):

- unreachable → daemon stopped → **proceed** (the intended mode).
- reachable and `paused == True` → **proceed** (enrichment quiesced).
- reachable and `paused == False` → **refuse**, print:
  `daemon is running and not paused — pause or stop it first (mcpbrain pause / launchctl unload …), then re-run`. Exit non-zero.

A `--force` flag overrides the guard for advanced use, with a logged warning.

## Cancellation

SIGINT/SIGTERM sets a flag (mirrors `drain_backlog.py`). The loop finishes the
**current wave's drain** (so no half-applied inbox files linger), then exits with
`status: "cancelled"`. In-flight Claude subprocesses are allowed to finish or
time out; their inbox files are drained on the way out.

## Testing (TDD)

Unit tests with a **fake `run_claude`** (returns canned valid/invalid JSON) and a
fake/real in-memory `Store`:

- `build_pending` returns the correct dict shape and unique `batch_id`; `prepare`
  behaviour unchanged.
- Partitioning splits a wave into ≤ `workers` disjoint sub-batches of ≤
  `batch_size`; no thread appears twice.
- Wave loop terminates when `group_unenriched_threads` returns empty.
- Drain barrier runs once per wave, after all workers.
- `max_waves` honoured.
- Backoff: a runner that raises rate-limit twice then succeeds → retried, batch
  applied; a runner that always times out → batch quarantined, loop continues.
- Bad JSON → quarantined, other batches in the wave still applied.
- Guard: refuses when daemon reachable + not paused; proceeds when unreachable or
  paused; `--force` overrides.
- Cancellation flag → finishes current drain, returns `cancelled`.

## Files

- `mcpbrain/prepare.py` — extract `build_pending()` (refactor).
- `mcpbrain/parallel_backfill.py` — new core module.
- `bin/fast_backfill.py` — new CLI.
- `tests/test_parallel_backfill.py` — new.
- `tests/test_prepare.py` — add `build_pending` cases.

## Operational note

This is a **tactical one-shot drainer**, like `drain_backlog.py` — run it to chew
through the 62k backlog, then return to the daemon's steady-state cadence. Run on
the Mac (deployment host) with the daemon paused.
