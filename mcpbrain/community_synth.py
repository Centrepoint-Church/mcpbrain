"""community_synthesis: generate titles + summaries for untitled communities.

Block contract:
  build_community_requests(store, *, cap) -> list[dict]
      Returns untitled communities (title='' or NULL, member_count >= 2)
      with their member names, ready to send to the LLM.

  drain_communities(store, inbox_obj) -> {"communities_written": N}
      Applies LLM-generated title/summary back to community_summaries and
      records a change_log entry per community.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def build_community_requests(store, *, cap: int = 10, member_sample: int = 40) -> list[dict]:
    """Return up to `cap` untitled communities with member_count >= 2.

    A community is untitled when its title column is NULL, empty string, or
    whitespace-only.  Returns a list of dicts:
        {community_id, member_count, members: [name, ...]}

    `members` is a SAMPLE of at most `member_sample` names, not the full roster:
    a community can have thousands of members, and dumping all of them made the
    spool (and the brain_enrich_pull MCP response) explode past the token cap.
    The LLM only needs a representative sample plus the true `member_count` to
    title/summarise the cluster.
    """
    with store._connect() as db:
        rows = db.execute(
            """
            SELECT community_id, member_count
            FROM community_summaries
            WHERE (title IS NULL OR trim(title) = '')
              AND member_count >= 2
            ORDER BY member_count DESC
            LIMIT ?
            """,
            (cap,),
        ).fetchall()

    results = []
    for row in rows:
        cid = row["community_id"]
        mc = row["member_count"]
        with store._connect() as db:
            members = db.execute(
                """
                SELECT e.name
                FROM entity_communities ec
                JOIN entities e ON e.id = ec.entity_id
                WHERE ec.community_id = ?
                ORDER BY e.name
                LIMIT ?
                """,
                (cid, member_sample),
            ).fetchall()
        results.append({
            "community_id": cid,
            "member_count": mc,
            "members": [m["name"] for m in members],
        })
    return results


def set_community_title(store, cid, title: str, summary: str | None = None, *,
                        source: str) -> bool:
    """Set a circle's title (and summary; None keeps the current one) and record
    an undoable `community_titled` change holding the prior pair, in one
    transaction. Returns False when the community does not exist."""
    from mcpbrain.change_undo import revert_json
    with store._connect(write=True) as db:
        prior = db.execute("SELECT title, summary FROM community_summaries "
                           "WHERE community_id=? ORDER BY level LIMIT 1", (cid,)).fetchone()
        if prior is None:
            return False
        new_summary = (prior["summary"] or "") if summary is None else summary
        db.execute("UPDATE community_summaries SET title=?, summary=? WHERE community_id=?",
                   (title, new_summary, cid))
        store.record_change_tx(
            db, "community_titled", ref_id=str(cid),
            summary=f"Title set for community {cid}: {title}", source=source,
            revert_ref=revert_json(
                {"title": prior["title"] or "", "summary": prior["summary"] or ""},
                {"title": title, "summary": new_summary}))
    return True


def drain_communities(store, inbox_obj: dict) -> dict:
    """Write LLM-generated titles/summaries back to community_summaries.

    Expects inbox_obj["community_synthesis"] to be a list of:
        {community_id, title, summary}
    Skips items with no title or where the community_id doesn't exist.
    Records one change_log entry per community written.
    Returns {"communities_written": N}.
    """
    items = inbox_obj.get("community_synthesis") or []
    written = 0

    for item in items:
        cid = item.get("community_id")
        title = (item.get("title") or "").strip()
        summary = (item.get("summary") or "").strip()

        if not title:
            log.debug("community_synthesis: skipping community_id=%s (no title)", cid)
            continue

        if not set_community_title(store, cid, title, summary, source="community_synthesis"):
            log.debug("community_synthesis: community_id=%s not found, skipping", cid)
            continue
        written += 1

    return {"communities_written": written}


# Register with drain.py so it is called automatically when this module is imported.
def _register():
    try:
        from mcpbrain.drain import BLOCK_DRAINERS  # noqa: PLC0415

        BLOCK_DRAINERS["community_synthesis"] = drain_communities
    except ImportError:
        log.debug("drain module not available; community_synthesis drainer not registered")


_register()
