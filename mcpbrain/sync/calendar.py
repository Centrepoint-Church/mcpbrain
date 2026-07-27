"""Google Calendar delta sync — syncToken path with HTTP 410 full-fetch fallback.

Normalises events to a single Chunk per event (doc_id = cal-<id>).
Cancelled events are skipped. Cursor (nextSyncToken) is written only after
all event chunks have been durably upserted.
"""

import json
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone

from googleapiclient.errors import HttpError

from mcpbrain.chunking import content_hash
from mcpbrain.graph_write import (
    _is_owner,
    _meeting_series_id,
    is_junk_entity,
    owner_identity_from_config,
    upsert_entity,
    upsert_relation,
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
        "recurring_event_id": event.get("recurringEventId", ""),
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


def _annotate_series_from_event(store, event, owner) -> bool:
    """Stamp a matching meeting series with this recurring event's id.

    Conservative: only fires for a recurring event whose (normalized summary,
    org) resolves to an EXISTING series entity. Writes a 'calendar_series'
    observation (value=recurringEventId). Never creates or re-keys an entity, so
    it cannot mis-merge two series. Org is unknown from a bare calendar event, so
    this only matches against the 'external'-scoped series id — the conservative
    org bucket a calendar-derived series falls under — and does not attempt
    owner-org matching.
    """
    rec_id = event.get("recurringEventId", "")
    summary = (event.get("summary") or "").strip()
    if not rec_id or not summary:
        return False
    candidate_orgs = []
    # owner's configured org (if any alias carries one) then external fallback.
    candidate_orgs.append("external")
    for org in candidate_orgs:
        eid = _meeting_series_id(summary, org)
        with store._connect(write=True) as db:
            exists = db.execute(
                "SELECT 1 FROM entities WHERE id=? AND type='meeting'", (eid,)).fetchone()
            if not exists:
                continue
            already = db.execute(
                "SELECT 1 FROM entity_observations WHERE entity_id=? "
                "AND attribute='calendar_series' AND value=?", (eid, rec_id)).fetchone()
            if already:
                return False
            db.execute(
                "INSERT INTO entity_observations "
                "(entity_id, attribute, value, source, valid_from, confidence_source) "
                "VALUES (?, 'calendar_series', ?, ?, ?, 'calendar')",
                (eid, rec_id, f"cal-{event.get('id','')}",
                 (event.get("start") or {}).get("date")
                 or (event.get("start") or {}).get("dateTime", "")[:10] or ""))
        return True
    return False


# ---------------------------------------------------------------------------
# Internal: paginated events.list
# ---------------------------------------------------------------------------

def _list_events(service, calendar_id: str, sync_token: str | None,
                 time_min: str | None, time_max: str | None, *, budget=None):
    """Page through events().list. Returns (items, next_sync_token, interrupted).

    Uses the syncToken path for delta syncs; falls back to timeMin +
    singleEvents for the initial full fetch (sync_token is None).

    time_max bounds the forward horizon: recurring events expanded via
    singleEvents=True can stretch arbitrarily far into the future, and we
    don't want to embed/enrich events years ahead. timeMax is rejected by
    Google when syncToken is set, so it applies only to the full-fetch path.

    This loop only reads (collects `items` in memory); it never writes to the
    store, so it needs a `budget` check (Task 2 duty-cycle fix) but no
    `bulk_section`. `interrupted=True` means the budget expired before the
    last page was reached — `next_sync` is naturally still None in that case
    (Google only returns `nextSyncToken` on the final page), so the existing
    `if next_sync:` cursor-advance guard downstream already does the right
    thing; `interrupted` is returned anyway so the caller can also gate its
    OWN item-loop's cursor-advance decision on it explicitly.
    """
    items: list[dict] = []
    page_token: str | None = None
    next_sync: str | None = None
    interrupted = False

    while True:
        if budget is not None and budget.expired():
            interrupted = True
            break
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

    return items, next_sync, interrupted


def backfill_calendar_window(service, store, *, time_min: str, time_max: str,
                             calendar_id: str = "primary",
                             max_events: int | None = None,
                             bulk_section=None) -> int:
    """List events in [time_min, time_max] and upsert them. No syncToken side effects.

    Used by the progressive-backfill loop to walk old history without resetting
    the delta cursor. Cancelled events are skipped via `normalise_calendar`.
    Returns the count of events that produced at least one chunk.

    Already item-bounded by `max_events` (the progressive-backfill step caps
    this at `_BACKFILL_MAX_PER_SOURCE`, default 200) and touches no delta
    cursor, so no budget/checkpoint logic is needed here — but `bulk_section`
    (defaulting to `contextlib.nullcontext`) still brackets each event's
    writes so even this bounded window doesn't hold `_bulk_lock` for its
    whole duration.
    """
    if bulk_section is None:
        bulk_section = nullcontext
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
    owner = owner_identity_from_config()
    for ev in items:
        if max_events is not None and count >= max_events:
            break
        with bulk_section():
            chunks = normalise_calendar(ev)
            for ch in chunks:
                store.upsert_chunk(ch.doc_id, ch.text, ch.content_hash, ch.metadata)
            if chunks:
                count += 1
                _apply_attendees_to_graph(store, ev, owner)
                _annotate_series_from_event(store, ev, owner)
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
    *,
    budget=None,
    bulk_section=None,
) -> int:
    """Delta sync via syncToken; full fetch on first run or HTTP 410 (expired token).

    Advances the cursor to nextSyncToken only after all event chunks are
    durably written, so a mid-run failure leaves the cursor at the last
    good position and the next run retries from there.

    time_min defaults to 30 days ago; time_max defaults to one year ahead.
    Bounding the forward window prevents singleEvents=True from expanding
    recurring events arbitrarily far into the future (and then needlessly
    embedding/enriching them).

    Bounded and INCREMENTALLY checkpoint-safe (Task 2 duty-cycle fix):
    `budget` (a `Budget`, or None for unbounded) is checked in `_list_events`'s
    pagination AND once per not-yet-resumed event below. On expiry the REAL
    cursor is NOT advanced — Google's `nextSyncToken` is only ever emitted on
    the delta's FINAL page, so an early advance would silently and
    permanently skip whatever this round hasn't reached yet.

    Merely "don't advance the cursor and let the next call re-walk the same
    window" is NOT sufficient once one delta is bigger than one budget's
    worth of events: every subsequent call would re-list the SAME syncToken,
    get the SAME ordered event list, and process the SAME prefix every time
    — the cursor would never advance and events past the prefix would never
    be ingested (this exact livelock was reproduced and fixed for Gmail; see
    sync_gmail's docstring). So a second, separate piece of state —
    `f"{source}:resume_ids"` (a JSON list of event ids already durably
    processed for the CURRENT, not-yet-committed delta round, stored via the
    same generic `sync_cursors` table) — is checked per event: an id already
    in it is skipped (no re-fetch-equivalent work, no re-upsert — genuine
    forward progress), and the set is grown and persisted after each call.
    Only once every event id in this round's (possibly still-growing) list is
    accounted for does the real cursor advance and the resume set clear.
    `store.upsert_chunk` being an idempotent upsert and the graph writes
    (`_apply_attendees_to_graph`/`_annotate_series_from_event`) being
    documented idempotent/accumulating (see their docstrings) means a
    resumed event that DID somehow get reprocessed is still safe — the
    resume set just avoids paying for that redundant work.

    `bulk_section` (a zero-arg context-manager factory, default
    `contextlib.nullcontext`) brackets each event's writes so `_bulk_lock` is
    released between events rather than held for the whole call.

    Returns the count of events that produced at least one chunk (i.e.
    non-cancelled events that were upserted) THIS call. May be a partial
    count when the budget expired mid-run; already-resumed events from a
    prior call are not re-counted.
    """
    if bulk_section is None:
        bulk_section = nullcontext
    cursor = store.get_cursor(source)
    owner = owner_identity_from_config()
    now = datetime.now(timezone.utc)
    if time_min is None:
        time_min = (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    if time_max is None:
        time_max = (now + timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Evict any calendar chunks that fell outside the new forward horizon —
    # for example, recurring events that an earlier (unbounded) sync expanded
    # years ahead. Cheap idempotent no-op once the store has caught up.
    # Writes/deletes `chunks` (DELETE FROM chunks/vec_chunks/fts_chunks) --
    # the same table the four gated maintenance passes mutate -- so it needs
    # the same bulk_section bracketing as every other write in this module
    # (lock-coverage regression found in adversarial review: this ran with no
    # lock at all in an earlier revision of this task).
    with bulk_section():
        store.delete_calendar_chunks_after(time_max)

    resume_key = f"{source}:resume_ids"
    try:
        resumed_ids: set = set(json.loads(store.get_cursor(resume_key) or "[]"))
    except (ValueError, TypeError):
        resumed_ids = set()

    if cursor:
        try:
            items, next_sync, interrupted = _list_events(
                service, calendar_id, cursor, time_min, time_max, budget=budget)
        except HttpError as e:
            resp = getattr(e, "resp", None)
            if resp is not None and resp.status == 410:
                # Sync token expired — fall back to full fetch.
                items, next_sync, interrupted = _list_events(
                    service, calendar_id, None, time_min, time_max, budget=budget)
            else:
                raise
    else:
        items, next_sync, interrupted = _list_events(
            service, calendar_id, None, time_min, time_max, budget=budget)

    count = 0
    newly_done: set = set()
    for ev in items:
        eid = ev.get("id")
        if eid and eid in resumed_ids:
            continue
        if budget is not None and budget.expired():
            interrupted = True
            break
        with bulk_section():
            chunks = normalise_calendar(ev)
            for ch in chunks:
                store.upsert_chunk(ch.doc_id, ch.text, ch.content_hash, ch.metadata)
            if chunks:
                count += 1
                _apply_attendees_to_graph(store, ev, owner)
                _annotate_series_from_event(store, ev, owner)
        if eid:
            newly_done.add(eid)

    if newly_done:
        resumed_ids |= newly_done
        store.set_cursor(resume_key, json.dumps(sorted(resumed_ids)))

    # Advance the REAL cursor only once pagination completed, the per-event
    # loop wasn't cut short, AND every (id-bearing) event from this round's
    # list is accounted for in the resume set.
    item_ids = {ev.get("id") for ev in items if ev.get("id")}
    if next_sync and not interrupted and item_ids <= resumed_ids:
        store.set_cursor(source, next_sync)
        store.set_cursor(resume_key, "[]")

    return count
