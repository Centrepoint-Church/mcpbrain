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

    def execute(self, num_retries=0):
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
    # One fewer expired() call than before: the first item is now written
    # unconditionally under the minimum-forward-progress guarantee, so the
    # cut-off lands one call earlier while the outcome under test is
    # unchanged (first item durable, second not, round still open).
    budget = _FakeBudget(expire_after_calls=1)
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


def test_event_edited_mid_round_is_picked_up_not_skipped(tmp_path):
    """New Critical found in adversarial review round 4: once an event's id
    landed in the resume set (round 3's fix), it was skipped for the REST OF
    THAT ROUND no matter what -- including if the event changed in between.
    The round then closed and the real syncToken advanced PAST the event's
    change record, with nothing left to re-surface it until the event
    happened to change again after the cursor had already moved on.

    Reproduced directly before this fix (against the round-3 code): a
    rescheduled event's stored text kept showing the OLD start time forever
    after the round closed. Fixed by keying the resume set on id+`updated`
    (_event_resume_key), not bare id, so an edit produces a DIFFERENT key
    and is recognized as new work rather than matched against the stale
    resume entry.
    """
    store = Store(tmp_path / "test.sqlite3", dim=4)
    store.init()
    store.set_cursor("calendar", "old")

    ev1 = _event("evt1", "Original meeting", start="2026-06-01T09:00:00Z")
    ev1["updated"] = "2026-05-01T00:00:00Z"
    ev2 = _event("evt2", "Other meeting")
    ev2["updated"] = "2026-05-01T00:00:00Z"
    resp = _resp([ev1, ev2], next_sync_token="tokNEW")
    svc = FakeCalService(on_synctoken=resp)

    upsert_calls = []
    orig_upsert = store.upsert_chunk

    def spy_upsert(*a, **kw):
        upsert_calls.append(a[0])  # doc_id
        return orig_upsert(*a, **kw)

    store.upsert_chunk = spy_upsert

    # Call 1: budget cuts off right after evt1 is processed with its
    # ORIGINAL content -- evt1's (stale) key lands in the resume set.
    # One fewer expired() call than before: the first event is now written
    # unconditionally under the minimum-forward-progress guarantee, so the
    # cut-off lands one call earlier while the scenario under test (a stale
    # resume key must not mask an edit) is unchanged.
    budget = _FakeBudget(expire_after_calls=1)
    sync_calendar(svc, store, budget=budget)
    assert store.get_cursor("calendar") == "old", "round must still be open"
    assert "09:00:00Z" in store.get_chunk("cal-evt1")["text"]

    # evt1 is rescheduled WHILE the round is still open -- Google bumps
    # `updated` on any modification, exactly like this fixture does.
    ev1_edited = _event("evt1", "Original meeting", start="2026-06-01T15:00:00Z")
    ev1_edited["updated"] = "2026-06-01T10:00:00Z"
    svc._events._syn = _resp([ev1_edited, ev2], next_sync_token="tokNEW")

    # Call 2: unbounded, completes the round.
    sync_calendar(svc, store, budget=None)

    assert store.get_cursor("calendar") == "tokNEW", "round must close"
    assert "15:00:00Z" in store.get_chunk("cal-evt1")["text"], (
        "evt1's reschedule must land -- the resume set must not have "
        "permanently skipped it just because its OLD id+updated key was "
        "already resumed from call 1"
    )
    assert store.get_cursor("calendar:resume_ids") == "[]"

    # evt1 was upserted exactly twice total across the two calls (once with
    # its original content in call 1, once with the edit in call 2) --
    # proving the edit is picked up exactly once per version, not repeatedly
    # re-applied within the same round nor silently dropped.
    assert upsert_calls.count("cal-evt1") == 2


