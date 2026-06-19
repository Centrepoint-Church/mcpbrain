"""profile_synthesis block — standing 2-4 sentence entity profiles.

build_profile_requests(store, *, cap=6):
    Returns a list of dicts describing person entities that need a profile
    written or refreshed. Each dict has keys: entity_id, name, org, role,
    relations.

drain_profiles(store, inbox_obj):
    Consumes {"profile_synthesis": [...]} from an inbox object. Each item
    must have entity_id and a non-empty profile string. Writes the profile
    to entities.profile + profile_updated_at and records a change_log row.
    Returns {"profiles_written": N}.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from mcpbrain.graph_write import fetch_role

log = logging.getLogger(__name__)

# Minimum email_count for a person to be considered worth profiling.
_EMAIL_FLOOR = 3

# Days before a profile is considered stale enough to re-request.
_STALE_DAYS = 30


def build_profile_requests(store, *, cap: int = 6) -> list[dict]:
    """Select unprofiled (or stale) person entities with enough signal.

    Filters to type='person', email_count >= _EMAIL_FLOOR, and either:
    - profile is empty/null, OR
    - profile_updated_at is older than _STALE_DAYS days (wall-clock backstop), OR
    - the entity has been re-observed (last_seen) SINCE its profile was written —
      change-driven staleness, so a profile written early in a backfill refreshes
      as more of that person's history lands instead of sticking for 30 days.

    Returns up to `cap` dicts, never-profiled and stalest-profiled first (so the
    refresh rotates rather than re-doing the same high-volume people each cycle).
    Each dict includes: entity_id, name, org, role, relations.
    """
    now_iso = datetime.now(timezone.utc).date().isoformat()
    sql = """
        SELECT id, name, org, email_count
        FROM   entities
        WHERE  type = 'person'
          AND  email_count >= ?
          AND  (
                 profile IS NULL
              OR profile = ''
              OR (
                   profile_updated_at IS NOT NULL
                   AND profile_updated_at != ''
                   AND date(profile_updated_at, '+' || ? || ' days') < date(?)
                 )
              OR (
                   profile_updated_at IS NOT NULL
                   AND profile_updated_at != ''
                   AND last_seen IS NOT NULL
                   AND last_seen != ''
                   AND last_seen > date(profile_updated_at)
                 )
              )
        ORDER  BY (profile_updated_at IS NULL OR profile_updated_at = '') DESC,
                  profile_updated_at ASC,
                  email_count DESC
        LIMIT  ?
    """
    with store._connect() as db:
        rows = db.execute(sql, (_EMAIL_FLOOR, _STALE_DAYS, now_iso, cap)).fetchall()

    results = []
    for row in rows:
        eid = row["id"]
        role = fetch_role(store, eid, current_only=False)
        relations = _fetch_relations(store, eid)
        results.append({
            "entity_id": eid,
            "name": row["name"],
            "org": row["org"] or "",
            "role": role,
            "relations": relations,
        })
    return results


def _fetch_relations(store, entity_id: str) -> list[str]:
    """Return up to 10 relation strings touching this entity (non-invalidated)."""
    with store._connect() as db:
        rows = db.execute(
            """SELECT entity_a, relation, entity_b
               FROM   entity_relations
               WHERE  (entity_a = ? OR entity_b = ?)
                 AND  (invalidated_at IS NULL OR invalidated_at = '')
               ORDER  BY last_seen DESC, id DESC
               LIMIT  10""",
            (entity_id, entity_id),
        ).fetchall()
    return [f"{r['entity_a']} {r['relation']} {r['entity_b']}" for r in rows]


def drain_profiles(store, inbox_obj: dict) -> dict:
    """Write profiles from a profile_synthesis inbox block.

    inbox_obj must be a dict with key "profile_synthesis" whose value is a
    list of {"entity_id": ..., "profile": ...} dicts. Items with a missing
    entity_id or empty profile are skipped silently. entity_id values that
    don't match any row are also skipped (rowcount == 0).

    Returns {"profiles_written": N}.
    """
    items = inbox_obj.get("profile_synthesis", [])
    if not isinstance(items, list):
        log.warning("profile_synthesis value is not a list; skipping")
        return {"profiles_written": 0}

    written = 0
    now_iso = datetime.now(timezone.utc).isoformat()

    for item in items:
        if not isinstance(item, dict):
            continue
        eid = item.get("entity_id", "")
        profile_text = item.get("profile", "")
        if not eid or not profile_text or not profile_text.strip():
            continue

        with store._connect() as db:
            cur = db.execute(
                "UPDATE entities SET profile=?, profile_updated_at=? WHERE id=?",
                (profile_text.strip(), now_iso, eid),
            )
            if cur.rowcount == 0:
                log.debug("profile_synthesis: entity_id %r not found; skipping", eid)
                continue

        store.record_change(
            "profile_updated",
            ref_id=str(eid),
            summary=f"Profile written for entity {eid}",
            source="profile_synthesis",
        )
        written += 1

    return {"profiles_written": written}


# Register this drainer so drain.py picks it up automatically when this
# module is imported (matches the BLOCK_DRAINERS pattern from Task 5).
# drain.py calls drainer(store, full_inbox_dict) — pass it straight through;
# drain_profiles already extracts the "profile_synthesis" key internally.
def _register():
    try:
        from mcpbrain.drain import BLOCK_DRAINERS  # noqa: PLC0415

        BLOCK_DRAINERS["profile_synthesis"] = drain_profiles
    except ImportError:
        log.debug("drain module not available; profile_synthesis drainer not registered")


_register()
