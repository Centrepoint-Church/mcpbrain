"""Retrieval eval harness: recall@k + MRR over tests/eval/retrieval_eval.jsonl.

Builds a small deterministic fixture store (real bge-small embeddings), runs
hybrid_search per query, and reports recall@k and MRR. Importable for the
regression test (run_eval) and runnable as a script to sweep fusion params:

    uv run python tests/eval/run_eval.py
    uv run python tests/eval/run_eval.py --rrf-k 30 --vec-weight 1.5 --kw-weight 1.0
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
EVAL = HERE / "retrieval_eval.jsonl"

# The fixture corpus. doc_ids MUST match expected_doc_ids in the jsonl. Text is
# deliberately varied so semantic paraphrase queries exercise the vector path
# and exact-term queries exercise FTS. Distractors stress precision.
FIXTURE_DOCS = {
    "doc-budget": "The annual budget review covers next year's ministry finances and spending plan.",
    "doc-roster": "Volunteer roster for Sunday services: who is serving on welcome, kids, and worship.",
    "doc-camp": "Youth summer camp logistics for the teenagers' retreat: transport, food, cabins.",
    "doc-facilities": "Building maintenance request: the air conditioning in the main hall is broken.",
    "doc-staffmtg": "Staff meeting agenda and leadership team gathering notes for this week.",
    "doc-safeguarding": "Child safety and safeguarding policy: background checks for kids ministry volunteers.",
    "doc-distract-1": "Coffee order for the cafe and the weekly grocery shopping list.",
    "doc-distract-2": "Car park resurfacing quote from the contractor for the south lot.",
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


def main() -> None:
    import tempfile

    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--rrf-k", type=int, default=60)
    ap.add_argument("--vec-weight", type=float, default=1.0)
    ap.add_argument("--kw-weight", type=float, default=1.0)
    args = ap.parse_args()
    with tempfile.TemporaryDirectory() as td:
        store, emb = build_fixture_store(Path(td))
        m = run_eval(store, emb, k=args.k, rrf_k=args.rrf_k,
                     vec_weight=args.vec_weight, kw_weight=args.kw_weight)
        print(f"recall@{m['k']}={m['recall_at_k']:.3f}  MRR={m['mrr']:.3f}  "
              f"(rrf_k={args.rrf_k}, vec_weight={args.vec_weight}, kw_weight={args.kw_weight})")


if __name__ == "__main__":
    main()
