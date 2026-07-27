"""Gmail incremental sync via the History API.

Implements the delta path + first-run bootstrap.
The initial bulk backfill (messages.list over recent mail) is a separate task.
"""

from contextlib import nullcontext

from googleapiclient.errors import HttpError

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
    advancing the stored cursor to that value before every page's
    `messagesAdded` records have actually been fetched-and-upserted would
    permanently skip whatever was on the unvisited pages (they occurred
    strictly before the new cursor value, so a future `startHistoryId=<new
    cursor>` query can never see them again). The only safe checkpoint is
    therefore "all or nothing": the cursor advances ONLY when both the
    history-list pagination AND every collected message's fetch+upsert
    completed without an early exit.

    This is not lossy on an early exit, only WASTEFUL: `store.upsert_chunk` is
    an idempotent upsert keyed on (doc_id, content_hash) — re-fetching and
    re-upserting an already-processed message on the next attempt is a fast
    no-op (one SELECT + hash compare, no write), not a duplicate or a
    corruption. So leaving the cursor untouched after a partial run means the
    next `sync_gmail` call simply re-lists the same delta window and re-does
    (cheaply) whatever was already durably upserted, then makes forward
    progress on what wasn't. Nothing is lost, nothing is double-counted.

    Returns the number of messages processed this call (may be a partial
    count when the budget expired before every message was reached — those
    messages ARE durably upserted; only the cursor is deliberately held back).
    """
    if bulk_section is None:
        bulk_section = nullcontext
    cursor = store.get_cursor(source)

    # First run — bootstrap
    if cursor is None:
        hid = service.users().getProfile(userId="me").execute()["historyId"]
        store.set_cursor(source, str(hid))
        return 0

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
            return 0
        raise

    # Fetch, normalise, and upsert each message.
    # A 404 on an individual id means the message was deleted between
    # history.list and our get — skip it rather than crashing the whole sync.
    # Other HttpErrors still propagate so the cursor stays at the last good
    # position and the next run retries them.
    messages_processed = 0
    fetch_interrupted = False
    for mid in new_message_ids:
        if budget is not None and budget.expired():
            fetch_interrupted = True
            break
        try:
            raw = service.users().messages().get(userId="me", id=mid, format="full").execute()
        except HttpError as e:
            resp = getattr(e, "resp", None)
            if resp is not None and resp.status == 404:
                continue
            raise
        with bulk_section():
            for chunk in normalise_gmail(raw):
                store.upsert_chunk(chunk.doc_id, chunk.text, chunk.content_hash, chunk.metadata)
        messages_processed += 1

    # Advance the cursor only when NEITHER loop was cut short by the budget —
    # see the "Checkpointing" note above for why a partial advance would be
    # unsafe (it would silently skip messages on unvisited history pages).
    if not pagination_interrupted and not fetch_interrupted:
        store.set_cursor(source, str(latest_history_id))

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
    while True:
        params = {"userId": "me", "q": q, "maxResults": 100}
        if page_token:
            params["pageToken"] = page_token
        resp = service.users().messages().list(**params).execute()
        for m in resp.get("messages", []):
            if max_messages is not None and processed >= max_messages:
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
            with bulk_section():
                for ch in normalise_gmail(raw):
                    store.upsert_chunk(ch.doc_id, ch.text, ch.content_hash, ch.metadata)
                processed += 1
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return processed
