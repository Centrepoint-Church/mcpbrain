"""B6 — Guarded voice.md apply (Phase B).

Two-phase commit with safety guards:
  1. Analysis-only (voice_analyser.py Phase A) — runs automatically weekly.
  2. Guarded apply — user-triggered (or MCP tool). Subject to:
       - Cooldown: min 3 days between successive applies
       - Diff cap: no single apply may change more than MAX_DIFF_LINES lines
       - Only 'pending' suggestions are applied; each is marked 'applied' after.

Revert: voice.md is written atomically (temp+replace) so the previous content
is captured in the backup alongside the new version. No separate revert file.

Public API:
  can_apply(store, home) -> tuple[bool, str]   # (allowed, reason_if_not)
  apply_suggestions(store, home, suggestion_ids=None) -> dict
"""

from __future__ import annotations

import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("mcpbrain.voice_apply")

_VOICE_FILENAME = "voice.md"
_COOLDOWN_DAYS = 3
_MAX_DIFF_LINES = 20     # guard: no apply may add/remove more than this many lines
_MIN_CONFIDENCE = 0.75


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _voice_path(home: str) -> Path:
    return Path(home) / _VOICE_FILENAME


def can_apply(store, home: str) -> tuple[bool, str]:
    """Return (True, '') if apply is permitted, else (False, reason)."""
    from mcpbrain import config
    if not config.procedural_memory_enabled(home):
        return False, "procedural_memory not enabled"

    last = store.get_voice_analyser_state("last_applied_at")
    if last:
        try:
            last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
            days_ago = (datetime.now(timezone.utc) - last_dt).total_seconds() / 86400.0
            if days_ago < _COOLDOWN_DAYS:
                remaining = _COOLDOWN_DAYS - days_ago
                return False, f"cooldown active — {remaining:.1f}d remaining"
        except Exception:
            pass

    pending = store.pending_voice_suggestions()
    if not pending:
        return False, "no pending voice suggestions"

    return True, ""


def apply_suggestions(store, home: str,
                      suggestion_ids: list[int] | None = None) -> dict:
    """Apply pending voice suggestions to voice.md.

    suggestion_ids: if provided, only apply these IDs. Otherwise apply all
    high-confidence pending suggestions (confidence >= _MIN_CONFIDENCE).

    Returns {"applied": N, "skipped": M, "blocked": reason_or_none}.
    """
    allowed, reason = can_apply(store, home)
    if not allowed:
        log.info("voice_apply: blocked — %s", reason)
        return {"applied": 0, "skipped": 0, "blocked": reason}

    pending = store.pending_voice_suggestions()
    if suggestion_ids is not None:
        id_set = set(suggestion_ids)
        pending = [s for s in pending if s["id"] in id_set]
    pending = [s for s in pending if float(s.get("confidence") or 0.0) >= _MIN_CONFIDENCE]

    if not pending:
        return {"applied": 0, "skipped": 0, "blocked": None}

    voice_path = _voice_path(home)
    existing = voice_path.read_text() if voice_path.exists() else ""
    existing_lines = existing.splitlines()

    additions: list[str] = []
    for s in pending:
        line = f"- [{s['kind']}] {s['rule']}"
        additions.append(line)

    # Diff cap: don't apply if the change exceeds MAX_DIFF_LINES
    if len(additions) > _MAX_DIFF_LINES:
        log.warning("voice_apply: %d additions exceed diff cap %d — truncating",
                    len(additions), _MAX_DIFF_LINES)
        additions = additions[:_MAX_DIFF_LINES]
        pending = pending[:_MAX_DIFF_LINES]

    # Write new content (append a section at the end of voice.md)
    ts = _now_iso()[:10]
    new_section = f"\n\n## Voice suggestions applied {ts}\n" + "\n".join(additions) + "\n"
    new_content = existing.rstrip() + new_section

    # Atomic write
    try:
        voice_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(voice_path.parent),
                                   prefix=".voice.", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            f.write(new_content)
        os.replace(tmp, voice_path)
    except OSError as exc:
        log.error("voice_apply: write failed: %s", exc)
        return {"applied": 0, "skipped": len(pending), "blocked": str(exc)}

    # Mark suggestions applied
    for s in pending:
        store.mark_voice_suggestion_applied(s["id"])

    store.set_voice_analyser_state("last_applied_at", _now_iso())
    store.record_change(
        "voice_apply",
        summary=f"Applied {len(pending)} voice suggestions to voice.md",
        source="voice_apply",
    )
    log.info("voice_apply: applied %d suggestions", len(pending))
    return {"applied": len(pending), "skipped": 0, "blocked": None}
