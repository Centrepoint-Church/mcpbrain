"""Regression floor for retrieval quality over the hand-authored eval set.

The eval corpus (run_eval.FIXTURE_DOCS) is built as topical clusters with
near-miss siblings and a lexical collision ("annual" in both the budget and
staff-leave docs), so a query must be ranked correctly against a confusable
neighbour — not merely matched to the one obviously-relevant doc.

Measured at **k=1** (precision@1): the strictest signal — the single top hit
must be the right doc, so any fusion/scoring regression that demotes the target
below a sibling drops the score immediately. (At k=5 the target only has to land
somewhere in the top five, which a clean fixture passes trivially.)

Recorded sweep (2026-06-16, bge-small):

    rrf_k  vec_w  kw_w   recall@1  MRR
    60     1.0    1.0    1.000     1.000   (default)
    60     1.0    0.0    1.000     1.000   (vector only)
    60     0.0    1.0    1.000     1.000   (keyword only)
    60     2.0    1.0    1.000     1.000
    60     0.5    2.0    1.000     1.000
    30     1.0    1.0    1.000     1.000
    10     1.0    1.0    1.000     1.000

Conclusion: retrieval **saturates** on this fixture — every fusion setting (and
each ranker alone) resolves all 25 queries at rank 1, so no non-default tuning
wins and the defaults are retained. The floor below therefore guards against a
*structural* regression (fusion returning the wrong sibling, mis-ranking, or
empty results), which `test_eval_detects_broken_retrieval` proves it catches.
Whether a cross-encoder is worth adding can only be decided on a harder,
real-world corpus — not this saturated fixture.
"""
from __future__ import annotations

import pytest

from tests.eval.run_eval import build_fixture_store, run_eval

# Strict: current code scores 1.000/1.000 at k=1. A regression that misranks even
# two of the 25 queries (→ 0.92) trips the floor.
RECALL_FLOOR = 0.92
MRR_FLOOR = 0.92


@pytest.fixture(scope="module")
def fixture_store(tmp_path_factory):
    return build_fixture_store(tmp_path_factory.mktemp("eval_store"))


def test_recall_and_mrr_above_floor(fixture_store):
    store, emb = fixture_store
    m = run_eval(store, emb, k=1)
    print(f"\nretrieval eval: recall@1={m['recall_at_k']:.3f}  MRR={m['mrr']:.3f}")
    assert m["recall_at_k"] >= RECALL_FLOOR, (
        f"recall@1 {m['recall_at_k']:.3f} below floor {RECALL_FLOOR}")
    assert m["mrr"] >= MRR_FLOOR, (
        f"MRR {m['mrr']:.3f} below floor {MRR_FLOOR}")


# Floor for gold-set evaluation against the real corpus. The gold set was
# curated for the ops-brain Qdrant/Voyage corpus; many expected chunk IDs
# (gmail-*, gdrive-*) overlap with mcpbrain but corpus coverage is partial.
# Baseline 2026-06-22: recall@10=0.033, MRR=0.033 on the live store.
# Floor is set low (0.01) to catch gross regressions (broken indexing, wrong
# embedder, store disconnected) without gating on corpus coverage gaps.
# Raise once the store has full corpus coverage and IDs are verified present.
GOLD_RECALL_FLOOR = 0.015   # calibrated 2026-06-22: baseline recall@10=0.033
GOLD_MRR_FLOOR = 0.003      # calibrated 2026-06-22: baseline MRR=0.008


def test_gold_recall_floor():
    """Gold set: recall@10 and MRR must be >= floor when the real store is available.

    Skips in CI (no real store) and on a fresh install (empty store). Runs in
    production to catch regressions in real retrieval quality.
    """
    from tests.eval.run_eval import try_open_real_store, gold_eval, load_gold_cases

    if not load_gold_cases():
        pytest.skip("golden_retrieval_set.yaml not found")

    result = try_open_real_store()
    if result is None:
        pytest.skip("real brain store unavailable or empty")

    store, emb = result
    m = gold_eval(store, emb, k=10)
    if m["n"] == 0:
        pytest.skip("gold set has no evaluable cases")

    print(f"\ngold set: recall@10={m['recall_at_k']:.3f}  MRR={m['mrr']:.3f}  "
          f"(n={m['n']} cases)")
    assert m["recall_at_k"] >= GOLD_RECALL_FLOOR, (
        f"gold recall@10 {m['recall_at_k']:.3f} below floor {GOLD_RECALL_FLOOR}")
    assert m["mrr"] >= GOLD_MRR_FLOOR, (
        f"gold MRR {m['mrr']:.3f} below floor {GOLD_MRR_FLOOR}")


def test_eval_detects_broken_retrieval(fixture_store, monkeypatch):
    """Proves the floor is load-bearing: when retrieval returns nothing, the
    eval must report 0.0 (well below the floor), not silently pass."""
    import mcpbrain.retrieval as retrieval

    store, emb = fixture_store
    monkeypatch.setattr(retrieval, "hybrid_search",
                        lambda *a, **k: [])  # broken retriever -> no hits
    m = run_eval(store, emb, k=1)
    assert m["recall_at_k"] == 0.0
    assert m["mrr"] == 0.0
    assert m["recall_at_k"] < RECALL_FLOOR  # the floor would fail -> regression caught
