"""B6 — Procedural/voice memory analyser (analysis-only Phase A).

Weekly: reads voice.md + recent draft_records → calls the claude CLI →
writes structured voice suggestions to voice_suggestions table.
Never mutates voice.md (that's voice_apply.py's job, and only after user review).

Three-strike auto-disable: three consecutive Phase A failures set
voice_analyser_state.disabled=1 and log a warning. A successful run resets.

Public API:
  run_analysis(store, home) -> list[dict]   — Phase A: suggest, write to DB
  is_disabled(store) -> bool
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("mcpbrain.voice_analyser")

_VOICE_FILENAME = "voice.md"
_CONFIDENCE_THRESHOLD = 0.75
_MAX_SUGGESTIONS = 5
_LOOKBACK_DAYS = 7
_TIMEOUT_S = 90


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_disabled(store) -> bool:
    """True when the three-strike auto-disable has fired."""
    try:
        return store.get_voice_analyser_state("disabled") == "1"
    except Exception:
        return False


def _voice_md_path(home: str) -> Path:
    """Return path to context/voice.md inside the records repo."""
    from mcpbrain import config as _cfg
    return Path(_cfg.records_dir(home)) / "context" / _VOICE_FILENAME


def _read_voice_md(home: str) -> str:
    p = _voice_md_path(home)
    if not p.exists():
        return ""
    try:
        return p.read_text()[:8000]
    except OSError:
        return ""


def _collect_draft_samples(store, lookback_days: int) -> list[dict]:
    """Return recent draft_records as authored samples."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    try:
        with store._connect() as db:
            rows = db.execute(
                "SELECT id, email_id, thread_id, intent, draft_text, created_at "
                "FROM draft_records "
                "WHERE created_at >= ? AND draft_text != '' "
                "ORDER BY created_at DESC LIMIT 40",
                (cutoff,),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        log.debug("voice_analyser: failed to collect draft samples: %s", exc)
        return []


_ANALYSIS_PROMPT = """\
You are a voice coach reviewing someone's recent email drafts against their voice rules.

CURRENT VOICE.MD:
{voice_md}

---

RECENT AUTHORED DRAFTS ({n_samples} drafts, last {lookback_days} days):
{samples_block}

---

Your task: identify voice patterns in the drafts that suggest voice.md needs updating.
Look for words/phrases used often, structural patterns, opening/closing patterns.

Return a JSON array of suggestion objects. Each must have:
  "kind": one of ["ban_word","ban_pattern","add_example","opening_pattern","closing_pattern","tone_note"]
  "rule": specific wording to add/change in voice.md (1-2 sentences)
  "confidence": 0.0-1.0
  "evidence_sample_ids": list of sample id strings (use the [id=N] markers)
  "explanation": one sentence on the pattern found

Constraints:
- Only include suggestions where confidence >= {confidence_threshold}
- Do not suggest words/patterns already in voice.md
- Return at most {max_suggestions} suggestions
- Return ONLY valid JSON array, no surrounding text. Return [] if nothing found.
"""


def _build_samples_block(samples: list[dict]) -> str:
    lines = []
    for s in samples[:30]:
        sid = s.get("id", "?")
        intent = (s.get("intent") or "")[:40]
        date = (s.get("created_at") or "")[:10]
        body = (s.get("draft_text") or "")[:400]
        lines.append(f"[id={sid}] intent={intent} date={date}\n{body}")
    return "\n\n".join(lines)


def _call_claude(prompt: str, home: str) -> str:
    from mcpbrain import config
    try:
        claude = config.find_claude()
    except RuntimeError:
        log.warning("voice_analyser: claude CLI not found")
        return ""
    try:
        result = subprocess.run(
            [claude, "-p", prompt, "--output-format", "text"],
            capture_output=True, text=True, timeout=_TIMEOUT_S,
        )
        if result.returncode != 0:
            log.warning("voice_analyser: claude returned %d: %s",
                        result.returncode, result.stderr[:200])
            return ""
        return (result.stdout or "").strip()
    except subprocess.TimeoutExpired:
        log.warning("voice_analyser: claude timed out after %ds", _TIMEOUT_S)
        return ""
    except Exception as exc:  # noqa: BLE001
        log.warning("voice_analyser: claude call failed: %s", exc)
        return ""


def _parse_suggestions(raw: str) -> list[dict]:
    raw = raw.strip()
    start = raw.find("[")
    end = raw.rfind("]")
    if start == -1 or end == -1:
        return []
    try:
        items = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return []
    valid = []
    required = {"kind", "rule", "confidence", "evidence_sample_ids", "explanation"}
    valid_kinds = {"ban_word", "ban_pattern", "add_example",
                   "opening_pattern", "closing_pattern", "tone_note"}
    for item in items:
        if not isinstance(item, dict):
            continue
        if not required.issubset(item.keys()):
            continue
        if item.get("kind") not in valid_kinds:
            continue
        try:
            item["confidence"] = float(item["confidence"])
        except (TypeError, ValueError):
            continue
        if not isinstance(item.get("evidence_sample_ids"), list):
            item["evidence_sample_ids"] = []
        valid.append(item)
    return valid


def _record_run(store, success: bool) -> int:
    """Update state; return current consecutive_failures count."""
    ts = _now_iso()
    store.set_voice_analyser_state("last_run_at", ts)
    store.set_voice_analyser_state("last_run_status", "ok" if success else "failed")
    if success:
        store.set_voice_analyser_state("consecutive_failures", "0")
        return 0
    prev = int(store.get_voice_analyser_state("consecutive_failures", "0") or "0")
    new_count = prev + 1
    store.set_voice_analyser_state("consecutive_failures", str(new_count))
    return new_count


def run_analysis(store, home: str) -> list[dict]:
    """Phase A: analyse recent drafts vs voice.md, write suggestions to DB.

    Returns list of suggestion dicts written. Raises on unrecoverable error;
    caller should call _record_run(store, success=False) on exception.
    """
    from mcpbrain import config
    if not config.procedural_memory_enabled(home):
        return []
    if is_disabled(store):
        log.warning("voice_analyser: disabled (three-strike). Re-enable via config.")
        return []

    voice_md = _read_voice_md(home)
    samples = _collect_draft_samples(store, _LOOKBACK_DAYS)

    if not samples:
        log.info("voice_analyser: no draft samples in last %d days — skipping", _LOOKBACK_DAYS)
        _record_run(store, success=True)
        return []

    prompt = _ANALYSIS_PROMPT.format(
        voice_md=voice_md or "(voice.md not found — suggest general patterns)",
        n_samples=len(samples),
        lookback_days=_LOOKBACK_DAYS,
        samples_block=_build_samples_block(samples),
        confidence_threshold=_CONFIDENCE_THRESHOLD,
        max_suggestions=_MAX_SUGGESTIONS,
    )

    raw = _call_claude(prompt, home)
    suggestions = _parse_suggestions(raw)
    suggestions = [s for s in suggestions if s["confidence"] >= _CONFIDENCE_THRESHOLD]
    suggestions = suggestions[:_MAX_SUGGESTIONS]

    written: list[dict] = []
    for s in suggestions:
        try:
            sid = store.insert_voice_suggestion(
                kind=s["kind"],
                rule=s["rule"],
                confidence=s["confidence"],
                evidence_ids=s.get("evidence_sample_ids", []),
                explanation=s.get("explanation", ""),
            )
            written.append({**s, "id": sid})
            log.info("voice_analyser: queued suggestion id=%d kind=%s conf=%.2f",
                     sid, s["kind"], s["confidence"])
        except Exception as exc:  # noqa: BLE001
            log.warning("voice_analyser: failed to insert suggestion: %s", exc)

    _record_run(store, success=True)
    log.info("voice_analyser: Phase A done — %d suggestions", len(written))
    return written


def maybe_run_analysis(store, home: str) -> list[dict]:
    """Entry point for the daemon cadence.

    Wraps run_analysis with three-strike tracking.
    """
    try:
        result = run_analysis(store, home)
        return result
    except Exception as exc:
        failures = _record_run(store, success=False)
        log.error("voice_analyser: Phase A failed (failure %d/3): %s",
                  failures, exc, exc_info=True)
        if failures >= 3:
            store.set_voice_analyser_state("disabled", "1")
            log.warning("voice_analyser: auto-disabled after 3 consecutive failures")
        return []
