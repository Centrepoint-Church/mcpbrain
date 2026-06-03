"""Thread synthesis: build synthesis requests and drain synthesis summaries.

This module handles the Phase 3 file-contract extension for thread synthesis:
- build_synthesis_requests: builds the synthesis block for pending.json
- attach_synthesis_block: attaches the synthesis block to the pending dict
- drain_synthesis: writes filled synthesis summaries into thread_context

No LLM calls happen here. The Cowork session (or a test stub) fills the
synthesis block; drain_synthesis merely writes the results back to the store.
The Mac install must never import claude_pool or run_claude.
"""

import logging

log = logging.getLogger(__name__)


def build_synthesis_requests(store, *, min_emails: int = 5, limit: int = 50) -> list[dict]:
    """Build synthesis request items for threads needing summaries.

    Reads threads_needing_summary from the store, fetches per-message summaries
    from email_context, and builds a request item for each thread that has at
    least one per-message summary. Threads whose email_context rows all have
    empty summary fields are omitted — there is nothing for Cowork to synthesise.
    """
    threads = store.threads_needing_summary(min_emails)[:limit]
    requests = []
    for t in threads:
        msgs = store.thread_messages(t["thread_id"])
        parts = []
        for m in msgs:
            if m.get("summary"):
                ctype = f" [{m['content_type']}]" if m.get("content_type") else ""
                parts.append(f"- {m.get('date_iso', '?')}{ctype}: {m['summary']}")
        if not parts:
            continue  # skip threads with no per-message summaries
        email_summaries = "\n".join(parts)
        first_date = msgs[0].get("date_iso", "?") if msgs else "?"
        last_date = msgs[-1].get("date_iso", "?") if msgs else "?"
        requests.append({
            "thread_id": t["thread_id"],
            "subject": t.get("subject", ""),
            "org": t.get("org", ""),
            "email_count": t.get("email_count", 0),
            "first_date": first_date,
            "last_date": last_date,
            "email_summaries": email_summaries,
        })
    return requests


def attach_synthesis_block(pending: dict, requests: list) -> dict:
    """Add synthesis block to pending.json data (cadence-gated: skip if empty).

    Returns a new dict with the synthesis key added. When requests is empty,
    the original dict is returned unchanged so the contract stays minimal.
    """
    if not requests:
        return pending
    return {**pending, "synthesis": requests}


def drain_synthesis(store, inbox_obj: dict) -> dict:
    """Write filled synthesis narratives into thread_context.contextual_summary.

    The synthesis pass produces the deep, cross-thread narrative — distinct from
    the one-line `summary` apply() already writes. Each answer carries the text
    under `contextual_summary` (preferred) or `summary` (back-compat). Items with
    no text or no thread_id are skipped silently.

    The stub filler in tests stands in for Cowork/claude_pool. On Nexus, the
    dry-run uses a claude_pool-backed filler (run_claude with the ported
    _BATCH_INSTRUCTIONS); on the Mac the Cowork session is the filler.
    No run_claude import here — the Mac install must never import claude_pool.
    """
    written = 0
    for item in inbox_obj.get("synthesis", []):
        thread_id = item.get("thread_id")
        text = item.get("contextual_summary") or item.get("summary", "")
        if not thread_id or not text:
            continue
        store.upsert_thread_context(thread_id, contextual_summary=text)
        written += 1
    return {"thread_context_written": written}
