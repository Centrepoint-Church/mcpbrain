import asyncio

from mcpbrain.store import Store
from mcpbrain.mcp_server import make_brain_search


class FakeEmbedder:
    dim = 4

    def embed_passages(self, texts):
        return [[1.0, 0, 0, 0] if "budget" in t else [0, 1.0, 0, 0] for t in texts]

    def embed_query(self, text):
        return [1.0, 0, 0, 0] if "budget" in text else [0, 1.0, 0, 0]


def _seed(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    s.upsert_chunk("d-budget", "the annual budget review", "h1", {})
    s.upsert_chunk("d-roster", "the volunteer roster", "h2", {})
    from mcpbrain.index import index_pending
    index_pending(s, FakeEmbedder())
    return s


def test_brain_search_passes_score_through(tmp_path):
    s = _seed(tmp_path)
    search = make_brain_search(s, FakeEmbedder())
    results = asyncio.run(search("budget", 5))
    assert results, "expected hits"
    assert all("score" in r for r in results)
    assert results[0]["score"] == 1.0
