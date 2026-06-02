"""Tests for mcpbrain.sync.calendar — fake service, no network."""

import httplib2
import pytest
from googleapiclient.errors import HttpError

from mcpbrain.store import Store
from mcpbrain.sync.calendar import normalise_calendar, sync_calendar


# ---------------------------------------------------------------------------
# Fake Calendar service
# ---------------------------------------------------------------------------

class _Req:
    def __init__(self, result=None, raise_410=False):
        self._r = result
        self._raise = raise_410

    def execute(self):
        if self._raise:
            raise HttpError(httplib2.Response({"status": 410}), b"Sync token expired")
        return self._r


class _Events:
    def __init__(self, on_synctoken=None, on_full=None, raise_410_on_synctoken=False):
        self._syn = on_synctoken
        self._full = on_full
        self._raise = raise_410_on_synctoken

    def list(self, **kw):
        if "syncToken" in kw:
            if self._raise:
                return _Req(raise_410=True)
            return _Req(self._syn)
        return _Req(self._full)


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

def test_first_run_full_fetch_sets_synctoken(tmp_path):
    """No cursor. Full fetch returns 1 event + nextSyncToken. After sync:
    chunk present, cursor == tok1, return value == 1."""
    store = Store(tmp_path / "test.sqlite3", dim=4)
    store.init()

    ev = _event("evt1", "Team meeting")
    full_resp = _resp([ev], next_sync_token="tok1")
    svc = FakeCalService(on_full=full_resp)

    result = sync_calendar(svc, store)

    assert result == 1
    assert store.get_cursor("calendar") == "tok1"
    chunk = store.get_chunk("cal-evt1")
    assert chunk is not None


def test_delta_fetch_with_synctoken(tmp_path):
    """Cursor pre-set to tok1. syncToken path returns changed event + tok2.
    Event upserted, cursor advances to tok2, return 1."""
    store = Store(tmp_path / "test.sqlite3", dim=4)
    store.init()
    store.set_cursor("calendar", "tok1")

    ev = _event("evt2", "Budget review")
    delta_resp = _resp([ev], next_sync_token="tok2")
    svc = FakeCalService(on_synctoken=delta_resp)

    result = sync_calendar(svc, store)

    assert result == 1
    assert store.get_cursor("calendar") == "tok2"
    assert store.get_chunk("cal-evt2") is not None


def test_cancelled_event_skipped(tmp_path):
    """Cancelled event must NOT be upserted and must NOT be counted."""
    store = Store(tmp_path / "test.sqlite3", dim=4)
    store.init()

    ev = _event("evt3", "Cancelled standup", status="cancelled")
    full_resp = _resp([ev], next_sync_token="tok_x")
    svc = FakeCalService(on_full=full_resp)

    result = sync_calendar(svc, store)

    assert result == 0
    assert store.get_chunk("cal-evt3") is None


def test_410_triggers_full_resync(tmp_path):
    """Cursor pre-set to 'old'. syncToken call raises HTTP 410. Full-fetch path
    returns 1 event + tok3. Cursor == tok3, event upserted, no exception escapes."""
    store = Store(tmp_path / "test.sqlite3", dim=4)
    store.init()
    store.set_cursor("calendar", "old")

    ev = _event("evt4", "Resync event")
    full_resp = _resp([ev], next_sync_token="tok3")
    svc = FakeCalService(raise_410_on_synctoken=True, on_full=full_resp)

    result = sync_calendar(svc, store)

    assert result == 1
    assert store.get_cursor("calendar") == "tok3"
    assert store.get_chunk("cal-evt4") is not None


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
            {"displayName": "Taryn Hamilton", "email": "taryn@centrepoint.church"},
            {"email": "joel@centrepoint.church"},
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
