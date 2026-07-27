"""Recall must not queue behind a bulk embedding batch."""
import threading
import time

from mcpbrain.embed import _LocalEmbedder


def test_embed_query_is_not_blocked_by_a_long_passage_batch():
    emb = _LocalEmbedder.__new__(_LocalEmbedder)
    emb.dim = 4
    emb._qp = ""
    emb._lock = threading.Lock()

    class _Model:
        def embed(self, texts):
            time.sleep(0.5)                      # a bulk batch
            return [[0.0] * 4 for _ in texts]

        def query_embed(self, texts):
            return iter([[0.0] * 4])

    emb._model = _Model()
    t = threading.Thread(target=lambda: emb.embed_passages(["x"] * 4), daemon=True)
    t.start()
    time.sleep(0.05)
    start = time.monotonic()
    emb.embed_query("hello")
    elapsed = time.monotonic() - start
    t.join(timeout=5)
    assert elapsed < 0.3, f"embed_query waited {elapsed:.2f}s behind the batch"
