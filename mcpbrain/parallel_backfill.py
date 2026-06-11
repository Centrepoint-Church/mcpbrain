"""Parallel enrichment backfill: fan the slow claude extractor calls out across
a thread pool, drain the results serially on the main thread.

Tactical one-shot drainer for a large un-enriched backlog. Standalone — opens
the store read-write and runs prepare/drain itself, so the daemon must be paused
or stopped (the CLI guards this). The main thread is the SOLE SQLite writer;
worker threads only run `claude --print` subprocesses and write inbox files.
Ongoing steady-state enrichment stays on the daemon/cowork path.

Continuous-pool design: `workers` sub-batches are kept in flight at all times.
As soon as ONE future completes the main thread drains it and immediately submits
a replacement. There are no wave boundaries — the slowest sub-batch never stalls
the others.

Single-writer invariant: ONLY the main thread calls group_unenriched_threads,
prepare._filter_noise, prepare.build_pending, and drain_fn. Worker threads run
ONLY _process_batch_worker (claude subprocess + inbox/quarantine file writes).

In-flight exclusion: threads that have been dispatched but not yet drained are
tracked in an `in_flight` set.  group_unenriched_threads queries enriched=0 rows,
so a dispatched-but-undrained thread would be returned again. _pull_batch() skips
any batch whose thread_id is in `in_flight`, preventing double-processing.

Cancellation: setting cancel_event stops NEW submissions immediately. Futures
already in flight are allowed to finish and are drained before the function
returns — cancellation is cooperative, not forceful.
"""
from __future__ import annotations

import collections
import json
import logging
import random
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path

from mcpbrain import config, prepare, drain as drain_mod
from mcpbrain.contract import validate_batch_file
from mcpbrain.thread_enrich import group_unenriched_threads
from mcpbrain.extractor_io import (
    extract_answer,
    parse_extractor_json,
    patch_extractions,
    atomic_write_inbox,
    quarantine,
    daemon_status,  # noqa: F401 — re-exported; bin/fast_backfill uses parallel_backfill.daemon_status
    claude_runner,
    format_eta,
    _PREAMBLE,
    _PENDING_DELIM,
)

log = logging.getLogger("mcpbrain.parallel_backfill")

_CHAR_BUDGET = 200_000


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
            # decorrelated jitter to avoid a thundering herd
            delay += random.uniform(0, delay * 0.25)
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
    try:
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
    except Exception as exc:  # noqa: BLE001
        log.exception("unexpected error in worker for batch %s", batch_id)
        try:
            quarantine(home, batch_id, "", f"unexpected: {exc}")
        except Exception:  # noqa: BLE001
            pass
        return False, f"unexpected: {exc}"


def _partition(items, *, batch_size):
    """Split a list into consecutive disjoint chunks of at most batch_size.

    Consecutive slicing guarantees no item appears in two chunks, so two
    workers never extract the same thread."""
    return [items[i:i + batch_size] for i in range(0, len(items), batch_size)]


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


def _safe_backlog(store) -> int | None:
    """Return store.chunk_count() - store.enriched_count(), or None on any error.

    Defensive helper: many tests pass store=object() which has no such methods.
    Returns None when the store lacks the methods or raises, so the wave loop
    degrades gracefully (skips ETA) rather than crashing.
    """
    try:
        return store.chunk_count() - store.enriched_count()
    except Exception:  # noqa: BLE001
        return None


