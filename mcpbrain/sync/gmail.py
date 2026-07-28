"""Gmail incremental sync via the History API.

Implements the delta path + first-run bootstrap.
The initial bulk backfill (messages.list over recent mail) is a separate task.
"""

import json
from contextlib import nullcontext

from googleapiclient.errors import HttpError

from mcpbrain import config
from mcpbrain.sync import attachments, ingest_report
from mcpbrain.sync.normalise import normalise_gmail


def sync_gmail(service, store, source: str = "gmail", *, budget=None,
               bulk_section=None) -> int:
    """Incremental Gmail sync via the History API.

    First run (no cursor): reads the current historyId from getProfile,
    stores it as the cursor, and returns 0 — no messages fetched; the bulk
    backfill is a separate task.

    Subsequent runs: lists history since the stored historyId, collects
    newly-added message ids (deduped, ordered), fetches each full message,
    normalises it, and upserts its chunks. Advances the cursor to the latest
    historyId ONLY after all messages are durably upserted.

    Bounded and checkpoint-safe (Task 2 duty-cycle fix): `budget` (a `Budget`,
    or None for unbounded) is checked once per history.list page and once per
    message fetch. On expiry the loop stops early WITHOUT advancing the
    cursor — see "Checkpointing" below for why that is the correct, safe
    behaviour rather than a naive partial cursor advance.

    `bulk_section` (a zero-arg context-manager factory, defaulting to
    `contextlib.nullcontext`) brackets each individual message's
    fetch-normalise-upsert — the only step here that mutates `chunks` — so
    `_bulk_lock` is released between messages instead of held for the whole
    call. A soak test showed a single lock hold spanning a whole (even
    budget-bounded) sync call still starves the maintenance thread's 5s
    acquire almost every time; per-message sections give it a real chance
    every message, not once per call.

    Checkpointing
    -------------
    Gmail's `history.list` "historyId" response field is a SNAPSHOT of the
    mailbox's current state as of the query, not a per-page incremental
    cursor — the same value is returned on every page of one delta round. So
    advancing the REAL stored cursor (`source`, e.g. "gmail") to that value
    before every page's `messagesAdded` records have actually been
    fetched-and-upserted would permanently skip whatever was on the unvisited
    pages (they occurred strictly before the new cursor value, so a future
    `startHistoryId=<new cursor>` query can never see them again). The real
    cursor therefore still only advances once the WHOLE delta round —
    pagination AND every collected message — completes without an early exit.

    That alone is NOT sufficient once a single delta round is bigger than one
    budget's worth of work: naively "just don't advance the cursor" means
    every subsequent call re-lists the SAME `startHistoryId`, gets the SAME
    ordered message-id list, and — with the SAME per-call budget — processes
    the SAME PREFIX of it every time. Reproduced directly: 5 identical calls
    over a 7-message delta with a budget that only covers 2 messages each
    time produced `processed=2` and an unmoved cursor on EVERY call — the
    delta never converges and messages past the prefix are silently and
    PERMANENTLY never ingested. This is genuinely worse than the pre-budget
    behaviour (unbounded, but always eventually completed).

    The fix is a second, separate piece of persisted state: `f"{source}:resume_ids"`
    (a JSON list, stored via the same generic `sync_cursors` table as the real
    cursor — no schema change) holding the message ids ALREADY durably
    upserted so far during the CURRENT, not-yet-committed delta round. Each
    call reads it, SKIPS any message id already in it (no re-fetch, no
    re-upsert — genuine forward progress, not just idempotent re-work), and
    persists the updated set after processing whatever it reaches this call.
    A message that 404s (deleted between listing and fetching) is also added
    to the resume set — there's nothing to write for it, but it must not
    permanently occupy the "next thing to retry" slot forever. Only once
    EVERY id in this round's message list is in the resume set (checked via
    set difference, not just "loop finished" — the id list itself can grow
    between calls if new mail keeps arriving) does the real cursor advance,
    and the resume set is cleared for the next fresh delta round.

    This makes forward progress monotonic: each call either finishes the
    round (cursor advances) or durably grows the resume set (skipping
    already-done ids next time, spending its budget only on genuinely new
    work) — never repeats the exact same prefix twice.

    Returns the number of messages processed this call (durably upserted just
    now; already-resumed ids from a prior call are not re-counted).
    """
    if bulk_section is None:
        bulk_section = nullcontext
    cursor = store.get_cursor(source)

    # First run — bootstrap
    if cursor is None:
        hid = service.users().getProfile(userId="me").execute()["historyId"]
        store.set_cursor(source, str(hid))
        return 0

    resume_key = f"{source}:resume_ids"
    try:
        resumed_ids: set = set(json.loads(store.get_cursor(resume_key) or "[]"))
    except (ValueError, TypeError):
        resumed_ids = set()

    # Delta run — page through history.list. This loop only READS (it collects
    # message ids into memory); it never mutates `chunks`, so it needs a budget
    # check but no bulk_section.
    new_message_ids: list[str] = []
    latest_history_id: str = cursor
    page_token = None
    pagination_interrupted = False

    try:
        while True:
            if budget is not None and budget.expired():
                pagination_interrupted = True
                break
            kwargs: dict = {
                "userId": "me",
                "startHistoryId": cursor,
                "historyTypes": ["messageAdded"],
            }
            if page_token is not None:
                kwargs["pageToken"] = page_token

            response = service.users().history().list(**kwargs).execute()

            # Track the most recent historyId seen; fall back to current if absent
            latest_history_id = response.get("historyId", latest_history_id)

            for record in response.get("history", []):
                for added in record.get("messagesAdded", []):
                    mid = (added.get("message") or {}).get("id")
                    if mid and mid not in new_message_ids:
                        new_message_ids.append(mid)

            page_token = response.get("nextPageToken")
            if page_token is None:
                break
    except HttpError as e:
        if getattr(e, "resp", None) is not None and e.resp.status in (404, 410):
            # historyId too old / invalid — reset to current and let a backfill fill the gap
            hid = service.users().getProfile(userId="me").execute()["historyId"]
            store.set_cursor(source, str(hid))
            store.set_cursor(resume_key, "[]")
            return 0
        raise

    # Fetch, normalise, and upsert each message NOT already in the resume set
    # (already durably processed in an earlier, budget-cut call for this same
    # round). A 404 on an individual id means the message was deleted between
    # history.list and our get — treated as "done" too (nothing to write, but
    # it must not block forward progress forever). Other HttpErrors still
    # propagate so the cursor stays at the last good position and the next
    # run retries the REST of the round — but the resume set is persisted
    # PER ITEM (not once after the whole loop) specifically so that when this
    # happens, the messages already durably upserted before the raise are not
    # discarded from the checkpoint: a plain "persist once after the loop"
    # would otherwise mean a poison message at a fixed position (or a process
    # death / STALL_S watchdog restart landing mid-loop) re-triggers the same
    # Critical-B livelock shape on a narrower trigger — the whole call's
    # progress lost even though the writes themselves were already durable.
    messages_processed = 0
    fetch_interrupted = False
    skips: dict = {}
    # Read once: the value cannot change mid-loop, and config.read_config does
    # an uncached exists()+read_text()+json.loads() per call — paying that once
    # per message would be the same class of overhead the 0.7.105 fix removed
    # from the per-chunk metadata queries.
    fetch_attachments = config.gmail_attachments(str(config.app_dir()))
    for mid in new_message_ids:
        if mid in resumed_ids:
            continue
        # Minimum forward progress: honour the budget only once this call
        # has written something. Checking before the first item means a
        # budget already spent upstream yields zero writes, leaves the
        # resume set unchanged, and re-does identical work next cycle --
        # the livelock reproduced in sync_drive. One item per call keeps
        # the round monotonic.
        if messages_processed and budget is not None and budget.expired():
            fetch_interrupted = True
            break
        try:
            raw = service.users().messages().get(userId="me", id=mid, format="full").execute()
        except HttpError as e:
            resp = getattr(e, "resp", None)
            if resp is not None and resp.status == 404:
                resumed_ids.add(mid)
                store.set_cursor(resume_key, json.dumps(sorted(resumed_ids)))
                continue
            raise
        # Attachment fetch is NETWORK I/O and must be hoisted OUT of the bulk
        # section — the daemon-scheduling work established that _bulk_lock must
        # never be held across network calls (see _cache_first_extract_one).
        att_chunks = (attachments.fetch_and_normalise(service, raw, store=store)
                      if fetch_attachments else [])
        with bulk_section():
            for chunk in normalise_gmail(raw, report=skips):
                store.upsert_chunk(chunk.doc_id, chunk.text, chunk.content_hash,
                                   chunk.metadata)
            for chunk in att_chunks:
                store.upsert_chunk(chunk.doc_id, chunk.text, chunk.content_hash,
                                   chunk.metadata)
        messages_processed += 1
        resumed_ids.add(mid)
        store.set_cursor(resume_key, json.dumps(sorted(resumed_ids)))

    # Advance the REAL cursor only once pagination completed AND every id in
    # this round's (possibly still-growing) message list is accounted for in
    # the resume set — not merely "the fetch loop reached the end" (the id
    # list can grow between pagination and this check on a live mailbox; a
    # plain loop-completed flag would miss that). Clearing the resume set
    # marks the round as closed so the next call starts a fresh one.
    if not pagination_interrupted and not fetch_interrupted and set(new_message_ids) <= resumed_ids:
        store.set_cursor(source, str(latest_history_id))
        store.set_cursor(resume_key, "[]")

    for reason, count in sorted(skips.items()):
        ingest_report.record_skip(store, f"gmail_{reason}", source, str(count))

    return messages_processed


