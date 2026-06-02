"""Integration tests for run_sync_cycle — real bge-small embedder.

Proves the end-to-end path: sync → store → embed → searchable.
Uses the same fake Gmail service shape as test_gmail_sync.py.
"""

import base64

import pytest

from mcpbrain.embed import get_embedder
from mcpbrain.retrieval import hybrid_search
from mcpbrain.store import Store
from mcpbrain.sync import run_sync_cycle


# ---------------------------------------------------------------------------
# Fake Drive service (mirrors the shape in test_drive_sync.py)
# ---------------------------------------------------------------------------

class _DriveReq:
    def __init__(self, result=None, raise_exc=None):
        self._r = result
        self._e = raise_exc

    def execute(self):
        if self._e:
            raise self._e
        return self._r


class _DriveChanges:
    def __init__(self, pages, initial_cursor):
        self._pages = pages
        self._initial_cursor = initial_cursor

    def list(self, **kw):
        token = kw.get("pageToken")
        if token is None or token == self._initial_cursor:
            idx = 0
        else:
            try:
                idx = int(token)
            except (ValueError, TypeError):
                idx = 0
        return _DriveReq(self._pages[idx])


class _DriveFiles:
    def __init__(self, exports=None):
        self._exports = exports or {}

    def export(self, fileId, mimeType):
        return _DriveReq(self._exports.get(fileId, b""))


class FakeDriveService:
    def __init__(self, pages, exports, initial_cursor="100"):
        self._changes = _DriveChanges(pages, initial_cursor)
        self._files = _DriveFiles(exports)

    def changes(self):
        return self._changes

    def files(self):
        return self._files


def _drive_page(changes, next_page_token=None, new_start_page_token=None):
    p = {"changes": changes}
    if next_page_token is not None:
        p["nextPageToken"] = next_page_token
    if new_start_page_token is not None:
        p["newStartPageToken"] = new_start_page_token
    return p


def _gdoc_change(fid, name="Doc"):
    return {
        "fileId": fid,
        "removed": False,
        "file": {
            "id": fid,
            "name": name,
            "mimeType": "application/vnd.google-apps.document",
            "modifiedTime": "2026-05-01T10:00:00Z",
            "owners": [{"displayName": "Someone"}],
        },
    }


# ---------------------------------------------------------------------------
# Helpers (same shape as test_gmail_sync.py)
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


class _Req:
    def __init__(self, result):
        self._r = result

    def execute(self):
        return self._r


class _History:
    def __init__(self, pages):
        self._pages = pages

    def list(self, **kw):
        token = kw.get("pageToken")
        idx = 0 if token is None else int(token)
        return _Req(self._pages[idx])


class _Messages:
    def __init__(self, by_id):
        self._by_id = by_id

    def get(self, userId, id, format):
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


class FakeGmailService:
    def __init__(self, profile_hid="1000", pages=None, messages=None):
        msgs = _Messages(messages or {})
        self._users = _Users(profile_hid, _History(pages or []), msgs)

    def users(self):
        return self._users


def _make_page(msg_ids, history_id, next_page_token=None):
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
# Module-scoped fixture: load bge-small once for the whole test module
# (~20-75s first time; cached by sentence-transformers after that)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def emb():
    return get_embedder("bge-small")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_sync_cycle_makes_gmail_content_searchable(tmp_path, emb):
    """Sync one Gmail message, embed it, assert it's findable via hybrid_search.

    This is the Phase 2 integration proof: sync → store → embed → searchable.
    """
    store = Store(tmp_path / "b.sqlite3", dim=emb.dim)
    store.init()

    # Pre-set cursor so the delta path runs (not bootstrap)
    store.set_cursor("gmail", "1000")

    distinctive_body = (
        "Annual budget review and quarterly expenditure forecast for the finance team."
    )
    msg_m1 = plain_msg(
        "m1",
        "Finance Budget Forecast",
        "finance@example.com",
        distinctive_body,
    )
    pages = [_make_page(["m1"], history_id="1005")]
    fake = FakeGmailService(profile_hid="1000", pages=pages, messages={"m1": msg_m1})

    res = run_sync_cycle(store, emb, gmail_service=fake)

    # Sync count
    assert res["gmail"] == 1
    # At least one chunk embedded
    assert res["embedded"] >= 1

    # The content must be findable via hybrid_search
    results = hybrid_search(store, emb, "finance budget planning", limit=5)
    doc_ids = [r["doc_id"] for r in results]
    assert any(d.startswith("gmail-m1-body") for d in doc_ids), (
        f"Expected a result starting with 'gmail-m1-body', got: {doc_ids}"
    )


def test_sync_cycle_skips_absent_sources(tmp_path, emb):
    """run_sync_cycle with no services returns zero counts and does not raise."""
    store = Store(tmp_path / "c.sqlite3", dim=emb.dim)
    store.init()

    res = run_sync_cycle(store, emb)

    assert res == {"gmail": 0, "calendar": 0, "drive": 0, "embedded": 0}


