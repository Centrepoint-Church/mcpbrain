"""The shared sync work loop.

Discovery (per source, in drive.py/gmail.py/calendar.py) writes rows; this
drains them. Splitting the two is what removes the livelock class: a round no
longer has to complete for progress to be durable, so a budget cutoff anywhere
costs nothing and repeats nothing.

Deliberately source-agnostic. The only per-source knowledge is the `handlers`
dict the caller passes, which maps a source prefix to the existing
fetch/extract/upsert code.
"""
import logging
from datetime import datetime, timezone

log = logging.getLogger("mcpbrain.sync.queue")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def work_queue(store, *, handlers: dict, limit: int, budget=None,
               now: str | None = None) -> dict:
    """Work up to `limit` queued items, newest-first. Returns counts.

    A handler that returns is a success (its row is deleted); one that raises
    is a failure (attempts+1, backoff, row retained). Nothing is ever dropped.
    """
    now = now or _utc_now_iso()
    processed = failed = 0
    for item in store.due_sync_items(limit=limit, now=now):
        if budget is not None and budget.expired():
            break               # free: every unreached row is still queued
        source = item["source"]
        handler = handlers.get(source.split(":", 1)[0])
        if handler is None:
            # Not a crash: an unknown source is this item's problem, not the
            # loop's. It backs off and stays visible rather than wedging sync.
            store.fail_sync_item(source, item["ref_id"],
                                 f"no handler for source {source!r}", now=now)
            failed += 1
            continue
        try:
            handler(item)
        except Exception as exc:  # noqa: BLE001 — one item must not kill the loop
            attempts = store.fail_sync_item(source, item["ref_id"], str(exc),
                                            now=now)
            log.warning("sync: %s/%s failed (attempt %d), will retry: %s",
                        source, item["ref_id"], attempts, exc)
            failed += 1
            continue
        store.complete_sync_item(source, item["ref_id"])
        processed += 1
    return {"processed": processed, "failed": failed}
