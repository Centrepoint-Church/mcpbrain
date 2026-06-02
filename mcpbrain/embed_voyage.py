import os

import voyageai


class VoyageEmbedder:
    dim = 1024

    def __init__(self, api_key: str | None = None):
        self._model = "voyage-4-lite"  # same tier for query AND passage (symmetric)
        self._client = voyageai.Client(api_key=api_key or os.environ["VOYAGE_API_KEY"])

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        return self._client.embed(texts, model=self._model, input_type="document").embeddings

    def embed_query(self, text: str) -> list[float]:
        return self._client.embed([text], model=self._model, input_type="query").embeddings[0]
