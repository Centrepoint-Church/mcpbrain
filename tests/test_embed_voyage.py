import pytest

# The Voyage embedder is an opt-in path; voyageai is an optional dependency
# (install with `pip install .[voyage]`). Skip this module when it is absent
# rather than failing collection on a default install.
pytest.importorskip("voyageai")

from mcpbrain.embed_voyage import VoyageEmbedder


class _FakeResult:
    embeddings = [[0.1] * 1024]


def test_voyage_dim_and_symmetric_model(monkeypatch):
    emb = VoyageEmbedder(api_key="x")
    monkeypatch.setattr(emb._client, "embed", lambda texts, model, input_type: _FakeResult())
    assert emb.dim == 1024
    assert len(emb.embed_query("budget")) == 1024
    # symmetric: query and passage use the SAME model tier (decision 2026-04-12)
    assert emb._model == "voyage-4-lite"
