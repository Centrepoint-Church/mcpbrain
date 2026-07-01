"""Graph population + quality metrics. The eval gate for the enrichment-depth work.

graph_metrics(store) is a pure read over the live schema; it does not mutate.
Run before and after each graph-shape change to prove improvement / no regression.
"""

import argparse
import json

# Module-level imports from tests.eval so tests can patch them.
# Try/except so this module still imports cleanly in production (tests/ may
# not be on the path for some install layouts).
try:
    from tests.eval.run_eval import load_gold_cases as _load_gold_cases
except ImportError:
    _load_gold_cases = None  # type: ignore

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


def gold_docs_cold_marked(store) -> dict:
    """Measure salience-gate false positives: how many gold-relevant docs are cold-marked?

    Of the hand-curated gold eval set's expected chunk IDs that are actually present
    in this store, how many are currently marked enrich_state='cold'? A near-zero count
    means the gate (prepare.should_enrich) is not false-positiving on docs that matter
    for retrieval quality.

    Args:
        store: A Store instance.

    Returns:
        dict with keys:
        - present: count of gold chunk IDs that exist in the store
        - cold: count of those present that are marked enrich_state='cold'
        - pct: percentage (100.0 * cold / present, or 0.0 if present==0)
    """
    # If gold cases are unavailable (tests/ not on path), degrade gracefully.
    if _load_gold_cases is None:
        return {"present": 0, "cold": 0, "pct": 0.0}

    # Load gold cases and collect all unique expected chunk IDs.
    cases = _load_gold_cases()
    all_expected_ids = set()
    for case in cases:
        all_expected_ids.update(case.get("expected_chunk_ids", []))

    if not all_expected_ids:
        return {"present": 0, "cold": 0, "pct": 0.0}

    # Query the chunks table to find which of these IDs exist and their enrich_state.
    with store._connect() as db:
        # Use placeholders for SQL injection safety. Build them dynamically.
        placeholders = ",".join("?" * len(all_expected_ids))
        rows = db.execute(
            f"SELECT doc_id, COALESCE(enrich_state, '') FROM chunks WHERE doc_id IN ({placeholders})",
            sorted(all_expected_ids)
        ).fetchall()

    # Count present and cold.
    present = len(rows)
    cold = sum(1 for doc_id, state in rows if state == 'cold')

    def pct(n, d):
        return round(100.0 * n / d, 1) if d else 0.0

    return {
        "present": present,
        "cold": cold,
        "pct": pct(cold, present)
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
