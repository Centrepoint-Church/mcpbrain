"""Outcome-grounded lessons-learned writer — S5.

Governing rule: lessons are written ONLY when the correction signal comes from
OUTSIDE the loop.  The signal here is observed recall_feedback rows with
event_type IN ('used', 'edited').  Today 'used' is the quote-back behavioural
proxy (an injected snippet's distinctive words reappearing in the assistant's
response — see prompt_recall._detect_quoteback), NOT a confirmed human judgement;
it is heuristic and, on a quiet store, sparse, so lessons may be thin until a
stronger user-confirmed signal exists.  What matters for the governing rule:
the trigger is observed behaviour on the transcript, never the model's opinion
of its own output.

Design:
  - read_recent_outcomes() queries recall_feedback for 'used'/'edited' events.
  - If none exist, write_lessons() returns immediately (nothing to learn from).
  - If events exist: one LLM call extracts a concrete lesson from the observations.
  - A second INDEPENDENT LLM call verifies the lesson is grounded and specific.
  - Only lessons that pass the independent check are written to `recall_lessons`.
  - Dedup by content hash — same lesson never written twice.
  - Gated on `lessons_enabled` config flag (default False).
  - Never raises.

Public API:
    from mcpbrain.lessons import write_lessons
    result = write_lessons(store, home)   # returns dict, never raises
"""
from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from datetime import datetime, timedelta, timezone

log = logging.getLogger("mcpbrain.lessons")

_EXTRACT_TIMEOUT = 20
_VERIFY_TIMEOUT = 15


# ---------------------------------------------------------------------------
# Schema (safe: no-op if already present)
# ---------------------------------------------------------------------------

def init_lessons_table(store) -> None:
    """Create recall_lessons table in brain.sqlite3 if it doesn't exist."""
    try:
        with store._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS recall_lessons (
                    id INTEGER PRIMARY KEY,
                    lesson_text TEXT NOT NULL,
                    source_events TEXT,      -- JSON list of doc_ids
                    content_hash TEXT UNIQUE,
                    verified INTEGER NOT NULL DEFAULT 1,
                    written_at TEXT
                )
            """)
    except Exception as exc:  # noqa: BLE001
        log.debug("lessons: could not create table: %s", exc)


# ---------------------------------------------------------------------------
# Outcome reads (external signal gate)
# ---------------------------------------------------------------------------

def read_recent_outcomes(store, days: int = 7) -> list[dict]:
    """Return recall_feedback rows with event_type in ('used','edited') from last N days.

    Returns [] (and does not raise) if the table is absent or the store is broken.
    """
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        with store._connect() as db:
            rows = db.execute(
                "SELECT doc_id, event_type, ts FROM recall_feedback "
                "WHERE event_type IN ('used','edited') AND ts >= ? "
                "ORDER BY ts DESC LIMIT 50",
                (cutoff,),
            ).fetchall()
        return [{"doc_id": r[0], "event_type": r[1], "ts": r[2]} for r in rows]
    except Exception as exc:  # noqa: BLE001
        log.debug("lessons: read_recent_outcomes failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# LLM helpers (subscription-only — claude CLI)
# ---------------------------------------------------------------------------

def _call_claude(prompt: str, timeout: int) -> str:
    from mcpbrain import config
    try:
        claude = config.find_claude()
    except RuntimeError:
        return ""
    try:
        result = subprocess.run(
            [claude, "-p", prompt, "--output-format", "text"],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            log.debug("lessons: claude returned %d: %s",
                      result.returncode, (result.stderr or "")[:200])
            return ""
        return (result.stdout or "").strip()
    except subprocess.TimeoutExpired:
        log.debug("lessons: timed out after %ds", timeout)
        return ""
    except Exception as exc:  # noqa: BLE001
        log.debug("lessons: claude call failed: %s", exc)
        return ""


_EXTRACT_PROMPT = """\
You are analyzing observed memory-recall usage to extract a concrete lesson.

OBSERVED USAGE EVENTS (the user actually used or edited recalled memory):
{events_block}

Extract ONE concrete, specific lesson about when recalling from memory is
particularly helpful.  The lesson must be grounded in the observations above
— do not add general knowledge.

Return JSON only:
{{"lesson": "one specific sentence about what makes recall valuable in these cases"}}

Rules:
- The lesson must be falsifiable: it could be proven wrong by future observations.
- Do not generalize beyond what the observations show.
- Maximum 80 words.
- If the observations are too sparse to draw a meaningful lesson, set lesson to null.
"""

_VERIFY_PROMPT = """\
You are an independent reviewer checking whether a proposed lesson is grounded
in the provided observations.

