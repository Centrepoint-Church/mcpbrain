"""Gmail incremental sync via the History API.

Implements the delta path + first-run bootstrap.
The initial bulk backfill (messages.list over recent mail) is a separate task.
"""

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext

from googleapiclient.errors import HttpError

from mcpbrain import config
from mcpbrain.chunking import CHUNKER_VERSION
from mcpbrain.sync import attachments, ingest_report
from mcpbrain.sync.normalise import normalise_gmail

log = logging.getLogger(__name__)

# googleapiclient's own exponential backoff for transient 5xx / 429 / quota
# errors, matching fleet_storage.py and backup.py. The parallel backfill pushes
# Gmail's 250-units/user/second budget far harder than the delta sync does
# (messages.get is 5 units, each attachments.get another 5), so leaving retries
# to the library is what keeps a wide fan-out from failing on rate limits.
_NUM_RETRIES = 5


def _utc_now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _parse_gmail_cursor(raw: str) -> tuple[str, str | None]:
    """Split a persisted gmail cursor into (history_id, page_token).

    Unlike Drive's changes.list, Gmail's history.list needs TWO pieces of
    state to resume mid-round: the fixed `startHistoryId` the whole delta is
    anchored to, and the `pageToken` for the specific page to continue from —
    a single rolling token (Drive's shape) isn't self-sufficient here. So the
    persisted cursor is JSON `{"history_id": ..., "page_token": ...}` while a
    round is still in progress, and collapses back to a bare historyId string
    (this function's fallback branch) once the round completes -- the same
    steady-state shape every other source's cursor already has.
    """
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return raw, None
    if isinstance(obj, dict) and "history_id" in obj:
        return str(obj["history_id"]), obj.get("page_token")
    return raw, None


def discover_gmail(service, store, source: str = "gmail", *, budget=None) -> int:
    """Page history().list, enqueue message ids, advance the cursor per page.

    modified_at is the DISCOVERY time: the history API exposes no per-message
    mtime, and a messageAdded event is new mail by definition, so discovery
    order is chronological order.

    Each page's rows and this page's resume state commit together via
    enqueue_and_advance, mirroring Drive's per-page invariant (Task 5): a
    budget cutoff mid-round costs nothing but a re-list of the current page,
    never a re-walk of the whole delta and never lost work -- the exact
    livelock this module's old sync_gmail docstring described in its own
    words ("PERMANENTLY never ingested").
    """
    cursor_raw = store.get_cursor(source)
    if cursor_raw is None:
        hid = service.users().getProfile(
            userId="me").execute(num_retries=_NUM_RETRIES)["historyId"]
        store.set_cursor(source, str(hid))
        return 0

    history_id, page_token = _parse_gmail_cursor(cursor_raw)

    enqueued = 0
    while True:
        if budget is not None and budget.expired():
            break
        kwargs: dict = {"userId": "me", "startHistoryId": history_id,
                        "historyTypes": ["messageAdded"]}
        if page_token is not None:
            kwargs["pageToken"] = page_token
        try:
            resp = service.users().history().list(
                **kwargs).execute(num_retries=_NUM_RETRIES)
        except HttpError as e:
            status = getattr(e, "resp", None) and e.resp.status
            if status in (404, 410):
                # historyId too old: reset to head and let backfill cover the gap.
                hid = service.users().getProfile(
                    userId="me").execute(num_retries=_NUM_RETRIES)["historyId"]
                store.set_cursor(source, str(hid))
                return enqueued
            if page_token is not None:
                # A resumed page's pageToken itself went stale. The round's anchor
                # (history_id) is still valid -- only persist THAT, discarding the
                # broken resume position, so the next call restarts the round
                # cleanly from page 1 instead of retrying the same dead pageToken
                # forever (a bounded re-list, never data loss).
                store.set_cursor(source, str(history_id))
            raise

        latest = resp.get("historyId", history_id)
        items = []
        for record in resp.get("history", []):
            for added in record.get("messagesAdded", []):
                mid = (added.get("message") or {}).get("id")
                if mid:
                    items.append({"ref_id": mid, "version": "", "event": "upsert",
                                  "modified_at": _utc_now_iso()})

        nxt = resp.get("nextPageToken")
        next_cursor = (json.dumps({"history_id": history_id, "page_token": nxt})
                      if nxt else str(latest))
        # THE invariant: this page's rows and this page's resume state, one commit.
        store.enqueue_and_advance(items, source=source, cursor=next_cursor)
        enqueued += len(items)

        if nxt is None:
            break
        page_token = nxt
    return enqueued


