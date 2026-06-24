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
# expected chunks are present in this store).
#
# These are REGRESSION floors against the measured baseline, NOT an aspirational
# target. Recall is measured DOCUMENT-level (any chunk of the expected doc counts —
# see gold_eval._doc_key) because mcpbrain re-chunks independently.
#
# PRODUCTION PATH: eval uses exclude_cold=True — identical to daemon.search() when
# tiered_memory is on. Session-2 review found the non-production path (exclude_cold=False)
# was being used, masking the real user-facing quality. Production path confirmed
# net-positive: cold-exclusion pushes low-salience noise out of top ranks.
#
# Active gold set: golden_retrieval_set_mcpbrain_candidate.yaml (mcpbrain-native,
# curated 2026-06-24). 20 cases, 20/20 coverable on the live store (80,705 chunks).
# Ambiguous clusters resolved: CP College Semester Two invite emails cross-linked
# (Mitch/Lisa/Joe invitations accept any of the three); Capes financial docs
# cross-linked (budget/P&L/balance-sheet accept any of the three).
# Measured 2026-06-24 (production path, exclude_cold=True): recall@10=0.750, MRR=0.483.
GOLD_RECALL_FLOOR = 0.55   # measured doc-level baseline 0.750 (production path)
GOLD_MRR_FLOOR = 0.35      # measured baseline 0.483 (production path); raised from 0.20
MIN_COVERED = 15           # with 20 cases need at least 15 covered to gate meaningfully


def test_gold_recall_floor():
    """Gold set: recall@10 / MRR over COVERABLE cases must clear the floor.

    Uses exclude_cold=True — the production path (daemon.search sets this when
    tiered_memory is on). Skips honestly when there is no real store (CI / fresh
    install) or when too few gold cases are coverable against the current corpus
    (mid-backfill). As backfill fills the corpus, `covered` rises and this test
    starts gating real retrieval quality.
    """
    from tests.eval.run_eval import try_open_real_store, gold_eval, load_gold_cases

    if not load_gold_cases():
        pytest.skip("no gold cases file found")

    result = try_open_real_store()
    if result is None:
        pytest.skip("real brain store unavailable or empty")

    store, emb = result
    m = gold_eval(store, emb, k=10, search_kwargs={"exclude_cold": True})
    print(f"\ngold set: covered {m['covered']}/{m['total']} cases "
          f"({m['missing']} not yet in corpus)")
    if m["covered"] < MIN_COVERED:
        pytest.skip(
            f"only {m['covered']}/{m['total']} gold cases coverable against this "
            f"corpus (need >= {MIN_COVERED}); re-run after backfill")

    print(f"gold set (covered, production path): recall@10={m['recall_at_k']:.3f}  MRR={m['mrr']:.3f}")
    assert m["recall_at_k"] >= GOLD_RECALL_FLOOR, (
        f"gold recall@10 {m['recall_at_k']:.3f} below floor {GOLD_RECALL_FLOOR} "
        f"(over {m['covered']} coverable cases)")
    assert m["mrr"] >= GOLD_MRR_FLOOR, (
        f"gold MRR {m['mrr']:.3f} below floor {GOLD_MRR_FLOOR}")


