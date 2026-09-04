"""Google Calendar delta sync — syncToken path with HTTP 410 full-fetch fallback.

Normalises events to one Chunk per event (doc_id = cal-<id>) in the common
case; a long agenda splits into cal-<id>-<i> chunks (Finding E) so an
over-2,000-char description is not silently truncated by the embedder.
Cancelled events are skipped. Cursor (nextSyncToken) is written only after
all event chunks have been durably upserted.
"""

from contextlib import nullcontext
from datetime import datetime, timedelta, timezone

from googleapiclient.errors import HttpError

from mcpbrain.chunking import CHUNKER_VERSION, chunk_text, content_hash, has_content
from mcpbrain.graph_write import (
    _is_owner,
    _meeting_series_id,
    is_junk_entity,
    owner_identity_from_config,
    resolve_owner_entity_id,
    upsert_entity,
    upsert_relation,
)
from mcpbrain.sync.normalise import Chunk

_NUM_RETRIES = 5  # see mcpbrain.backup._NUM_RETRIES for the full rationale

# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def normalise_calendar(event: dict) -> list[Chunk]:
    """Convert a Calendar event dict to a list of Chunks.

    Returns an empty list for cancelled events (or an event whose rendered
    text has no content at all). doc_id format: cal-<event_id> for the common
    single-chunk case (preserved exactly, so existing chunks/relations/
    delete_calendar_chunks_after keep matching); cal-<event_id>-<i> only when
    a long description needs to split across more than one chunk.
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
        "chunker_version": CHUNKER_VERSION,
        "event_id": eid,
        "summary": summary[:200],
        "start": start[:40],
        "end": end[:40],
        "location": location[:200],
        "attendees": attendees[:300],
        "status": event.get("status", "confirmed"),
        "recurring_event_id": event.get("recurringEventId", ""),
    }
    # Finding E: this emitted exactly one chunk per event with the description
    # inlined, never calling chunk_text, so a long agenda was truncated by the
    # embedder rather than split. Impact is small — of 1,149 live calendar
    # chunks, max length 2,977 and only 4 exceed 2,000 chars — so the
    # single-chunk case stays byte-identical and only those 4 take a suffix.
    pieces = [p for p in chunk_text(text) if has_content(p)]
    if not pieces:
        return []
    if len(pieces) == 1:
        return [Chunk(doc_id=f"cal-{eid}", text=pieces[0],
                      content_hash=content_hash(pieces[0]),
                      metadata={**meta, "chunk_index": 0, "chunk_total": 1})]
    return [Chunk(doc_id=f"cal-{eid}-{i}", text=p, content_hash=content_hash(p),
                  metadata={**meta, "chunk_index": i, "chunk_total": len(pieces)})
            for i, p in enumerate(pieces)]


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
    - The relation is written only when this graph HAS an owner node
      (resolve_owner_entity_id): the attendee pass never mints one, and an edge
      from an id with no row is a dangling edge the graph explorer silently
      drops. The attendee entity is written either way.

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
    # The `attended` edges start AT the owner, so they need the owner's real
    # node id — NOT owner.entity_id, which is a recogniser slug that need not
    # exist (it did not on the live store: 7 dangling 'joshua-kemp' rows beside
    # the real 'josh-kemp' owner node). '' means this graph has no owner node
    # yet; the attendee entities are still written, the edge is skipped rather
    # than dangled.
    owner_eid = resolve_owner_entity_id(store, owner, owner_email)
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
        if not entity_id or entity_id in (owner.entity_id, owner_eid):
            continue

        if owner_eid:
            upsert_relation(
                store, owner_eid, "attended", entity_id,
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

        resp = service.events().list(**params).execute(num_retries=_NUM_RETRIES)
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

    A chunk whose text is byte-identical to what is already stored still has
    its METADATA refreshed, exactly as `drive.upsert_file_chunks` does.
    `store.upsert_chunk` short-circuits on an unchanged content_hash and
    writes nothing at all — text, embedding AND metadata — so without this a
    re-ingest would never acquire the current `chunker_version`. That matters
    specifically because this function is ALSO the calendar arm of
    `bin/repair.py reingest-stale`, whose selector `store.stale_chunker_ids`
    picks items on exactly that field and orders by MIN(rowid): a stale event
    that re-renders identically would be re-selected and re-fetched on every
    run forever, burning Calendar quota with zero progress while reporting
    success. And identical re-rendering is the NORMAL case for calendar, not a
    corner one — the CHUNKER_VERSION 2->3 bump changed sync/tabular.py only,
    not `chunk_text`, so nothing about a calendar event's rendering changed.
    `patch_chunk_metadata` merges without touching content_hash or `embedded`,
    so nothing is spuriously re-queued for embedding.

    The delta path (`discover_calendar`/`handle_calendar_item`) deliberately
    does NOT need this: it only ever sees events Google reports as changed, so
    it is not what a version-bump sweep re-drives and has no non-convergent
    selector behind it.
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
        resp = service.events().list(**params).execute(num_retries=_NUM_RETRIES)
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
                if not store.upsert_chunk(ch.doc_id, ch.text, ch.content_hash,
                                          ch.metadata):
                    store.patch_chunk_metadata(ch.doc_id, **ch.metadata)
            if chunks:
                count += 1
                _apply_attendees_to_graph(store, ev, owner)
                _annotate_series_from_event(store, ev, owner)
    return count


def _utc_now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def discover_calendar(service, store, source: str = "calendar",
                      calendar_id: str = "primary", time_min: str | None = None,
                      time_max: str | None = None, *, budget=None,
                      bulk_section=None) -> int:
    """List calendar events via _list_events and enqueue them.

    _list_events already pages events().list() to completion (or budget
    expiry) entirely in memory -- reused here rather than reimplemented.
    Rows are enqueued whether or not the list completed; the cursor advances
    to next_sync ONLY when it did (interrupted=False), because Google emits
    nextSyncToken only on the final page. An interrupted call costs a re-list
    next cycle, never a re-work -- what was enqueued here is already durable.
    """
    bulk_section = bulk_section or nullcontext
    cursor = store.get_cursor(source)
    now = datetime.now(timezone.utc)
    if time_min is None:
        time_min = (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    if time_max is None:
        time_max = (now + timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Window-hygiene eviction: idempotent, not per-item, so it belongs in
    # discovery even though it mutates chunks -- see the module-level design
    # note in the plan brief for why this is a deliberate exception to
    # "discovery never touches chunks."
    with bulk_section():
        store.delete_calendar_chunks_after(time_max)

    try:
        items, next_sync, interrupted = _list_events(
            service, calendar_id, cursor, time_min, time_max, budget=budget)
    except HttpError as e:
        resp = getattr(e, "resp", None)
        if resp is not None and resp.status == 410:
            items, next_sync, interrupted = _list_events(
                service, calendar_id, None, time_min, time_max, budget=budget)
        else:
            raise

    queue_items = []
    for ev in items:
        eid = ev.get("id")
        if not eid:
            continue
        queue_items.append({
            "ref_id": eid,
            "version": ev.get("updated", ""),
            "event": "remove" if ev.get("status") == "cancelled" else "upsert",
            "modified_at": ev.get("updated") or _utc_now_iso(),
        })

    if next_sync and not interrupted:
        store.enqueue_and_advance(queue_items, source=source, cursor=next_sync)
    else:
        # Enqueue without moving the cursor: durable partial progress, and the
        # next call re-lists from the SAME cursor (cheap: listing only).
        store.enqueue_and_advance(queue_items, source=source, cursor=cursor)
    return len(queue_items)


def handle_calendar_item(service, store, item, *, calendar_id: str = "primary",
                         bulk_section=None) -> None:
    """Work one queued calendar event. Raises on failure so the loop backs it off."""
    bulk_section = bulk_section or nullcontext
    if item["event"] == "remove":
        with bulk_section():
            # Mirrors Drive's doc_ids_for_file: doc_ids_for_messages resolves a
            # 'cal-<event_id>' key against chunks.metadata.event_id (see its
            # own docstring's "Calendar events are the fourth" case), which is
            # exactly how a split event's cal-<id>-<i> chunks are found -- no
            # per-event delete existed before this; the old round-based sync
            # handled removal only implicitly via the window-wide
            # delete_calendar_chunks_after sweep.
            doc_ids = store.doc_ids_for_messages([f"cal-{item['ref_id']}"])
            if doc_ids:
                store.delete_chunks(doc_ids)
        return
    ev = service.events().get(calendarId=calendar_id,
                              eventId=item["ref_id"]).execute(num_retries=_NUM_RETRIES)
    owner = owner_identity_from_config()
    with bulk_section():
        chunks = normalise_calendar(ev)
        for ch in chunks:
            store.upsert_chunk(ch.doc_id, ch.text, ch.content_hash, ch.metadata)
        if chunks:
            _apply_attendees_to_graph(store, ev, owner)
            _annotate_series_from_event(store, ev, owner)
