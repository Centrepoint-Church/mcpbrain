"""Tests for mcpbrain.sync.gmail — fake service, no network."""

import base64

import httplib2
import pytest
from googleapiclient.errors import HttpError

from mcpbrain.store import Store
from mcpbrain.sync.gmail import sync_gmail


# ---------------------------------------------------------------------------
# Helpers shared with test_normalise.py
# ---------------------------------------------------------------------------

def b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode()


def plain_msg(mid: str, subject: str, sender: str, body: str) -> dict:
    return {
        "id": mid,
        "threadId": "t-" + mid,
        "labelIds": ["INBOX"],
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": sender},
            ],
            "body": {"data": b64(body)},
        },
    }


# ---------------------------------------------------------------------------
# Fake Gmail service
# ---------------------------------------------------------------------------

class _Req:
    def __init__(self, result):
        self._r = result

    def execute(self):
        return self._r


class _History:
    def __init__(self, pages, raise_on_list=None):
        # pages is a list of page dicts; pageToken "1","2",... indexes self._pages
        self._pages = pages
        self._raise = raise_on_list  # if set, raise this on list()

    def list(self, **kw):
        if self._raise is not None:
            raise self._raise
        token = kw.get("pageToken")
        idx = 0 if token is None else int(token)
        return _Req(self._pages[idx])


class _Messages:
    def __init__(self, by_id):
        self._by_id = by_id
        self.get_call_count = {}  # mid -> count

    def get(self, userId, id, format):
        self.get_call_count[id] = self.get_call_count.get(id, 0) + 1
        result = self._by_id[id]
        if isinstance(result, Exception):
            raise result
        return _Req(result)


class _Users:
    def __init__(self, profile_hid, history, messages):
        self._p = profile_hid
        self._h = history
        self._m = messages

    def getProfile(self, userId):
        return _Req({"historyId": self._p, "emailAddress": "test@example.com"})

    def history(self):
        return self._h

    def messages(self):
        return self._m


class FakeService:
    def __init__(self, profile_hid="1000", pages=None, messages=None, raise_on_list=None):
        msgs = _Messages(messages or {})
        self._users = _Users(profile_hid, _History(pages or [], raise_on_list=raise_on_list), msgs)
        self._messages = msgs  # expose for call-count assertions

    def users(self):
        return self._users


def _make_page(msg_ids, history_id, next_page_token=None):
    """Build a history.list response page."""
    history = [
        {
            "id": f"h-{mid}",
            "messagesAdded": [{"message": {"id": mid, "labelIds": ["INBOX"]}}],
        }
        for mid in msg_ids
    ]
    page = {"history": history, "historyId": history_id}
    if next_page_token is not None:
        page["nextPageToken"] = next_page_token
    return page


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_bootstrap_sets_cursor_no_messages(tmp_path):
    """First run: no cursor stored. Should read historyId from profile, store it,
    return 0, and leave the chunk store empty."""
    store = Store(tmp_path / "test.sqlite3", dim=4)
    store.init()

    svc = FakeService(profile_hid="1000")
    result = sync_gmail(svc, store)

    assert result == 0
    assert store.get_cursor("gmail") == "1000"
    assert store.unembedded_chunks() == []


def test_delta_sync_fetches_and_upserts(tmp_path):
    """Delta run: cursor at 1000, one history page with m1, cursor advances to 1005,
    m1's chunk is upserted, return value is 1."""
    store = Store(tmp_path / "test.sqlite3", dim=4)
    store.init()
    store.set_cursor("gmail", "1000")

    msg_m1 = plain_msg("m1", "Budget update", "alice@example.com",
                       "The quarterly budget review is scheduled for next week.")
    pages = [_make_page(["m1"], history_id="1005")]
    svc = FakeService(profile_hid="1000", pages=pages, messages={"m1": msg_m1})

    result = sync_gmail(svc, store)

    assert result == 1
    assert store.get_cursor("gmail") == "1005"
    chunk = store.get_chunk("gmail-m1-body-0")
    assert chunk is not None
    assert "budget" in chunk["text"].lower()


def test_pagination_collects_all_ids(tmp_path):
    """Two pages of history. Both m1 and m2 should be upserted; cursor = last page's historyId."""
    store = Store(tmp_path / "test.sqlite3", dim=4)
    store.init()
    store.set_cursor("gmail", "1000")

    msg_m1 = plain_msg("m1", "First message", "alice@example.com",
                       "Content of the first message, here for testing.")
    msg_m2 = plain_msg("m2", "Second message", "bob@example.com",
                       "Content of the second message, also for testing.")

    # page0 has nextPageToken "1" → indexes pages[1]
    pages = [
        _make_page(["m1"], history_id="1003", next_page_token="1"),
        _make_page(["m2"], history_id="1007"),
    ]
    svc = FakeService(
        profile_hid="1000",
        pages=pages,
        messages={"m1": msg_m1, "m2": msg_m2},
    )

    result = sync_gmail(svc, store)

    assert result == 2
    assert store.get_chunk("gmail-m1-body-0") is not None
    assert store.get_chunk("gmail-m2-body-0") is not None
    assert store.get_cursor("gmail") == "1007"


