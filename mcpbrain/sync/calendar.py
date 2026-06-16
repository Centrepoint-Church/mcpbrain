"""Google Calendar delta sync — syncToken path with HTTP 410 full-fetch fallback.

Normalises events to a single Chunk per event (doc_id = cal-<id>).
Cancelled events are skipped. Cursor (nextSyncToken) is written only after
all event chunks have been durably upserted.
"""

from datetime import datetime, timedelta, timezone

from googleapiclient.errors import HttpError

from mcpbrain.chunking import content_hash
from mcpbrain.graph_write import (
    is_junk_entity,
    upsert_entity,
    upsert_relation,
    _is_owner,
)
from mcpbrain.sync.normalise import Chunk


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def normalise_calendar(event: dict) -> list[Chunk]:
    """Convert a Calendar event dict to a list containing one Chunk.

    Returns an empty list for cancelled events.
    doc_id format: cal-<event_id> (no suffix; one chunk per event).
    """
    if event.get("status") == "cancelled":
        return []

    eid = event["id"]
    summary = event.get("summary", "(no title)")
    start = (event.get("start") or {}).get("dateTime") or (event.get("start") or {}).get("date", "")
    end = (event.get("end") or {}).get("dateTime") or (event.get("end") or {}).get("date", "")
    location = event.get("location", "")
    description = event.get("description", "")
    attendees = ", ".join(
        a.get("displayName") or a.get("email", "")
        for a in event.get("attendees", [])
    )

    lines = [summary]
    if start:
        lines.append(f"When: {start}" + (f" to {end}" if end else ""))
    if location:
        lines.append(f"Location: {location}")
    if attendees:
        lines.append(f"Attendees: {attendees}")
    if description:
        lines.append(description)
    text = "\n".join(lines).strip()

    meta = {
        "source_type": "calendar",
        "event_id": eid,
        "summary": summary[:200],
        "start": start[:40],
        "end": end[:40],
        "location": location[:200],
        "attendees": attendees[:300],
        "status": event.get("status", "confirmed"),
    }
    return [Chunk(doc_id=f"cal-{eid}", text=text, content_hash=content_hash(text), metadata=meta)]


def _attendee_valid_from(event: dict) -> str:
    """YYYY-MM-DD for the event's start (the date the meeting was attended).

    Uses start.date or the date portion of start.dateTime; falls back to UTC
    today so a malformed/floating event still produces a valid bi-temporal
    valid_from (upsert_relation rejects an empty valid_from).
    """
    start = (event.get("start") or {})
    raw = start.get("dateTime") or start.get("date") or ""
    if raw[:10] and raw[4:5] == "-":
        return raw[:10]
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _apply_attendees_to_graph(store, event: dict, owner) -> int:
    """Write each external attendee as a person entity + an `attended` relation
    from the owner to that attendee. Pure structured-data: no LLM, no enrich.

    - Excludes the owner/self (by name aliases AND by email match).
    - Filters junk/role names via graph_write.is_junk_entity.
    - Idempotent on re-sync: upsert_entity dedups by email/name; upsert_relation
      bumps the existing `attended` row (accumulating relation) rather than
      duplicating it.

    Returns the number of attendees written (entities upserted).
    """
    attendees = event.get("attendees") or []
    if not attendees:
        return 0

    owner_email = ""
    for a in owner.aliases:
        if "@" in a:
            owner_email = a
            break

    valid_from = _attendee_valid_from(event)
    event_id = event.get("id", "")
    written = 0
    for a in attendees:
        email_addr = (a.get("email") or "").strip().lower()
        name = (a.get("displayName") or a.get("email") or "").strip()
        if not name:
            continue
        # Self-exclusion: by configured name/alias, or by owner email.
        if _is_owner(name, owner):
            continue
        if owner_email and email_addr == owner_email:
            continue
        # Skip room resources / junk names. Google marks rooms with
        # resource=True; treat that as junk regardless of the display name.
        if a.get("resource") is True:
            continue
        if is_junk_entity(name, "person"):
            continue

        entity_id = upsert_entity(
            store, name=name, entity_type="person", email_addr=email_addr)
        if not entity_id or entity_id == owner.entity_id:
            continue

        upsert_relation(
            store, owner.entity_id, "attended", entity_id,
            valid_from=valid_from,
            evidence=f"cal-{event_id}" if event_id else "",
            source_doc_id=f"cal-{event_id}" if event_id else None)
        written += 1
    return written


