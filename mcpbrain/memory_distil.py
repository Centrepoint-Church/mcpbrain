"""memory_distil: review, expire, and promote session memory notes.

Block contract:
  build_distil_requests(store, *, cap) -> list[dict]
      Returns live memory notes ready for LLM review.

  drain_distil(store, inbox_obj) -> {"expired": N, "promotions_flagged": N}
      Applies verdicts:
        "keep"    — no-op
        "expire"  — patches chunk metadata expired=True, records memory_expired
        "promote" — records a memory_promotion finding; keeps the note live
      Unknown doc_id or unknown verdict: silently skipped.
"""

from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)

_VALID_VERDICTS = {"keep", "expire", "promote"}


def build_distil_requests(store, *, cap: int = 30) -> list[dict]:
    """Return up to `cap` live memory notes as LLM-ready request dicts.

    Each dict contains:
        {doc_id, title, content, captured_at}
    content is the body after the first blank line, capped at 300 chars.
    """
    chunks = store.note_chunks(observation_type="memory", limit=cap)
    results = []
    for c in chunks:
        text = c["text"] or ""
        meta = c.get("metadata") or {}

        # Split on first double-newline to get body; fall back to full text.
        parts = text.split("\n\n", 1)
        body = parts[1] if len(parts) > 1 else text
        content = body[:300]

        results.append({
            "doc_id": c["doc_id"],
            "title": meta.get("title", ""),
            "content": content,
            "captured_at": meta.get("captured_at", ""),
        })
    return results


def drain_distil(store, inbox_obj: dict) -> dict:
    """Apply LLM verdicts to memory notes.

    Expects inbox_obj["memory_distil"] to be a list of:
        {doc_id, verdict, reason?, target_hint?}

    Returns {"expired": N, "promotions_flagged": N}.
    """
    items = inbox_obj.get("memory_distil") or []
    expired_count = 0
    promoted_count = 0

    for item in items:
        doc_id = item.get("doc_id", "")
        verdict = item.get("verdict", "")

        # Skip unknown verdicts immediately.
        if verdict not in _VALID_VERDICTS:
            log.debug("memory_distil: skipping doc_id=%s unknown verdict=%s", doc_id, verdict)
            continue

        if verdict == "keep":
            continue

        # Verify the chunk exists before acting.
        chunk = store.get_chunk(doc_id)
        if chunk is None:
            log.debug("memory_distil: doc_id=%s not found, skipping", doc_id)
            continue

        if verdict == "expire":
            ok = store.patch_chunk_metadata(doc_id, expired=True)
            if ok:
                reason = item.get("reason", "")
                store.record_change(
                    "memory_expired",
                    ref_id=doc_id,
                    summary=f"Memory note expired: {doc_id}",
                    detail=reason,
                    source="memory_distil",
                )
                expired_count += 1

        elif verdict == "promote":
            reason = item.get("reason", "")
            target_hint = item.get("target_hint", "")
            # get_chunk returns metadata already parsed to a dict; guard for a
            # raw JSON string defensively.
            meta = chunk.get("metadata") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    meta = {}
            store.record_finding(
                "memory_promotion",
                ref_id=doc_id,
                org=meta.get("org", ""),
                summary=f"Memory note flagged for promotion: {doc_id}",
                detail=f"reason={reason} target_hint={target_hint}",
            )
            promoted_count += 1

    return {"expired": expired_count, "promotions_flagged": promoted_count}


# Register with drain.py so it is called automatically when this module is imported.
def _register():
    try:
        from mcpbrain.drain import BLOCK_DRAINERS  # noqa: PLC0415

        BLOCK_DRAINERS["memory_distil"] = drain_distil
    except ImportError:
        log.debug("drain module not available; memory_distil drainer not registered")


_register()