def test_duplicate_message_id_fetched_once(tmp_path):
    """m1 appears in two messagesAdded entries. messages.get should be called exactly once."""
    store = Store(tmp_path / "test.sqlite3", dim=4)
    store.init()
    store.set_cursor("gmail", "1000")

    msg_m1 = plain_msg("m1", "Duplicate test", "carol@example.com",
                       "This message appears twice in the history feed.")

    # Craft a page where m1 appears in two separate history records
    page = {
        "history": [
            {"id": "h1", "messagesAdded": [{"message": {"id": "m1", "labelIds": ["INBOX"]}}]},
            {"id": "h2", "messagesAdded": [{"message": {"id": "m1", "labelIds": ["INBOX"]}}]},
        ],
        "historyId": "1010",
    }
    svc = FakeService(profile_hid="1000", pages=[page], messages={"m1": msg_m1})

    result = sync_gmail(svc, store)

    # Only one message processed
    assert result == 1
    # messages.get called exactly once for m1
    assert svc._messages.get_call_count.get("m1", 0) == 1
    # No duplicate chunks in the store
    chunks = store.unembedded_chunks()
    doc_ids = [c["doc_id"] for c in chunks]
    assert len(doc_ids) == len(set(doc_ids)), "Duplicate doc_ids found in store"


def test_cursor_not_advanced_on_fetch_error(tmp_path):
    """If messages.get raises, sync_gmail propagates the error and leaves cursor unchanged."""
    store = Store(tmp_path / "test.sqlite3", dim=4)
    store.init()
    store.set_cursor("gmail", "1000")

    pages = [_make_page(["m1"], history_id="1005")]
    # Make messages.get raise for m1
    svc = FakeService(
        profile_hid="1000",
        pages=pages,
        messages={"m1": RuntimeError("Network error")},
    )

    with pytest.raises(RuntimeError, match="Network error"):
        sync_gmail(svc, store)

    # Cursor must be unchanged
    assert store.get_cursor("gmail") == "1000"


def test_expired_historyid_rebootstraps(tmp_path):
    """history().list() raises 404 INVALID_HISTORY_ID -> re-bootstrap to fresh historyId, return 0."""
    store = Store(tmp_path / "test.sqlite3", dim=4)
    store.init()
    # Pre-seed an old cursor to trigger the delta path
    store.set_cursor("gmail", "1000")

    error = HttpError(httplib2.Response({"status": 404}), b"INVALID_HISTORY_ID")
    # profile_hid = "5000" is what getProfile returns during re-bootstrap
    svc = FakeService(profile_hid="5000", raise_on_list=error)

    result = sync_gmail(svc, store)

    assert result == 0
    assert store.get_cursor("gmail") == "5000"


def test_expired_historyid_410_rebootstraps(tmp_path):
    """history().list() raises 410 -> also triggers re-bootstrap."""
    store = Store(tmp_path / "test.sqlite3", dim=4)
    store.init()
    store.set_cursor("gmail", "2000")

    error = HttpError(httplib2.Response({"status": 410}), b"Sync token expired")
    svc = FakeService(profile_hid="6000", raise_on_list=error)

    result = sync_gmail(svc, store)

    assert result == 0
    assert store.get_cursor("gmail") == "6000"


def test_non_404_httperror_propagates(tmp_path):
    """history().list() raises HttpError with status 500 -> propagates, not swallowed."""
    store = Store(tmp_path / "test.sqlite3", dim=4)
    store.init()
    store.set_cursor("gmail", "1000")

    error = HttpError(httplib2.Response({"status": 500}), b"Internal Server Error")
    svc = FakeService(profile_hid="1000", raise_on_list=error)

    with pytest.raises(HttpError):
        sync_gmail(svc, store)

    # Cursor must be unchanged
    assert store.get_cursor("gmail") == "1000"


# ---------------------------------------------------------------------------
# Task 2 duty-cycle fix: budget-interrupted mid-fetch must checkpoint safely
# ---------------------------------------------------------------------------

class _FakeBudget:
    """expired() returns False for the first `expire_after_calls` calls, True
    from then on — lets a test pin EXACTLY which iteration a real Budget's
    wall-clock expiry would have landed on, deterministically."""

    def __init__(self, expire_after_calls):
        self.calls = 0
        self.expire_after_calls = expire_after_calls

    def expired(self) -> bool:
        self.calls += 1
        return self.calls > self.expire_after_calls