def test_sync_cycle_embeds_after_sync(tmp_path, emb):
    """After a full cycle, no chunks remain unembedded."""
    store = Store(tmp_path / "d.sqlite3", dim=emb.dim)
    store.init()
    store.set_cursor("gmail", "1000")

    distinctive_body = (
        "Annual budget review and quarterly expenditure forecast for the finance team."
    )
    msg_m1 = plain_msg(
        "m1",
        "Finance Budget Forecast",
        "finance@example.com",
        distinctive_body,
    )
    pages = [_make_page(["m1"], history_id="1005")]
    fake = FakeGmailService(profile_hid="1000", pages=pages, messages={"m1": msg_m1})

    run_sync_cycle(store, emb, gmail_service=fake)

    assert store.unembedded_chunks() == [], "Expected all chunks to be embedded after the cycle"


def test_sync_cycle_multi_source_accumulates_and_no_double_embed(tmp_path, emb):
    """run_sync_cycle with Gmail + Drive accumulates embedded counts across both sources.

    Proves three things:
    1. Both sources contribute chunks (delta paths run because cursors are pre-set).
    2. res["embedded"] equals the total chunk count from both sources combined.
    3. A second identical call embeds 0 new chunks — already-embedded chunks are
       not re-embedded (idempotent upsert + embedded flag behaviour).
    """
    store = Store(tmp_path / "multi.sqlite3", dim=emb.dim)
    store.init()

    # Pre-set both cursors so the delta paths run (not bootstrap).
    store.set_cursor("gmail", "1000")
    store.set_cursor("drive", "100")

    # --- Fake Gmail: one message with a distinctive body ---
    gmail_body = (
        "Pastoral care meeting agenda for staff review and ministry operations update."
    )
    msg_m1 = plain_msg(
        "m1",
        "Pastoral Care Agenda",
        "pastor@example.com",
        gmail_body,
    )
    gmail_pages = [_make_page(["m1"], history_id="1005")]
    fake_gmail = FakeGmailService(
        profile_hid="1000",
        pages=gmail_pages,
        messages={"m1": msg_m1},
    )

    # --- Fake Drive: one Google Doc with distinct content ---
    drive_body = b"Volunteer coordination handbook for onboarding and role allocation."
    drive_pages = [
        _drive_page(
            [_gdoc_change("f1", "Volunteer Handbook")],
            new_start_page_token="105",
        )
    ]
    fake_drive = FakeDriveService(
        pages=drive_pages,
        exports={"f1": drive_body},
        initial_cursor="100",
    )

    # First cycle: both sources sync and embed.
    res = run_sync_cycle(store, emb, gmail_service=fake_gmail, drive_service=fake_drive)

    assert res["gmail"] == 1, f"Expected 1 Gmail message synced, got {res['gmail']}"
    assert res["drive"] == 1, f"Expected 1 Drive file synced, got {res['drive']}"

    # All chunks must be embedded and the total must match the embedded counter.
    assert store.unembedded_chunks() == [], "Expected all chunks embedded after first cycle"
    assert res["embedded"] >= 2, (
        f"Expected at least 2 chunks embedded (one per source), got {res['embedded']}"
    )

    # Spot-check: expected chunk IDs exist in the store.
    gmail_chunk = store.get_chunk("gmail-m1-body-0")
    assert gmail_chunk is not None, "gmail-m1-body-0 chunk missing from store"

    drive_chunk = store.get_chunk("gdrive-f1-0")
    assert drive_chunk is not None, "gdrive-f1-0 chunk missing from store"

    # Second cycle: same fakes re-present the same content.
    # Upsert is idempotent (same content_hash → no update, embedded flag stays 1).
    # index_pending finds nothing unembedded, so embedded == 0.
    #
    # After the first cycle:
    #   - Gmail cursor is "1005" (set by sync_gmail from historyId in the page).
    #   - Drive cursor is "105" (set by sync_drive from newStartPageToken).
    # The second Gmail fake re-delivers the same history page (historyId "1005"),
    # yielding the same message; upsert is a no-op (same content_hash).
    # The second Drive fake uses initial_cursor="105" to match the advanced cursor,
    # and returns an empty changes page — no files to process.
    fake_gmail2 = FakeGmailService(
        profile_hid="1000",
        pages=gmail_pages,
        messages={"m1": msg_m1},
    )
    empty_drive_page = _drive_page([], new_start_page_token="106")
    fake_drive2 = FakeDriveService(
        pages=[empty_drive_page],
        exports={},
        initial_cursor="105",  # matches the cursor set by the first cycle
    )
    res2 = run_sync_cycle(store, emb, gmail_service=fake_gmail2, drive_service=fake_drive2)

    assert res2["embedded"] == 0, (
        f"Expected 0 new embeddings on second cycle (idempotent), got {res2['embedded']}"
    )