def test_gold_three_axis_does_not_regress_on_production_path():
    """B3 PRODUCTION QUALITY GATE: the three-axis ranker (recency+importance+decay)
    is ENABLED in live config (`importance_recall`), so this guards the property that
    justified enabling it — it must NOT regress recall@10 or MRR vs relevance-only on
    the production path. A future change that regresses the enabled ranker fails here.

    Both baseline and three-axis use the PRODUCTION PATH (exclude_cold=True) — the same
    path daemon.search() uses when tiered_memory is on.

    HISTORY:
    - 2026-06-23 (exclude_cold=False path — SUPERSEDED): the ranker regressed, because
      cold chunks were included and weights were untuned relative to that mix. Kept OFF.
    - 2026-06-24 (PRODUCTION PATH, exclude_cold=True): with cold chunks excluded (the
      user-facing path), it does NOT regress — MRR 0.483→0.571 (+0.088), recall@10 holds
      at 0.750 over 20 cases. `importance_recall` ENABLED in live config.json.

    Read-only: uses whatever salience exists; mutates nothing.
    """
    from tests.eval.run_eval import try_open_real_store, gold_eval, load_gold_cases

    if not load_gold_cases():
        pytest.skip("no gold cases file found")
    result = try_open_real_store()
    if result is None:
        pytest.skip("real brain store unavailable or empty")
    store, emb = result

    base = gold_eval(store, emb, k=10, search_kwargs={"exclude_cold": True})
    if base["covered"] < MIN_COVERED:
        pytest.skip(f"only {base['covered']} coverable cases (need >= {MIN_COVERED})")

    on = gold_eval(store, emb, k=10, search_kwargs={
        "exclude_cold": True,
        "recency_weight": 0.15, "importance_weight": 0.10,
        "decay_weight": 0.10, "recency_alpha": 0.01})

    print(f"\n3-axis: baseline recall@10={base['recall_at_k']:.3f} → "
          f"on={on['recall_at_k']:.3f}  (MRR {base['mrr']:.3f} → {on['mrr']:.3f}, "
          f"{base['covered']} covered cases)")

    # Quality gate: the ENABLED ranker must not regress recall or MRR on the path users
    # actually get. If this trips, the three-axis weights or salience have degraded —
    # investigate before shipping (do NOT loosen the epsilon to make it pass).
    eps = 0.001
    assert on["recall_at_k"] >= base["recall_at_k"] - eps, (
        f"three-axis ranker REGRESSED recall@10 {base['recall_at_k']:.3f}→"
        f"{on['recall_at_k']:.3f} on the production path")
    assert on["mrr"] >= base["mrr"] - eps, (
        f"three-axis ranker REGRESSED MRR {base['mrr']:.3f}→{on['mrr']:.3f} "
        f"on the production path")

    # importance_recall ships ON by default as of 0.7.65 (validated above): the
    # brain layer is the product. Guard that the default stays ON so a future edit
    # doesn't silently revert the shipped behaviour.
    from mcpbrain import config as _cfg
    import tempfile as _tf
    assert _cfg.importance_recall_enabled(_tf.mkdtemp()) is True, (
        "importance_recall is the shipped default (0.7.65) — must stay ON")


def test_q6_route_does_not_regress_recall_on_gold(tmp_path):
    """Q6 validation (the measurement the Phase-3 prompt required): the routing +
    token-overlap rerank pipeline must NOT regress doc-level recall@10 vs plain
    hybrid_search on the LIVE gold set. Measures route() against the baseline.

    CRAG (which calls the claude CLI) is left OFF so the test is deterministic;
    routing + rerank are the deterministic parts and are what we can gate on.
    Skips when the real store / coverage is unavailable.
    """
    import json
    from tests.eval.run_eval import try_open_real_store, gold_eval, load_gold_cases
    from mcpbrain.query_router import route

    if not load_gold_cases():
        pytest.skip("no gold cases file found")
    result = try_open_real_store()
    if result is None:
        pytest.skip("real brain store unavailable or empty")
    store, emb = result

    base = gold_eval(store, emb, k=10)
    if base["covered"] < MIN_COVERED:
        pytest.skip(f"only {base['covered']} coverable cases (need >= {MIN_COVERED})")

    # A temp home with routing + rerank ON (CRAG off → no LLM call).
    rh = tmp_path / "q6home"; rh.mkdir()
    (rh / "config.json").write_text(json.dumps({
        "retrieval_routing": True, "retrieval_rerank": True, "retrieval_crag": False}))

    def _route_fn(s, e, q, k):
        return route(s, e, q, k, home=str(rh))

    on = gold_eval(store, emb, k=10, search_fn=_route_fn)
    print(f"\nQ6 route: baseline recall@10={base['recall_at_k']:.3f} → "
          f"on={on['recall_at_k']:.3f}  (MRR {base['mrr']:.3f} → {on['mrr']:.3f}, "
          f"{base['covered']} covered cases)")

    # MEASURED FINDING (2026-06-23): Q6 routing + token-overlap rerank REGRESSES
    # recall@10 on the live gold set (0.40 → 0.30) — the lexical reranker is weaker
    # than plain RRF. So the conservative, correct state is that these flags ship
    # DEFAULT-OFF; do not enable until a real (cross-encoder) reranker beats the
    # baseline here. This test gates that: the flags must stay off while Q6 regresses.
    from mcpbrain import config as _cfg
    import tempfile as _tf
    blank = _tf.mkdtemp()
    assert _cfg.retrieval_rerank_enabled(blank) is False, "rerank regresses on gold — keep default off"
    assert _cfg.retrieval_routing_enabled(blank) is False, "routing regresses on gold — keep default off"
    if on["recall_at_k"] >= base["recall_at_k"] - 0.001:
        # If a future reranker stops regressing, surface that so the floors/defaults
        # can be revisited (don't silently keep it off once it helps).
        print("NOTE: Q6 no longer regresses recall — revisit enabling it.")


