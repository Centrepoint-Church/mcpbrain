"""Gmail incremental sync via the History API.

Implements the delta path + first-run bootstrap.
The initial bulk backfill (messages.list over recent mail) is a separate task.
"""

from googleapiclient.errors import HttpError

from mcpbrain.sync.normalise import normalise_gmail


def sync_gmail(service, store, source: str = "gmail") -> int:
    """Incremental Gmail sync via the History API.

    First run (no cursor): reads the current historyId from getProfile,
    stores it as the cursor, and returns 0 — no messages fetched; the bulk
    backfill is a separate task.

    Subsequent runs: lists history since the stored historyId, collects
    newly-added message ids (deduped, ordered), fetches each full message,
    normalises it, and upserts its chunks. Advances the cursor to the latest
    historyId ONLY after all messages are durably upserted.

    Returns the number of messages processed.
    """
    cursor = store.get_cursor(source)

    # First run — bootstrap
    if cursor is None:
        hid = service.users().getProfile(userId="me").execute()["historyId"]
        store.set_cursor(source, str(hid))
        return 0

    # Delta run — page through history.list
    new_message_ids: list[str] = []
    latest_history_id: str = cursor
    page_token = None

    try:
        while True:
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
    for mid in new_message_ids:
        try:
            raw = service.users().messages().get(userId="me", id=mid, format="full").execute()
        except HttpError as e:
            resp = getattr(e, "resp", None)
            if resp is not None and resp.status == 404:
                continue
            raise
        for chunk in normalise_gmail(raw):
            store.upsert_chunk(chunk.doc_id, chunk.text, chunk.content_hash, chunk.metadata)
        messages_processed += 1

    # Advance cursor only after all writes are durable
    store.set_cursor(source, str(latest_history_id))

    return messages_processed


def backfill_gmail(service, store, after: str, before: str | None = None,
                   max_messages: int | None = None) -> int:
    """One-shot bounded backfill via messages.list with an `after:YYYY/MM/DD` query.

    Fetches each matched message (format=full), normalises, upserts its chunks.
    Does NOT touch the History cursor. Returns the number of messages indexed.

    `before` (YYYY/MM/DD) optionally caps the upper bound so callers can walk a
    historical window without re-fetching newer mail. Omit it for the original
    "everything since X" semantics.
    """
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
            for ch in normalise_gmail(raw):
                store.upsert_chunk(ch.doc_id, ch.text, ch.content_hash, ch.metadata)
            processed += 1
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return processed
