"""One-off graph hygiene pass.

The relation/org guards in graph_write apply to NEW writes; this cleans the edges
and org tags already in the store from before the guards landed (0.7.34):

  1. self-loops (entity_a == entity_b) — always noise
  2. type-invalid person-centric edges — e.g. "topic works_at org", "meeting
     works_at" — the LLM over-applied works_at/reports_to/manages to non-person
     entities (see graph_write._RELATION_ENDPOINT_TYPES)
  3. org-tag drift — fold "Centrepoint" / "centrepoint" → the canonical
     "Centrepoint Church" via the configured taxonomy

Idempotent: running it twice is a no-op the second time. Returns a counts dict.
"""
from __future__ import annotations

from mcpbrain import graph_write, orgs


def cleanup_graph(store, *, taxonomy=None) -> dict:
    if taxonomy is None:
        taxonomy = orgs.taxonomy_from_config()
    counts = {"self_loops": 0, "type_invalid": 0, "orgs_folded": 0}
    with store._connect() as conn:
        # 1. self-loops
        cur = conn.execute("DELETE FROM entity_relations WHERE entity_a = entity_b")
        counts["self_loops"] = cur.rowcount or 0

        # 2. type-invalid person-centric edges
        for relation, (src_ok, tgt_ok) in graph_write._RELATION_ENDPOINT_TYPES.items():
            src_list = ",".join("?" for _ in src_ok)
            tgt_list = ",".join("?" for _ in tgt_ok)
            cur = conn.execute(
                f"""DELETE FROM entity_relations
                    WHERE relation = ?
                      AND id IN (
                        SELECT er.id FROM entity_relations er
                        JOIN entities ea ON ea.id = er.entity_a
                        JOIN entities eb ON eb.id = er.entity_b
                        WHERE er.relation = ?
                          AND (ea.type NOT IN ({src_list})
                               OR eb.type NOT IN ({tgt_list})))""",
                (relation, relation, *src_ok, *tgt_ok),
            )
            counts["type_invalid"] += cur.rowcount or 0

        # 3. org-tag drift — fold each distinct org to its canonical form
        rows = conn.execute(
            "SELECT DISTINCT org FROM entities WHERE COALESCE(org,'') != ''"
        ).fetchall()
        for row in rows:
            raw = row[0]
            canon = graph_write.canonical_org(raw, taxonomy)
            if canon and canon != raw:
                cur = conn.execute(
                    "UPDATE entities SET org = ? WHERE org = ?", (canon, raw))
                counts["orgs_folded"] += cur.rowcount or 0
    return counts
