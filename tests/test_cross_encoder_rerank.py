import pytest
from mcpbrain import embed


@pytest.mark.slow
def test_get_reranker_scores_relevant_higher():
    rr = embed.get_reranker()
    scores = rr.rerank("how do I reset my password?",
                       ["Password reset instructions are here.",
                        "The cafeteria menu for Tuesday."])
    assert scores[0] > scores[1]
