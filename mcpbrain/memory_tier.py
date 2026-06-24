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


_MAX_CORE_ITEMS = 12

_IDENTITY_SEED_DOC_ID = "note-core-identity-seed"
_IDENTITY_SEED_SALIENCE = 6.5  # high band — durable identity is the most stable fact we hold


def seed_core_identity(store, home: str) -> int:
    """Write a stable identity/standing-commitment chunk as a core-tier semantic note.

    Sources: config.json owner fields + records/context/identity.md. The chunk
    has a fixed doc_id so it is idempotent (upsert overwrites on re-run). Salience
    is set explicitly to _IDENTITY_SEED_SALIENCE (high band) so recompute_core
    always picks it up as a top core candidate.

    This is the B4/B2 fix from Session 3: the original core note was a grab-bag
    of transient ops-email. Durable identity content (who Josh is, what org, contact)
    should always be in the always-injected block, independent of what the
    consolidation cadence happens to have summarised most recently.

    Returns 1 if the seed was written, 0 if there was no content to write.
    """
    import hashlib
    from pathlib import Path

    from mcpbrain import config

    cfg = config.read_config(home)
    lines = []

    # Owner identity from config
    name = cfg.get("owner_full_name") or cfg.get("owner_name") or ""
    role = cfg.get("owner_role") or ""
    email = cfg.get("owner_email") or ""
    orgs = [o.get("name", "") for o in (cfg.get("orgs") or []) if o.get("name")]

    if name:
        line = name
        if role:
            line += f" — {role}"
        if orgs:
            line += f" at {', '.join(orgs)}"
        lines.append(line)
    if email:
        lines.append(f"Email: {email}")

    # Standing content from identity.md (skip headers, HTML comments, placeholders)
    identity_path = Path(home) / "records" / "context" / "identity.md"
    if identity_path.exists():
        raw = identity_path.read_text()
        in_comment = False
        for ln in raw.splitlines():
            stripped = ln.strip()
            # Track multi-line HTML comments
            if "<!--" in stripped:
                in_comment = True
            if in_comment:
                if "-->" in stripped:
                    in_comment = False
                continue
            if (not stripped or stripped.startswith("#")
                    or stripped.startswith("(") or stripped.startswith("*")):
                continue
            lines.append(stripped)

    if not lines:
        return 0

    text = "\n".join(lines)
    content_hash = hashlib.sha256(text.encode()).hexdigest()[:16]
    metadata = {
        "observation_type": "identity_seed",
        "source": "config+identity.md",
    }
    store.upsert_chunk(_IDENTITY_SEED_DOC_ID, text, content_hash, metadata)
    store.set_chunk_type(_IDENTITY_SEED_DOC_ID, "semantic")
    store.set_chunk_tier(_IDENTITY_SEED_DOC_ID, "core")
    store.set_chunk_salience(_IDENTITY_SEED_DOC_ID, _IDENTITY_SEED_SALIENCE)
    log.info("memory_tier: identity seed written (%d chars, salience=%.1f)",
             len(text), _IDENTITY_SEED_SALIENCE)
    return 1


def recompute_core(store, home: str, *, max_items: int = _MAX_CORE_ITEMS) -> int:
    """Recompute the 'core' tier — the always-injected durable facts.

    Core = the top `max_items` highest-salience DURABLE notes (memory_type
    semantic/procedural — consolidated knowledge + the voice/procedural model, NOT
    raw episodic email). Chunks that fall out revert to 'hot' (reversible; nothing
    deleted). THIS is the promoter that was missing — without it the core tier is
    never populated and the always-injected block is empty. Returns the core size.

    Bounded and deterministic (no LLM); safe on the tier-maintenance cadence.
    """
    from mcpbrain import config
    if not config.tiered_memory_enabled(home):
        return 0
    # Seed the durable identity note first (idempotent) so the always-injected
    # core block is useful day-one — independent of whether consolidation has yet
    # produced any semantic notes. Without this, a fresh install's core is empty.
    try:
        seed_core_identity(store, home)
    except Exception as exc:  # noqa: BLE001 — core seeding must never break the tier pass
        log.debug("memory_tier: seed_core_identity failed (non-fatal): %s", exc)
    keep = {c["doc_id"] for c in store.top_core_candidates(max_items)}
    for c in store.chunks_by_tier("core", limit=500):
        if c["doc_id"] not in keep:
            store.set_chunk_tier(c["doc_id"], "hot")   # demote, not delete
    for doc_id in keep:
        store.set_chunk_tier(doc_id, "core")
    if keep:
        log.info("memory_tier: core tier now %d durable notes", len(keep))
    return len(keep)


def run_tier_pass(store, home: str, *,
                  salience_floor: float = 3.5,
                  hot_strength_threshold: float = 7.0) -> dict:
    """Periodic tier maintenance: promote high-strength warm→hot, demote
    low-salience→cold, and recompute the core tier.

    Returns {"promoted": N, "demoted": M, "core": K}. Cheap (no LLM), nightly-safe.
    """
    from mcpbrain import config
    if not config.tiered_memory_enabled(home):
        return {"promoted": 0, "demoted": 0, "core": 0}

    # Promote: warm/untiered chunks recalled enough to build strength (B5 bumps
    # memory_strength on each recall) become hot.
    to_hot = [c["doc_id"] for c in
              store.warm_chunks_above_strength(hot_strength_threshold, limit=2000)]
    promoted_count = promote_to_hot(store, to_hot) if to_hot else 0

    # Demote: chunks with salience below the floor that aren't core/hot.
    candidates = store.chunks_for_decay_pass(limit=2000)
    to_cold = [
        c["doc_id"]
        for c in candidates
        if (float(c.get("salience") or 0.0) < salience_floor
            and c.get("memory_tier", "") not in ("core", "hot"))
    ]
    demoted_count = demote_to_cold(store, to_cold) if to_cold else 0

    core_count = recompute_core(store, home)
    return {"promoted": promoted_count, "demoted": demoted_count, "core": core_count}
