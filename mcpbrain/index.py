# mcpbrain/index.py
from mcpbrain.embed import contextual_prefix


def index_pending(store, embedder, batch_size: int = 32) -> int:
    pending = store.unembedded_chunks()
    done = 0
    for i in range(0, len(pending), batch_size):
        batch = pending[i:i + batch_size]
        vectors = embedder.embed_passages(
            [(contextual_prefix(c["metadata"]) + c["text"]) for c in batch]
        )
        for c, v in zip(batch, vectors):
            store.write_embedding(c["rowid"], v)
            done += 1
    return done
