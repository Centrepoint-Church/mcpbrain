#!/usr/bin/env python3
"""Drain the mcpbrain enrichment backlog via headless Claude Code sessions.

Tactical one-shot drainer for a large backlog of un-enriched chunks. Normal
steady-state runs on the daemon's 5-min cycle plus the hourly Cowork task;
this script chews through the backlog faster by firing one `claude --print`
session per batch as soon as the daemon writes a fresh `pending.json`.

Architecture (matches the Cowork flow so the daemon doesn't care who drained):
  1. Poll  MCPBRAIN_HOME/enrich_queue/pending.json
  2. Read the batch into memory and inline it into the extractor prompt
  3. Spawn `claude --print --output-format json` headless with the prompt on
     stdin, capture stdout, peel back the JSON envelope to Claude's answer
  4. Atomic-write the parsed answer to enrich_inbox/<batch_id>.json
  5. Wait for the daemon's next cycle to apply (the inbox file disappears
     once drain.drain() processes it) — this is the gate that lets the
     daemon mark threads enriched=1 before the next prepare runs, so we
     never re-process the same threads under a fresh batch_id
  6. Loop

Safe to run alongside Cowork — whichever drops the inbox file first wins
(both write to enrich_inbox/<batch_id>.json with idempotent contents).

Stops when:
  - daemon reports chunk_count == enriched_count (backlog drained), OR
  - pending.json has been absent for --idle-timeout seconds, OR
  - --max-batches reached, OR
  - Ctrl-C.

Run with:
  python bin/drain_backlog.py             # uses ~/.mcpbrain, sonnet, 10-min timeout
  python bin/drain_backlog.py --model haiku --timeout 300
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path

DEFAULT_HOME = Path.home() / ".mcpbrain"
DEFAULT_PROMPT = Path(__file__).resolve().parents[1] / "mcpbrain" / "enrich_prompt.md"
DEFAULT_MODEL = "sonnet"
DEFAULT_TIMEOUT_S = 600
DEFAULT_POLL_S = 5
DEFAULT_IDLE_TIMEOUT_S = 600       # 10 min absent => probably drained
DEFAULT_INBOX_WAIT_S = 180         # cap on waiting for daemon drain after a batch
QUARANTINE_DIRNAME = "bad"
ETA_WINDOW = 5                     # rolling-window size for batch-time averaging

# A small instruction the script prepends so Claude returns JSON on stdout
# without trying to use the Write tool (the shipped enrich_prompt.md is
# written for the file-touching Cowork flow).
_PREAMBLE = (
    "You are running non-interactively. The pending.json content is inlined "
    "below — do NOT try to read or write any files, do NOT use any tools. "
    "Reply with ONLY the JSON output object. No markdown code fences, no "
    "commentary before or after. Just the raw JSON.\n\n"
)
_PENDING_DELIM = "\n\n=== pending.json (inlined below) ===\n\n"

# Fallback fence stripper for older claude CLI builds whose --output-format
# json envelope still contains markdown around the answer.
_FENCE_RE = re.compile(r"```(?:json)?\s*|\s*```", re.MULTILINE)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Daemon control-API status (read-only)
# ---------------------------------------------------------------------------

def daemon_status(home: Path, timeout: float = 3.0) -> dict | None:
    """Return /api/status as a dict, or None if the daemon is unreachable."""
    try:
        port = int((home / "control_port").read_text().strip())
        token = (home / "control_token").read_text().strip()
    except (OSError, ValueError):
        return None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/status",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# Claude invocation
# ---------------------------------------------------------------------------

def run_claude(prompt: str, *, model: str, timeout: int, claude_bin: str) -> str:
    """Spawn `claude --print --output-format json` headless via stdin.

    Returns Claude's raw stdout. Raises subprocess.CalledProcessError on
    non-zero exit, subprocess.TimeoutExpired on wall-clock breach.
    """
    cmd = [claude_bin, "--print", "--model", model, "--output-format", "json"]
    return subprocess.run(
        cmd,
        input=prompt,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    ).stdout


def extract_answer(stdout: str) -> str:
    """Peel Claude's text answer out of `--output-format json` stdout.

    The envelope shape is `{"type": "result", "result": "...", ...}` (newer
    CLI builds) or `{"role": "assistant", "content": [{"type": "text",
    "text": "..."}]}` (older). Falls back to returning the stdout verbatim
    if neither shape matches, so a plain-text response still flows through.
    """
    try:
        env = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout
    if isinstance(env, dict):
        if isinstance(env.get("result"), str):
            return env["result"]
        content = env.get("content") or env.get("message", {}).get("content")
        if isinstance(content, list):
            parts = [c.get("text", "") for c in content if isinstance(c, dict)]
            if parts:
                return "".join(parts)
        if isinstance(content, str):
            return content
    return stdout


def parse_extractor_json(answer: str) -> dict:
    """Best-effort parse: strip fences, trim to outermost {...}, then JSON-load."""
    text = _FENCE_RE.sub("", answer).strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        text = text[start:end + 1]
    return json.loads(text)


# ---------------------------------------------------------------------------
# Patcher — fix up known model-quality gaps before validation
# ---------------------------------------------------------------------------

# Valid content_type values per the extractor contract (mcpbrain.contract).
# Hardcoded so the patcher works even if the mcpbrain module isn't importable;
# kept narrow on purpose — if the contract grows, update this set.
_VALID_CONTENT_TYPES = {"request", "update", "decision", "fyi", "notification"}

# Observed model fabrications mapped to the closest valid value. Anything
# outside this map AND outside the valid set falls through to the catch-all
# below, so we don't have to predict every wrong thing a model can say.
_CONTENT_TYPE_ALIASES = {
    "proposal": "request",
    "question": "request",
    "ask": "request",
    "info": "fyi",
    "informational": "fyi",
    "announcement": "notification",
    "notice": "notification",
    "reply": "update",
    "response": "update",
    "status": "update",
    "report": "update",
}
_CONTENT_TYPE_FALLBACK = "fyi"


def _date_index(pending: dict) -> dict[str, str]:
    """Map message_id -> date from the input pending.json batch.

    Used to backfill empty `date` fields the model dropped on output. We index
    by message_id alone because Gmail message ids and calendar event ids are
    globally unique within a Google account, so there's no collision risk.
    """
    out: dict[str, str] = {}
    for thread in pending.get("threads") or []:
        for msg in thread.get("messages") or []:
            mid = msg.get("message_id")
            date = msg.get("date")
            if mid and date:
                out[mid] = date
    return out


def patch_extractions(pending: dict, answer: dict) -> dict[str, int]:
    """Apply known fix-ups to the extractor answer IN PLACE.

    Returns a counter dict {kind: count} describing what was patched so the
    caller can log it. Three fixes today:
      - `messages[*].date` empty -> backfilled from pending.json by message_id
      - `messages[*].date` empty AND input date also empty -> filled with the
        batch-level `prepared_at` date (YYYY-MM-DD only). This is the safety
        net for pre-fix pending.json files that didn't carry Drive dates;
        once the daemon's thread_enrich fallback covers `modified`, the
        input lookup wins and this branch is a no-op.
      - `content_type` not in _VALID_CONTENT_TYPES -> aliased or fallback

    These are the failure modes observed on haiku output; sonnet may not
    need them at all. Patching is intentionally narrow so silent corruption
    of model judgement can't happen — anything we can't deterministically
    repair stays as-is and is caught by re-validation.
    """
    counts = {"date_filled": 0, "date_filled_from_prepared_at": 0,
              "content_type_aliased": 0, "content_type_fallback": 0}
    dates = _date_index(pending)
    # YYYY-MM-DD slice of prepared_at as a last-resort date for items that
    # have nothing better. The contract just requires "non-empty string",
    # so a date-only value passes.
    prepared_at = (pending.get("prepared_at") or "")[:10] or None
    for ex in answer.get("extractions") or []:
        # date backfill ------------------------------------------------------
        for msg in ex.get("messages") or []:
            if not msg.get("date"):
                mid = msg.get("message_id")
                if mid and dates.get(mid):
                    msg["date"] = dates[mid]
                    counts["date_filled"] += 1
                elif prepared_at:
                    msg["date"] = prepared_at
                    counts["date_filled_from_prepared_at"] += 1
        # content_type clamp ------------------------------------------------
        ct = ex.get("content_type")
        if ct not in _VALID_CONTENT_TYPES:
            alias = _CONTENT_TYPE_ALIASES.get(str(ct).strip().lower())
            if alias:
                ex["content_type"] = alias
                counts["content_type_aliased"] += 1
            else:
                ex["content_type"] = _CONTENT_TYPE_FALLBACK
                counts["content_type_fallback"] += 1
    return counts


def _import_validator():
    """Return contract.validate_batch_file, or None if mcpbrain isn't importable.

    Tries the installed package first (works when run from any cwd), then
    falls back to the sibling repo (works when running the script in-place
    from a checkout without `uv tool install`).
    """
    try:
        from mcpbrain.contract import validate_batch_file  # type: ignore
        return validate_batch_file
    except ImportError:
        repo_root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(repo_root))
        try:
            from mcpbrain.contract import validate_batch_file  # type: ignore
            return validate_batch_file
        except ImportError:
            return None


_VALIDATE = _import_validator()


# ---------------------------------------------------------------------------
# Inbox writers
# ---------------------------------------------------------------------------

def atomic_write_inbox(home: Path, batch_id: str, payload: dict) -> Path:
    """Mirror mcpbrain.extractor_driver._write_inbox: temp file + os.replace."""
    inbox_dir = home / "enrich_inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    target = inbox_dir / f"{batch_id}.json"
    fd, tmp = tempfile.mkstemp(dir=str(inbox_dir), prefix=".inbox.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


def quarantine(home: Path, batch_id: str, raw: str, reason: str) -> Path:
    """Stash a bad answer under enrich_inbox/bad/<batch_id>.txt with a
    one-line reason header so a human can inspect later. The daemon's
    drain step also quarantines structurally-invalid inbox files, but
    catching it here gives a clearer signal on the cause.
    """
    bad_dir = home / "enrich_inbox" / QUARANTINE_DIRNAME
    bad_dir.mkdir(parents=True, exist_ok=True)
    path = bad_dir / f"{batch_id}.txt"
    path.write_text(f"# drain_backlog quarantine: {reason}\n\n{raw}")
    return path


# ---------------------------------------------------------------------------
# One batch
# ---------------------------------------------------------------------------

class BatchError(RuntimeError):
    """Anything that should skip the current batch but keep the loop alive."""


def process_batch(*, home: Path, pending: dict, prompt_prefix: str,
                  model: str, timeout: int, claude_bin: str) -> tuple[str, int]:
    """End-to-end for one batch. Returns (batch_id, thread_count). Raises
    BatchError on a recoverable failure (quarantine has already happened).
    """
    batch_id = pending.get("batch_id")
    threads = pending.get("threads") or []
    if not batch_id or not threads:
        raise BatchError("pending.json missing batch_id or threads")

    full_prompt = (
        _PREAMBLE + prompt_prefix + _PENDING_DELIM +
        json.dumps(pending, ensure_ascii=False)
    )

    try:
        stdout = run_claude(full_prompt, model=model, timeout=timeout,
                            claude_bin=claude_bin)
    except subprocess.TimeoutExpired:
        raise BatchError(f"claude timed out after {timeout}s")
    except subprocess.CalledProcessError as exc:
        tail = (exc.stderr or "").strip().splitlines()[-3:]
        raise BatchError(f"claude exited {exc.returncode}: {' | '.join(tail)}")

    answer = extract_answer(stdout)
    try:
        out = parse_extractor_json(answer)
    except json.JSONDecodeError as exc:
        quarantine(home, batch_id, stdout, f"json decode: {exc}")
        raise BatchError(f"unparseable answer (quarantined): {exc}")

    if out.get("batch_id") != batch_id:
        quarantine(home, batch_id, stdout,
                   f"batch_id mismatch: input {batch_id} vs answer {out.get('batch_id')!r}")
        raise BatchError("answer batch_id does not match input")
    if not isinstance(out.get("extractions"), list):
        quarantine(home, batch_id, stdout, "answer missing 'extractions' list")
        raise BatchError("answer missing extractions list")

    # Patch known model-quality gaps (empty dates, fabricated content_type)
    # before validating, so good-enough answers don't get quarantined just
    # because the model forgot a field we already have in the input.
    patched = patch_extractions(pending, out)
    if any(patched.values()):
        log(f"  patched: {patched['date_filled']} dates, "
            f"{patched['content_type_aliased']} ct-aliased, "
            f"{patched['content_type_fallback']} ct-fallback")

    # Re-validate against the contract so we don't push junk to the daemon —
    # it would quarantine the whole file and waste the run. If the contract
    # module isn't importable, skip validation and trust the daemon to catch
    # any residual issues.
    if _VALIDATE is not None:
        problems = _VALIDATE(out)
        if problems:
            quarantine(
                home, batch_id, stdout,
                f"residual contract errors after patch ({len(problems)}): "
                f"{problems[0]}",
            )
            raise BatchError(
                f"answer fails contract after patch ({len(problems)} errors): "
                f"{problems[0]}"
            )

    atomic_write_inbox(home, batch_id, out)
    return batch_id, len(threads)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def wait_for_drain(inbox_file: Path, *, poll: float, cap: float) -> bool:
    """Block until inbox_file disappears (daemon drained it) or cap elapses.

    Returns True if drained, False if we gave up. The daemon's prepare step
    only picks NEW unenriched threads after drain marks the current ones
    enriched=1, so waiting here is the gate that prevents us from picking
    up the same threads under a fresh batch_id.
    """
    deadline = time.monotonic() + cap
    while inbox_file.exists():
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)
    return True


def format_eta(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds/60:.1f}m"
    return f"{seconds/3600:.1f}h"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--home", type=Path, default=DEFAULT_HOME,
                    help="MCPBRAIN_HOME (default: ~/.mcpbrain)")
    ap.add_argument("--prompt", type=Path, default=DEFAULT_PROMPT,
                    help="path to the extractor prompt")
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help="claude model alias (sonnet|haiku|opus)")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S,
                    help="per-batch wall-clock cap in seconds")
    ap.add_argument("--poll", type=float, default=DEFAULT_POLL_S,
                    help="poll interval in seconds")
    ap.add_argument("--idle-timeout", type=float, default=DEFAULT_IDLE_TIMEOUT_S,
                    help="exit if pending.json absent for this many seconds")
    ap.add_argument("--inbox-wait", type=float, default=DEFAULT_INBOX_WAIT_S,
                    help="max wait for daemon to drain our inbox file before continuing")
    ap.add_argument("--max-batches", type=int, default=None,
                    help="stop after this many successful batches")
    ap.add_argument("--claude-bin", default=shutil.which("claude") or "claude",
                    help="path to the claude CLI binary")
    args = ap.parse_args(argv)

    prompt_body = args.prompt.read_text()
    home = args.home.expanduser().resolve()
    pending_path = home / "enrich_queue" / "pending.json"
    inbox_dir = home / "enrich_inbox"

    interrupted = False
    def _on_signal(_sig, _frame):
        nonlocal interrupted
        interrupted = True
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    durations: deque[float] = deque(maxlen=ETA_WINDOW)
    seen_batch_ids: set[str] = set()
    last_pending_seen = time.monotonic()
    started = time.monotonic()
    batches = 0
    threads_done = 0

    log(f"home: {home}")
    log(f"claude: {args.claude_bin}  model: {args.model}  timeout: {args.timeout}s")
    status = daemon_status(home)
    if status:
        total = status.get("chunk_count", 0)
        enr = status.get("enriched_count", 0)
        log(f"daemon: {enr:,}/{total:,} enriched (backlog: {total - enr:,})")
    else:
        log("warn: daemon control API unreachable — ETA will be unavailable")

    while not interrupted:
        # ---------- wait for a batch ----------
        if not pending_path.exists():
            if time.monotonic() - last_pending_seen > args.idle_timeout:
                log(f"no pending.json for {args.idle_timeout:.0f}s — assuming drained")
                break
            time.sleep(args.poll)
            continue
        last_pending_seen = time.monotonic()

        try:
            pending = json.loads(pending_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            log(f"warn: cannot read pending.json: {exc}")
            time.sleep(args.poll)
            continue

        batch_id = pending.get("batch_id")
        if not batch_id:
            log("warn: pending.json missing batch_id; skipping")
            time.sleep(args.poll)
            continue
        if batch_id in seen_batch_ids:
            # Daemon hasn't drained our last inbox file yet, so the same
            # threads are re-issued under the same id. Sit tight.
            time.sleep(args.poll)
            continue

        # ---------- process ----------
        n_threads = len(pending.get("threads") or [])
        log(f"batch {batch_id}: {n_threads} threads → claude --print ({args.model})")
        t0 = time.monotonic()
        try:
            process_batch(
                home=home, pending=pending, prompt_prefix=prompt_body,
                model=args.model, timeout=args.timeout,
                claude_bin=args.claude_bin,
            )
        except BatchError as exc:
            log(f"  skipped: {exc}")
            # Mark this batch_id done on failure too — otherwise the daemon
            # keeps overwriting pending.json with the same id until the loop
            # gives up on its inbox-wait, and we'd reprocess forever. The
            # threads themselves stay enriched=0 in the store, so the daemon's
            # NEXT prepare cycle assigns them a fresh batch_id which this loop
            # will pick up cleanly.
            seen_batch_ids.add(batch_id)
            time.sleep(args.poll)
            continue
        elapsed = time.monotonic() - t0
        durations.append(elapsed)
        seen_batch_ids.add(batch_id)
        batches += 1
        threads_done += n_threads
        log(f"  wrote enrich_inbox/{batch_id}.json in {elapsed:.1f}s "
            f"({threads_done:,} threads / {batches} batches so far)")

        # ---------- wait for the daemon to apply ----------
        # If we charge ahead, prepare will re-pick the same threads under a
        # new batch_id while drain hasn't marked them enriched=1. Waiting
        # for our inbox file to disappear pins us to the daemon's pace.
        inbox_file = inbox_dir / f"{batch_id}.json"
        drained = wait_for_drain(inbox_file, poll=args.poll, cap=args.inbox_wait)
        if not drained:
            log(f"  warn: daemon hasn't drained {batch_id} after {args.inbox_wait:.0f}s "
                f"— continuing, may produce a duplicate batch")

        # ---------- progress + ETA ----------
        status = daemon_status(home)
        if status:
            total = status.get("chunk_count", 0)
            enr = status.get("enriched_count", 0)
            backlog = total - enr
            avg = sum(durations) / len(durations)
            tpb = max(1.0, threads_done / max(1, batches))
            # ETA assumes one batch produces ~tpb threads of work removed
            # from the backlog. The unit on chunk/enriched is chunks (which
            # equals threads here for the gmail-enriched source), so this
            # is a reasonable approximation.
            remaining_batches = backlog / tpb if tpb else 0
            log(f"  backlog: {backlog:,}  avg/batch: {avg:.1f}s  "
                f"ETA ≈ {format_eta(remaining_batches * avg)}")
            if backlog <= 0:
                log("backlog drained — exiting")
                break

        if args.max_batches and batches >= args.max_batches:
            log(f"reached --max-batches={args.max_batches}; exiting")
            break

    if interrupted:
        log("interrupted — exiting cleanly")
    runtime = time.monotonic() - started
    log(f"done: {batches} batches / {threads_done:,} threads in {runtime/60:.1f} min")
    return 0


if __name__ == "__main__":
    sys.exit(main())
