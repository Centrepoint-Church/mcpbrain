"""Tests for bounded initial backfill executor — fake services, no network.

Covers:
  - backfill_gmail (messages.list + messages.get path)
  - backfill_drive (files.list + export/get_media path)
  - initial_backfill (orchestration + embed)

All tests use fake services that mirror the shapes in test_gmail_sync.py
and test_drive_sync.py. No real API calls are made.
"""

import base64
from datetime import datetime, timezone

import pytest

from mcpbrain.store import Store
from mcpbrain.sync.gmail import backfill_gmail
from mcpbrain.sync.drive import backfill_drive
from mcpbrain.sync import initial_backfill


# ---------------------------------------------------------------------------
# Shared helpers
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
# Fake Gmail service — mirrors messages.list + messages.get
# ---------------------------------------------------------------------------

class _Req:
    def __init__(self, result):
        self._r = result

    def execute(self):
        return self._r


class _MessagesList:
    """Fake messages().list() that returns pages of message stubs."""
    def __init__(self, pages: list[dict], recorded_queries: list | None = None):
        # pages keyed by pageToken value; None -> first page
        self._pages = pages
        self._recorded = recorded_queries  # accumulates 'q' params when set

    def list(self, **kw):
        if self._recorded is not None:
            self._recorded.append(kw.get("q"))
        token = kw.get("pageToken")
        if token is None:
            idx = 0
        else:
            try:
                idx = int(token)
            except (ValueError, TypeError):
                idx = 0
        return _Req(self._pages[idx])

    def get(self, userId, id, format):
        # Handled by _MessagesGet — should not be called on this object
        raise AssertionError("get() called on _MessagesList")


class _MessagesGet:
    """Fake messages().get() that returns full message dicts by id."""
    def __init__(self, by_id: dict):
        self._by_id = by_id

    def get(self, userId, id, format):
        result = self._by_id[id]
        if isinstance(result, Exception):
            raise result
        return _Req(result)


class _Messages:
    """Combined fake: routes list() and get() to separate handlers."""
    def __init__(self, list_pages: list[dict], by_id: dict,
                 recorded_queries: list | None = None):
        self._list = _MessagesList(list_pages, recorded_queries)
        self._get = _MessagesGet(by_id)

    def list(self, **kw):
        return self._list.list(**kw)

    def get(self, userId, id, format):
        return self._get.get(userId, id, format)


class _Users:
    def __init__(self, messages: _Messages):
        self._m = messages

    def messages(self):
        return self._m


class FakeGmailService:
    """Fake Gmail service with messages.list + messages.get support."""
    def __init__(self, list_pages: list[dict], by_id: dict,
                 recorded_queries: list | None = None):
        self._users = _Users(_Messages(list_pages, by_id, recorded_queries))

    def users(self):
        return self._users


def _list_page(msg_ids: list[str], next_page_token=None) -> dict:
    """Build a messages.list response page."""
    page: dict = {"messages": [{"id": mid} for mid in msg_ids]}
    if next_page_token is not None:
        page["nextPageToken"] = next_page_token
    return page


# ---------------------------------------------------------------------------
# Fake Drive service — mirrors files.list + export/get_media
# ---------------------------------------------------------------------------

class _DriveReq:
    def __init__(self, result=None):
        self._r = result

    def execute(self):
        return self._r


class _FilesListFake:
    """Fake files().list() that returns pages of file stubs."""
    def __init__(self, pages: list[dict], recorded_queries: list | None = None):
        self._pages = pages
        self._recorded = recorded_queries

    def list(self, **kw):
        if self._recorded is not None:
            self._recorded.append(kw.get("q"))
        token = kw.get("pageToken")
        if token is None:
            idx = 0
        else:
            try:
                idx = int(token)
            except (ValueError, TypeError):
                idx = 0
        return _DriveReq(self._pages[idx])

    def export(self, fileId, mimeType):
        raise AssertionError("export() called on _FilesListFake")

    def get_media(self, fileId):
        raise AssertionError("get_media() called on _FilesListFake")


