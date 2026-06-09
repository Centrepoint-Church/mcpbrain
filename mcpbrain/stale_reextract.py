"""Re-extraction trigger for stale-looking open actions (Gap A).

The keyword stale heuristic (retrieval.action_is_stale) decides ONLY which
already-enriched threads deserve another LLM at-bat — never whether to close.
Resetting a thread to enriched=0 lets the normal enrichment cycle re-extract it
with its open_actions in context; the existing resolved_action_ids path makes
any actual close decision. A per-thread content signature prevents re-triggering
the same unchanged thread (which would re-pay the re-extraction token cost).
"""
from __future__ import annotations

import logging

from mcpbrain.retrieval import action_is_stale

log = logging.getLogger(__name__)

STALE_REEXTRACT_MAX = 20


def sweep(store, *, now: str, cap: int = STALE_REEXTRACT_MAX) -> dict:
    """Trigger re-extraction for stale open actions whose threads are idle.

    `now` is an ISO timestamp string (injected so the daemon owns the clock).
    Returns {"triggered": int, "deferred": int, "threads": [thread_id, ...]}.
    """
    candidates: list[tuple[str, str]] = []   # (thread_id, signature)
    seen: set[str] = set()
    for action in store.unified_actions(status="open"):
        thread_id = action.get("thread_id")
        if not thread_id or thread_id in seen:
            continue
        if not action_is_stale(store, action):
            continue
        if store.thread_has_unenriched(thread_id):
            continue  # the normal enrichment path already owns this thread
        sig = store.thread_signature(thread_id)
        prev = store.get_stale_reextract(thread_id)
        if prev and prev.get("signature") == sig:
            continue  # already had its at-bat at this content-state
        seen.add(thread_id)
        candidates.append((thread_id, sig))

    triggered: list[str] = []
    for thread_id, sig in candidates[:cap]:
        store.mark_thread_unenriched(thread_id)
        store.set_stale_reextract(thread_id, sig, now)
        triggered.append(thread_id)

    deferred = max(0, len(candidates) - cap)
    if deferred:
        log.info("stale-reextract: triggered %d thread(s), deferred %d to next "
                 "run (cap=%d)", len(triggered), deferred, cap)
    return {"triggered": len(triggered), "deferred": deferred,
            "threads": triggered}