def handle_gmail_item(service, store, item, *, fetch_attachments: bool = False,
                      bulk_section=None) -> None:
    """Work one queued Gmail message. Raises on failure so the loop backs off.

    Matches the per-message body of the old (now-deleted, pre-queue)
    sync_gmail's `for mid in new_message_ids:` loop -- verbatim, not invented:
    normalise_gmail takes a `report` dict and writes chunk-by-chunk via
    store.upsert_chunk (singular), and attachments are a SEPARATE fetch
    (attachments.fetch_and_normalise) hoisted OUTSIDE bulk_section because it
    is network I/O, not a store write.

    A 404 means the message was deleted between discovery and now: that is
    DONE, not a failure -- returning (rather than raising) deletes the row.

    `fetch_attachments` must be read ONCE (config.gmail_attachments(home)) and
    passed in by the Task 8 wiring, not re-read per call -- config.read_config
    does an uncached exists()+read_text()+json.loads() per call, and paying
    that once per message is the exact overhead class the 0.7.105 fix removed
    from metadata queries.
    """
    bulk_section = bulk_section or nullcontext
    try:
        raw = service.users().messages().get(
            userId="me", id=item["ref_id"], format="full").execute(
                num_retries=_NUM_RETRIES)
    except HttpError as e:
        if getattr(e, "resp", None) is not None and e.resp.status == 404:
            return
        raise
    skips: dict = {}
    att_chunks = (attachments.fetch_and_normalise(service, raw, store=store)
                  if fetch_attachments else [])
    with bulk_section():
        for chunk in normalise_gmail(raw, report=skips):
            store.upsert_chunk(chunk.doc_id, chunk.text, chunk.content_hash,
                               chunk.metadata)
        for chunk in att_chunks:
            store.upsert_chunk(chunk.doc_id, chunk.text, chunk.content_hash,
                               chunk.metadata)


def _fetch_one(service, mid: str, *, fetch_attachments: bool,
               att_report: dict | None) -> tuple[dict | None, list]:
    """Fetch one message and its attachments. Returns (raw, attachment_chunks).

    Pure network + pure transformation: touches the STORE not at all, so this is
    safe to run on a worker thread while the caller does every write. Returns
    (None, []) for a message that 404s (deleted between list and get — normal,
    not an error).

    Attachment skips are TALLIED into `att_report` rather than written, because a
    write from here would break the single-writer rule and, over a whole
    mailbox's images and .zips, evict the 500-row change_log.
    """
    try:
        raw = service.users().messages().get(
            userId="me", id=mid, format="full").execute(num_retries=_NUM_RETRIES)
    except HttpError as e:
        resp_err = getattr(e, "resp", None)
        if resp_err is not None and resp_err.status == 404:
            return None, []
        raise
    att = (attachments.fetch_and_normalise(service, raw, report=att_report)
           if fetch_attachments else [])
    return raw, att