class _FilesFull:
    """Fake files() that handles list + export + get_media."""
    def __init__(self, list_pages: list[dict], exports: dict, media: dict,
                 recorded_queries: list | None = None):
        self._list_fake = _FilesListFake(list_pages, recorded_queries)
        self._exports = exports
        self._media = media

    def list(self, **kw):
        return self._list_fake.list(**kw)

    def export(self, fileId, mimeType):
        return _DriveReq(self._exports.get(fileId, b""))

    def get_media(self, fileId):
        return _DriveReq(self._media.get(fileId, b""))


class FakeDriveService:
    """Fake Drive service with files.list + export/get_media support."""
    def __init__(self, list_pages: list[dict], exports: dict = None,
                 media: dict = None, recorded_queries: list | None = None):
        self._files = _FilesFull(
            list_pages, exports or {}, media or {}, recorded_queries
        )

    def files(self):
        return self._files


def _drive_list_page(files: list[dict], next_page_token=None) -> dict:
    """Build a files.list response page."""
    page: dict = {"files": files}
    if next_page_token is not None:
        page["nextPageToken"] = next_page_token
    return page


def _gdoc_meta(fid: str, name: str = "Doc") -> dict:
    return {
        "id": fid,
        "name": name,
        "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-05-20T10:00:00Z",
        "owners": [{"displayName": "Test Owner"}],
    }


def _image_meta(fid: str, name: str = "photo.png") -> dict:
    return {
        "id": fid,
        "name": name,
        "mimeType": "image/png",
        "modifiedTime": "2026-05-20T10:00:00Z",
        "owners": [],
    }


# ---------------------------------------------------------------------------
# Store helper
# ---------------------------------------------------------------------------

def _store(tmp_path, dim=4):
    s = Store(tmp_path / "test.sqlite3", dim=dim)
    s.init()
    return s


# ---------------------------------------------------------------------------
# Module-scoped real embedder — loaded once for the whole module
# (~20-75 s first time; cached after that)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def emb():
    from mcpbrain.embed import get_embedder
    return get_embedder("bge-small")


# ===========================================================================
# backfill_gmail tests
# ===========================================================================

def test_backfill_gmail_indexes_matched_messages(tmp_path):
    """messages.list returns two ids; both are fetched, normalised, and upserted.
    The gmail delta cursor must remain None (backfill does not set it)."""
    store = _store(tmp_path)

    msg_m1 = plain_msg("m1", "Budget Q3", "finance@example.com",
                       "Quarterly budget review for the finance team.")
    msg_m2 = plain_msg("m2", "Staff update", "hr@example.com",
                       "Monthly staff update — please review before the meeting.")

    pages = [_list_page(["m1", "m2"])]
    svc = FakeGmailService(pages, {"m1": msg_m1, "m2": msg_m2})

    result = backfill_gmail(svc, store, after="2026/05/21")

    assert result == 2
    assert store.get_chunk("gmail-m1-body-0") is not None
    assert store.get_chunk("gmail-m2-body-0") is not None

    # Cursor must be untouched — backfill does not write the gmail cursor
    assert store.get_cursor("gmail") is None, (
        "backfill_gmail must not touch the gmail delta cursor"
    )


def test_backfill_gmail_respects_max_messages(tmp_path):
    """messages.list returns 5 ids; max_messages=2 stops after 2 processed."""
    store = _store(tmp_path)

    msgs = {
        f"m{i}": plain_msg(f"m{i}", f"Subject {i}", "x@example.com",
                           f"Body content for message {i} used in testing.")
        for i in range(1, 6)
    }
    pages = [_list_page([f"m{i}" for i in range(1, 6)])]
    svc = FakeGmailService(pages, msgs)

    result = backfill_gmail(svc, store, after="2026/05/21", max_messages=2)

    assert result == 2
    # Exactly 2 messages should be in the store
    upserted = [f"gmail-m{i}-body-0" for i in range(1, 6)
                if store.get_chunk(f"gmail-m{i}-body-0") is not None]
    assert len(upserted) == 2


