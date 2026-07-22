# mcpbrain/index.py
from mcpbrain.embed import contextual_prefix


def index_pending(store, embedder, batch_size: int = 32, *, home: str | None = None) -> int:
    """Embed pending chunks, prepending the Q6 contextual-retrieval prefix to each
    passage when enabled.

    Contextual retrieval is ON by default — validated on the live gold set to lift
    recall@10 +0.10 / MRR +0.175 (A/B 2026-06-24). It is gated by the
    `contextual_retrieval` config flag so it can be rolled back; the prefix is
    PASSAGE-ONLY (embed.contextual_prefix), never applied to the query side. `home`
    selects which config to read (defaults to the app dir).
    """
    pending = store.unembedded_chunks()
    done = 0
    if pending:
        from mcpbrain import config
        use_prefix = config.contextual_retrieval_enabled(home or str(config.app_dir()))
        for i in range(0, len(pending), batch_size):
            batch = pending[i:i + batch_size]
            texts = [
                (contextual_prefix(c["metadata"]) + c["text"]) if use_prefix else c["text"]
                for c in batch
            ]
            vectors = embedder.embed_passages(texts)
            for c, v in zip(batch, vectors):
                store.write_embedding(c["rowid"], v)
                done += 1
    # Phase C: drain the contextual-BM25 FTS re-index backfill in bounded
    # batches (no re-embed) so existing chunks pick up the C1 contextual
    # prefix. Runs every cycle — including when nothing is pending — so it
    # actually converges once the corpus is fully embedded.
    try:
        store.reindex_fts_batch(cap=5000)
    except Exception:  # noqa: BLE001
        pass
    return done