# ---------------------------------------------------------------------------
# Internal: paginated events.list
# ---------------------------------------------------------------------------

def _list_events(service, calendar_id: str, sync_token: str | None,
                 time_min: str | None, time_max: str | None):
    """Page through events().list. Returns (items, next_sync_token).

    Uses the syncToken path for delta syncs; falls back to timeMin +
    singleEvents for the initial full fetch (sync_token is None).

    time_max bounds the forward horizon: recurring events expanded via
    singleEvents=True can stretch arbitrarily far into the future, and we
    don't want to embed/enrich events years ahead. timeMax is rejected by
    Google when syncToken is set, so it applies only to the full-fetch path.
    """
    items: list[dict] = []
    page_token: str | None = None
    next_sync: str | None = None

    while True:
        params: dict = {"calendarId": calendar_id, "showDeleted": True}
        if sync_token:
            params["syncToken"] = sync_token
        else:
            params["singleEvents"] = True
            if time_min:
                params["timeMin"] = time_min
            if time_max:
                params["timeMax"] = time_max
        if page_token:
            params["pageToken"] = page_token

        resp = service.events().list(**params).execute()
        items.extend(resp.get("items", []))
        next_sync = resp.get("nextSyncToken", next_sync)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return items, next_sync


def backfill_calendar_window(service, store, *, time_min: str, time_max: str,
                             calendar_id: str = "primary",
                             max_events: int | None = None) -> int:
    """List events in [time_min, time_max] and upsert them. No syncToken side effects.

    Used by the progressive-backfill loop to walk old history without resetting
    the delta cursor. Cancelled events are skipped via `normalise_calendar`.
    Returns the count of events that produced at least one chunk.
    """
    items: list[dict] = []
    page_token: str | None = None
    while True:
        params: dict = {
            "calendarId": calendar_id,
            "showDeleted": False,
            "singleEvents": True,
            "timeMin": time_min,
            "timeMax": time_max,
        }
        if page_token:
            params["pageToken"] = page_token
        resp = service.events().list(**params).execute()
        items.extend(resp.get("items", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    count = 0
    for ev in items:
        if max_events is not None and count >= max_events:
            break
        chunks = normalise_calendar(ev)
        for ch in chunks:
            store.upsert_chunk(ch.doc_id, ch.text, ch.content_hash, ch.metadata)
        if chunks:
            count += 1
    return count


# ---------------------------------------------------------------------------
# Public sync entry point
# ---------------------------------------------------------------------------

def sync_calendar(
    service,
    store,
    source: str = "calendar",
    calendar_id: str = "primary",
    time_min: str | None = None,
    time_max: str | None = None,
) -> int:
    """Delta sync via syncToken; full fetch on first run or HTTP 410 (expired token).

    Advances the cursor to nextSyncToken only after all event chunks are
    durably written, so a mid-run failure leaves the cursor at the last
    good position and the next run retries from there.

    time_min defaults to 30 days ago; time_max defaults to one year ahead.
    Bounding the forward window prevents singleEvents=True from expanding
    recurring events arbitrarily far into the future (and then needlessly
    embedding/enriching them).

    Returns the count of events that produced at least one chunk (i.e.
    non-cancelled events that were upserted).
    """
    cursor = store.get_cursor(source)
    now = datetime.now(timezone.utc)
    if time_min is None:
        time_min = (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    if time_max is None:
        time_max = (now + timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Evict any calendar chunks that fell outside the new forward horizon —
    # for example, recurring events that an earlier (unbounded) sync expanded
    # years ahead. Cheap idempotent no-op once the store has caught up.
    store.delete_calendar_chunks_after(time_max)

    if cursor:
        try:
            items, next_sync = _list_events(service, calendar_id, cursor, time_min, time_max)
        except HttpError as e:
            resp = getattr(e, "resp", None)
            if resp is not None and resp.status == 410:
                # Sync token expired — fall back to full fetch.
                items, next_sync = _list_events(service, calendar_id, None, time_min, time_max)
            else:
                raise
    else:
        items, next_sync = _list_events(service, calendar_id, None, time_min, time_max)

    count = 0
    for ev in items:
        chunks = normalise_calendar(ev)
        for ch in chunks:
            store.upsert_chunk(ch.doc_id, ch.text, ch.content_hash, ch.metadata)
        if chunks:
            count += 1

    # Advance cursor only after all upserts are durable.
    if next_sync:
        store.set_cursor(source, next_sync)

    return count
