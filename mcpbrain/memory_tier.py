"""B2 — Tiered memory management.

Tiers (stored as chunks.memory_tier):
  core  — small, durable facts; injected into EVERY recall (always-injected block)
  hot   — consolidated semantic notes; included in default recall
  warm  — recent episodic content; included in default recall
  cold  — low-salience or decayed; embedding-only, excluded from default recall
  ''    — untiered (treated as warm for recall purposes)

Core block:
  get_core_block(store, home, *, max_chars) -> str
      Returns a compact block of core-tier chunks, token-budgeted.

Tier transitions (all reversible; nothing is deleted):
  promote_to_hot(store, doc_ids) -> int    — warm→hot on high access
  demote_to_cold(store, doc_ids) -> int    — low-salience/decayed chunks

The always-injected core block is written by graph_write or the capture path
when a note is tagged observation_type='core'; warm is the default tier.
"""

from __future__ import annotations

import logging

log = logging.getLogger("mcpbrain.memory_tier")

_CORE_HEADER = "## Core context (always)"
_MAX_CORE_CHARS = 700


def get_core_block(store, home: str, *, max_chars: int = _MAX_CORE_CHARS) -> str:
    """Return the always-injected core block as a formatted string.

    Reads chunks in the 'core' tier and formats them as a bullet list.
    Returns '' when tiered_memory is disabled or no core chunks exist.
    """
    from mcpbrain import config
    if not config.tiered_memory_enabled(home):
        return ""

    chunks = store.core_chunks(max_chars)
    if not chunks:
        return ""

    lines = []
    total = 0
    for c in chunks:
        snippet = " ".join((c.get("text") or "").split())[:200].strip()
        if not snippet:
            continue
        if total + len(snippet) > max_chars:
            break
        lines.append(f"- {snippet}")
        total += len(snippet)

    if not lines:
        return ""
    return _CORE_HEADER + "\n" + "\n".join(lines)


def promote_to_hot(store, doc_ids: list[str]) -> int:
    """Promote warm/untiered chunks to 'hot' on repeated access.

    Only moves chunks that are currently '' or 'warm' (never demotes core).
    Returns the number of chunks actually promoted.
    """
    promoted = 0
    for doc_id in doc_ids:
        if store.promote_chunk_tier(doc_id, "warm", "hot"):
            promoted += 1
        elif store.promote_chunk_tier(doc_id, "", "hot"):
            promoted += 1
    if promoted:
        log.debug("memory_tier: promoted %d chunks to hot", promoted)
    return promoted


def demote_to_cold(store, doc_ids: list[str]) -> int:
    """Demote low-salience/decayed chunks to 'cold'.

    Cold chunks are excluded from default recall but remain findable by
    explicit cue (hybrid_search with exclude_cold=False). Never demotes
    'core' chunks. Returns count actually demoted.
    """
    count = store.demote_chunks_to_cold(doc_ids)
    if count:
        log.info("memory_tier: demoted %d chunks to cold", count)
    return count


def run_tier_pass(store, home: str, *,
                  salience_floor: float = 3.5,
                  hot_access_threshold: int = 3) -> dict:
    """Periodic tier maintenance: promote high-access, demote low-salience.

    Returns {"promoted": N, "demoted": M}.
    This pass is cheap (no LLM) and safe to run nightly.
    """
    from mcpbrain import config
    if not config.tiered_memory_enabled(home):
        return {"promoted": 0, "demoted": 0}

    promoted_count = 0
    demoted_count = 0

    # Demote: chunks with salience below floor that aren't core/hot
    candidates = store.chunks_for_decay_pass(limit=2000)
    to_cold = [
        c["doc_id"]
        for c in candidates
        if (float(c.get("salience") or 0.0) < salience_floor
            and c.get("memory_tier", "") not in ("core", "hot"))
    ]
    if to_cold:
        demoted_count = demote_to_cold(store, to_cold)

    return {"promoted": promoted_count, "demoted": demoted_count}