def test_budget_interrupted_mid_fetch_resumes_without_skip_or_duplicate(tmp_path):
    """A budget that expires partway through the message-fetch loop must not
    advance the cursor. Gmail's history.list "historyId" field is a SNAPSHOT
    of the mailbox's current state, not a per-page cursor — advancing to it
    before every message on this delta round is fetched-and-upserted would
    silently skip whatever wasn't reached yet, forever (a future
    startHistoryId=<new cursor> query can never see it again).

    Verifies both halves of the checkpoint contract:
    (1) the interrupted call durably upserts only the messages it reached
        before the budget expired, and leaves the cursor at its OLD value;
    (2) a follow-up call with no budget completes the resume — the remaining
        messages get upserted, the cursor advances to the true final
        historyId, and m1 (already durably done in the first call) is
        genuinely SKIPPED on resume (via the persisted `gmail:resume_ids`
        set), not re-fetched-and-re-upserted — this is the incremental
        checkpoint, not just an idempotent re-walk (see
        test_budget_interrupted_across_many_cycles_eventually_completes for
        why "just re-walk the whole window every time" is actually a
        livelock on a delta bigger than one budget).
    """
    store = Store(tmp_path / "test.sqlite3", dim=4)
    store.init()
    store.set_cursor("gmail", "1000")

    msg_m1 = plain_msg("m1", "First", "alice@example.com",
                       "first message body content, long enough to matter")
    msg_m2 = plain_msg("m2", "Second", "bob@example.com",
                       "second message body content, long enough to matter")
    msg_m3 = plain_msg("m3", "Third", "carol@example.com",
                       "third message body content, long enough to matter")
    # Single page (no nextPageToken), so the pagination loop makes exactly one
    # budget.expired() check before consuming it; the message-fetch loop then
    # checks once per NOT-YET-RESUMED message id, in order, before fetching it.
    pages = [_make_page(["m1", "m2", "m3"], history_id="1010")]
    svc = FakeService(profile_hid="1000", pages=pages,
                      messages={"m1": msg_m1, "m2": msg_m2, "m3": msg_m3})

    # Call 1: pagination's own check (not expired). Call 2: fetch-loop check
    # before m1 (not expired -> m1 IS processed). Call 3: fetch-loop check
    # before m2 (expired -> loop stops; m2/m3 never fetched this call).
    # One fewer expired() call than before: the first item is now written
    # unconditionally under the minimum-forward-progress guarantee, so the
    # cut-off lands one call earlier while the outcome under test is
    # unchanged (first item durable, second not, round still open).
    budget = _FakeBudget(expire_after_calls=1)
    result = sync_gmail(svc, store, budget=budget)

    assert result == 1, "only the message(s) processed before budget expiry should count"
    assert store.get_cursor("gmail") == "1000", (
        "cursor must NOT advance on a partial run — an early advance would "
        "silently and permanently skip m2/m3 (see docstring)"
    )
    assert store.get_chunk("gmail-m1-body-0") is not None
    assert store.get_chunk("gmail-m2-body-0") is None
    assert store.get_chunk("gmail-m3-body-0") is None
    assert svc._messages.get_call_count.get("m1") == 1

    # Resume: cursor is unchanged, so this re-lists the SAME delta window, but
    # m1 is now in the persisted resume set and must be SKIPPED (not
    # re-fetched) — only m2/m3 are genuinely new work this call.
    result2 = sync_gmail(svc, store, budget=None)

    assert result2 == 2, "only the genuinely new messages (m2, m3) should be counted/fetched on resume"
    assert store.get_cursor("gmail") == "1010"
    assert store.get_chunk("gmail-m1-body-0") is not None
    assert store.get_chunk("gmail-m2-body-0") is not None
    assert store.get_chunk("gmail-m3-body-0") is not None
    # m1 was fetched via the API exactly once total, across both calls — the
    # resume genuinely skipped it rather than harmlessly re-fetching it.
    assert svc._messages.get_call_count.get("m1") == 1
    assert store.get_cursor("gmail:resume_ids") == "[]", "resume set must be cleared once the round closes"

    # No duplicate-visible-effect regardless: upsert_chunk keys on doc_id, so
    # even if m1 HAD been re-processed it would still be exactly one row.
    with store._connect() as db:
        count = db.execute(
            "SELECT COUNT(*) FROM chunks WHERE doc_id='gmail-m1-body-0'"
        ).fetchone()[0]
    assert count == 1


