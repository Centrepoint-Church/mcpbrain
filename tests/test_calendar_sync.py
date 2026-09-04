"""Tests for mcpbrain.sync.calendar — fake service, no network."""

import httplib2
from googleapiclient.errors import HttpError

from mcpbrain.store import Store
from mcpbrain.sync.calendar import discover_calendar, handle_calendar_item, normalise_calendar


# ---------------------------------------------------------------------------
# Fake Calendar service
# ---------------------------------------------------------------------------

class _Req:
    def __init__(self, result=None, raise_410=False):
        self._r = result
        self._raise = raise_410

    def execute(self, num_retries=0):
        if self._raise:
            raise HttpError(httplib2.Response({"status": 410}), b"Sync token expired")
        return self._r


class _Events:
    def __init__(self, on_synctoken=None, on_full=None, raise_410_on_synctoken=False,
                by_id=None):
        self._syn = on_synctoken
        self._full = on_full
        self._raise = raise_410_on_synctoken
        self._by_id = by_id or {}

    def list(self, **kw):
        if "syncToken" in kw:
            if self._raise:
                return _Req(raise_410=True)
            return _Req(self._syn)
        return _Req(self._full)

    def get(self, calendarId=None, eventId=None):
        return _Req(self._by_id[eventId])


class FakeCalService:
    def __init__(self, **kw):
        self._events = _Events(**kw)

    def events(self):
        return self._events


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _event(eid, summary, status="confirmed", start="2026-06-01T09:00:00Z",
           end="2026-06-01T10:00:00Z", location="", description="", attendees=None):
    ev = {
        "id": eid,
        "summary": summary,
        "status": status,
        "start": {"dateTime": start},
        "end": {"dateTime": end},
    }
    if location:
        ev["location"] = location
    if description:
        ev["description"] = description
    if attendees:
        ev["attendees"] = attendees
    return ev


def _resp(events, next_sync_token=None, next_page_token=None):
    r = {"items": events}
    if next_sync_token:
        r["nextSyncToken"] = next_sync_token
    if next_page_token:
        r["nextPageToken"] = next_page_token
    return r


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_discover_calendar_first_run_sets_synctoken_and_enqueues(tmp_path):
    """No cursor. Full fetch returns 1 event + nextSyncToken. discover_calendar
    enqueues it and advances the cursor; the chunk itself is written by
    handle_calendar_item, tested separately below."""
    store = Store(tmp_path / "test.sqlite3", dim=4)
    store.init()

    ev = _event("evt1", "Team meeting")
    full_resp = _resp([ev], next_sync_token="tok1")
    svc = FakeCalService(on_full=full_resp)

    n = discover_calendar(svc, store)

    assert n == 1
    assert store.get_cursor("calendar") == "tok1"
    assert store.sync_queue_pending("calendar") == 1


def test_handle_calendar_item_fetches_and_upserts(tmp_path):
    """Work one queued calendar event: fetch it, normalise, upsert its chunk --
    the fetch/write half discover_calendar no longer does itself."""
    store = Store(tmp_path / "test.sqlite3", dim=4)
    store.init()

    ev = _event("evt2", "Budget review")
    svc = FakeCalService(by_id={"evt2": ev})

    handle_calendar_item(svc, store, {"ref_id": "evt2", "version": "",
                                      "event": "upsert",
                                      "modified_at": "2026-09-04T00:00:00"})

    assert store.get_chunk("cal-evt2") is not None


def test_handle_calendar_item_remove_deletes_chunks(tmp_path):
    """A queued 'remove' item (discover_calendar's mapping for a cancelled
    event) deletes the event's existing chunk rather than fetching/upserting."""
    store = Store(tmp_path / "test.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("cal-evt3", "Cancelled standup", "h1",
                       {"source_type": "calendar", "event_id": "evt3"})

    handle_calendar_item(FakeCalService(), store,
                         {"ref_id": "evt3", "version": "", "event": "remove",
                          "modified_at": "2026-09-04T00:00:00"})

    assert store.get_chunk("cal-evt3") is None


