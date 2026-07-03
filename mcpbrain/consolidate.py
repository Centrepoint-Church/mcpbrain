"""One-shot, attended consolidation migrations for meetings and topics.

These are the ONLY destructive operations in the series/topic feature (they call
store.merge_entities, which deletes the loser row). Always run behind a full DB
backup + gold eval — see bin/consolidate.py. Going-forward consolidation
(graph_write.apply) does no merges and needs none of this.
"""

import logging

from mcpbrain import topics
from mcpbrain.chunking import slugify

log = logging.getLogger(__name__)


def _renormalize_email_context_topics(store, home) -> int:
    """Renormalize every email_context.topics string in place.

    Historical rows hold un-normalized tags ("budgets") written before this
    migration folded the topic entities themselves; left alone, the min-2-org
    gate (graph_write.apply) would transiently see both old and new forms as
    distinct tags. Splits on comma, normalizes each tag, drops empties, dedups
    order-preserving, and rejoins with ", " — writing back only rows that
    actually changed. Returns the number of rows updated."""
    with store._connect() as db:
        rows = db.execute(
            "SELECT message_id, topics FROM email_context "
            "WHERE COALESCE(topics,'') != ''").fetchall()
    updated = 0
    for row in rows:
        message_id, raw_topics = row["message_id"], row["topics"]
        tags = [topics.normalize_topic(t, home) for t in raw_topics.split(",")]
        new_topics = ", ".join(dict.fromkeys(t for t in tags if t))
        if new_topics != raw_topics:
            with store._connect() as db:
                db.execute(
                    "UPDATE email_context SET topics=? WHERE message_id=?",
                    (new_topics, message_id))
            updated += 1
    return updated


def remap_topics(store, home) -> dict:
    """Fold each topic entity into its normalized topic-<canonical> id, and
    renormalize historical email_context.topics strings to match."""
    with store._connect() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT id, name FROM entities WHERE type='topic'").fetchall()]
    merged = 0
    canon_ids = set()
    for r in rows:
        canonical = topics.normalize_topic(r["name"], home)
        if not canonical:
            continue
        new_id = slugify(f"topic-{canonical}")
        canon_ids.add(new_id)
        if new_id == r["id"]:
            continue
        # Ensure the canonical entity exists, then fold the variant into it.
        store.upsert_entity(new_id, canonical, "topic", "", "")
        store.merge_entities(r["id"], new_id, method="topic_consolidation")
        merged += 1
    rows_renormalized = _renormalize_email_context_topics(store, home)
    return {"merged": merged, "canonical": len(canon_ids), "rows_renormalized": rows_renormalized}


def reset_meeting_sources(store) -> dict:
    """Snapshot current meeting ids and reset their source chunks for re-extract.

    A meeting-source chunk that is `enrich_state='cold'` (Q1 salience gate)
    would otherwise be reset to enriched=0 but never re-queue: unenriched_chunks()
    excludes cold chunks. Bounded to exactly the scoped meeting-source doc_ids
    (this migration's intent), so this un-colds only what it just reset — clear
    the cold state for those doc_ids too so they actually re-extract."""
    with store._connect() as db:
        pre_ids = [r["id"] for r in db.execute(
            "SELECT id FROM entities WHERE type='meeting'").fetchall()]
    doc_ids = store.meeting_source_doc_ids()
    chunks_reset = store.reset_enriched(doc_ids)
    with store._connect() as db:
        ph = ",".join("?" * len(doc_ids)) if doc_ids else ""
        was_cold = (db.execute(
            f"SELECT COUNT(*) FROM chunks WHERE enrich_state='cold' AND doc_id IN ({ph})",
            doc_ids).fetchone()[0] if doc_ids else 0)
    store.set_enrich_state(doc_ids, "")
    log.info("reset_meeting_sources: %d meeting entities, %d chunks reset, %d uncolded",
             len(pre_ids), chunks_reset, was_cold)
    return {"pre_ids": pre_ids, "chunks_reset": chunks_reset, "uncold": was_cold}


def _has_occurrence(store, entity_id: str) -> bool:
    """True if entity_id has at least one 'occurrence' observation — the signal
    that distinguishes a GENUINE re-extracted meeting series (written via
    store.append_occurrence) from a legacy bare 'meeting-*' slug that pre-dates
    the series scheme and merely happens to share the 'meeting-' prefix."""
    with store._connect() as db:
        return db.execute(
            "SELECT 1 FROM entity_observations "
            "WHERE entity_id=? AND attribute='occurrence' LIMIT 1",
            (entity_id,)).fetchone() is not None


def retire_meeting_duplicates(store, pre_ids) -> dict:
    """Merge each pre-migration bare meeting id into its unique new series.

    Runs AFTER re-extraction has produced the meeting-<org>-<series> nodes. A
    pre-id with zero or ambiguous series matches is LEFT as a single-occurrence
    entity (non-destructive policy). Skips ids that are already GENUINE series
    (carry an 'occurrence' observation) — e.g. on a re-run — rather than any id
    that merely starts with 'meeting-': a legacy bare slug like
    'meeting-with-bob' also starts with 'meeting-' but has no occurrence, so it
    must still be processed for retirement."""
    retired = 0
    left = 0
    for old_id in pre_ids:
        if _has_occurrence(store, old_id):
            continue  # already a genuine series (e.g. a re-run)
        with store._connect() as db:
            still = db.execute("SELECT 1 FROM entities WHERE id=?", (old_id,)).fetchone()
        if not still:
            continue
        series = store.meeting_series_for_old(old_id)
        if series:
            store.merge_entities(old_id, series, method="meeting_series")
            retired += 1
        else:
            left += 1
    log.info("retire_meeting_duplicates: retired=%d left=%d", retired, left)
    return {"retired": retired, "left": left}