def test_backfill_gmail_max_zero_indexes_nothing(tmp_path):
    """max_messages=0 must index nothing and return 0."""
    store = _store(tmp_path)

    msgs = {
        f"m{i}": plain_msg(f"m{i}", f"Subject {i}", "x@example.com",
                           f"Body content for message {i} used in testing.")
        for i in range(1, 4)
    }
    pages = [_list_page([f"m{i}" for i in range(1, 4)])]
    svc = FakeGmailService(pages, msgs)

    result = backfill_gmail(svc, store, after="2026/05/21", max_messages=0)

    assert result == 0
    assert store.unembedded_chunks() == []


def test_backfill_gmail_paginates(tmp_path):
    """Two pages of message ids; all messages from both pages are indexed."""
    store = _store(tmp_path)

    msg_m1 = plain_msg("m1", "First page message", "a@example.com",
                       "Content from the first page of results.")
    msg_m2 = plain_msg("m2", "Second page message", "b@example.com",
                       "Content from the second page of results.")
    msg_m3 = plain_msg("m3", "Also second page", "c@example.com",
                       "Another message from the second page of results.")

    # page 0 has nextPageToken "1" -> routes to pages[1]
    pages = [
        _list_page(["m1"], next_page_token="1"),
        _list_page(["m2", "m3"]),
    ]
    svc = FakeGmailService(pages, {"m1": msg_m1, "m2": msg_m2, "m3": msg_m3})

    result = backfill_gmail(svc, store, after="2026/05/21")

    assert result == 3
    assert store.get_chunk("gmail-m1-body-0") is not None
    assert store.get_chunk("gmail-m2-body-0") is not None
    assert store.get_chunk("gmail-m3-body-0") is not None


# ===========================================================================
# backfill_drive tests
# ===========================================================================

def test_backfill_drive_indexes_text_files(tmp_path):
    """files.list returns one Google Doc and one image/png.
    The doc is upserted; the image (binary) is skipped. Return value is 1.
    The drive delta cursor must remain None."""
    store = _store(tmp_path)

    gdoc = _gdoc_meta("f1", "Budget Plan")
    img = _image_meta("f2", "photo.png")

    pages = [_drive_list_page([gdoc, img])]
    svc = FakeDriveService(
        pages,
        exports={"f1": b"Budget plan for Q3 fiscal year. Details follow."},
    )

    result = backfill_drive(svc, store, modified_after="2026-05-21T00:00:00Z")

    assert result == 1
    assert store.get_chunk("gdrive-f1-0") is not None, "Google Doc should be upserted"
    assert store.get_chunk("gdrive-f2-0") is None, "Image/png should be skipped"

    # Drive delta cursor must be untouched
    assert store.get_cursor("drive") is None, (
        "backfill_drive must not touch the drive delta cursor"
    )


def test_backfill_drive_respects_max_files(tmp_path):
    """files.list returns 3 Google Docs; max_files=1 stops after the first."""
    store = _store(tmp_path)

    docs = [_gdoc_meta(f"f{i}", f"Doc {i}") for i in range(1, 4)]
    exports = {f"f{i}": f"Content of document {i} for the test.".encode()
               for i in range(1, 4)}

    pages = [_drive_list_page(docs)]
    svc = FakeDriveService(pages, exports=exports)

    result = backfill_drive(svc, store, modified_after="2026-05-21T00:00:00Z",
                            max_files=1)

    assert result == 1