def test_410_recovery_applies_content_despite_a_leftover_resume_entry(tmp_path):
    """The HTTP 410 (stale sync token) recovery path is the REPAIR for a
    broken token. A round-4 revision of this fix had it explicitly RESET the
    resume set in the 410 branch, reasoning that a leftover entry from the
    round that was open when the token went stale could otherwise cause
    recovery to re-skip content. Round 5 found that reasoning wrong on both
    counts:

    (1) The reset was unnecessary. With `_event_resume_key` keyed on
        id+`updated`, a leftover entry either no longer matches the CURRENT
        version of that event (so it's naturally reprocessed with no reset
        needed -- exactly what this test exercises: the seeded leftover key's
        `updated` value deliberately does NOT match the post-recovery
        event's, so it's a non-match by construction) or still matches
        because the event genuinely hasn't changed (in which case skipping
        it loses nothing -- that version's content is already durably
        upserted).
    (2) The reset was actively unsafe: unlike Gmail's equivalent 404/410
        reset (which ALSO advances the real cursor in the same call, so it
        fires at most once), Calendar's reset did NOT advance the cursor --
        so on a PERSISTENTLY-stale token it fired every single cycle,
        wiping the round's progress before it could ever accumulate enough
        to close. See test_persistently_410ing_token_eventually_recovers_
        not_livelocked below for that reproduction; THIS test only proves
        the single-call recovery-applies-content behaviour still holds
        without any special-case reset.
    """
    store = Store(tmp_path / "test.sqlite3", dim=4)
    store.init()
    store.set_cursor("calendar", "old-token")
    # Simulate a leftover resume set from an earlier interrupted round,
    # anchored at the now-stale token -- its `updated` value is stale, so it
    # does not match the post-recovery event's actual (current) key.
    store.set_cursor("calendar:resume_ids", '["evt1|stale-updated-value"]')

    ev1 = _event("evt1", "Meeting one (post-recovery)")
    ev1["updated"] = "2026-06-01T00:00:00Z"
    full_resp = _resp([ev1], next_sync_token="tokRECOVERED")
    svc = FakeCalService(raise_410_on_synctoken=True, on_full=full_resp)

    result = sync_calendar(svc, store, budget=None)

    assert result == 1, "evt1 must be applied on recovery, not skipped as already-resumed"
    assert store.get_cursor("calendar") == "tokRECOVERED"
    assert store.get_chunk("cal-evt1") is not None
    assert store.get_cursor("calendar:resume_ids") == "[]"


def test_persistently_410ing_token_eventually_recovers_not_livelocked(tmp_path):
    """Critical bug found in adversarial review round 5: a PREVIOUS revision
    of this fix had the 410 branch reset the resume set on every call. That
    reset never advances the real cursor itself (only the round-close path
    further down does), so a PERSISTENTLY-stale token -- one that keeps
    410ing on every attempt, not just once, e.g. a client/token integration
    that is fundamentally broken rather than merely due for a refresh --
    wiped the round's progress every single cycle before it could ever
    accumulate enough to close. A single-call test (like the one above)
    can't catch this class of bug: a lone 410-then-recover looks identical
    with or without the reset. Only a MULTI-cycle, budget-truncated,
    persistently-410ing scenario discriminates.

    Reproduced directly against the buggy (reset-in-place) code: 6+
    consecutive cycles all showed the cursor stuck at the original stale
    token, never progressing. This drives the same shape against the FIXED
    code (no reset) and asserts the cursor eventually reaches the recovered
    token instead of looping forever, and that every event is durably
    ingested by the time it does.
    """
    store = Store(tmp_path / "test.sqlite3", dim=4)
    store.init()
    store.set_cursor("calendar", "stale-token")

    n = 7
    events = [_event(f"evt{i}", f"Meeting {i}") for i in range(1, n + 1)]
    for ev in events:
        ev["updated"] = "2026-05-01T00:00:00Z"
    full_resp = _resp(events, next_sync_token="tokRECOVERED")
    # raise_410_on_synctoken=True means EVERY syncToken-bearing call 410s,
    # even after a cycle "recovers" and stores a new cursor -- simulating a
    # persistently broken token/client, not a one-time expiry.
    svc = FakeCalService(raise_410_on_synctoken=True, on_full=full_resp)

    per_call_capacity = 2
    max_cycles = 20
    for _cycle in range(max_cycles):
        if store.get_cursor("calendar") != "stale-token":
            break
        budget = _FakeBudget(expire_after_calls=1 + per_call_capacity)
        sync_calendar(svc, store, budget=budget)
    else:
        raise AssertionError(
            f"cursor never advanced past the stale token after {max_cycles} "
            f"cycles of persistent 410s -- this is the livelock the fix targets"
        )

    assert store.get_cursor("calendar") == "tokRECOVERED"
    assert store.get_cursor("calendar:resume_ids") == "[]"
    for i in range(1, n + 1):
        assert store.get_chunk(f"cal-evt{i}") is not None, f"evt{i} was never ingested"


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
