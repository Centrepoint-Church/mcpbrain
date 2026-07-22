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