def test_budget_interrupted_across_many_cycles_eventually_completes(tmp_path):
    """Critical-B reproduction (adversarial review, Task 2 round 3).

    A delta bigger than one budget's worth of fetches used to LIVELOCK: every
    call re-listed the same unmoved cursor, got the same ordered message-id
    list, and — with the same per-call budget — processed the exact same
    PREFIX every time, so the cursor never advanced and messages past the
    prefix were silently and permanently never ingested. Reproduced directly
    against the pre-fix code: 5 identical calls over a 7-message delta with a
    budget covering only 2 messages each time produced `processed=2,
    cursor=1000` on EVERY call, no matter how many times it was retried.

    This exercises the same 7-message/2-per-call shape across enough calls to
    span the whole delta, and asserts: (1) the cursor DOES eventually advance
    (proving forward progress, not a livelock); (2) every message ends up
    ingested exactly once (via a per-doc_id COUNT, not just "no exception");
    (3) each message was fetched via the API at most once total across every
    call (proving genuine skip-on-resume, not merely idempotent re-work every
    round — the bug-for-bug distinction between this fix and "just don't
    advance the cursor").
    """
    store = Store(tmp_path / "test.sqlite3", dim=4)
    store.init()
    store.set_cursor("gmail", "1000")

    n = 7
    msgs = {
        f"m{i}": plain_msg(f"m{i}", f"Subject {i}", "sender@example.com",
                          f"message body number {i}, long enough to matter")
        for i in range(1, n + 1)
    }
    pages = [_make_page([f"m{i}" for i in range(1, n + 1)], history_id="2000")]
    svc = FakeService(profile_hid="1000", pages=pages, messages=msgs)

    per_call_capacity = 2   # matches the reviewer's exact reproduction shape
    max_cycles = 20         # generous upper bound; real convergence is ~4 cycles
    for _cycle in range(max_cycles):
        if store.get_cursor("gmail") != "1000":
            break
        budget = _FakeBudget(expire_after_calls=1 + per_call_capacity)
        sync_gmail(svc, store, budget=budget)
    else:
        raise AssertionError(
            f"cursor never advanced past the original delta window after "
            f"{max_cycles} cycles — this is the livelock the fix targets"
        )

    assert store.get_cursor("gmail") == "2000", "cursor must eventually reach the true final historyId"
    assert store.get_cursor("gmail:resume_ids") == "[]"
    for i in range(1, n + 1):
        mid = f"m{i}"
        assert store.get_chunk(f"gmail-{mid}-body-0") is not None, f"{mid} was never ingested"
        with store._connect() as db:
            count = db.execute(
                "SELECT COUNT(*) FROM chunks WHERE doc_id=?", (f"gmail-{mid}-body-0",)
            ).fetchone()[0]
        assert count == 1, f"{mid} produced more than one chunk row"
        # Genuine skip-on-resume: each message is fetched via the real API at
        # most once across every cycle, not re-fetched every round.
        assert svc._messages.get_call_count.get(mid, 0) == 1, (
            f"{mid} was fetched {svc._messages.get_call_count.get(mid, 0)} times — "
            "expected exactly once (re-fetching already-done messages every "
            "cycle would still 'work' via idempotent upserts, but defeats the "
            "point of incremental checkpointing and wastes API calls)"
        )


def test_budget_interrupted_mid_pagination_never_advances_cursor(tmp_path):
    """Same checkpoint contract, but the budget expires during history.list
    PAGINATION itself (before any message is even collected) rather than
    during the fetch loop — the cursor must still not move."""
    store = Store(tmp_path / "test.sqlite3", dim=4)
    store.init()
    store.set_cursor("gmail", "1000")

    msg_m1 = plain_msg("m1", "First", "alice@example.com",
                       "first message body content, long enough to matter")
    # Two pages, so a budget that's already expired before the SECOND page
    # request stops pagination with page 2 (and its message) never fetched.
    pages = [
        _make_page(["m1"], history_id="1003", next_page_token="1"),
        _make_page(["m2"], history_id="1007"),
    ]
    svc = FakeService(profile_hid="1000", pages=pages, messages={"m1": msg_m1})

    # Call 1: pagination check before page 0 (not expired -> page 0 fetched,
    # m1 collected). Call 2: pagination check before page 1 (expired -> loop
    # stops; page 1 / m2 never even listed).
    budget = _FakeBudget(expire_after_calls=1)
    result = sync_gmail(svc, store, budget=budget)

    # m1 WAS collected before pagination stopped, and the minimum-forward-
    # progress guarantee writes one item per call rather than none -- durable,
    # checkpointed, and re-listed next cycle. The contract this test exists for
    # is the cursor: an interrupted round must never advance it, because
    # page 1 / m2 was never even listed.
    assert result == 1, "the one collected message should still be written"
    assert store.get_cursor("gmail") == "1000", "interrupted round must not advance"
    assert store.get_chunk("gmail-m1-body-0") is not None
