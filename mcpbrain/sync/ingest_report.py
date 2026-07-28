"""One durable seam for 'ingestion dropped something'.

The findings register's recurring failure mode is invisibility, not loss:
`_fetch_text` returns None for an unsupported type, `normalise_gmail` returns []
for bulk mail, and eight `except Exception: return ""` sites in extractors.py
all produce the same nothing — while the `processed` counters keep incrementing
and the dashboard reports a clean sync.

`record_change` is used rather than a bespoke table because it is already
durable, already queryable and already surfaced in the change log, so a skip
becomes auditable with no schema change.
"""

import logging

log = logging.getLogger(__name__)


def record_skip(store, kind: str, ref_id: str, detail: str = "") -> None:
    """Record that one item was not ingested, and why.

    `kind` is the reason class, and the classes must stay distinguishable —
    'unsupported_mime' (we never could) and 'extraction_empty' (we should have
    and did not) demand different responses, and B7 exists precisely because
    they were indistinguishable. Strictly best-effort in both directions: a
    missing store and a raising store are both fine.
    """
    summary = f"{kind}: {detail}" if detail else kind
    log.info("ingest skip [%s] %s %s", kind, ref_id, detail)
    if store is None:
        return
    try:
        store.record_change("ingest_skip", ref_id=ref_id, summary=summary)
    except Exception as exc:  # noqa: BLE001 — bookkeeping must not break a sync
        log.debug("ingest_report: could not record skip: %s", exc)
