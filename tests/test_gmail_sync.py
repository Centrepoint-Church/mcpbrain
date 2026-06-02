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