def test_cancelled_event_enqueued_as_remove_end_to_end(tmp_path):
    """Cancelled event: discover_calendar enqueues it as a remove, and working
    that item must NOT create a chunk."""
    store = Store(tmp_path / "test.sqlite3", dim=4)
    store.init()

    ev = _event("evt3", "Cancelled standup", status="cancelled")
    full_resp = _resp([ev], next_sync_token="tok_x")
    svc = FakeCalService(on_full=full_resp)

    discover_calendar(svc, store)
    row = store.due_sync_items(limit=10, now="2099-01-01T00:00:00")[0]
    assert row["event"] == "remove"

    handle_calendar_item(svc, store, row)
    assert store.get_chunk("cal-evt3") is None


def test_410_triggers_full_resync(tmp_path):
    """Cursor pre-set to 'old'. syncToken call raises HTTP 410. Full-fetch path
    returns 1 event + tok3. discover_calendar's cursor advances to tok3 and
    the event is enqueued, with no exception escaping."""
    store = Store(tmp_path / "test.sqlite3", dim=4)
    store.init()
    store.set_cursor("calendar", "old")

    ev = _event("evt4", "Resync event")
    full_resp = _resp([ev], next_sync_token="tok3")
    svc = FakeCalService(raise_410_on_synctoken=True, on_full=full_resp)

    n = discover_calendar(svc, store)

    assert n == 1
    assert store.get_cursor("calendar") == "tok3"
    assert store.sync_queue_pending("calendar") == 1


def test_normalise_includes_key_fields(tmp_path):
    """normalise_calendar on a rich event: chunk text contains summary,
    description, attendee name, and location. doc_id == cal-<id>.
    metadata source_type == 'calendar'."""
    ev = _event(
        "evt5",
        "Leadership Offsite",
        location="Novotel Perth Langley",
        description="Annual strategy review day.",
        attendees=[
            {"displayName": "Taryn Hamilton", "email": "taryn@example.org"},
            {"email": "joel@example.org"},
        ],
    )

    chunks = normalise_calendar(ev)

    assert len(chunks) == 1
    ch = chunks[0]
    assert ch.doc_id == "cal-evt5"
    assert "Leadership Offsite" in ch.text
    assert "Novotel Perth Langley" in ch.text
    assert "Annual strategy review day." in ch.text
    assert "Taryn Hamilton" in ch.text
    assert ch.metadata["source_type"] == "calendar"


# ---------------------------------------------------------------------------
# Finding E: long-agenda splitting + chunk_total
# ---------------------------------------------------------------------------

def test_a_short_event_keeps_its_exact_doc_id():
    """Finding E's fix must not change the common case: delete_calendar_chunks_
    after and the calendar enrichment path both key on cal-<event_id>, so a
    suffix here would orphan every existing calendar chunk."""
    from mcpbrain.sync.calendar import normalise_calendar

    chunks = normalise_calendar({"id": "e1", "summary": "Standup",
                                 "start": {"dateTime": "2026-06-02T09:00:00Z"}})

    assert [c.doc_id for c in chunks] == ["cal-e1"]
    assert chunks[0].metadata["chunk_total"] == 1


def test_a_very_long_agenda_is_split():
    """Finding E: normalise_calendar emitted exactly one chunk per event with the
    description inlined, never calling chunk_text, so a long agenda was truncated
    by the embedder rather than split. Only 4 of 1,149 live chunks are affected."""
    from mcpbrain.sync.calendar import normalise_calendar

    chunks = normalise_calendar({"id": "e2", "summary": "Board",
                                 "start": {"dateTime": "2026-06-02T09:00:00Z"},
                                 "description": "agenda item. " * 500})

    assert len(chunks) > 1
    assert [c.doc_id for c in chunks] == [f"cal-e2-{i}" for i in range(len(chunks))]
    assert all(c.metadata["chunk_total"] == len(chunks) for c in chunks)


