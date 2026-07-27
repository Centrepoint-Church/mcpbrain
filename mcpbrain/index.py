# mcpbrain/index.py
import logging
from contextlib import nullcontext

from mcpbrain.embed import contextual_prefix

log = logging.getLogger(__name__)


def index_pending(store, embedder, batch_size: int = 32, *, home: str | None = None,
                  budget=None, max_items: int | None = None,
                  stats: dict | None = None, bulk_section=None) -> int:
    """Embed pending chunks, prepending the Q6 contextual-retrieval prefix to each
    passage when enabled.

    Contextual retrieval is ON by default — validated on the live gold set to lift
    recall@10 +0.10 / MRR +0.175 (A/B 2026-06-24). It is gated by the
    `contextual_retrieval` config flag so it can be rolled back; the prefix is
    PASSAGE-ONLY (embed.contextual_prefix), never applied to the query side. `home`
    selects which config to read (defaults to the app dir).

    Bounded: `max_items` caps how many chunks one call fetches, and `budget`
    stops the loop between batches once the cycle's wall-clock slice is spent.
    Remaining chunks keep embedded=0 and are picked up next cycle — the work is
    resumable because it is driven by that predicate, not an in-memory cursor.

    `bulk_section` (Task 2 duty-cycle fix), if given, is a zero-arg context-
    manager factory bracketing ONE BATCH's writes (`store.write_embedding` for
    every chunk in that batch) — not the whole call. A soak test showed that
    wrapping an entire multi-batch call in one `_bulk_lock` hold (even though it
    was already `budget`-bounded to CYCLE_BUDGET_S=60s) still starved the
    maintenance thread's 5s-bounded acquire almost every time; releasing the
    lock between batches (a batch is ~32 chunks, sub-second) gives it a real,
    frequent opportunity instead of one per call. Defaults to
    `contextlib.nullcontext` so direct callers/tests that don't pass one keep
    running unlocked, exactly as before.

    `stats`, if given, is filled in with `{"capped": bool}`: True when this call
    stopped because it hit `max_items` (not because the pending set ran out or
    the budget expired), i.e. there is very likely more embedding work waiting
    right now. The caller folds that into the cycle's `more_work` so the loop
    re-wakes promptly instead of sleeping a full interval on a live backlog.
    An out-param rather than a changed return type: `-> int` is what every
    caller and a dozen tests already consume.
    """
    from mcpbrain import config
    _home = home or str(config.app_dir())
    if bulk_section is None:
        bulk_section = nullcontext
    if stats is not None:
        stats["capped"] = False
    if budget is not None and budget.expired():
        return 0
    pending = store.unembedded_chunks(limit=max_items)
    done = 0
    if pending:
        use_prefix = config.contextual_retrieval_enabled(_home)
        for i in range(0, len(pending), batch_size):
            if budget is not None and budget.expired():
                log.info("index_pending: budget spent after %d chunks", done)
                break
            batch = pending[i:i + batch_size]
            texts = [
                (contextual_prefix(c["metadata"]) + c["text"]) if use_prefix else c["text"]
                for c in batch
            ]
            vectors = embedder.embed_passages(texts)
            with bulk_section():
                for c, v in zip(batch, vectors):
                    store.write_embedding(c["rowid"], v, home=_home)
                    done += 1
    # `pending` was fetched with limit=max_items, so embedding exactly that many
    # means the fetch — not the pending set and not the budget — is what stopped
    # us. (A budget cut leaves done < max_items, so it can't false-positive.)
    if stats is not None and max_items is not None and done >= max_items:
        stats["capped"] = True
    # Phase C: drain the contextual-BM25 FTS re-index backfill in bounded
    # batches (no re-embed) so existing chunks pick up the C1 contextual
    # prefix. Runs every cycle — including when nothing is pending — so it
    # actually converges once the corpus is fully embedded.
    try:
        store.reindex_fts_batch(cap=5000)
    except Exception:  # noqa: BLE001
        log.warning("reindex_fts_batch failed; FTS contextual backfill deferred", exc_info=True)
    return done
