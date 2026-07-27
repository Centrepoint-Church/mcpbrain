"""Tests for mcpbrain.sync.calendar — fake service, no network."""

import httplib2
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
# Task 2 duty-cycle fix: budget-interrupted mid-delta must checkpoint
# INCREMENTALLY, not just "don't advance the cursor" (that alone livelocks
# once a delta is bigger than one budget — see sync_gmail's docstring for the
# reproduced-and-fixed original case).
# ---------------------------------------------------------------------------

class _FakeBudget:
    """expired() returns False for the first `expire_after_calls` calls, True
    from then on — pins EXACTLY which iteration a real Budget's wall-clock
    expiry would have landed on, deterministically."""

    def __init__(self, expire_after_calls):
        self.calls = 0
        self.expire_after_calls = expire_after_calls

    def expired(self) -> bool:
        self.calls += 1
        return self.calls > self.expire_after_calls


def test_budget_interrupted_mid_event_loop_resumes_incrementally(tmp_path):
    """A budget that expires partway through the per-event loop must not
    advance the cursor, and a resumed call must SKIP the already-processed
    event rather than merely re-doing it idempotently."""
    store = Store(tmp_path / "test.sqlite3", dim=4)
    store.init()
    store.set_cursor("calendar", "old")

    events = [_event(f"evt{i}", f"Meeting {i}") for i in range(1, 4)]
    resp = _resp(events, next_sync_token="tokNEW")
    svc = FakeCalService(on_synctoken=resp)

    # Call 1: _list_events' single-page pagination check (not expired). Call
    # 2: event-loop check before evt1 (not expired -> evt1 processed). Call
    # 3: before evt2 (expired -> stop; evt2/evt3 not reached this call).
    budget = _FakeBudget(expire_after_calls=2)
    result = sync_calendar(svc, store, budget=budget)

    assert result == 1
    assert store.get_cursor("calendar") == "old", "cursor must not advance on a partial run"
    assert store.get_chunk("cal-evt1") is not None
    assert store.get_chunk("cal-evt2") is None
    assert store.get_chunk("cal-evt3") is None

    # Resume: evt1 is now in the persisted resume set and must be skipped;
    # only evt2/evt3 are genuinely new work this call.
    result2 = sync_calendar(svc, store, budget=None)

    assert result2 == 2, "only the genuinely new events (evt2, evt3) should be counted this call"
    assert store.get_cursor("calendar") == "tokNEW"
    assert store.get_cursor("calendar:resume_ids") == "[]"
    for i in range(1, 4):
        assert store.get_chunk(f"cal-evt{i}") is not None


def test_budget_interrupted_across_many_cycles_eventually_completes(tmp_path):
    """Critical-B reproduction, calendar variant (adversarial review, Task 2
    round 3): a delta bigger than one budget's worth of events must not
    livelock. Drives a 7-event delta through repeated budget-truncated calls
    (2 events' worth of capacity each) and asserts the cursor eventually
    reaches the true final syncToken and every event is ingested."""
    store = Store(tmp_path / "test.sqlite3", dim=4)
    store.init()
    store.set_cursor("calendar", "old")

    n = 7
    events = [_event(f"evt{i}", f"Meeting {i}") for i in range(1, n + 1)]
    resp = _resp(events, next_sync_token="tokFINAL")
    svc = FakeCalService(on_synctoken=resp)

    per_call_capacity = 2
    max_cycles = 20
    for _cycle in range(max_cycles):
        if store.get_cursor("calendar") != "old":
            break
        budget = _FakeBudget(expire_after_calls=1 + per_call_capacity)
        sync_calendar(svc, store, budget=budget)
    else:
        raise AssertionError(
            f"cursor never advanced past the original delta window after "
            f"{max_cycles} cycles — this is the livelock the fix targets"
        )

    assert store.get_cursor("calendar") == "tokFINAL"
    assert store.get_cursor("calendar:resume_ids") == "[]"
    for i in range(1, n + 1):
        assert store.get_chunk(f"cal-evt{i}") is not None
