import asyncio

from mcpbrain.mcp_server import make_brain_search


class FakeControlClient:
    """brain_search is now a thin passthrough to ControlClient.recall() (the
    daemon embeds server-side and computes score); this fakes that boundary
    to check the score field survives the passthrough untouched."""

    def __init__(self, results):
        self._results = results

    def recall(self, query, limit=10):
        return self._results


def test_brain_search_passes_score_through(tmp_path):
    client = FakeControlClient(
        results=[{"doc_id": "d-budget", "text": "the annual budget review", "score": 1.0}]
    )
    search = make_brain_search(client)
    results = asyncio.run(search("budget", 5))
    assert results, "expected hits"
    assert all("score" in r for r in results)
    assert results[0]["score"] == 1.0
