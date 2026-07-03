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
                obs = [dict(o) for o in db.execute(
                    "SELECT attribute, value, valid_from, valid_to, source "
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
    """Name search for the merge type-ahead. [] on blank q; excludes suppressed."""
    q = (q or "").strip()
    if not q:
        return []
    path = store._path if hasattr(store, "_path") else store.path
    try:
        db = _open_ro(Path(path))
        try:
            supp = ("LEFT JOIN entity_suppressions s ON s.entity_id = e.id"
                    if _table_exists(db, "entity_suppressions") else "")
            where_supp = "AND s.entity_id IS NULL" if supp else ""
            # Escape LIKE wildcards so a query containing % or _ matches literally.
            qesc = q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            rows = db.execute(
                f"SELECT e.id, e.name, e.type, COALESCE(e.org,'') AS org "
                f"FROM entities e {supp} "
                f"WHERE lower(e.name) LIKE :q ESCAPE '\\' {where_supp} "
                f"ORDER BY (lower(e.name)=lower(:exact)) DESC, length(e.name) ASC "
                f"LIMIT :lim",
                {"q": f"%{qesc}%", "exact": q, "lim": int(limit)}).fetchall()
            return [dict(r) for r in rows]
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


def merge_entities(store, loser_id: str, winner_id: str) -> dict:
    """Merge loser into winner, guarded. Refuses self-merge and role-inbox pairs."""
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
    store.merge_entities(loser_id, winner_id)
    store.record_change("entity_merged", ref_id=winner_id,
                        summary=f"merged {loser_id} into {winner_id}")
    return {"ok": True}


def suppress_entity(store, entity_id: str) -> dict:
    """Soft-delete (reversible) an entity."""
    ok = store.suppress_entity(entity_id, reason="graph-ui")
    if ok:
        store.record_change("entity_suppressed", ref_id=entity_id, summary="suppressed via graph")
    return {"ok": bool(ok)}
