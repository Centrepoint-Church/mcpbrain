"""Shared pure extractor helpers for headless Claude invocation.

This module owns the constants and functions that are common to both the
serial drain_backlog script and the parallel backfill path.  Moving them
here means they are part of the installed package and are importable from
a wheel-only install (where bin/ is absent).

Moved verbatim from bin/drain_backlog.py; docstrings and comments preserved.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from mcpbrain.chunking import _VALID_CONTENT_TYPES

QUARANTINE_DIRNAME = "bad"

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

def claude_runner(prompt: str, *, model: str = "sonnet", timeout: int = 600,
                  claude_bin: str | None = None) -> str:
    """Shell to the local claude CLI headless (--print --output-format json);
    prompt via stdin. Returns raw stdout. Raises subprocess.CalledProcessError
    (carrying stderr) on non-zero exit so callers can classify rate-limit/overload
    responses, or TimeoutExpired on breach.

    Lean startup WITHOUT --bare: `--strict-mcp-config --mcp-config '{}'` loads no
    MCP servers and `--settings '{"disableAllHooks":true}'` disables hooks. We do
    NOT use --bare here: --bare also skips the settings/OAuth config that carries
    the Claude Code login, so a --bare headless call fails with "Not logged in".
    None of this changes extraction quality — the full prompt + bundled
    pending.json context still reach the model unchanged."""
    from mcpbrain import config as _config
    claude = claude_bin or _config.find_claude()
    return subprocess.run(
        [claude, "--print", "--model", model, "--output-format", "json",
         "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
         "--settings", '{"disableAllHooks":true}'],
        input=prompt, capture_output=True, text=True, timeout=timeout, check=True,
    ).stdout


# ---------------------------------------------------------------------------
# Answer extraction and parsing
# ---------------------------------------------------------------------------

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
# ETA formatting (shared between drain_backlog and other backfill drivers)
# ---------------------------------------------------------------------------

def format_eta(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds/60:.1f}m"
    return f"{seconds/3600:.1f}h"