def backfill_gmail(service, store, after: str, before: str | None = None,
                   max_messages: int | None = None, bulk_section=None,
                   q_extra: str = "", max_workers: int = 1,
                   service_factory=None) -> int:
    """One-shot bounded backfill via messages.list with an `after:YYYY/MM/DD` query.

    Fetches each matched message (format=full), normalises, upserts its chunks.
    Does NOT touch the History cursor. Returns the number of messages indexed.

    `before` (YYYY/MM/DD) optionally caps the upper bound so callers can walk a
    historical window without re-fetching newer mail. Omit it for the original
    "everything since X" semantics.

    `max_messages` bounds the run (the progressive-backfill step caps it at
    `_BACKFILL_MAX_PER_SOURCE`, default 200); pass None for "the whole window".
    No cursor is touched either way, so an interrupted run simply re-runs and the
    content-hash keying makes re-processing a no-op.

    Parallelism
    -----------
    `max_workers` > 1 (with a `service_factory`) fetches a PAGE of messages
    concurrently — each message costs a `messages.get` plus one
    `attachments.get` per attachment, so a full-history attachment pass is
    entirely network-bound and sequential fetching wastes hours.

    The split is the one proven in sync/drive.reingest_files: workers FETCH,
    the calling thread WRITES. Two hard constraints drive it —
      * the store is single-writer, so every `upsert_chunk` must stay on one
        thread; and
      * googleapiclient's Resource wraps a stateful httplib2.Http that is not
        safe to share across threads, hence `service_factory` (one Resource per
        worker, built lazily and reused via thread-local storage) rather than
        passing `service` in.
    `max_workers` > 1 WITHOUT a factory stays sequential rather than silently
    sharing one Resource.

    Work is fanned out one page (<=100 messages) at a time, not over the whole
    mailbox: it bounds both memory and how much re-fetching an interruption
    costs, while still keeping every worker busy.

    `bulk_section` (default `contextlib.nullcontext`) still brackets each
    message's writes, and attachment fetching stays OUTSIDE it — see
    `handle_gmail_item` for why `_bulk_lock` must never be held across
    network I/O.
    """
    if bulk_section is None:
        bulk_section = nullcontext
    q = f"after:{after}"
    if before:
        q += f" before:{before}"
    if q_extra:
        # Server-side narrowing, appended verbatim. The attachment repair passes
        # "has:attachment" so a full-history pass fetches ONLY attachment-bearing
        # mail: A1's fix works on new mail via the delta discovery/work path,
        # but Gmail sync is delta-driven and never revisits history, so every
        # attachment already in the mailbox stays invisible without a pass
        # like this.
        q += f" {q_extra}"
    page_token, processed = None, 0
    skips: dict = {}
    att_report: dict = {}
    # Read once — see handle_gmail_item for why this must not sit inside the loop.
    fetch_attachments = config.gmail_attachments(str(config.app_dir()))
    parallel = max_workers > 1 and service_factory is not None

    def _flush_skips() -> None:
        for reason, count in sorted(skips.items()):
            ingest_report.record_skip(store, f"gmail_{reason}", "gmail", str(count))
        attachments.flush_skip_report(store, att_report)

    def _write(raw, att_chunks) -> None:
        """The ONLY writer. Always runs on the calling thread."""
        nonlocal processed
        with bulk_section():
            for ch in normalise_gmail(raw, report=skips):
                store.upsert_chunk(ch.doc_id, ch.text, ch.content_hash, ch.metadata)
            for ch in att_chunks:
                store.upsert_chunk(ch.doc_id, ch.text, ch.content_hash, ch.metadata)
            processed += 1

    pool = _local = None
    if parallel:
        pool = ThreadPoolExecutor(max_workers=max_workers)
        _local = threading.local()

    def _worker_service():
        if not hasattr(_local, "service"):
            _local.service = service_factory()
        return _local.service

    def _fetch(mid):
        """Runs ON the worker thread — which is the point.

        `_worker_service()` MUST be called here, not passed in at submit time:
        `pool.submit(_fetch_one, _worker_service(), mid)` evaluates it eagerly on
        the SUBMITTING thread, handing every worker the same Resource. That
        interleaves reads on one httplib2.Http socket and surfaces as
        `'NoneType' object has no attribute 'read'` / `IncompleteRead(N bytes
        read)` — and corrupts the main thread's own pagination call too, killing
        the run. Same closure shape as drive.reingest_files._fetch.
        """
        return _fetch_one(_worker_service(), mid,
                          fetch_attachments=fetch_attachments,
                          att_report=att_report)

    try:
        while True:
            params = {"userId": "me", "q": q, "maxResults": 100}
            if page_token:
                params["pageToken"] = page_token
            resp = service.users().messages().list(
                **params).execute(num_retries=_NUM_RETRIES)
            ids = [m["id"] for m in resp.get("messages", [])]
            if max_messages is not None:
                # Trim to what is still wanted. The page is the fan-out unit, so
                # a parallel run can overshoot by less than one page; bounding
                # here keeps that to zero.
                ids = ids[:max(0, max_messages - processed)]

            if not parallel:
                for mid in ids:
                    raw, att = _fetch_one(service, mid,
                                          fetch_attachments=fetch_attachments,
                                          att_report=att_report)
                    if raw is not None:
                        _write(raw, att)
            else:
                futures = [pool.submit(_fetch, mid) for mid in ids]
                for future in as_completed(futures):
                    try:
                        raw, att = future.result()
                    except Exception as exc:  # noqa: BLE001 — one message must not end the run
                        log.warning("backfill: message fetch failed: %s", exc)
                        continue
                    if raw is not None:
                        _write(raw, att)

            page_token = resp.get("nextPageToken")
            if not page_token or (max_messages is not None
                                  and processed >= max_messages):
                break
    finally:
        if pool is not None:
            pool.shutdown(wait=True)
    _flush_skips()
    return processed