def run_parallel_backfill(*, store, embedder, home=None, model="sonnet",
                          workers=8, batch_size=20, char_budget=_CHAR_BUDGET,
                          timeout=600, max_batches=None, run_claude=None,
                          apply=None, cancel_event=None, drain_fn=None) -> dict:
    """Drain the backlog with a continuous worker pool of `workers` claude sessions.

    Keeps `workers` sub-batches in flight at all times. As soon as one future
    completes the main thread drains it (single-writer) and immediately submits
    a replacement, so the slowest sub-batch never stalls the others.

    Single-writer invariant: group_unenriched_threads, prepare._filter_noise,
    prepare.build_pending, and drain_fn are ONLY called from the main thread.
    Worker threads run only _process_batch_worker.

    In-flight exclusion: an `in_flight` set of thread_ids prevents re-dispatching
    threads that have been submitted but not yet drained (they still appear as
    enriched=0 in the DB until drain_fn runs).

    Gated on config.is_configured. Returns a summary dict with keys:
    status ("done"|"max_batches"|"cancelled"|"not_configured"), batches,
    threads_dispatched, quarantined.

    Cancellation: setting cancel_event stops NEW submissions immediately. Futures
    already in flight are allowed to finish and are drained before returning.
    """
    home = home or str(config.app_dir())
    if not config.is_configured(home):
        return {"status": "not_configured", "batches": 0, "threads_dispatched": 0,
                "quarantined": 0}
    if run_claude is None:
        run_claude = claude_runner
    if apply is None:
        from mcpbrain.graph_write import apply as _apply
        apply = _apply
    if drain_fn is None:
        drain_fn = drain_mod.drain

    import datetime
    home_path = Path(home)

    # enrich_prompt.md is the canonical HEADLESS extractor prompt (NOT the
    # cowork/enrichment.md skill body, which is written for the file-touching
    # Cowork flow). Enrichment-quality changes must be mirrored into BOTH files.
    prompt_prefix = Path(__file__).with_name("enrich_prompt.md").read_text()

    def _cancelled():
        return cancel_event is not None and cancel_event.is_set()

    batches_done = 0
    threads_dispatched = 0
    quarantined = 0
    batch_seq = 0

    # Main-thread state for the continuous pool.
    in_flight: set[str] = set()          # thread_ids dispatched but not yet drained
    fut_threads: dict = {}               # Future -> set[str] of thread_ids

    drain_durations: collections.deque = collections.deque(maxlen=5)
    start_t = time.monotonic()

    def _pull_batch():
        """Pull the next batch of fresh (not in-flight) threads from the store.

        Returns (pending_dict, thread_id_set) or (None, set()) when no work
        remains or cancellation is requested. Marks noise threads enriched (DB
        write) as a side-effect — safe because this runs on the main thread only.

        The noise loop is guaranteed to terminate: each pass marks all-noise
        chunks enriched, so `fresh` shrinks toward empty.
        """
        nonlocal batch_seq
        while True:
            if _cancelled():
                return None, set()
            # Pull with slack so that in-flight threads can be excluded and we
            # still fill a full batch_size chunk from the remaining fresh ones.
            cap = len(in_flight) + batch_size * 2
            all_batches = group_unenriched_threads(store, thread_cap=cap)
            fresh = [b for b in all_batches if b.thread_id not in in_flight]
            if not fresh:
                return None, set()          # truly drained (or all in-flight)
            chunk = fresh[:batch_size]
            kept = prepare._filter_noise(store, chunk)  # marks noise enriched (DB write)
            if _cancelled():
                return None, set()
            if kept:
                now = datetime.datetime.now(datetime.timezone.utc)
                batch_seq += 1
                batch_id = f"fastbf-{batch_seq}-{now:%H%M%S%f}"
                pending = prepare.build_pending(
                    store, kept, char_budget=char_budget, now=now, batch_id=batch_id)
                tids = {b.thread_id for b in kept}
                in_flight.update(tids)
                return pending, tids
            # chunk was entirely noise (now marked enriched) — loop to fetch next

    def _try_submit(pool, futures):
        """Submit one new batch to the pool. Returns True if submitted."""
        if _cancelled():
            return False
        if max_batches is not None and (batches_done + len(futures)) >= max_batches:
            return False
        pending, tids = _pull_batch()
        if pending is None:
            return False
        fut = pool.submit(
            _process_batch_worker,
            home=home_path, pending=pending, prompt_prefix=prompt_prefix,
            run_claude=run_claude, model=model, timeout=timeout)
        fut_threads[fut] = tids
        futures.add(fut)
        return True

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures: set = set()

        # Prime the pool: fill up to `workers` concurrent futures.
        for _ in range(workers):
            if not _try_submit(pool, futures):
                break

        while futures:
            done, futures = wait(futures, return_when=FIRST_COMPLETED)

            # Serial drain — ONLY the main thread touches the store.
            drain_t0 = time.monotonic()
            drain_fn(store=store, home=home, apply=apply, embedder=embedder)
            drain_durations.append(time.monotonic() - drain_t0)

            for fut in done:
                tids = fut_threads.pop(fut)
                ok, _reason = fut.result()   # worker never raises; returns (ok, reason)
                batches_done += 1
                threads_dispatched += len(tids)
                if not ok:
                    quarantined += 1
                in_flight -= tids            # drained (enriched) or failed (re-queues later)

            # Progress log: throughput-oriented, emitted after each drain cycle.
            backlog = _safe_backlog(store)
            elapsed = max(time.monotonic() - start_t, 0.001)
            tpm = threads_dispatched / elapsed * 60
            if backlog is not None:
                eta_s = (backlog / max(tpm / 60, 0.001)) if tpm > 0 else None
                log.info(
                    "drain cycle: batches=%d dispatched=%d backlog=%d "
                    "quarantined=%d rate=%.1f threads/min ETA~%s",
                    batches_done, threads_dispatched, backlog, quarantined,
                    tpm, format_eta(eta_s) if eta_s is not None else "?",
                )
            else:
                log.info(
                    "drain cycle: batches=%d dispatched=%d quarantined=%d "
                    "rate=%.1f threads/min",
                    batches_done, threads_dispatched, quarantined, tpm,
                )

            # Refill the pool to keep it saturated.
            while len(futures) < workers:
                if not _try_submit(pool, futures):
                    break

    if _cancelled():
        status = "cancelled"
    elif max_batches is not None and batches_done >= max_batches:
        status = "max_batches"
    else:
        status = "done"

    return {"status": status, "batches": batches_done,
            "threads_dispatched": threads_dispatched, "quarantined": quarantined}
