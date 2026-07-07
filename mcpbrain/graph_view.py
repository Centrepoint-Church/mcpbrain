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
_EMPTY = {"nodes": [], "links": [], "communities": {}}


def graph_canvas(store, *, min_conn: int = 7, org: str = "", community: str = "",
                 types: list[str] | None = None, recency_days: int = 0,
                 max_links: int = 5000) -> dict:
    """Return Sigma-shaped {nodes, links, communities} or a too_large marker."""
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

            where = ["COALESCE(e.degree, 0) >= :min_conn"]
            params: dict = {"min_conn": int(min_conn)}
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
                where.append("e.type IN (" + ",".join(f":t{i}" for i in range(len(types))) + ")")
                for i, t in enumerate(types):
                    params[f"t{i}"] = t
            if recency_days and int(recency_days) > 0:
                cutoff = (datetime.now(timezone.utc) - timedelta(days=int(recency_days))).strftime("%Y-%m-%d")
                where.append("COALESCE(e.last_seen, '') >= :cutoff")
                params["cutoff"] = cutoff

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
                WHERE {' AND '.join(where)}
                LIMIT 5001
            """, params).fetchall()

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
                    WHERE {' AND '.join(where)}
                """, params).fetchone()["n"]
                return {"error": "too_large", "cap": _MAX_NODES, "candidate_count": total}

            node_ids = {r["id"] for r in rows}
            nodes = [{
                "id": r["id"], "name": r["name"], "type": r["type"] or "person",
                "org": r["org"], "email_count": r["email_count"],
                "email_addr": r["email_addr"], "connections": r["degree"],
                "community": r["community_id"],
                "first_seen": r["first_seen"], "last_seen": r["last_seen"],
                "origin": r["origin"],
            } for r in rows]

            link_cap = max(100, min(50000, int(max_links)))
            links = []
            for e in db.execute(
                "SELECT entity_a AS source, entity_b AS target, "
                "COALESCE(relation, '') AS relation, COALESCE(strength, 1) AS strength "
                "FROM entity_relations WHERE COALESCE(strength, 0) > 0 "
                "ORDER BY strength DESC"
            ):
                if e["source"] in node_ids and e["target"] in node_ids:
                    links.append({"source": e["source"], "target": e["target"],
                                  "relation": e["relation"], "strength": e["strength"]})
                    if len(links) >= link_cap:
                        break

            communities: dict = {}
            for r in rows:
                cid = r["community_id"]
                if cid is not None and str(cid) not in communities:
                    communities[str(cid)] = r["community_title"] or f"Community {cid}"

            return {"nodes": nodes, "links": links, "communities": communities}
        finally:
            db.close()
    except sqlite3.Error as exc:
        log.warning("graph_canvas: read failed (%s) — returning empty", exc)
        return dict(_EMPTY)


def _table_exists(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                      (name,)).fetchone() is not None


def graph_ego(store, entity_id: str, hops: int = 1) -> dict | None:
    """One entity plus its N-hop neighbourhood, ignoring the min-connections/org/
    type filters entirely. Used to jump to a search hit that graph_canvas()'s
    degree floor would otherwise hide from the map. Returns None if the entity
    is unknown or suppressed."""
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

            seen = {entity_id}
            frontier = {entity_id}
            for _ in range(hops):
                if not frontier or len(seen) >= _MAX_NODES:
                    break
                qmarks = ",".join("?" * len(frontier))
                rows = db.execute(
                    f"SELECT entity_a, entity_b FROM entity_relations "
                    f"WHERE COALESCE(strength, 0) > 0 "
                    f"AND (entity_a IN ({qmarks}) OR entity_b IN ({qmarks}))",
                    (*frontier, *frontier)).fetchall()
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
            nodes = [{
                "id": r["id"], "name": r["name"], "type": r["type"] or "person",
                "org": r["org"], "email_count": r["email_count"],
                "email_addr": r["email_addr"], "connections": r["degree"],
                "community": r["community_id"],
                "first_seen": r["first_seen"], "last_seen": r["last_seen"],
                "origin": r["origin"],
            } for r in rows]
            if entity_id not in found_ids:
                return None  # existed above, but excluded here (e.g. race with a suppress)

            links = []
            for r in db.execute(
                "SELECT entity_a AS source, entity_b AS target, "
                "COALESCE(relation, '') AS relation, COALESCE(strength, 1) AS strength "
                "FROM entity_relations WHERE COALESCE(strength, 0) > 0"
            ):
                if r["source"] in found_ids and r["target"] in found_ids:
                    links.append({"source": r["source"], "target": r["target"],
                                  "relation": r["relation"], "strength": r["strength"]})

            communities: dict = {}
            for r in rows:
                cid = r["community_id"]
                if cid is not None and str(cid) not in communities:
                    communities[str(cid)] = r["community_title"] or f"Community {cid}"

            return {"nodes": nodes, "links": links, "communities": communities}
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
            rows = db.execute(
                "SELECT r.entity_a, r.relation, r.entity_b, COALESCE(r.strength,1) AS strength, "
                "       ea.name AS a_name, eb.name AS b_name "
                "FROM entity_relations r "
                "LEFT JOIN entities ea ON ea.id=r.entity_a "
                "LEFT JOIN entities eb ON eb.id=r.entity_b "
                "WHERE (r.entity_a=:id OR r.entity_b=:id) AND r.invalidated_at IS NULL "
                "ORDER BY r.strength DESC",
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
                obs = [dict(o) for o in db.execute(
                    f"SELECT attribute, value, valid_from, valid_to, source, {count_col}, {last_col} "
                    "FROM entity_observations WHERE entity_id=? "
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
        store.rename_entity(entity_id, name); changed.append("name")
    if org is not None:
        store.update_entity_org(entity_id, org); changed.append("org")
    if email_addr is not None:
        store.set_entity_email(entity_id, email_addr); changed.append("email")
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
    tie to the winner's. So merging never downgrades 'Josh Kemp' to 'J.K.'."""
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
                "message": "One of these is keyed on a shared/role inbox "
                           "(e.g. office@) — merging could fuse distinct people. Refused."}
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
    """Soft-delete (reversible) an entity."""
    ok = store.suppress_entity(entity_id, reason="graph-ui")
    if ok:
        store.record_change("entity_suppressed", ref_id=entity_id, summary="suppressed via graph")
    return {"ok": bool(ok)}