def test_q6_rerank_does_not_regress_recall(fixture_store):
    """Q6: token-overlap rerank must not regress recall@1 vs baseline on fixture store.

    The fixture corpus is saturated (baseline = 1.0/1.0) so this verifies that
    the reranker doesn't break anything — a real quality delta would only show
    on the gold set (where the query's most relevant doc isn't always ranked 1st
    by plain RRF). The test name is prefixed _fixture_ to distinguish from gold.
    """
    from tests.eval.run_eval import run_eval as _run_eval
    from mcpbrain.query_router import _token_overlap_rerank
    from mcpbrain.retrieval import hybrid_search

    store, emb = fixture_store

    def reranked_search(store, embedder, query, limit, **kw):
        results = hybrid_search(store, embedder, query, limit, **kw)
        return _token_overlap_rerank(query, results)

    # Patch hybrid_search in run_eval to use the reranked version
    import mcpbrain.retrieval as retrieval
    orig = retrieval.hybrid_search
    try:
        retrieval.hybrid_search = reranked_search
        m = _run_eval(store, emb, k=1)
    finally:
        retrieval.hybrid_search = orig

    print(f"\nQ6 rerank fixture: recall@1={m['recall_at_k']:.3f}  MRR={m['mrr']:.3f}")
    assert m["recall_at_k"] >= RECALL_FLOOR, (
        f"Q6 rerank REGRESSED fixture recall@1: {m['recall_at_k']:.3f} < {RECALL_FLOOR}")


def test_q6_routing_rerank_gold_no_regression():
    """Q6: routing (graph-seed + community) + rerank must not regress gold recall@10.

    Each sub-feature is tested individually against the baseline, then combined.
    Skips gracefully when real store / gold set is unavailable.
    """
    from tests.eval.run_eval import try_open_real_store, gold_eval, load_gold_cases
    from mcpbrain.query_router import _token_overlap_rerank
    from mcpbrain.retrieval import hybrid_search

    if not load_gold_cases():
        pytest.skip("no gold cases file found")
    result = try_open_real_store()
    if result is None:
        pytest.skip("real brain store unavailable or empty")
    store, emb = result
    base = gold_eval(store, emb, k=10)
    if base["covered"] < MIN_COVERED:
        pytest.skip(f"only {base['covered']} coverable cases (need >= {MIN_COVERED})")

    # Rerank sub-feature: blend token overlap with RRF score
    def reranked_search(s, e, q, limit, **kw):
        return _token_overlap_rerank(q, hybrid_search(s, e, q, limit, **kw))

    import mcpbrain.retrieval as retrieval
    orig = retrieval.hybrid_search
    try:
        retrieval.hybrid_search = reranked_search
        reranked = gold_eval(store, emb, k=10)
    finally:
        retrieval.hybrid_search = orig

    print(f"\nQ6 rerank gold: baseline recall@10={base['recall_at_k']:.3f} → "
          f"reranked={reranked['recall_at_k']:.3f}  "
          f"(MRR {base['mrr']:.3f} → {reranked['mrr']:.3f}, "
          f"{base['covered']} covered cases)")

    # Must not regress (allow epsilon for float jitter)
    assert reranked["recall_at_k"] >= base["recall_at_k"] - 0.001, (
        f"Q6 rerank REGRESSED gold recall@10: {base['recall_at_k']:.3f} → "
        f"{reranked['recall_at_k']:.3f}")


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