def reingest_messages(service, store, thread_ids: list, *,
                      report: dict | None = None) -> dict:
    """Re-fetch and re-chunk specific Gmail threads by id, under the current
    chunker version.

    **Deliberately sequential, unlike its Drive twin `reingest_files`.** That
    function cleanly separates a pure fetch step (`_reingest_one`, returning a
    tagged outcome) from an unwrapped write step (`_apply`), so the fetch half
    can run on worker threads. Here, one message's fetch, normalise, AND
    write (upsert_chunk / patch_chunk_metadata) share a single try/except (see
    the "Any OTHER failure" paragraph below) so a write-time error is retried
    like a fetch-time one -- splitting that boundary to add worker threads
    would mean re-deriving which failures are retryable vs. permanent, which
    is exactly the class of change worth doing deliberately, not as a side
    effect of adding `max_workers`. `bin/repair.py reingest-stale --workers N`
    therefore only parallelizes the Drive phase; the Gmail phase always runs
    single-threaded, regardless of `--workers` (see phase_reingest_stale).

    The mechanism the repair needs and the sync layer lacks: ordinary delta
    sync (`discover_gmail`/`handle_gmail_item`) only ever touches NEW messages
    via the History API, so a message chunked by an older chunker (e.g.
    before a `chunk_text` change) is never revisited by it -- see
    `store.stale_chunker_ids`, which this feeds.

    Per message: fetch -> normalise_gmail (+ normalise_attachment, via
    `_fetch_one`'s own attachment fetch) -> upsert_chunk, which replaces the
    chunks' text/metadata (and CHUNKER_VERSION) in place.

    **Attachments are re-fetched too**, gated on the same
    `config.gmail_attachments` flag `handle_gmail_item`/`backfill_gmail` read. They
    are not incidental here: `gmail-<mid>-att-<idx>-<i>` chunks are produced
    by `sync/attachments.normalise_attachment`, which routes spreadsheets
    through `sync/tabular.render_chunks` -- so an emailed workbook's chunks
    are exactly the ones the tabular-rendering fix (and the CHUNKER_VERSION
    bump behind it) exists to repair. A body-only re-ingest would refresh the
    body chunks, leave every attachment chunk on the old version, and the
    thread would therefore be re-selected by `store.stale_chunker_ids`
    forever -- the same non-convergence this function's missing/empty stamping
    guards against, on a different axis.

    Attachment chunks are ALSO the one shape here that can SHRINK: the whole
    point of the tabular fix is that a phantom-column-inflated sheet collapses
    to fewer, correctly-rendered chunks, which would leave the surplus tail
    doc_ids behind as searchable garbage (B5). So attachment doc_ids get a
    Drive-style orphan sweep (`store.delete_chunks`, which clears the
    vec_chunks/fts_chunks mirrors too), scoped to `-att-` doc_ids only -- a
    message BODY cannot shrink (immutable once received), so body doc_ids need
    no sweep and are deliberately never touched by it. The sweep is skipped
    entirely when this message's attachment tally recorded a fetch failure or
    an empty extraction, mirroring `drive.upsert_file_chunks`' `partial` guard:
    `attachments.fetch_and_normalise` is best-effort per attachment (a 404 or a
    failed extraction silently yields fewer chunks), and deleting on THAT would
    destroy previously-good chunks over a transient error. It is also skipped
    when attachment fetching is off in config -- with the flag off `att_chunks`
    is empty for every message, which is not evidence that anything shrank.

    Isolation is per THREAD, but within a thread each of its already-chunked
    messages is re-fetched individually (a doc_id's second `-`-separated
    segment is the message id, `gmail-<mid>-body-<i>` / `gmail-<mid>-att-...`
    both parse the same way). Two outcomes are NOT left alone the way
    "nothing acted on it" would suggest -- both stamp the message's existing
    chunks to the current chunker_version anyway, exactly like
    sync/drive.py's reingest_files stamps a missing/empty Drive file's
    chunks, or store.stale_chunker_ids keeps re-selecting the same dead
    message on every single repair run forever (the non-convergence Drive's
    reingest_files measured live before that guard existed: ~46 repeat
    fetches of the same 10 files in 41 minutes):
      * `missing` -- the message 404s (deleted/inaccessible).
      * `empty` -- the message fetches fine but normalises to zero chunks,
        body AND attachments (e.g. no body, or a future
        `has_content`/`chunk_text` tightening). Reachable even though a
        message's raw bytes never shrink, because this whole function only
        runs after CHUNKER_VERSION itself changed. The half-empty case (body
        normalises to nothing, attachments still produce chunks) is counted as
        a normal re-ingest, but its orphaned body doc_ids are stamped the same
        way -- see the inline comment; without that, folding attachments into
        the emptiness check would have LOST the convergence guard for exactly
        that shape.
    Any OTHER failure -- a transient 5xx, a network error, or an exception
    from the write path itself (patch_chunk_metadata/normalise_gmail/
    upsert_chunk) -- is retryable, so the WHOLE per-message operation is
    wrapped in one try/except (mirroring drive.py's `_reingest_one`, which
    wraps its metadata fetch, content fetch, AND normalise_drive in a single
    try/except so a normalise-time failure still resolves to "failed" rather
    than escaping and aborting every remaining thread_id in the batch). It is
    counted as `failed` and left untouched -- stamping a merely-transient
    failure would wrongly converge it out of the selector too.

    Attachment skips are TALLIED, never written per attachment, for the same
    reason `backfill_gmail` tallies them: `change_log` is pruned to 500 rows
    and doubles as the user-facing digest, so one row per skipped image/.zip
    across a repair sweep would evict the whole audit trail. When the caller
    supplies `report`, the tally is merged into it and the caller flushes
    (matching how `bin/repair.py` flushes Drive's); otherwise this flushes its
    own one-row-per-kind summary before returning.

    Returns {"messages": n_reingested, "missing": n, "empty": n, "failed": n}.
    `messages` counts only messages that actually wrote fresh chunks; `empty`
    is its own counter (not folded into `messages`) since nothing was
    written for those.
    """
    summary = {"messages": 0, "missing": 0, "empty": 0, "failed": 0}
    # Read once, not per message -- config.read_config is an uncached
    # exists()+read_text()+json.loads() (see handle_gmail_item for the same hoist).
    fetch_attachments = config.gmail_attachments(str(config.app_dir()))
    att_skips: dict = {}
    for thread_id in thread_ids:
        doc_ids = [c["doc_id"] for c in store.thread_chunks(thread_id)]
        message_ids = {
            d.split("-")[1] for d in doc_ids if d.startswith("gmail-")
        } or {thread_id}
        for mid in message_ids:
            own_doc_ids = [d for d in doc_ids if d.split("-")[1] == mid]
            # This message's OWN tally, so the orphan sweep can tell a
            # best-effort attachment failure apart from a genuine shrink;
            # merged into the run-wide tally below either way.
            msg_att_skips: dict = {}
            try:
                raw, att_chunks = _fetch_one(
                    service, mid, fetch_attachments=fetch_attachments,
                    att_report=msg_att_skips)
                for key, count in msg_att_skips.items():
                    att_skips[key] = att_skips.get(key, 0) + count
                if raw is None:
                    # 404 -- stamp the existing chunks so the selector stops
                    # re-picking this dead message. Exact id-segment match,
                    # not a substring check -- "m1" must not match a doc_id
                    # for "m10".
                    for doc_id in own_doc_ids:
                        store.patch_chunk_metadata(
                            doc_id, chunker_version=CHUNKER_VERSION,
                            reextract_missing=True)
                    summary["missing"] += 1
                    continue
                skips: dict = {}
                body_chunks = list(normalise_gmail(raw, report=skips))
                chunks = body_chunks + list(att_chunks)
                if not chunks:
                    # Fetched fine, nothing survived normalisation. Stamp
                    # anyway -- mirrors Drive's "empty" outcome -- or this
                    # message is re-selected and re-fetched forever.
                    log.info("reingest_messages: %s yielded no chunks", mid)
                    for doc_id in own_doc_ids:
                        store.patch_chunk_metadata(
                            doc_id, chunker_version=CHUNKER_VERSION,
                            reextract_empty=True)
                    summary["empty"] += 1
                    continue
                if not body_chunks:
                    # The BODY normalised to nothing while attachments still
                    # produced chunks -- the `empty` outcome above, narrowed
                    # to one half of the message. Its body doc_ids are not in
                    # `written`, so without this they keep the old
                    # chunker_version and re-select the thread forever.
                    # Body normalisation is deterministic on the fetched raw
                    # (no best-effort partiality, unlike attachments), so
                    # converging it here is safe.
                    for doc_id in own_doc_ids:
                        if doc_id.startswith(f"gmail-{mid}-body-"):
                            store.patch_chunk_metadata(
                                doc_id, chunker_version=CHUNKER_VERSION,
                                reextract_empty=True)
                for c in chunks:
                    # store.upsert_chunk writes NOTHING -- text, embedding AND
                    # metadata -- when the content_hash is unchanged, so
                    # without this fallback a byte-identical re-chunk never
                    # acquires the current chunker_version and
                    # store.stale_chunker_ids re-selects the thread on every
                    # run forever (burning Gmail quota, reporting success).
                    # That is the NORMAL case here, not a corner one: the
                    # CHUNKER_VERSION 2->3 bump changed sync/tabular.py only,
                    # so a plain-prose body re-chunks identically. Same
                    # pattern, same reason, as drive.upsert_file_chunks.
                    if not store.upsert_chunk(c.doc_id, c.text, c.content_hash,
                                              c.metadata):
                        store.patch_chunk_metadata(c.doc_id, **c.metadata)
                _sweep_attachment_orphans(
                    store, mid, own_doc_ids, chunks,
                    fetch_attachments=fetch_attachments,
                    att_skips=msg_att_skips)
                summary["messages"] += 1
            except Exception as exc:  # noqa: BLE001 -- one bad message must not end the run
                log.warning("reingest_messages: %s failed: %s", mid, exc)
                summary["failed"] += 1
    if report is not None:
        for key, count in att_skips.items():
            report[key] = report.get(key, 0) + count
    elif att_skips:
        attachments.flush_skip_report(store, att_skips,
                                      source="repair:reingest")
    return summary


