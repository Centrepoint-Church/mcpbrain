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


def remap_topics(store, home) -> dict:
    """Fold each topic entity into its normalized topic-<canonical> id."""
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
    return {"merged": merged, "canonical": len(canon_ids)}


def reset_meeting_sources(store) -> dict:
    """Snapshot current meeting ids and reset their source chunks for re-extract."""
    with store._connect() as db:
        pre_ids = [r["id"] for r in db.execute(
            "SELECT id FROM entities WHERE type='meeting'").fetchall()]
    doc_ids = store.meeting_source_doc_ids()
    chunks_reset = store.reset_enriched(doc_ids)
    log.info("reset_meeting_sources: %d meeting entities, %d chunks reset",
             len(pre_ids), chunks_reset)
    return {"pre_ids": pre_ids, "chunks_reset": chunks_reset}


def retire_meeting_duplicates(store, pre_ids) -> dict:
    """Merge each pre-migration bare meeting id into its unique new series.

    Runs AFTER re-extraction has produced the meeting-<org>-<series> nodes. A
    pre-id with zero or ambiguous series matches is LEFT as a single-occurrence
    entity (non-destructive policy). Skips ids that are already series ids."""
    retired = 0
    left = 0
    for old_id in pre_ids:
        if old_id.startswith("meeting-"):
            continue  # already a series id (e.g. a re-run)
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