def test_backfill_drive_paginates(tmp_path):
    """Two pages of files; all text-native files from both pages are indexed."""
    store = _store(tmp_path)

    doc_a = _gdoc_meta("fa", "Doc A")
    doc_b = _gdoc_meta("fb", "Doc B")

    pages = [
        _drive_list_page([doc_a], next_page_token="1"),
        _drive_list_page([doc_b]),
    ]
    exports = {
        "fa": b"Content of document A used in drive pagination test.",
        "fb": b"Content of document B used in drive pagination test.",
    }
    svc = FakeDriveService(pages, exports=exports)

    result = backfill_drive(svc, store, modified_after="2026-05-21T00:00:00Z")

    assert result == 2
    assert store.get_chunk("gdrive-fa-0") is not None
    assert store.get_chunk("gdrive-fb-0") is not None


# ===========================================================================
# initial_backfill orchestration tests
# ===========================================================================

def test_initial_backfill_orchestrates_and_embeds(tmp_path, emb):
    """End-to-end: fake gmail_service with one finance/budget message.
    Checks res["gmail"] >= 1, res["embedded"] >= 1, and that the message
    is findable via hybrid_search after backfill."""
    from mcpbrain.retrieval import hybrid_search

    store = Store(tmp_path / "b.sqlite3", dim=emb.dim)
    store.init()

    body = (
        "Annual budget review and quarterly expenditure forecast for the "
        "finance team. Please review the attached figures before Thursday."
    )
    msg_m1 = plain_msg("m1", "Finance Budget Forecast", "finance@example.com", body)

    pages = [_list_page(["m1"])]
    gmail_svc = FakeGmailService(pages, {"m1": msg_m1})

    now = datetime(2026, 5, 31, tzinfo=timezone.utc)
    res = initial_backfill(
        store, emb,
        gmail_service=gmail_svc,
        now=now,
        days=10,
    )

    assert res["gmail"] >= 1, f"Expected gmail >= 1, got {res}"
    assert res["embedded"] >= 1, f"Expected embedded >= 1, got {res}"

    results = hybrid_search(store, emb, "budget finance", limit=5)
    doc_ids = [r["doc_id"] for r in results]
    assert any(d.startswith("gmail-") for d in doc_ids), (
        f"Expected a gmail- doc_id in hybrid_search results, got: {doc_ids}"
    )


def test_initial_backfill_date_math(tmp_path):
    """With now=2026-05-31 and days=10, the after/modified_after strings
    passed to the fake services must be 2026/05/21 (Gmail) and
    2026-05-21T00:00:00Z (Drive)."""
    from mcpbrain.embed import get_embedder

    class _FakeEmbedder:
        dim = 4

        def embed_passages(self, texts):
            return [[0.0, 0.0, 0.0, 0.0]] * len(texts)

        def embed_query(self, text):
            return [0.0, 0.0, 0.0, 0.0]

    store = _store(tmp_path)

    # Record what queries each service receives
    gmail_queries: list[str] = []
    drive_queries: list[str] = []

    # Gmail: single empty page (no messages needed for date-math test)
    gmail_pages = [{"messages": []}]
    gmail_svc = FakeGmailService(gmail_pages, {}, recorded_queries=gmail_queries)

    # Drive: single empty page
    drive_pages = [{"files": []}]
    drive_svc = FakeDriveService(drive_pages, recorded_queries=drive_queries)

    now = datetime(2026, 5, 31, tzinfo=timezone.utc)
    initial_backfill(
        store, _FakeEmbedder(),
        gmail_service=gmail_svc,
        drive_service=drive_svc,
        now=now,
        days=10,
    )

    assert len(gmail_queries) >= 1, "Expected at least one gmail messages.list call"
    assert "after:2026/05/21" in gmail_queries[0], (
        f"Gmail query should contain 'after:2026/05/21', got: {gmail_queries[0]!r}"
    )

    assert len(drive_queries) >= 1, "Expected at least one drive files.list call"
    assert "2026-05-21T00:00:00Z" in drive_queries[0], (
        f"Drive query should contain '2026-05-21T00:00:00Z', got: {drive_queries[0]!r}"
    )