def test_a_split_events_chunks_are_all_evicted_when_the_horizon_shrinks(tmp_path):
    """delete_calendar_chunks_after filters on metadata (source_type + start),
    not on doc_id shape, so it must delete every chunk of a split event just as
    it deletes a single-chunk one — confirming Step 8's LIKE-pattern concern
    does not apply here (this sweep never matches on doc_id at all)."""
    from mcpbrain.store import Store
    from mcpbrain.sync.calendar import normalise_calendar

    store = Store(tmp_path / "b.sqlite3", dim=4)
    store.init()
    chunks = normalise_calendar({"id": "e3", "summary": "Board",
                                 "start": {"dateTime": "2027-01-01T09:00:00Z"},
                                 "description": "agenda item. " * 500})
    assert len(chunks) > 1, "fixture must actually exercise the split path"
    for c in chunks:
        store.upsert_chunk(c.doc_id, c.text, c.content_hash, c.metadata)
    for c in chunks:
        assert store.get_chunk(c.doc_id) is not None

    store.delete_calendar_chunks_after("2026-12-31T00:00:00Z")

    for c in chunks:
        assert store.get_chunk(c.doc_id) is None, f"{c.doc_id} survived the sweep"


# ---------------------------------------------------------------------------
# Task 5: _list_events should pass num_retries to .execute()
# ---------------------------------------------------------------------------

def test_list_events_calls_pass_num_retries():
    from mcpbrain.sync import calendar

    calls = []

    class _FakeExec:
        def execute(self, num_retries=0):
            calls.append(num_retries)
            return {"items": [], "nextSyncToken": None}

    class _FakeEvents:
        def list(self, **kw):
            return _FakeExec()

    class _FakeService:
        def events(self):
            return _FakeEvents()

    calendar._list_events(_FakeService(), "primary", None,
                          "2026-01-01T00:00:00Z", "2026-12-31T00:00:00Z")

    assert calls == [calendar._NUM_RETRIES]


# ---------------------------------------------------------------------------
# Final-review C1: backfill_calendar_window must refresh metadata even when
# the re-render is byte-identical
# ---------------------------------------------------------------------------

def test_backfill_calendar_window_stamps_version_on_a_byte_identical_rechunk(tmp_path):
    """This function is the calendar arm of `bin/repair.py reingest-stale`.

    CHUNKER_VERSION 2->3 changed sync/tabular.py only, so a re-fetched event
    renders BYTE-IDENTICALLY -- the normal case, not a corner one.
    store.upsert_chunk short-circuits on an unchanged content_hash and writes
    nothing at all (metadata included), so without the patch_chunk_metadata
    fallback the chunk never acquires the new chunker_version,
    store.stale_chunker_ids re-selects the same event on every run, and the
    sweep burns Calendar quota forever while reporting success.
    """
    from mcpbrain.chunking import CHUNKER_VERSION
    from mcpbrain.sync.calendar import backfill_calendar_window

    store = Store(tmp_path / "test.sqlite3", dim=4)
    store.init()

    ev = _event("e1", "Standup")
    # Seed EXACTLY what the re-fetch will render, only on the old version.
    expected = normalise_calendar(ev)
    assert expected, "fixture must produce at least one chunk"
    for ch in expected:
        store.upsert_chunk(ch.doc_id, ch.text, ch.content_hash,
                           {**ch.metadata, "chunker_version": 1})

    class _WinExec:
        def execute(self, num_retries=0):
            return {"items": [ev]}

    class _WinEvents:
        def list(self, **kw):
            return _WinExec()

    class _WinService:
        def events(self):
            return _WinEvents()

    n = backfill_calendar_window(_WinService(), store,
                                 time_min="2026-01-01T00:00:00Z",
                                 time_max="2026-12-31T00:00:00Z")

    assert n == 1
    for ch in expected:
        got = store.get_chunk(ch.doc_id)
        assert got["metadata"]["chunker_version"] == CHUNKER_VERSION, ch.doc_id
        assert got["text"] == ch.text          # unchanged, as expected
