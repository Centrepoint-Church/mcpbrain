"""Periodic waiting-on reconciliation pass.

Scans inbound chunks since the last cursor position and clears waiting_on
on open actions whose awaited person has replied.

Port note: Nexus hooks waiting_on_reconciler.reconcile_waiting_on into
enrich_gmail._process_message (per-message). mcpbrain uses a periodic sweep
over new inbound chunks — same match logic, different trigger.

The causal-log call (engine_db.log_signal) from the Nexus version is not
ported: it is Nexus-only and there is no equivalent table here.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

log = logging.getLogger("mcpbrain.waiting_on")


def _normalise(name: str | None) -> str:
    """Port verbatim from Nexus waiting_on_reconciler.py:21-26."""
    if not name:
        return ""
    s = re.sub(r"[^\w\s]", "", name.lower())
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _matches(chunk: dict, waiting_on: str | None, entity_id: str | None) -> bool:
    """Adapted from Nexus: chunk metadata has sender/sender_email, not message dict."""
    meta = chunk.get("metadata", {})
    if isinstance(meta, str):
        meta = json.loads(meta)
    sender_entity = meta.get("sender_entity_id") or meta.get("sender_id", "")
    if entity_id and sender_entity and entity_id == sender_entity:
        return True
    sender_name = meta.get("sender") or meta.get("sender_name", "")
    if waiting_on and _normalise(sender_name) == _normalise(waiting_on):
        return True
    return False


def _is_outbound(chunk: dict, identity: str = "") -> bool:
    """True if the chunk is outbound (sent by the install owner / self)."""
    meta = chunk.get("metadata", {})
    if isinstance(meta, str):
        meta = json.loads(meta)
    # Check labels for SENT label or is_inbound=False
    labels = meta.get("labels", [])
    if isinstance(labels, str):
        labels = json.loads(labels) if labels.startswith("[") else labels.split(",")
    if "SENT" in labels:
        return True
    if meta.get("is_inbound") is False:
        return True
    sender_email = (meta.get("sender_email") or meta.get("sender", "")).lower()
    if identity and sender_email and sender_email in identity.lower():
        return True
    return False


def reconcile(
    store,
    chunks: list,
    *,
    now: str | None = None,
    window_days: int = 30,
    identity: str | None = None,
) -> int:
    """Clear waiting_on on open actions when the awaited person's chunk arrives.

    Note: drops the Nexus engine_db.log_signal causal-log call (Nexus-only).
    """
    if now is None:
        now = datetime.now(timezone.utc).isoformat()

    cleared = 0
    waiting_actions = store.open_waiting_actions(window_days=window_days, now=now)

    for action in waiting_actions:
        for chunk in chunks:
            if _is_outbound(chunk, identity or ""):
                continue
            if _matches(chunk, action.get("waiting_on"), action.get("waiting_on_entity_id")):
                store.clear_waiting(action["id"], chunk["doc_id"], now)
                cleared += 1
                _trigger_reextract(store, action.get("thread_id"), now)
                break  # only clear once per action

    return cleared


def _trigger_reextract(store, thread_id: str | None, now: str) -> None:
    """A reply arrived from the person an action was waiting_on -- that is
    NOT the same as the ask being resolved ("I'll look at it next week" is a
    reply, not a resolution). So this never closes the action itself; it only
    puts the thread back in front of the LLM extractor (same mechanism as
    stale_reextract.sweep) with the reply now in context, so a genuine close
    goes through the normal resolved_action_ids path.
    """
    if not thread_id:
        return
    if store.thread_has_unenriched(thread_id):
        return  # the normal enrichment path already owns this thread
    sig = store.thread_signature(thread_id)
    prev = store.get_stale_reextract(thread_id)
    if prev and prev.get("signature") == sig:
        return  # already re-triggered at this content-state
    store.mark_thread_unenriched(thread_id)
    store.set_stale_reextract(thread_id, sig, now)


def run(store, *, now: str | None = None, identity: str | None = None) -> dict:
    """Periodic reconcile sweep over new inbound chunks."""
    if now is None:
        now = datetime.now(timezone.utc).isoformat()

    cursor = store.get_meta("waiting_on_cursor")
    chunks = store.inbound_chunks_since(cursor)

    cleared = reconcile(store, chunks, now=now, identity=identity)

    # Advance cursor to the newest chunk's date
    if chunks:
        newest_date = max(
            (c["metadata"].get("date") or c["metadata"].get("date_iso") or "")
            for c in chunks
        )
        if newest_date:
            store.set_meta("waiting_on_cursor", newest_date)

    return {"cleared": cleared}
