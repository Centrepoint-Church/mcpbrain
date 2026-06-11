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
import json
import logging
import subprocess
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


def run_parallel_backfill(*, store, embedder, home=None, model="sonnet",
                          workers=8, batch_size=20, char_budget=_CHAR_BUDGET,
                          timeout=600, max_waves=None, run_claude=None,
                          apply=None, cancel_event=None, drain_fn=None) -> dict:
    """Drain the backlog wave-by-wave with `workers` parallel claude sessions.

    Gated on config.is_configured. Returns a summary dict with keys:
    status ("done"|"max_waves"|"cancelled"|"not_configured"), waves, threads,
    quarantined.
    """
    home = home or str(config.app_dir())
    if not config.is_configured(home):
        return {"status": "not_configured", "waves": 0, "threads": 0,
                "quarantined": 0}
    if run_claude is None:
        run_claude = local_claude_runner
    if apply is None:
        from mcpbrain.graph_write import apply as _apply
        apply = _apply
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