# Best-effort attachment outcomes that make "fewer chunks than before"
# ambiguous: fetch_failed is a transient 404/5xx, and attachment_empty
# conflates "genuinely no extractable content" with "the extractor raised"
# (normalise_attachment returns [] for both). Either one present means the
# shorter output is not evidence of a shrink, so nothing is deleted.
#
# attachment_unsupported is included too, even though today it's genuinely
# deterministic (an unsupported mime type stays unsupported between when a
# chunk was written and a later repair run) -- that's only true as long as
# the supported-mime-type set never SHRINKS. If a future release ever drops
# support for a type, this outcome would otherwise be treated as ordinary
# and the orphan sweep would delete previously-good chunks for it. Treating
# it as ambiguous like the other two costs a rare missed cleanup; treating
# it as safe costs real chunks if that assumption ever breaks -- the cheaper
# mistake to make by default.
_ATT_PARTIAL_SKIPS = ("attachment_fetch_failed", "attachment_empty",
                      "attachment_unsupported")


def _sweep_attachment_orphans(store, mid: str, own_doc_ids: list, chunks: list,
                              *, fetch_attachments: bool,
                              att_skips: dict) -> int:
    """Delete this message's leftover `-att-` chunks that the re-render no
    longer produces. Returns the number deleted (0 when skipped).

    Split out of reingest_messages because the three conditions that must
    hold before deleting anything are the whole substance of the operation --
    see reingest_messages' docstring for why each one is load-bearing.
    """
    if not fetch_attachments:
        return 0
    if any(kind in _ATT_PARTIAL_SKIPS for kind, _mime in att_skips):
        log.info("reingest_messages: %s had best-effort attachment skips; "
                 "skipping the orphan sweep so good chunks are not deleted", mid)
        return 0
    prefix = f"gmail-{mid}-att-"
    written = {c.doc_id for c in chunks}
    orphans = [d for d in own_doc_ids
               if d.startswith(prefix) and d not in written]
    if not orphans:
        return 0
    log.info("reingest_messages: %s re-rendered to fewer attachment chunks; "
             "deleting %d orphan(s)", mid, len(orphans))
    store.delete_chunks(orphans)
    return len(orphans)
