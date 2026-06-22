"""Retrieval eval harness: recall@k + MRR.

Two eval modes:

1. **Synthetic fixture** (default / always runs): builds a small deterministic
   in-process store from FIXTURE_DOCS, runs hybrid_search, and reports recall@k
   and MRR. Fast structural floor — saturates at 1.0/1.0, gates against
   regressions in fusion/scoring logic. Not a quality signal.

2. **Gold set** (optional / skips when real store unavailable): loads
   golden_retrieval_set.yaml (30+ hand-curated query→chunk cases from Josh's
   real brain), runs hybrid_search against the live brain.sqlite3, and reports
   recall@k and MRR. The quality signal — measures real retrieval performance.
   Skips cleanly when the real store is empty or absent (e.g., in CI).

Runnable as a script to sweep fusion params:

    uv run python tests/eval/run_eval.py
    uv run python tests/eval/run_eval.py --rrf-k 30 --vec-weight 1.5
    uv run python tests/eval/run_eval.py --gold          # gold set against real store
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
EVAL = HERE / "retrieval_eval.jsonl"

# The fixture corpus. doc_ids MUST match expected_doc_ids in the jsonl.
#
# Organised into TOPICAL CLUSTERS (two near-miss docs per theme) plus lexical
# collisions ("annual" appears in both the budget and staff-leave docs), so a
# query must be ranked correctly against a confusable sibling — not merely
# matched to the one obviously-relevant doc. This is what lets recall@5/MRR drop
# below 1.0 and makes the regression floor load-bearing; a topically-disjoint
# corpus scores 1.0/1.0 under any fusion params and measures nothing.
FIXTURE_DOCS = {
    # budget cluster
    "doc-budget-annual": "Annual church budget review: next financial year's ministry income, "
                         "expenditure and savings plan approved by the board.",
    "doc-budget-camp": "Youth camp budget breakdown: cost per camper, bus hire, catering and the "
                       "deposit for the campsite.",
    # roster cluster
    "doc-roster-sunday": "Sunday service volunteer roster: welcome team, kids church, worship band "
                         "and sound desk for each week.",
    "doc-roster-camp": "Camp leaders roster: which youth leaders supervise each cabin and activity "
                       "during the retreat.",
    # facilities cluster
    "doc-facilities-aircon": "Maintenance request: the air conditioning in the main auditorium is "
                             "broken and needs a technician.",
    "doc-facilities-carpark": "Facilities project: resurfacing the south car park and repainting "
                              "the line markings.",
    # camp cluster
    "doc-camp-logistics": "Youth summer camp logistics: transport timetable, cabin allocation and "
                          "the meal roster for the teenagers' retreat.",
    "doc-camp-program": "Camp program and session themes: morning devotions, afternoon activities "
                        "and evening worship for the youth retreat.",
    # staff cluster ('annual leave' collides lexically with the annual budget doc)
    "doc-staff-agenda": "Staff meeting agenda: this week's leadership team discussion items and "
                        "decisions.",
    "doc-staff-leave": "Staff leave policy: how to request annual leave, sick days and time in lieu.",
    # safeguarding cluster
    "doc-safeguard-policy": "Child safeguarding policy: background checks and working-with-children "
                            "clearances for kids ministry volunteers.",
    "doc-safeguard-incident": "Safeguarding incident report form: how to record and escalate a "
                              "child safety concern.",
    # distractors
    "doc-distract-cafe": "Cafe coffee order and the weekly grocery shopping list for the kitchen.",
    "doc-distract-newsletter": "Monthly newsletter draft: announcements, upcoming events and a "
                               "thank-you to volunteers.",
}


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def build_fixture_store(tmp_dir: Path):
    """Return (store, embedder) seeded with FIXTURE_DOCS and fully indexed."""
    from mcpbrain.embed import get_embedder
    from mcpbrain.index import index_pending
    from mcpbrain.store import Store

    emb = get_embedder("bge-small")
    store = Store(tmp_dir / "eval.sqlite3", dim=emb.dim)
    store.init()
    for doc_id, text in FIXTURE_DOCS.items():
        store.upsert_chunk(doc_id, text, _hash(text), {})
    index_pending(store, emb)
    return store, emb


def load_cases() -> list[dict]:
    return [json.loads(line) for line in EVAL.read_text().splitlines() if line.strip()]


def run_eval(store, embedder, *, k: int = 5, rrf_k: int = 60,
             vec_weight: float = 1.0, kw_weight: float = 1.0) -> dict:
    """Return {"recall_at_k": float, "mrr": float, "k": k} over the eval set."""
    from mcpbrain.retrieval import hybrid_search

    cases = load_cases()
    recalls: list[float] = []
    rrs: list[float] = []
    for case in cases:
        expected = set(case["expected_doc_ids"])
        results = hybrid_search(store, embedder, case["query"], limit=k,
                                rrf_k=rrf_k, vec_weight=vec_weight, kw_weight=kw_weight)
        retrieved = [r["doc_id"] for r in results]
        hits = set(retrieved[:k]) & expected
        recalls.append(len(hits) / len(expected) if expected else 0.0)
        rr = 0.0
        for i, doc_id in enumerate(retrieved):
            if doc_id in expected:
                rr = 1.0 / (i + 1)
                break
        rrs.append(rr)
    n = len(cases) or 1
    return {"recall_at_k": sum(recalls) / n, "mrr": sum(rrs) / n, "k": k}


def load_gold_cases() -> list[dict]:
    """Load hand-curated gold cases from golden_retrieval_set.yaml.

    Returns [] when the file is absent or yaml is not installed.
    """
    path = HERE / "golden_retrieval_set.yaml"
    if not path.exists():
        return []
    try:
        import yaml  # pyyaml — dev dependency
        return yaml.safe_load(path.read_text()) or []
    except Exception:
        return []


def try_open_real_store():
    """Try to open the live brain store in read-only mode.

    Returns (store, embedder) if the store exists and has data, else None.
    This is the gate for gold-set eval: skips gracefully in CI.
    """
    try:
        from mcpbrain import config
        from mcpbrain.embed import get_embedder
        from mcpbrain.store import Store, store_dim_from_path

        path = config.store_path()
        if not path.exists():
            return None
        dim = store_dim_from_path(path)
        if not dim:
            return None
        store = Store(path, dim=dim, read_only=True)
        if store.chunk_count() == 0:
            return None
        emb = get_embedder("bge-small")
        return store, emb
    except Exception:
        return None


def gold_eval(store, embedder, *, k: int = 10) -> dict:
    """Run recall@k and MRR over the hand-curated gold set.

    Returns {"recall_at_k", "mrr", "k", "n"} where n is the number of cases
    that had at least one expected_chunk_id. Cases where all expected IDs are
    missing from the store still count (recall=0 for that case).
    """
    from mcpbrain.retrieval import hybrid_search

    cases = load_gold_cases()
    if not cases:
        return {"recall_at_k": 0.0, "mrr": 0.0, "k": k, "n": 0}

    recalls: list[float] = []
    rrs: list[float] = []
    for case in cases:
        expected = set(case.get("expected_chunk_ids") or [])
        if not expected:
            continue
        results = hybrid_search(store, embedder, case.get("query", ""), limit=k)
        retrieved = [r["doc_id"] for r in results]
        hits = set(retrieved[:k]) & expected
        recalls.append(len(hits) / len(expected))
        rr = 0.0
        for i, doc_id in enumerate(retrieved):
            if doc_id in expected:
                rr = 1.0 / (i + 1)
                break
        rrs.append(rr)

    n = len(recalls) or 1
    return {
        "recall_at_k": sum(recalls) / n,
        "mrr": sum(rrs) / n,
        "k": k,
        "n": len(recalls),
    }


def main() -> None:
    import tempfile

    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--rrf-k", type=int, default=60)
    ap.add_argument("--vec-weight", type=float, default=1.0)
    ap.add_argument("--kw-weight", type=float, default=1.0)
    ap.add_argument("--gold", action="store_true",
                    help="Run gold set eval against the real brain store (requires populated store)")
    args = ap.parse_args()

    if args.gold:
        result = try_open_real_store()
        if result is None:
            print("Gold set eval: real store unavailable or empty — skipped")
            return
        store, emb = result
        m = gold_eval(store, emb, k=args.k)
        if m["n"] == 0:
            print("Gold set eval: no cases loaded (golden_retrieval_set.yaml missing?)")
        else:
            print(f"Gold set: recall@{m['k']}={m['recall_at_k']:.3f}  MRR={m['mrr']:.3f}  "
                  f"(n={m['n']} cases)")
        return

    with tempfile.TemporaryDirectory() as td:
        store, emb = build_fixture_store(Path(td))
        m = run_eval(store, emb, k=args.k, rrf_k=args.rrf_k,
                     vec_weight=args.vec_weight, kw_weight=args.kw_weight)
        print(f"recall@{m['k']}={m['recall_at_k']:.3f}  MRR={m['mrr']:.3f}  "
              f"(rrf_k={args.rrf_k}, vec_weight={args.vec_weight}, kw_weight={args.kw_weight})")


if __name__ == "__main__":
    main()