def backfill_gmail(service, store, after: str, before: str | None = None,
                   max_messages: int | None = None, bulk_section=None) -> int:
    """One-shot bounded backfill via messages.list with an `after:YYYY/MM/DD` query.

    Fetches each matched message (format=full), normalises, upserts its chunks.
    Does NOT touch the History cursor. Returns the number of messages indexed.

    `before` (YYYY/MM/DD) optionally caps the upper bound so callers can walk a
    historical window without re-fetching newer mail. Omit it for the original
    "everything since X" semantics.

    Already item-bounded by `max_messages` (the progressive-backfill step caps
    this at `_BACKFILL_MAX_PER_SOURCE`, default 200) and touches no cursor, so
    no budget/checkpoint logic is needed — `bulk_section` (default
    `contextlib.nullcontext`) still brackets each message's writes.
    """
    if bulk_section is None:
        bulk_section = nullcontext
    q = f"after:{after}"
    if before:
        q += f" before:{before}"
    page_token, processed = None, 0
    skips: dict = {}
    # Read once — see sync_gmail for why this must not sit inside the loop.
    fetch_attachments = config.gmail_attachments(str(config.app_dir()))

    def _flush_skips() -> None:
        for reason, count in sorted(skips.items()):
            ingest_report.record_skip(store, f"gmail_{reason}", "gmail", str(count))

    while True:
        params = {"userId": "me", "q": q, "maxResults": 100}
        if page_token:
            params["pageToken"] = page_token
        resp = service.users().messages().list(**params).execute()
        for m in resp.get("messages", []):
            if max_messages is not None and processed >= max_messages:
                _flush_skips()
                return processed
            try:
                raw = service.users().messages().get(
                    userId="me", id=m["id"], format="full"
                ).execute()
            except HttpError as e:
                resp_err = getattr(e, "resp", None)
                if resp_err is not None and resp_err.status == 404:
                    continue
                raise
            # Attachment fetch is NETWORK I/O and must be hoisted OUT of the
            # bulk section — see sync_gmail for why _bulk_lock must never be
            # held across network calls.
            att_chunks = (attachments.fetch_and_normalise(service, raw, store=store)
                          if fetch_attachments else [])
            with bulk_section():
                for ch in normalise_gmail(raw, report=skips):
                    store.upsert_chunk(ch.doc_id, ch.text, ch.content_hash, ch.metadata)
                for ch in att_chunks:
                    store.upsert_chunk(ch.doc_id, ch.text, ch.content_hash, ch.metadata)
                processed += 1
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    _flush_skips()
    return processed
