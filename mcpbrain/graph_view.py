"""Sigma-shaped knowledge-graph data for the dashboard graph explorer.

graph_canvas() returns {nodes, links, communities} for the entities/relations
graph, filtered to a manageable subset (degree threshold + optional org/type/
community/recency) and capped at 5000 nodes. Read-only; degrades to an empty
payload on any DB error so the page never breaks.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mcpbrain.dashboard import _open_ro

log = logging.getLogger(__name__)

_MAX_NODES = 5000
# Entities with no links have nothing to lay out around, and a store can hold
# thousands: the unlinked view draws the newest this many and says how many
# there are in all (total, truncated) instead of refusing with too_large.
_UNLINKED_CAP = 1500
_EMPTY = {"nodes": [], "links": [], "communities": {}}
# A fact the owner adds is one short value, the limit graph_write applies to
# every observation it writes.
FACT_MAX = 80
ATTRIBUTE_MAX = 40


def graph_canvas(store, *, min_conn: int = 7, org: str = "", community: str = "",
                 types: list[str] | None = None, recency_days: int = 0,
                 max_links: int = 5000, unlinked: bool = False,
                 since_hours: int = 0) -> dict:
    """Return Sigma-shaped {nodes, links, communities} or a too_large marker.

    unlinked: only entities with no live relation on either side (the
    Overview's "entities with no links", console_stats.graph_counts), newest
    first and capped at _UNLINKED_CAP; min_conn does not apply and the payload
    adds {total, truncated}.
    since_hours: only entities that gained a relation or observation from a
    source first seen in that many hours (change_undo.added_since)."""
    path = store._path if hasattr(store, "_path") else store.path

    try:
        db = _open_ro(Path(path))
        try:
            # entity_suppressions is optional: it only exists once the graph-edit
            # (suppress/delete) feature has run, so most stores don't have it yet.
            # Join + filter on it only when present — otherwise the whole read
            # would error and degrade to an empty graph for every such store.
            has_supp = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='entity_suppressions'"
            ).fetchone() is not None
            supp_join = ("LEFT JOIN entity_suppressions s ON s.entity_id = e.id"
                         if has_supp else "")

            # origin (local/org) predates some test/legacy schemas that create
            # entities manually without it — select a literal default rather
            # than erroring the whole read for stores missing the column.
            has_origin = any(row["name"] == "origin"
                             for row in db.execute("PRAGMA table_info(entities)"))
            origin_col = ("COALESCE(e.origin, 'local') AS origin" if has_origin
                         else "'local' AS origin")

            where: list[str] = []
            params: dict = {}
            if not unlinked:
                where.append("COALESCE(e.degree, 0) >= :min_conn")
                params["min_conn"] = int(min_conn)
            if unlinked:
                where.append(_unlinked_sql(db))
            if since_hours and int(since_hours) > 0:
                if not (_table_exists(db, "relation_sources")
                        and _table_exists(db, "observation_sources")):
                    return dict(_EMPTY)
                from mcpbrain.change_undo import ADDED_SINCE_IDS_SQL, added_cutoff
                where.append("e.id IN (" + ADDED_SINCE_IDS_SQL.replace(":cutoff", ":added_cutoff") + ")")
                params["added_cutoff"] = added_cutoff(int(since_hours))
            if has_supp:
                where.append("s.entity_id IS NULL")
            if org:
                where.append("COALESCE(e.org, '') = :org")
                params["org"] = "" if org == "unassigned" else org
            if community:
                try:
                    params["community"] = int(community)
                except (TypeError, ValueError):
                    return dict(_EMPTY)
                where.append("ec.community_id = :community")
            if types:
                # graph_types reports an empty or missing type as 'unknown', so
                # asking for 'unknown' also matches those rows.
                type_expr = ("COALESCE(NULLIF(e.type, ''), 'unknown')" if "unknown" in types
                             else "e.type")
                where.append(f"{type_expr} IN (" + ",".join(f":t{i}" for i in range(len(types))) + ")")
                for i, t in enumerate(types):
                    params[f"t{i}"] = t
            if recency_days and int(recency_days) > 0:
                cutoff = (datetime.now(timezone.utc) - timedelta(days=int(recency_days))).strftime("%Y-%m-%d")
                where.append("COALESCE(e.last_seen, '') >= :cutoff")
                params["cutoff"] = cutoff

            order_limit = (f"ORDER BY COALESCE(e.last_seen, '') DESC, e.name LIMIT {int(_UNLINKED_CAP)}"
                           if unlinked else "LIMIT 5001")
            rows = db.execute(f"""
                SELECT e.id, e.name, e.type, COALESCE(e.org, '') AS org,
                       COALESCE(e.email_count, 0) AS email_count,
                       COALESCE(e.email_addr, '') AS email_addr,
                       COALESCE(e.first_seen, '') AS first_seen,
                       COALESCE(e.last_seen, '') AS last_seen,
                       {origin_col},
                       ec.community_id, cs.title AS community_title,
                       COALESCE(e.degree, 0) AS degree
                FROM entities e
                {supp_join}
                LEFT JOIN (
                    SELECT entity_id, MIN(community_id) AS community_id
                    FROM entity_communities
                    WHERE level = 0
                    GROUP BY entity_id
                ) ec ON ec.entity_id = e.id
                LEFT JOIN community_summaries cs
                       ON cs.community_id = ec.community_id AND cs.level = 0
                WHERE {' AND '.join(where) or '1'}
                {order_limit}
            """, params).fetchall()

            if unlinked:
                total = db.execute(
                    f"SELECT COUNT(*) AS n FROM entities e {supp_join} "
                    f"LEFT JOIN (SELECT entity_id, MIN(community_id) AS community_id "
                    f"FROM entity_communities WHERE level = 0 GROUP BY entity_id) ec "
                    f"ON ec.entity_id = e.id WHERE {' AND '.join(where)}",
                    params).fetchone()["n"]
                out = {"nodes": _node_rows(rows), "links": [],
                       "communities": _community_titles(rows),
                       "total": total, "truncated": total > len(rows)}
                return out

            if len(rows) > _MAX_NODES:
                total = db.execute(f"""
                    SELECT COUNT(*) AS n
                    FROM entities e
                    {supp_join}
                    LEFT JOIN (
                        SELECT entity_id, MIN(community_id) AS community_id
                        FROM entity_communities
                        WHERE level = 0
                        GROUP BY entity_id
                    ) ec ON ec.entity_id = e.id
                    WHERE {' AND '.join(where) or '1'}
                """, params).fetchone()["n"]
                return {"error": "too_large", "cap": _MAX_NODES, "candidate_count": total}

            node_ids = {r["id"] for r in rows}
            nodes = _node_rows(rows)

            link_cap = max(100, min(50000, int(max_links)))
            links = []
            rel_where, rel_order = _rel_filters(db)
            for e in db.execute(
                "SELECT entity_a AS source, entity_b AS target, "
                "COALESCE(relation, '') AS relation, COALESCE(strength, 1) AS strength "
                f"FROM entity_relations WHERE COALESCE(strength, 0) > 0{rel_where} "
                f"ORDER BY {rel_order}strength DESC"
            ):
                if e["source"] in node_ids and e["target"] in node_ids:
                    links.append({"source": e["source"], "target": e["target"],
                                  "relation": e["relation"], "strength": e["strength"]})
                    if len(links) >= link_cap:
                        break

            return {"nodes": nodes, "links": links, "communities": _community_titles(rows)}
        finally:
            db.close()
    except sqlite3.Error as exc:
        log.warning("graph_canvas: read failed (%s) — returning empty", exc)
        return dict(_EMPTY)


def _node_rows(rows) -> list[dict]:
    return [{
        "id": r["id"], "name": r["name"], "type": r["type"] or "person",
        "org": r["org"], "email_count": r["email_count"],
        "email_addr": r["email_addr"], "connections": r["degree"],
        "community": r["community_id"],
        "first_seen": r["first_seen"], "last_seen": r["last_seen"],
        "origin": r["origin"],
    } for r in rows]


def _community_titles(rows) -> dict:
    communities: dict = {}
    for r in rows:
        cid = r["community_id"]
        if cid is not None and str(cid) not in communities:
            communities[str(cid)] = r["community_title"] or f"Community {cid}"
    return communities


def _unlinked_sql(db) -> str:
    """WHERE clause for "no live relation on either side", the definition
    console_stats.graph_counts uses for the Overview's unlinked count."""
    if not _table_exists(db, "entity_relations"):
        return "1"
    cols = {r["name"] for r in db.execute("PRAGMA table_info(entity_relations)")}
    live = "AND r.invalidated_at IS NULL" if "invalidated_at" in cols else ""
    return (f"NOT EXISTS (SELECT 1 FROM entity_relations r WHERE r.entity_a = e.id {live}) "
            f"AND NOT EXISTS (SELECT 1 FROM entity_relations r WHERE r.entity_b = e.id {live})")


def graph_types(store) -> list[dict]:
    """Every entity type present with its count, largest first:
    [{type, count}]. Counts every entity as console_stats.graph_counts does
    (the Overview's numbers), so a kind's figure there matches the graph's."""
    path = store._path if hasattr(store, "_path") else store.path
    try:
        db = _open_ro(Path(path))
        try:
            if not _table_exists(db, "entities"):
                return []
            rows = db.execute(
                "SELECT COALESCE(NULLIF(type, ''), 'unknown') AS type, COUNT(*) AS n "
                "FROM entities GROUP BY 1 ORDER BY n DESC, type").fetchall()
            return [{"type": r["type"], "count": int(r["n"])} for r in rows]
        finally:
            db.close()
    except sqlite3.Error as exc:
        log.warning("graph_types: read failed (%s)", exc)
        return []


def hidden_count(store) -> int:
    """How many entities are hidden from the graph (a suppression whose
    entity still exists, as list_suppressed lists them)."""
    path = store._path if hasattr(store, "_path") else store.path
    try:
        db = _open_ro(Path(path))
        try:
            if not _table_exists(db, "entity_suppressions"):
                return 0
            return int(db.execute(
                "SELECT COUNT(*) FROM entity_suppressions s "
                "JOIN entities e ON e.id = s.entity_id").fetchone()[0])
        finally:
            db.close()
    except sqlite3.Error as exc:
        log.warning("hidden_count: read failed (%s)", exc)
        return 0


def _rel_filters(db, alias: str = "") -> tuple[str, str]:
    """(extra WHERE, ORDER prefix) for strength reads of entity_relations.

    Strength counts independent primary sources (spec 2026-10-22 §3): a
    derived=1 row (written from mcpbrain's own notes/briefs/drafts) is no
    evidence and is excluded. A LEGACY row — no EVIDENCE-class relation_sources
    row, i.e. its strength predates source counting (+1 per sighting) — never
    outranks a source-counted one. legacy_strength is only the saved prior
    value (ruling I8): a legacy row that gains a real source is source-counted.
    An origin='org' row is never legacy (M9). Invalidated (superseded) rows
    are excluded too. Column/table aware: a store that predates them reads as before."""
    from mcpbrain.graph_write import legacy_relation_sql
    cols = {r["name"] for r in db.execute("PRAGMA table_info(entity_relations)")}
    col = alias + "." if alias else "entity_relations."
    where = f" AND COALESCE({col}derived, 0) = 0" if "derived" in cols else ""
    # Live links only: a superseded relation (an old employer) is history the
    # drawer doesn't count, so no map or relation view draws it either.
    if "invalidated_at" in cols:
        where += f" AND {col}invalidated_at IS NULL"
    order = ""
    if "id" in cols and _table_exists(db, "relation_sources"):
        order = f"{legacy_relation_sql(alias, has_origin='origin' in cols)}, "
    return where, order


def _table_exists(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                      (name,)).fetchone() is not None


# The relation filter's word for "links with no relation name" (stored as ''
# or NULL): an empty query value would read as "no filter".
EMPTY_RELATION = "_none"


def graph_ego(store, entity_id: str, hops: int = 1, relation: str = "") -> dict | None:
    """One entity plus its N-hop neighbourhood, ignoring the min-connections/org/
    type filters entirely. Used to jump to a search hit that graph_canvas()'s
    degree floor would otherwise hide from the map. relation, when given,
    follows and returns only links of that relation (the drawer's "works_at 12"
    group opened in the graph); EMPTY_RELATION follows the unnamed ones.
    Returns None if the entity is unknown or suppressed."""
    hops = max(0, min(3, int(hops)))
    path = store._path if hasattr(store, "_path") else store.path
    try:
        db = _open_ro(Path(path))
        try:
            has_supp = _table_exists(db, "entity_suppressions")
            if has_supp and db.execute(
                "SELECT 1 FROM entity_suppressions WHERE entity_id=?", (entity_id,)
            ).fetchone():
                return None
            if db.execute("SELECT 1 FROM entities WHERE id=?", (entity_id,)).fetchone() is None:
                return None

            rel_where, _ = _rel_filters(db)
            rel_params: tuple = ()
            if relation == EMPTY_RELATION:
                rel_where += " AND COALESCE(relation, '') = ''"
            elif relation:
                rel_where += " AND relation = ?"
                rel_params = (relation,)
            seen = {entity_id}
            frontier = {entity_id}
            for _ in range(hops):
                if not frontier or len(seen) >= _MAX_NODES:
                    break
                qmarks = ",".join("?" * len(frontier))
                rows = db.execute(
                    f"SELECT entity_a, entity_b FROM entity_relations "
                    f"WHERE COALESCE(strength, 0) > 0{rel_where} "
                    f"AND (entity_a IN ({qmarks}) OR entity_b IN ({qmarks}))",
                    (*rel_params, *frontier, *frontier)).fetchall()
                nxt = {other for r in rows for other in (r["entity_a"], r["entity_b"])
                       if other not in seen}
                seen |= nxt
                frontier = nxt
            node_ids = list(seen)[:_MAX_NODES]

            has_origin = any(row["name"] == "origin" for row in db.execute("PRAGMA table_info(entities)"))
            origin_col = ("COALESCE(e.origin, 'local') AS origin" if has_origin
                         else "'local' AS origin")
            supp_join = "LEFT JOIN entity_suppressions s ON s.entity_id = e.id" if has_supp else ""
            where_supp = "AND s.entity_id IS NULL" if has_supp else ""
            idmarks = ",".join("?" * len(node_ids))
            rows = db.execute(f"""
                SELECT e.id, e.name, e.type, COALESCE(e.org, '') AS org,
                       COALESCE(e.email_count, 0) AS email_count,
                       COALESCE(e.email_addr, '') AS email_addr,
                       COALESCE(e.first_seen, '') AS first_seen,
                       COALESCE(e.last_seen, '') AS last_seen,
                       {origin_col},
                       ec.community_id, cs.title AS community_title,
                       COALESCE(e.degree, 0) AS degree
                FROM entities e
                {supp_join}
                LEFT JOIN (
                    SELECT entity_id, MIN(community_id) AS community_id
                    FROM entity_communities
                    WHERE level = 0
                    GROUP BY entity_id
                ) ec ON ec.entity_id = e.id
                LEFT JOIN community_summaries cs
                       ON cs.community_id = ec.community_id AND cs.level = 0
                WHERE e.id IN ({idmarks}) {where_supp}
            """, node_ids).fetchall()

            found_ids = {r["id"] for r in rows}
            nodes = _node_rows(rows)
            if entity_id not in found_ids:
                return None  # existed above, but excluded here (e.g. race with a suppress)

            links = []
            for r in db.execute(
                "SELECT entity_a AS source, entity_b AS target, "
                "COALESCE(relation, '') AS relation, COALESCE(strength, 1) AS strength "
                f"FROM entity_relations WHERE COALESCE(strength, 0) > 0{rel_where}",
                rel_params
            ):
                if r["source"] in found_ids and r["target"] in found_ids:
                    links.append({"source": r["source"], "target": r["target"],
                                  "relation": r["relation"], "strength": r["strength"]})

            return {"nodes": nodes, "links": links, "communities": _community_titles(rows)}
        finally:
            db.close()
    except sqlite3.Error as exc:
        log.warning("graph_ego: read failed (%s)", exc)
        return None


def entity_detail(store, entity_id: str) -> dict | None:
    """Full drawer payload for one entity, or None if unknown/suppressed."""
    path = store._path if hasattr(store, "_path") else store.path
    try:
        db = _open_ro(Path(path))
        try:
            if _table_exists(db, "entity_suppressions"):
                if db.execute("SELECT 1 FROM entity_suppressions WHERE entity_id=?",
                              (entity_id,)).fetchone():
                    return None
            e = db.execute("SELECT * FROM entities WHERE id=?", (entity_id,)).fetchone()
            if e is None:
                return None
            rels, backs = [], []
            rel_where, rel_order = _rel_filters(db, "r")
            rows = db.execute(
                "SELECT r.entity_a, r.relation, r.entity_b, COALESCE(r.strength,1) AS strength, "
                "       ea.name AS a_name, eb.name AS b_name "
                "FROM entity_relations r "
                "LEFT JOIN entities ea ON ea.id=r.entity_a "
                "LEFT JOIN entities eb ON eb.id=r.entity_b "
                f"WHERE (r.entity_a=:id OR r.entity_b=:id) AND r.invalidated_at IS NULL{rel_where} "
                f"ORDER BY {rel_order}r.strength DESC",
                {"id": entity_id}).fetchall()
            for r in rows:
                if r["entity_a"] == entity_id:
                    rels.append({"other_id": r["entity_b"], "other_name": r["b_name"] or r["entity_b"],
                                 "relation": r["relation"], "strength": r["strength"], "direction": "out"})
                else:
                    backs.append({"other_id": r["entity_a"], "other_name": r["a_name"] or r["entity_a"],
                                  "relation": r["relation"], "strength": r["strength"], "direction": "in"})
            obs = []
            if _table_exists(db, "entity_observations"):
                eo_cols = {r["name"] for r in db.execute("PRAGMA table_info(entity_observations)")}
                count_col = "COALESCE(observed_count,1) AS observed_count" if "observed_count" in eo_cols else "1 AS observed_count"
                last_col = "last_seen" if "last_seen" in eo_cols else "NULL AS last_seen"
                obs_derived = " AND COALESCE(derived, 0) = 0" if "derived" in eo_cols else ""
                obs = [dict(o) for o in db.execute(
                    f"SELECT attribute, value, valid_from, valid_to, source, {count_col}, {last_col} "
                    f"FROM entity_observations WHERE entity_id=?{obs_derived} "
                    "ORDER BY COALESCE(valid_from,'') DESC, id DESC", (entity_id,)).fetchall()]
            return {
                "id": e["id"], "name": e["name"], "type": e["type"],
                "org": e["org"] or "", "email_addr": e["email_addr"] or "",
                "aliases": e["aliases"] or "", "notes": e["notes"] or "",
                "connections": e["degree"] or 0,
                "relations": rels, "backlinks": backs, "observations": obs,
            }
        finally:
            db.close()
    except sqlite3.OperationalError as exc:
        log.warning("entity_detail: read failed (%s)", exc)
        return None


def search_entities(store, q: str, limit: int = 10) -> list[dict]:
    """Ranked entity search for the graph search box + merge type-ahead.

    Matches on name AND aliases (so a merged-away name still finds its survivor),
    ranked exact > name-prefix > name-contains > alias-only, then by connectivity
    (degree) so the most important match surfaces first. Returns degree and a
    `via_alias` flag for display. [] on blank q; excludes suppressed."""
    q = (q or "").strip()
    if not q:
        return []
    path = store._path if hasattr(store, "_path") else store.path
    try:
        db = _open_ro(Path(path))
        try:
            ent_cols = {r["name"] for r in db.execute("PRAGMA table_info(entities)")}
            alias_col = "COALESCE(e.aliases,'')" if "aliases" in ent_cols else "''"
            degree_col = "COALESCE(e.degree,0)" if "degree" in ent_cols else "0"
            supp = ("LEFT JOIN entity_suppressions s ON s.entity_id = e.id"
                    if _table_exists(db, "entity_suppressions") else "")
            where_supp = "AND s.entity_id IS NULL" if supp else ""
            # Escape LIKE wildcards so a query containing % or _ matches literally.
            qesc = q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            rows = db.execute(
                f"SELECT e.id, e.name, e.type, COALESCE(e.org,'') AS org, "
                f"       {degree_col} AS degree, "
                f"       (lower(e.name) NOT LIKE :contains ESCAPE '\\') AS via_alias, "
                f"       CASE WHEN lower(e.name) = lower(:exact) THEN 0 "
                f"            WHEN lower(e.name) LIKE :prefix ESCAPE '\\' THEN 1 "
                f"            WHEN lower(e.name) LIKE :contains ESCAPE '\\' THEN 2 "
                f"            ELSE 3 END AS rank "
                f"FROM entities e {supp} "
                f"WHERE (lower(e.name) LIKE :contains ESCAPE '\\' "
                f"       OR lower({alias_col}) LIKE :contains ESCAPE '\\') {where_supp} "
                f"ORDER BY rank ASC, degree DESC, length(e.name) ASC "
                f"LIMIT :lim",
                {"contains": f"%{qesc}%", "prefix": f"{qesc}%", "exact": q,
                 "lim": int(limit)}).fetchall()
            return [{"id": r["id"], "name": r["name"], "type": r["type"], "org": r["org"],
                     "degree": r["degree"], "via_alias": bool(r["via_alias"])} for r in rows]
        finally:
            db.close()
    except sqlite3.OperationalError as exc:
        log.warning("search_entities: read failed (%s)", exc)
        return []


def update_entity(store, entity_id: str, *, name=None, org=None,
                  email_addr=None, notes=None) -> dict | None:
    """Correct existing fields on an entity. Returns the fresh detail, or None
    if the entity is unknown. Only provided (non-None) fields are written."""
    if store.get_entity(entity_id) is None:
        return None
    changed = []
    if name is not None and name.strip():
        store.rename_entity(entity_id, name, user=True); changed.append("name")
    if org is not None:
        store.update_entity_org(entity_id, org, user=True); changed.append("org")
    if email_addr is not None:
        store.set_entity_email(entity_id, email_addr, user=True); changed.append("email")
    if notes is not None:
        store.set_entity_notes(entity_id, notes); changed.append("notes")
    if changed:
        store.record_change("entity_edited", ref_id=entity_id,
                            summary=f"edited {', '.join(changed)}")
    return entity_detail(store, entity_id)


def _merge_score(entity: dict) -> tuple:
    """Connectivity rank for choosing a merge survivor: most relations wins,
    ties broken by mention count. Mirrors ops-brain's degree-first heuristic."""
    return (entity.get("degree") or 0, entity.get("mentions") or 0)


def _is_proper_name(name: str) -> int:
    return 1 if " " in (name or "").strip() else 0


def _best_name(winner: dict, loser: dict) -> str:
    """Pick the better display name across the two entities, independent of which
    id survives: prefer a 'proper' full name (has a space), then the longer one,
    tie to the winner's. So merging never downgrades 'Dana Okafor' to 'D.O.'."""
    wn, ln = (winner.get("name") or "").strip(), (loser.get("name") or "").strip()
    if not wn:
        return ln
    if not ln:
        return wn
    if _is_proper_name(ln) != _is_proper_name(wn):
        return ln if _is_proper_name(ln) > _is_proper_name(wn) else wn
    return ln if len(ln) > len(wn) else wn


def _best_email(winner: dict, loser: dict) -> str:
    """Prefer a real (non-empty, non-role) address; the winner's if both real."""
    from mcpbrain.resolve import is_role_address
    we, le = (winner.get("email_addr") or "").strip(), (loser.get("email_addr") or "").strip()
    real = lambda e: bool(e) and not is_role_address(e)
    if real(we):
        return we
    if real(le):
        return le
    return we or le


def _merge_notes(winner: dict, loser: dict) -> str:
    """Union of both entities' note lines (winner first), de-duped, nothing lost."""
    out, seen = [], set()
    for src in (winner.get("notes") or "", loser.get("notes") or ""):
        for line in src.split("\n"):
            key = line.strip().lower()
            if key and key not in seen:
                seen.add(key)
                out.append(line.rstrip())
    return "\n".join(out)


def _merge_result(winner: dict, loser: dict, name_override: str | None = None) -> dict:
    """Field-level best-of result of merging loser into winner. Each field is
    taken from whichever side has the better value, not blindly from the winner."""
    org = winner.get("org") or ""
    if org.strip().lower() in ("", "unknown"):
        org = loser.get("org") or org
    return {
        "name": (name_override or "").strip() or _best_name(winner, loser),
        "type": winner.get("type") or loser.get("type") or "person",
        "org": org,
        "email_addr": _best_email(winner, loser),
        "notes": _merge_notes(winner, loser),
    }


def _orient(store, loser_id: str, winner_id: str):
    """Shared guard + connectivity orientation for merge/preview. Returns
    (winner, loser) dicts, or a dict with an error for the caller to return."""
    from mcpbrain.resolve import is_role_address
    if loser_id == winner_id:
        return {"ok": False, "error": "self_merge",
                "message": "Can't merge an entity into itself."}
    loser, winner = store.get_entity(loser_id), store.get_entity(winner_id)
    if loser is None or winner is None:
        return {"ok": False, "error": "not_found", "message": "Entity not found."}
    if is_role_address(loser.get("email_addr", "")) or is_role_address(winner.get("email_addr", "")):
        return {"ok": False, "error": "role_inbox",
                "message": "One of these is keyed on a shared inbox (such as office@), "
                           "so merging could fuse different people. Not merged."}
    if store.is_distinct_pair(loser_id, winner_id):
        return {"ok": False, "error": "marked_distinct",
                "message": "These were marked as different entities. Undo that correction first."}
    # Keep the caller's winner unless the loser is STRICTLY more connected.
    if _merge_score(loser) > _merge_score(winner):
        winner, loser = loser, winner
    return winner, loser


def merge_preview(store, loser_id: str, winner_id: str) -> dict:
    """Dry-run a merge: which entity survives + the field-level best-of result,
    without mutating anything. Lets the UI show the outcome before confirming."""
    oriented = _orient(store, loser_id, winner_id)
    if isinstance(oriented, dict):
        return oriented
    winner, loser = oriented
    return {"ok": True,
            "winner_id": winner["id"], "winner_name": winner["name"],
            "winner_conn": winner.get("degree") or 0,
            "loser_id": loser["id"], "loser_name": loser["name"],
            "loser_conn": loser.get("degree") or 0,
            "result": _merge_result(winner, loser)}


def merge_entities(store, loser_id: str, winner_id: str, name_override: str | None = None) -> dict:
    """Merge two entities, guarded. Refuses self-merge and role-inbox pairs.

    The survivor is oriented by connectivity (the more-connected node keeps its
    id + relations), but every FIELD is reconciled best-of across both sides —
    the better name, a real email over a role/blank one, unioned notes — so
    nothing worth keeping is dropped. `name_override` lets the user set the final
    name from the merge preview. Returns the surviving id + resolved fields.
    """
    oriented = _orient(store, loser_id, winner_id)
    if isinstance(oriented, dict):
        return oriented
    winner, loser = oriented
    result = _merge_result(winner, loser, name_override)
    store.merge_entities(loser["id"], winner["id"], canonical_name=result["name"])
    # store.merge_entities reconciles name/org/aliases/mentions but not email or
    # notes — apply the best-of values for those explicitly onto the survivor.
    if result["email_addr"] and result["email_addr"] != (winner.get("email_addr") or ""):
        store.set_entity_email(winner["id"], result["email_addr"])
    if result["notes"] and result["notes"] != (winner.get("notes") or ""):
        store.set_entity_notes(winner["id"], result["notes"])
    store.record_change("entity_merged", ref_id=winner["id"],
                        summary=f"merged {loser['id']} into {winner['id']}")
    return {"ok": True, "winner_id": winner["id"], "winner_name": result["name"],
            "loser_id": loser["id"], "loser_name": loser["name"], "result": result}


def suppress_entity(store, entity_id: str) -> dict:
    """Soft-delete (reversible) an entity. The change id comes back so the page
    can offer Undo in place (change_undo undoes entity_suppressed rows)."""
    ok = store.suppress_entity(entity_id, reason="graph-ui")
    if not ok:
        return {"ok": False}
    ent = store.get_entity(entity_id) or {}
    change_id = store.record_change(
        "entity_suppressed", ref_id=entity_id, source="dashboard",
        summary=f"Hid {ent.get('name') or entity_id} from the graph")
    return {"ok": True, "change_id": change_id}


def add_fact(store, entity_id: str, attribute, value) -> dict:
    """Record a fact the owner states about an entity, as an undoable change.

    {"ok": True, "change_id": N, "entity": <entity_detail>} on success;
    {"ok": False, "error": "invalid", "reason": ...} for an empty attribute or
    value, or one too long; {"ok": False, "error": "not_found"} for an unknown
    entity; {"ok": False, "error": "hidden", "reason": ...} for a hidden one.
    Written source='manual' with high confidence and superseding the current
    manual fact of the same attribute, like a role set through
    graph_corrections, so entity_detail shows it."""
    attribute = " ".join(str(attribute or "").split())
    value = " ".join(str(value or "").split())
    if not attribute:
        return {"ok": False, "error": "invalid", "reason": "Say what the fact is about."}
    if len(attribute) > ATTRIBUTE_MAX:
        return {"ok": False, "error": "invalid",
                "reason": f"Keep what it is about to {ATTRIBUTE_MAX} characters."}
    if not value:
        return {"ok": False, "error": "invalid", "reason": "Write the fact."}
    if len(value) > FACT_MAX:
        return {"ok": False, "error": "invalid",
                "reason": f"Keep the fact to {FACT_MAX} characters or fewer."}
    ent = store.get_entity(entity_id)
    if ent is None:
        return {"ok": False, "error": "not_found"}
    change = {"change_type": "fact_added", "source": "dashboard",
              "summary": f"Added a fact to {ent.get('name') or entity_id}: {attribute} {value}"}
    from mcpbrain.store import EntityHidden
    try:
        if store.add_manual_observation(entity_id, attribute, value, change=change) is None:
            return {"ok": False, "error": "not_found"}
    except EntityHidden:
        return {"ok": False, "error": "hidden",
                "reason": "It is hidden from the graph. Restore it in Settings first."}
    try:
        from mcpbrain import entity_resource
        entity_resource.invalidate_cache()
    except Exception:  # noqa: BLE001 -- the write itself already committed
        pass
    return {"ok": True, "change_id": change.get("change_log_id"),
            "entity": entity_detail(store, entity_id)}


def list_suppressed(store) -> list[dict]:
    """Every hidden entity, newest first: {id, name, type, suppressed_at, reason}.

    Only suppressions whose entity still exists: an org import or merge can
    delete the entity row and leave its suppression behind, and that row has
    nothing to restore, so listing it would offer a Restore that can't work."""
    with store._connect() as db:
        rows = db.execute(
            "SELECT s.entity_id AS id, COALESCE(NULLIF(e.name, ''), s.entity_id) AS name, "
            "COALESCE(e.type, '') AS type, COALESCE(s.suppressed_at, '') AS suppressed_at, "
            "COALESCE(s.reason, '') AS reason "
            "FROM entity_suppressions s JOIN entities e ON e.id = s.entity_id "
            "ORDER BY s.suppressed_at DESC, s.entity_id").fetchall()
    return [dict(r) for r in rows]


def restore_entity(store, entity_id: str) -> dict:
    """Bring a hidden entity back into the graph, as an undoable change.

    {"ok": True, "change_id": N} on success; {"ok": False, "error": "not_found"}
    for an unknown entity, {"ok": False, "error": "not_hidden"} when it is not
    hidden. The change_log row (entity_unsuppressed) records the suppression it
    removed, so undo hides the entity again with the same reason."""
    ent = store.get_entity(entity_id)
    if ent is None:
        return {"ok": False, "error": "not_found"}
    name = ent.get("name") or entity_id
    change = {"change_type": "entity_unsuppressed", "source": "dashboard",
              "summary": f"Restored {name} to the graph"}
    if not store.unsuppress_entity(entity_id, change=change):
        return {"ok": False, "error": "not_hidden"}
    try:
        from mcpbrain import entity_resource
        entity_resource.invalidate_cache()
    except Exception:  # noqa: BLE001 -- the restore itself already committed
        pass
    return {"ok": True, "change_id": change.get("change_log_id")}
