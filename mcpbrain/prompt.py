"""Context-block builders for the enrichment prepare step (Phase 1, Task 4+6).

prepare._build_context puts each list returned here verbatim into pending.json's
`context` block, under keys `projects`, `areas`, and `known_people`. The LLM
consumes them per mcpbrain/enrich_prompt.md:

  - `projects` / `areas` are the valid `project_id` / `area_id` sets. The `id`
    is the only load-bearing field; name/org/status give the model enough
    context to match a thread against the right id.
  - `known_people` entries carry a confirmed `org` and `role`. The LLM is told
    to trust them and not re-derive a person's org or role.

The Claude-authored prompt (Task 4.1's old build_thread_prompt) is superseded:
the static instructions live in enrich_prompt.md, and this module supplies only
the context data those instructions reference.

Ported and adapted from src/enrich_gmail.py (_build_known_context_block,
_build_active_projects_block, _build_active_areas_block). Differences from
Nexus: reads through store._connect() rather than memory_db; areas have a
`description` column directly (no hierarchy module); role observations use the
Spec-7 bitemporal shape (current role = valid_to IS NULL), ranked by
graph_write._source_rank.
"""

from mcpbrain.graph_write import (
    _JUNK_ROLE_VALUES,
    _SOURCE_RANK,
    OwnerIdentity,
    _is_owner as _gw_is_owner,
    owner_identity_from_config,
)


# A SQL CASE that mirrors graph_write._source_rank ordering, so the role chosen
# here matches the source the write path treats as authoritative. Highest rank
# (most authoritative) wins, so the subquery orders by this DESC. Sources absent
# from the map fall through to 0 via the ELSE, matching _source_rank's default.
_ROLE_SOURCE_CASE = (
    "CASE "
    + " ".join(f"WHEN eo.source = '{src}' THEN {rank}" for src, rank in _SOURCE_RANK.items())
    + " ELSE 0 END"
)

# Confirmed-org threshold for the global core (enrich_gmail.py:605 used 5).
_CORE_EMAIL_COUNT_MIN = 5


def _is_install_owner(entity_id: str, name: str, owner: OwnerIdentity) -> bool:
    """The install owner is never a known_people entry. Matches the owner's
    entity slug or name, mirroring graph_write's owner exclusion."""
    return entity_id == owner.entity_id or _gw_is_owner(name, owner)


def _clean_role(value):
    """Drop junk/contextual roles so they don't masquerade as job titles."""
    if value and value.strip().lower() not in _JUNK_ROLE_VALUES:
        return value
    return None


def read_projects(store) -> list[dict]:
    """Active projects for the context block.

    Active = archived_at IS NULL. Shape per entry:
        {"id", "name", "org" (org_tag, '' if unset),
         "status_line" (truncated to 120 chars, '' if unset)}.
    The `id` is the valid project_id the LLM may attach to an action.
    """
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT id, name, org_tag, status_line FROM projects "
            "WHERE archived_at IS NULL ORDER BY id"
        ).fetchall()
    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "name": r["name"],
            "org": r["org_tag"] or "",
            "status_line": (r["status_line"] or "")[:120],
        })
    return out


def read_areas(store) -> list[dict]:
    """Active areas for the context block.

    Active = active=1 AND archived_at IS NULL. Shape per entry:
        {"id", "name", "org" (org_id), "description" ('' if unset)}.
    The `id` is the valid area_id the LLM may attach to an action.
    """
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT id, org_id, name, description FROM areas "
            "WHERE active = 1 AND archived_at IS NULL ORDER BY id"
        ).fetchall()
    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "name": r["name"],
            "org": r["org_id"] or "",
            "description": r["description"] or "",
        })
    return out


def build_known_people(store, *, batch_thread_ids, core_cap=40, owner=None) -> list[dict]:
    """People whose org and role are confirmed, for the context block.

    Two sources, unioned and deduped by entity id, with the install owner
    excluded (owner=None resolves from config):

      1. Global core (port of enrich_gmail.py:588-610): person entities with
         email_count >= 5, a confirmed org (org NOT IN ('','unknown')), and a
         current role (an entity_observations row, attribute 'role',
         valid_to IS NULL, length 3..70, highest-ranked source). Ordered by
         email_count desc, capped at core_cap.
      2. Batch overlay: any person linked to a thread in batch_thread_ids,
         via email_context (thread_id) -> message_id -> email_entities ->
         entity_id. These appear even without a confirmed org/role so the LLM
         sees the people actually in this batch.

    Shape per entry: {"id", "name", "org", "role"}. `role` is the cleaned
    current role or None; `org` may be '' for batch-overlay people. The LLM
    treats present org/role as confirmed (enrich_prompt.md).
    """
    if owner is None:
        owner = owner_identity_from_config()
    # SQL pre-filter on the short name; _add re-checks with the full alias set.
    owner_like = f"%{owner.name.lower()}%"

    seen: set[str] = set()
    out: list[dict] = []

    def _add(entity_id, name, org, role):
        if entity_id in seen or _is_install_owner(entity_id, name, owner):
            return
        seen.add(entity_id)
        out.append({
            "id": entity_id,
            "name": name,
            "org": org or "",
            "role": _clean_role(role),
        })

    with store._connect() as conn:
        core_rows = conn.execute(
            f"""
            SELECT e.id, e.name, e.org,
                   (SELECT eo.value FROM entity_observations eo
                    WHERE eo.entity_id = e.id
                      AND eo.attribute = 'role'
                      AND eo.valid_to IS NULL
                      AND eo.invalidated_at IS NULL
                      AND length(eo.value) BETWEEN 3 AND 70
                    ORDER BY {_ROLE_SOURCE_CASE} DESC, eo.confidence DESC
                    LIMIT 1) AS best_role
            FROM entities e
            WHERE e.type = 'person'
              AND e.email_count >= ?
              AND e.org NOT IN ('', 'unknown')
              AND lower(e.name) NOT LIKE ?
            ORDER BY e.email_count DESC
            LIMIT ?
            """,
            (_CORE_EMAIL_COUNT_MIN, owner_like, core_cap),
        ).fetchall()
        for r in core_rows:
            _add(r["id"], r["name"], r["org"], r["best_role"])

        if batch_thread_ids:
            placeholders = ",".join("?" for _ in batch_thread_ids)
            batch_rows = conn.execute(
                f"""
                SELECT DISTINCT e.id, e.name, e.org,
                       (SELECT eo.value FROM entity_observations eo
                        WHERE eo.entity_id = e.id
                          AND eo.attribute = 'role'
                          AND eo.valid_to IS NULL
                          AND eo.invalidated_at IS NULL
                          AND length(eo.value) BETWEEN 3 AND 70
                        ORDER BY {_ROLE_SOURCE_CASE} DESC, eo.confidence DESC
                        LIMIT 1) AS best_role
                FROM email_context ec
                JOIN email_entities ee ON ee.message_id = ec.message_id
                JOIN entities e ON e.id = ee.entity_id
                WHERE ec.thread_id IN ({placeholders})
                  AND e.type = 'person'
                  AND e.id != ?
                  AND lower(e.name) NOT LIKE ?
                """,
                list(batch_thread_ids) + [owner.entity_id, owner_like],
            ).fetchall()
            for r in batch_rows:
                _add(r["id"], r["name"], r["org"], r["best_role"])

    return out
