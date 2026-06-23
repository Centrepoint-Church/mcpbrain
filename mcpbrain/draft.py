"""Email draft context assembly for mcpbrain.

Entry point:
  draft_context(store, home, email_id, intent="") → dict

The dict is passed to the brain_draft_context MCP tool and consumed by
the Cowork draft-reply skill. All LLM calls happen in the skill; this
module is pure context assembly with no subprocess calls.

Helpers: _load_voice_rules, _get_email_context, _get_samples
"""
from __future__ import annotations

import logging
from pathlib import Path

from mcpbrain import config

log = logging.getLogger(__name__)


def _load_voice_rules(home: str) -> str:
    """Read the records repo's context/voice.md. Returns '' if not found.
    # NOTE: voice.md lives in records_dir/context/, not app_dir/context/ (MCP resources
    # serve from the latter). The two paths will be aligned in a future pass.
    """
    p = Path(config.records_dir(home)) / "context" / "voice.md"
    try:
        return p.read_text()
    except OSError:
        return ""


def _get_email_context(store, email_id: str) -> dict:
    """Return email_context row for email_id, or {} if not found."""
    try:
        with store._connect() as db:
            row = db.execute(
                "SELECT * FROM email_context WHERE message_id=?", (email_id,)).fetchone()
            return dict(row) if row else {}
    except Exception as exc:
        log.warning("_get_email_context failed for %s: %s", email_id, exc)
        return {}


def _get_samples(store, thread_id: str, n: int = 3) -> str:
    """Return recent thread context summaries as a formatted string."""
    if not thread_id:
        return ""
    try:
        with store._connect() as db:
            rows = db.execute(
                "SELECT date_iso, sender, summary FROM email_context "
                "WHERE thread_id=? ORDER BY date_iso DESC LIMIT ?",
                (thread_id, n),
            ).fetchall()
            if not rows:
                return ""
            lines = [f"[{r['date_iso']}] {r['sender']}: {r['summary']}" for r in rows]
            return "\n".join(lines)
    except Exception as exc:
        log.warning("_get_samples failed: %s", exc)
        return ""


def draft_context(store, home: str, email_id: str, intent: str = "") -> dict:
    """Assemble context for drafting a reply. Returns a dict for brain_draft_context MCP tool.
    Returns {"error": "email not found"} if email_id is unknown.

    When draft_critic_enabled=true in config, also runs the voice/coverage/grounding
    critic and appends a "critique" key to the returned dict.
    """
    ec = _get_email_context(store, email_id)
    if not ec:
        return {"error": "email not found"}
    body = ec.get("contextual_summary") or ec.get("summary", "")
    result = {
        "subject": ec.get("subject", ""),
        # email_context stores no raw body; use contextual_summary when available
        # (richer situational narrative), falling back to the one-line summary.
        "body": body,
        "sender": ec.get("sender", ""),
        "thread_id": ec.get("thread_id", ""),
        "voice_rules": _load_voice_rules(home),
        "samples": _get_samples(store, ec.get("thread_id", "")),
        "intent": intent,
    }
    if config.draft_critic_enabled(home):
        try:
            from mcpbrain.draft_critic import critique as _critique
            result["critique"] = _critique(intent or body, result, home=home)
        except Exception as exc:
            log.debug("draft_context: critic failed (non-fatal): %s", exc)
    return result
