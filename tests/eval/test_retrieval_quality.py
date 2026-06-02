"""Retrieval quality gate: recall@5 and nDCG@5 over a synthetic golden set.

Seeds a fresh bge-small-backed Store with 12 synthetic ministry/office-ops
documents and runs hybrid_search against 10 golden query cases (5 semantic
paraphrases, 5 keyword queries).

Gate is baseline-anchored, not absolute-threshold: scores are written to
tests/eval/baselines/quality_baseline.json on first run and the test fails
only if a subsequent run regresses by more than TOLERANCE (0.05 per metric).

First run: writes baseline, prints scores, passes.
Second run: compares against baseline, passes (delta 0).

Run as part of the normal suite:
    .venv/bin/python -m pytest products/mcp-ops-brain/tests/eval/test_retrieval_quality.py -v -s
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
GOLDEN = HERE.parent / "fixtures" / "golden.json"
BASELINE = HERE / "baselines" / "quality_baseline.json"
TOLERANCE = 0.05  # absolute regression budget per metric


def _ndcg_at_k(retrieved: list[str], relevant: set[str], k: int) -> float:
    dcg = 0.0
    for i, doc_id in enumerate(retrieved[:k]):
        if doc_id in relevant:
            dcg += 1.0 / math.log2(i + 2)
    ideal = sum(1.0 / math.log2(i + 2) for i in range(min(len(relevant), k)))
    return dcg / ideal if ideal else 0.0


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


@pytest.fixture(scope="module")
def seeded_store(tmp_path_factory):
    """Build a real bge-small-backed store with the golden documents.

    scope="module" so the model is loaded once for all cases in this file.
    """
    from mcpbrain.embed import get_embedder
    from mcpbrain.index import index_pending
    from mcpbrain.store import Store

    golden = json.loads(GOLDEN.read_text())
    emb = get_embedder("bge-small")

    tmp = tmp_path_factory.mktemp("quality_store")
    store = Store(tmp / "quality.sqlite3", dim=emb.dim)
    store.init()

    for doc in golden["documents"]:
        store.upsert_chunk(
            doc_id=doc["doc_id"],
            text=doc["text"],
            content_hash=_hash(doc["text"]),
            metadata={},
        )

    index_pending(store, emb)
    return store, emb


def test_retrieval_quality(seeded_store):
    from mcpbrain.retrieval import hybrid_search

    store, emb = seeded_store
    golden = json.loads(GOLDEN.read_text())

    recalls, ndcgs = [], []
    for case in golden["cases"]:
        relevant = set(case["expected_doc_ids"])
        results = hybrid_search(store, emb, case["query"], limit=5)
        retrieved = [r["doc_id"] for r in results]
        hit_count = len(set(retrieved[:5]) & relevant)
        recalls.append(hit_count / len(relevant))
        ndcgs.append(_ndcg_at_k(retrieved, relevant, k=5))

    mean_recall = sum(recalls) / len(recalls) if recalls else 0.0
    mean_ndcg = sum(ndcgs) / len(ndcgs) if ndcgs else 0.0

    BASELINE.parent.mkdir(parents=True, exist_ok=True)

    if not BASELINE.exists():
        BASELINE.write_text(
            json.dumps(
                {"mean_recall_at_5": mean_recall, "mean_ndcg_at_5": mean_ndcg},
                indent=2,
            )
        )
        print(
            f"\nRetrieval quality gate: recall@5={mean_recall:.3f}, "
            f"nDCG@5={mean_ndcg:.3f} (baseline recorded)"
        )
        return

    prior = json.loads(BASELINE.read_text())
    prior_recall = prior["mean_recall_at_5"]
    prior_ndcg = prior["mean_ndcg_at_5"]
    print(
        f"\nRetrieval quality gate: recall@5={mean_recall:.3f} "
        f"(baseline {prior_recall:.3f}, delta {mean_recall - prior_recall:+.3f}); "
        f"nDCG@5={mean_ndcg:.3f} "
        f"(baseline {prior_ndcg:.3f}, delta {mean_ndcg - prior_ndcg:+.3f})"
    )
    assert mean_recall >= prior_recall - TOLERANCE, (
        f"Retrieval recall@5 regressed: {mean_recall:.3f} vs baseline {prior_recall:.3f}"
    )
    assert mean_ndcg >= prior_ndcg - TOLERANCE, (
        f"Retrieval nDCG@5 regressed: {mean_ndcg:.3f} vs baseline {prior_ndcg:.3f}"
    )
