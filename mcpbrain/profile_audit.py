"""profile_audit block — apply LLM-reviewed corrections to entity profiles.

build_audit_requests(store, *, cap=10):
    Returns a list of dicts describing profiled person entities that can be
    reviewed for corrections. Each dict has keys: entity_id, name, org,
    profile, role.

drain_audit(store, inbox_obj, *, max_corrections=10):
    Consumes {"profile_audit": [...]} from an inbox object. Each item must
    have entity_id and a list of corrections. Only "role" and "org" fields
    are accepted; unknown fields are silently skipped. Writes corrections
    with full supersession and audit trail. Returns {"corrections_applied": N}.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from mcpbrain.graph_write import fetch_role, write_role_observation, _JUNK_ROLE_VALUES

log = logging.getLogger(__name__)

# Minimum email_count for an entity to be eligible for audit.
_EMAIL_FLOOR = 3

# Fields that profile_audit may correct.
_ALLOWED_FIELDS = {"role", "org"}


def build_audit_requests(store, *, cap: int = 10) -> list[dict]:
    """Select profiled person entities suitable for correction review.

    Filters to type='person', email_count >= _EMAIL_FLOOR, non-empty profile, AND
    audit-worthy: never audited, OR the profile changed since the last audit, OR the
    person was re-observed since then. Ordered never-audited + stalest-audited first,
    so the per-cycle cap rotates through everyone instead of re-auditing the same
    most-active profiles every cycle. Returns up to `cap` dicts including:
    entity_id, name, org, profile, role.
    """
    sql = """
        SELECT id, name, org, email_count, profile
        FROM   entities
        WHERE  type = 'person'
          AND  email_count >= ?
          AND  profile IS NOT NULL
          AND  profile != ''
          AND  (
                 profile_audited_at IS NULL
              OR profile_audited_at = ''
              OR (profile_updated_at IS NOT NULL AND profile_updated_at > profile_audited_at)
              OR (last_seen != '' AND last_seen > date(profile_audited_at))
               )
        ORDER  BY (profile_audited_at IS NULL OR profile_audited_at = '') DESC,
                  profile_audited_at ASC,
                  last_seen DESC
        LIMIT  ?
    """
    with store._connect() as db:
        rows = db.execute(sql, (_EMAIL_FLOOR, cap)).fetchall()

    results = []
    for row in rows:
        eid = row["id"]
        role = fetch_role(store, eid, current_only=True)
        results.append({
            "entity_id": eid,
            "name": row["name"],
            "org": row["org"] or "",
            "profile": row["profile"],
            "role": role,
        })
    return results


def drain_audit(store, inbox_obj: dict, *, max_corrections: int = 10) -> dict:
    """Apply profile corrections from a profile_audit inbox block.

    inbox_obj must be a dict with key "profile_audit" whose value is a list
    of {"entity_id": ..., "corrections": [...]} dicts. Each correction must
    have "field" (only "role" and "org" accepted) and "new_value". Unknown
    fields are silently skipped. entity_id values that don't match any row
    are also skipped.

    For "role" corrections: reads the current role for revert_ref, then calls
    write_role_observation to supersede it (rank 4 — profile_audit).
    For "org" corrections: updates entities.org directly, records old value.

    Each applied correction records a change_log row with source="profile_audit"
    and revert_ref="{field}:{old_value}" for undo.

    Returns {"corrections_applied": N}.
    """
    items = inbox_obj.get("profile_audit", [])
    if not isinstance(items, list):
        log.warning("profile_audit value is not a list; skipping")
        return {"corrections_applied": 0}

    applied = 0
    today = datetime.now(timezone.utc).date().isoformat()
    now_iso = datetime.now(timezone.utc).isoformat()

    for item in items:
        if applied >= max_corrections:
            log.info("profile_audit: per-run cap reached")
            break
        if not isinstance(item, dict):
            continue
        eid = item.get("entity_id", "")
        corrections = item.get("corrections", [])
        if not eid or not isinstance(corrections, list):
            continue

        # Verify entity exists.
        with store._connect() as db:
            row = db.execute("SELECT id FROM entities WHERE id=?", (eid,)).fetchone()
        if not row:
            log.debug("profile_audit: entity_id %r not found; skipping", eid)
            continue
        # Mark audited (even when corrections is empty) so it rotates out and only
        # re-audits after a later change — not the same active profiles every cycle.
        with store._connect() as db:
            db.execute("UPDATE entities SET profile_audited_at=? WHERE id=?", (now_iso, eid))

        for correction in corrections:
            if applied >= max_corrections:
                log.info("profile_audit: per-run cap reached")
                break
            if not isinstance(correction, dict):
                continue
            field = correction.get("field", "")
            new_value = correction.get("new_value", "")
            if field not in _ALLOWED_FIELDS:
                log.debug(
                    "profile_audit: skipping unknown field %r for entity %r", field, eid
                )
                continue
            if not new_value:
                continue

            if field == "role":
                if new_value.strip().lower() in _JUNK_ROLE_VALUES:
                    log.debug(
                        "profile_audit: role %r is junk value, skipping", new_value
                    )
                    continue
                old_value = fetch_role(store, eid, current_only=True)
                # write_role_observation uses source="profile_audit" (rank 4)
                # to supersede any existing lower-ranked role.
                write_role_observation(
                    store, eid, new_value, "profile_audit", today, "high"
                )
                store.record_change(
                    "role_corrected",
                    ref_id=str(eid),
                    summary=f"Role corrected to {new_value!r} for entity {eid}",
                    detail=correction.get("evidence", ""),
                    revert_ref=f"role:{old_value}",
                    source="profile_audit",
                )
                applied += 1

            elif field == "org":
                with store._connect() as db:
                    existing = db.execute(
                        "SELECT org FROM entities WHERE id=?", (eid,)
                    ).fetchone()
                    old_org = existing["org"] if existing else ""
                    db.execute(
                        "UPDATE entities SET org=? WHERE id=?", (new_value, eid)
                    )
                store.record_change(
                    "org_corrected",
                    ref_id=str(eid),
                    summary=f"Org corrected to {new_value!r} for entity {eid}",
                    detail=correction.get("evidence", ""),
                    revert_ref=f"org:{old_org}",
                    source="profile_audit",
                )
                applied += 1

    return {"corrections_applied": applied}


def _register():
    try:
        from mcpbrain.drain import BLOCK_DRAINERS  # noqa: PLC0415
        BLOCK_DRAINERS["profile_audit"] = drain_audit
    except ImportError:
        log.debug("drain module not available; profile_audit drainer not registered")


_register()