OBSERVATIONS:
{events_block}

PROPOSED LESSON:
{lesson}

Answer: Is this lesson directly supported by the observations (not general
knowledge)? Is it specific and falsifiable?

Return JSON only:
{{"grounded": true, "reason": "one sentence"}}
or
{{"grounded": false, "reason": "why the lesson is not grounded"}}
"""


def _format_events(outcomes: list[dict]) -> str:
    lines = [
        f"  [{r.get('ts', '')}] doc={r.get('doc_id', '')} event={r.get('event_type', '')}"
        for r in outcomes[:20]
    ]
    return "\n".join(lines) if lines else "  (none)"


# ---------------------------------------------------------------------------
# Lesson persistence
# ---------------------------------------------------------------------------

def _content_hash(lesson: str) -> str:
    return hashlib.sha256(lesson.lower().strip().encode()).hexdigest()[:16]


def _already_written(store, h: str) -> bool:
    try:
        with store._connect() as db:
            row = db.execute(
                "SELECT 1 FROM recall_lessons WHERE content_hash=?", (h,)
            ).fetchone()
        return row is not None
    except Exception:  # noqa: BLE001
        return False


def _write_lesson(store, lesson: str, source_events: list[dict]) -> None:
    h = _content_hash(lesson)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    doc_ids = list({r.get("doc_id") for r in source_events if r.get("doc_id")})
    try:
        with store._connect() as db:
            db.execute(
                "INSERT INTO recall_lessons(lesson_text, source_events, content_hash, "
                "verified, written_at) VALUES(?,?,?,1,?)",
                (lesson, json.dumps(doc_ids), h, now),
            )
    except Exception as exc:  # noqa: BLE001
        log.debug("lessons: _write_lesson failed: %s", exc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def write_lessons(store, home: str) -> dict:
    """Extract and persist lessons from observed recall outcomes.

    Returns {"written": int, "skipped": str|None, "lesson": str|None}.
    Never raises.
    """
    from mcpbrain import config
    if not config.lessons_enabled(home):
        return {"written": 0, "skipped": "lessons_enabled=false", "lesson": None}

    try:
        init_lessons_table(store)
        outcomes = read_recent_outcomes(store)
        if not outcomes:
            return {"written": 0, "skipped": "no observed outcomes", "lesson": None}

        events_block = _format_events(outcomes)

        # Step 1 — extract a lesson from the observations
        raw_extract = _call_claude(
            _EXTRACT_PROMPT.format(events_block=events_block),
            timeout=_EXTRACT_TIMEOUT,
        )
        if not raw_extract:
            return {"written": 0, "skipped": "LLM unavailable", "lesson": None}

        try:
            start = raw_extract.find("{")
            end = raw_extract.rfind("}")
            extract_obj = json.loads(raw_extract[start:end + 1]) if start >= 0 else {}
            lesson = extract_obj.get("lesson")
        except Exception:  # noqa: BLE001
            lesson = None

        if not lesson or not isinstance(lesson, str):
            return {"written": 0, "skipped": "no lesson extracted", "lesson": None}

        lesson = lesson.strip()
        h = _content_hash(lesson)
        if _already_written(store, h):
            return {"written": 0, "skipped": "duplicate lesson", "lesson": lesson}

        # Step 2 — independent verification (external guard; not self-critique)
        raw_verify = _call_claude(
            _VERIFY_PROMPT.format(events_block=events_block, lesson=lesson),
            timeout=_VERIFY_TIMEOUT,
        )
        if not raw_verify:
            # Verification unavailable — skip (don't write unverified lessons)
            return {"written": 0, "skipped": "verification unavailable", "lesson": lesson}

        try:
            start = raw_verify.find("{")
            end = raw_verify.rfind("}")
            verify_obj = json.loads(raw_verify[start:end + 1]) if start >= 0 else {}
            grounded = bool(verify_obj.get("grounded"))
        except Exception:  # noqa: BLE001
            grounded = False

        if not grounded:
            return {"written": 0, "skipped": "failed independent check", "lesson": lesson}

        _write_lesson(store, lesson, outcomes)
        log.info("lessons: wrote lesson (hash=%s)", h)
        return {"written": 1, "skipped": None, "lesson": lesson}

    except Exception as exc:  # noqa: BLE001
        log.debug("lessons: write_lessons failed: %s", exc)
        return {"written": 0, "skipped": f"error: {exc}", "lesson": None}
