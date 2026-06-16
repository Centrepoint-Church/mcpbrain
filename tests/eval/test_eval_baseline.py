"""Regression floor for retrieval quality over the hand-authored eval set.

Builds the fixture store once (module scope; loads bge-small once) and asserts
recall@5 and MRR stay above a conservative floor. The floor is intentionally
loose — it catches a fusion/scoring regression, not normal noise.
"""
from __future__ import annotations

import pytest

from tests.eval.run_eval import build_fixture_store, run_eval

RECALL_FLOOR = 0.80
MRR_FLOOR = 0.70


@pytest.fixture(scope="module")
def fixture_store(tmp_path_factory):
    return build_fixture_store(tmp_path_factory.mktemp("eval_store"))


def test_recall_and_mrr_above_floor(fixture_store):
    store, emb = fixture_store
    m = run_eval(store, emb, k=5)
    print(f"\nretrieval eval: recall@5={m['recall_at_k']:.3f}  MRR={m['mrr']:.3f}")
    assert m["recall_at_k"] >= RECALL_FLOOR, (
        f"recall@5 {m['recall_at_k']:.3f} below floor {RECALL_FLOOR}")
    assert m["mrr"] >= MRR_FLOOR, (
        f"MRR {m['mrr']:.3f} below floor {MRR_FLOOR}")
