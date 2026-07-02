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

    where = ["COALESCE(e.degree, 0) >= :min_conn", "s.entity_id IS NULL"]
    params: dict = {"min_conn": int(min_conn)}
    if org:
        where.append("COALESCE(e.org, '') = :org")
        params["org"] = "" if org == "unassigned" else org
    if community:
        where.append("ec.community_id = :community")
        try:
            params["community"] = int(community)
        except (TypeError, ValueError):
            return dict(_EMPTY)
    if types:
        where.append("e.type IN (" + ",".join(f":t{i}" for i in range(len(types))) + ")")
        for i, t in enumerate(types):
            params[f"t{i}"] = t
    if recency_days and int(recency_days) > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=int(recency_days))).strftime("%Y-%m-%d")
        where.append("COALESCE(e.last_seen, '') >= :cutoff")
        params["cutoff"] = cutoff

    try:
        db = _open_ro(Path(path))
        try:
            rows = db.execute(f"""
                SELECT e.id, e.name, e.type, COALESCE(e.org, '') AS org,
                       COALESCE(e.email_count, 0) AS email_count,
                       COALESCE(e.email_addr, '') AS email_addr,
                       COALESCE(e.first_seen, '') AS first_seen,
                       COALESCE(e.last_seen, '') AS last_seen,
                       ec.community_id, cs.title AS community_title,
                       COALESCE(e.degree, 0) AS degree
                FROM entities e
                LEFT JOIN entity_suppressions s ON s.entity_id = e.id
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
                    LEFT JOIN entity_suppressions s ON s.entity_id = e.id
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
