# mcpbrain/sync/cursors.py
"""Per-source sync cursors with advance-after-durable-write semantics."""


def get_cursor(store, source: str) -> str | None:
    return store.get_cursor(source)


def set_cursor(store, source: str, cursor: str) -> None:
    store.set_cursor(source, cursor)


def advance_after(store, source: str, cursor: str, write_batch) -> None:
    """Run write_batch() to durably persist a sync batch, THEN advance the cursor.

    If write_batch() raises, the cursor is left at its previous value so the next
    run re-fetches from the last durably-recorded point. This is the
    'advance only after durable write' guarantee from the plan.

    Retention note: the per-source sync modules (sync_gmail, sync_drive,
    sync_calendar) each inline this same advance-after-durable-write pattern
    directly and are individually tested. This helper is retained as the
    intended cursor-advance utility for the Phase 3 backfill/daemon runner,
    which will call it rather than inlining the pattern again. It is not
    currently dead code; it is a deliberate forward-facing API kept ready for
    that upcoming consumer.
    """
    write_batch()
    store.set_cursor(source, cursor)
