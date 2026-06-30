"""Graph population + quality metrics. The eval gate for the enrichment-depth work.

graph_metrics(store) is a pure read over the live schema; it does not mutate.
Run before and after each graph-shape change to prove improvement / no regression.
"""

import argparse
import json

_STRUCTURAL_RELATIONS = frozenset({"involved_in", "authored", "instance_of"})


def graph_metrics(store) -> dict:
    with store._connect() as db:
        def scalar(sql, *a):
            return db.execute(sql, a).fetchone()[0]

        rel_total = scalar("SELECT COUNT(*) FROM entity_relations")
        rel_doc = scalar("SELECT COUNT(*) FROM entity_relations WHERE COALESCE(source_doc_id,'')!=''")
        rel_sem = scalar(
            "SELECT COUNT(*) FROM entity_relations WHERE relation NOT IN (?,?,?)",
            *sorted(_STRUCTURAL_RELATIONS))
        ent_total = scalar("SELECT COUNT(*) FROM entities")
        persons = scalar("SELECT COUNT(*) FROM entities WHERE type='person'")
        persons_email = scalar(
            "SELECT COUNT(*) FROM entities WHERE type='person' AND COALESCE(email_addr,'')!=''")
        obs = dict(db.execute(
            "SELECT attribute, COUNT(*) FROM entity_observations GROUP BY attribute").fetchall())
        rel_types = dict(db.execute(
            "SELECT relation, COUNT(*) FROM entity_relations GROUP BY relation").fetchall())

    def pct(n, d):
        return round(100.0 * n / d, 1) if d else 0.0

    return {
        "relations_total": rel_total,
        "relations_with_doc_id_pct": pct(rel_doc, rel_total),
        "relations_semantic_pct": pct(rel_sem, rel_total),
        "entities_total": ent_total,
        "person_email_pct": pct(persons_email, persons),
        "observation_attributes": obs,
        "relation_type_counts": rel_types,
    }


def _open_store():
    from mcpbrain import config
    from mcpbrain.store import Store
    from mcpbrain.embed import get_embedder
    return Store(config.store_path(), dim=get_embedder("bge-small").dim)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", help="write metrics JSON to this path")
    ap.add_argument("--compare", help="diff current metrics against this saved baseline")
    args = ap.parse_args(argv)
    m = graph_metrics(_open_store())
    if args.baseline:
        with open(args.baseline, "w") as f:
            json.dump(m, f, indent=2)
    if args.compare:
        with open(args.compare) as f:
            base = json.load(f)
        for k in ("relations_with_doc_id_pct", "relations_semantic_pct",
                  "person_email_pct", "relations_total"):
            print(f"{k}: {base.get(k)} -> {m.get(k)}")
    print(json.dumps(m, indent=2))


if __name__ == "__main__":
    main()
