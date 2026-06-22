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


# Floor for gold-set evaluation, applied ONLY over coverable cases (those whose
# expected chunks are present in this store). The gold set is shared with ops-brain
# and references its full corpus; mcpbrain may hold a subset mid-backfill, so we
# never average missing-chunk cases in as 0 (that would mask real quality).
#
# These are REGRESSION floors against the measured baseline, NOT an aspirational
# target. Measured 2026-06-22 over 10 coverable cases: recall@10=0.150, MRR=0.025.
# The absolute numbers are low partly because the gold ground-truth is ops-brain's
# *specific* chunk for each query — a different-but-equally-valid mcpbrain chunk
# counts as a miss — so this gates against retrieval REGRESSIONS, not absolute
# quality, until the gold set's expected_chunk_ids are re-verified against the
# mcpbrain corpus (tracked on #6). Raise the floors as coverage + quality improve.
GOLD_RECALL_FLOOR = 0.10   # measured baseline 0.150 (10 coverable cases, 2026-06-22)
GOLD_MRR_FLOOR = 0.01      # measured baseline 0.025
MIN_COVERED = 5            # need at least this many coverable cases to gate meaningfully


def test_gold_recall_floor():
    """Gold set: recall@10 / MRR over COVERABLE cases must clear the floor.

    Skips honestly when there is no real store (CI / fresh install) or when too few
    gold cases are coverable against the current corpus (mid-backfill) — rather than
    passing a near-zero floor on uncoverable cases. As backfill fills the corpus,
    `covered` rises and this test starts gating real retrieval quality.
    """
    from tests.eval.run_eval import try_open_real_store, gold_eval, load_gold_cases

    if not load_gold_cases():
        pytest.skip("golden_retrieval_set.yaml not found")

    result = try_open_real_store()
    if result is None:
        pytest.skip("real brain store unavailable or empty")

    store, emb = result
    m = gold_eval(store, emb, k=10)
    print(f"\ngold set: covered {m['covered']}/{m['total']} cases "
          f"({m['missing']} not yet in corpus)")
    if m["covered"] < MIN_COVERED:
        pytest.skip(
            f"only {m['covered']}/{m['total']} gold cases coverable against this "
            f"corpus (need >= {MIN_COVERED}); re-run after backfill")

    print(f"gold set (covered): recall@10={m['recall_at_k']:.3f}  MRR={m['mrr']:.3f}")
    assert m["recall_at_k"] >= GOLD_RECALL_FLOOR, (
        f"gold recall@10 {m['recall_at_k']:.3f} below floor {GOLD_RECALL_FLOOR} "
        f"(over {m['covered']} coverable cases)")
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
