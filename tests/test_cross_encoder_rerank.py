import pytest
from mcpbrain import embed, query_router as qr


@pytest.mark.slow
def test_get_reranker_scores_relevant_higher():
    rr = embed.get_reranker()
    scores = rr.rerank("how do I reset my password?",
                       ["Password reset instructions are here.",
                        "The cafeteria menu for Tuesday."])
    assert scores[0] > scores[1]


class _FakeRR:
    def rerank(self, query, passages):
        # score = 1.0 if "match" in passage else 0.0
        return [1.0 if "match" in p else 0.0 for p in passages]


def test_cross_encoder_rerank_reorders_by_score():
    results = [{"doc_id": "a", "text": "nope", "score": 0.9},
               {"doc_id": "b", "text": "match here", "score": 0.5}]
    out = qr._cross_encoder_rerank("q", results, _FakeRR())
    assert [r["doc_id"] for r in out] == ["b", "a"]


def test_rerank_model_default(tmp_path):
    from mcpbrain import config
    assert config.rerank_model(str(tmp_path)) == "Xenova/ms-marco-MiniLM-L-6-v2"


def test_route_rerank_falls_back_to_lexical_when_model_is_lexical(monkeypatch, tmp_path):
    # rerank_model='lexical' must use the token-overlap path, not load a model
    import mcpbrain.config as config
    (tmp_path / "config.json").write_text(
        '{"retrieval_rerank": true, "rerank_model": "lexical"}')
    called = {"cross": False}
    monkeypatch.setattr(qr, "_cross_encoder_rerank",
                        lambda *a, **k: called.__setitem__("cross", True) or a[1])
    results = [{"doc_id": "a", "text": "alpha beta", "score": 1.0}]
    out = qr._apply_rerank("alpha", results, home=str(tmp_path))
    assert called["cross"] is False
    assert out == results  # lexical path ran, order unchanged for single result
