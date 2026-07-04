"""Tests for mcpbrain.sync.drive — fake service, no network."""

import pytest

from mcpbrain.store import Store
from mcpbrain.sync.drive import sync_drive, backfill_drive, normalise_drive, _fetch_text


# ---------------------------------------------------------------------------
# Fake Drive service
# ---------------------------------------------------------------------------

class _Req:
    def __init__(self, result=None, raise_exc=None):
        self._r = result
        self._e = raise_exc

    def execute(self):
        if self._e:
            raise self._e
        return self._r


class _Changes:
    def __init__(self, start_token="100", pages=None, initial_cursor=None):
        self._start = start_token
        self._pages = pages or []
        # The first delta call uses the stored cursor as pageToken.
        # We always route that to pages[0]. Subsequent nextPageToken values
        # are string integers ("1", "2", …) that index directly into _pages.
        self._initial_cursor = initial_cursor  # set by FakeDriveService

    def getStartPageToken(self, **_kw):          # accept driveId/supportsAllDrives
        return _Req({"startPageToken": self._start})

    def list(self, **kw):
        token = kw.get("pageToken")
        if token is None or token == self._initial_cursor:
            # First call in a delta run
            idx = 0
        else:
            try:
                idx = int(token)
            except (ValueError, TypeError):
                idx = 0
        return _Req(self._pages[idx])


class _Files:
    def __init__(self, exports=None, media=None, export_raises=None, file_list=None):
        self._exports = exports or {}
        self._media = media or {}
        self._raise = export_raises or {}
        # file_list: list of file metadata dicts returned by files().list()
        self._file_list = file_list or []

    def export(self, fileId, mimeType, supportsAllDrives=None):
        assert supportsAllDrives is True, (
            "export() must pass supportsAllDrives=True — required by the real "
            "Drive v3 API for files inside a Shared Drive"
        )
        if fileId in self._raise:
            return _Req(raise_exc=self._raise[fileId])
        return _Req(self._exports.get(fileId, b""))

    def get_media(self, fileId, supportsAllDrives=None):
        assert supportsAllDrives is True, (
            "get_media() must pass supportsAllDrives=True — required by the real "
            "Drive v3 API for files inside a Shared Drive"
        )
        return _Req(self._media.get(fileId, b""))

    def list(self, **_kw):
        return _Req({"files": self._file_list})


class _Drives:
    def __init__(self, drives=None):
        self._drives = drives or []

    def list(self, **_kw):
        return _Req({"drives": self._drives})


class FakeDriveService:
    def __init__(self, **kw):
        # initial_cursor is the pageToken the first delta call will carry.
        # Defaults to start_token so the most common case (cursor=="100")
        # routes correctly without needing to pass it explicitly.
        start = kw.get("start_token", "100")
        initial = kw.get("initial_cursor", start)
        self._changes = _Changes(start, kw.get("pages"), initial_cursor=initial)
        self._files = _Files(
            kw.get("exports"),
            kw.get("media"),
            kw.get("export_raises"),
            kw.get("file_list"),
        )
        self._drives = _Drives(kw.get("shared_drives"))

    def changes(self):
        return self._changes

    def files(self):
        return self._files

    def drives(self):
        return self._drives


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _gdoc_change(fid, name="Doc", removed=False):
    ch = {"fileId": fid, "removed": removed}
    if not removed:
        ch["file"] = {
            "id": fid,
            "name": name,
            "mimeType": "application/vnd.google-apps.document",
            "modifiedTime": "2026-05-01T10:00:00Z",
            "owners": [{"displayName": "Someone"}],
        }
    return ch


def _plain_change(fid, name="Note", mime="text/plain"):
    return {
        "fileId": fid,
        "removed": False,
        "file": {
            "id": fid,
            "name": name,
            "mimeType": mime,
            "modifiedTime": "2026-05-01T10:00:00Z",
            "owners": [{"displayName": "Owner"}],
        },
    }


def _page(changes, next_page_token=None, new_start_page_token=None):
    p = {"changes": changes}
    if next_page_token is not None:
        p["nextPageToken"] = next_page_token
    if new_start_page_token is not None:
        p["newStartPageToken"] = new_start_page_token
    return p


def _store(tmp_path):
    s = Store(tmp_path / "test.sqlite3", dim=4)
    s.init()
    return s


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_bootstrap_sets_cursor_no_files(tmp_path):
    """First run: no cursor. getStartPageToken returns "100".
    sync_drive returns 0, cursor is set to "100", no chunks upserted."""
    store = _store(tmp_path)
    svc = FakeDriveService(start_token="100")

    result = sync_drive(svc, store)

    assert result == 0
    assert store.get_cursor("drive") == "100"
    assert store.unembedded_chunks() == []


def test_delta_google_doc_exported_and_upserted(tmp_path):
    """Delta run: cursor "100", one Google Doc change, text exported and upserted.
    Cursor advances to "105", return value 1, chunk present."""
    store = _store(tmp_path)
    store.set_cursor("drive", "100")

    pages = [
        _page(
            [_gdoc_change("f1", "Budget Plan")],
            new_start_page_token="105",
        )
    ]
    svc = FakeDriveService(
        pages=pages,
        exports={"f1": b"Budget plan for Q3"},
    )

    result = sync_drive(svc, store)

    assert result == 1
    assert store.get_cursor("drive") == "105"
    chunk = store.get_chunk("gdrive-f1-0")
    assert chunk is not None
    assert "Budget plan" in chunk["text"]


def test_text_file_via_get_media(tmp_path):
    """text/plain file fetched via get_media, upserted as gdrive-f2-0."""
    store = _store(tmp_path)
    store.set_cursor("drive", "100")

    pages = [
        _page(
            [_plain_change("f2", "Meeting Notes", "text/plain")],
            new_start_page_token="106",
        )
    ]
    svc = FakeDriveService(
        pages=pages,
        media={"f2": b"meeting notes here"},
    )

    result = sync_drive(svc, store)

    assert result == 1
    chunk = store.get_chunk("gdrive-f2-0")
    assert chunk is not None
    assert "meeting notes" in chunk["text"]


def test_removed_change_skipped(tmp_path):
    """A change with removed=True is not upserted and not counted."""
    store = _store(tmp_path)
    store.set_cursor("drive", "100")

    removed_change = {"fileId": "f3", "removed": True}
    pages = [
        _page([removed_change], new_start_page_token="107")
    ]
    svc = FakeDriveService(pages=pages)

    result = sync_drive(svc, store)

    assert result == 0
    assert store.get_chunk("gdrive-f3-0") is None


def test_unsupported_mime_skipped(tmp_path):
    """image/png file: _fetch_text returns None, not upserted, not counted."""
    store = _store(tmp_path)
    store.set_cursor("drive", "100")

    img_change = {
        "fileId": "f4",
        "removed": False,
        "file": {
            "id": "f4",
            "name": "photo.png",
            "mimeType": "image/png",
            "modifiedTime": "2026-05-01T10:00:00Z",
            "owners": [],
        },
    }
    pages = [
        _page([img_change], new_start_page_token="108")
    ]
    svc = FakeDriveService(pages=pages)

    result = sync_drive(svc, store)

    assert result == 0
    assert store.get_chunk("gdrive-f4-0") is None


def test_pagination_processes_all(tmp_path):
    """Two pages: page 0 has nextPageToken -> page 1; page 1 has newStartPageToken.
    Files on both pages are upserted; cursor equals last newStartPageToken."""
    store = _store(tmp_path)
    store.set_cursor("drive", "0")  # '0' maps to pages[0]

    pages = [
        # page 0: index 0, nextPageToken "1" -> routes to pages[1]
        _page(
            [_gdoc_change("fa", "Doc A")],
            next_page_token="1",
        ),
        # page 1: index 1, last page carries newStartPageToken
        _page(
            [_gdoc_change("fb", "Doc B")],
            new_start_page_token="200",
        ),
    ]
    svc = FakeDriveService(
        pages=pages,
        exports={
            "fa": b"Content of Doc A for testing",
            "fb": b"Content of Doc B for testing",
        },
    )

    result = sync_drive(svc, store)

    assert result == 2
    assert store.get_chunk("gdrive-fa-0") is not None
    assert store.get_chunk("gdrive-fb-0") is not None
    assert store.get_cursor("drive") == "200"


def test_cursor_not_advanced_on_fetch_error(tmp_path):
    """If export raises RuntimeError, sync_drive propagates it and cursor stays unchanged."""
    store = _store(tmp_path)
    store.set_cursor("drive", "100")

    pages = [
        _page(
            [_gdoc_change("f5", "Failing Doc")],
            new_start_page_token="110",
        )
    ]
    svc = FakeDriveService(
        pages=pages,
        export_raises={"f5": RuntimeError("export failed")},
    )

    with pytest.raises(RuntimeError, match="export failed"):
        sync_drive(svc, store)

    assert store.get_cursor("drive") == "100"


# ---------------------------------------------------------------------------
# Unit tests for helpers
# ---------------------------------------------------------------------------

def test_fetch_text_google_doc():
    """_fetch_text routes Google Doc to export and returns decoded text."""
    meta = {"id": "x1", "mimeType": "application/vnd.google-apps.document"}
    svc = FakeDriveService(exports={"x1": b"Hello world"})
    assert _fetch_text(svc, meta) == "Hello world"


def test_fetch_text_plain_via_get_media():
    """_fetch_text routes text/plain to get_media."""
    meta = {"id": "x2", "mimeType": "text/plain"}
    svc = FakeDriveService(media={"x2": b"plain text content"})
    assert _fetch_text(svc, meta) == "plain text content"


def test_fetch_text_image_still_skipped():
    """_fetch_text returns None for image/png — images are not extracted."""
    meta = {"id": "x3", "mimeType": "image/png"}
    svc = FakeDriveService()
    assert _fetch_text(svc, meta) is None


def test_normalise_drive_produces_correct_doc_ids():
    """normalise_drive: doc_id pattern is gdrive-<id>-<i>; metadata has expected fields."""
    meta = {
        "id": "abc123",
        "name": "Test File",
        "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-05-01T10:00:00Z",
        "owners": [{"displayName": "Test Owner"}],
    }
    chunks = normalise_drive(meta, "Some meaningful content for testing chunking behaviour here.")

    assert len(chunks) >= 1
    assert chunks[0].doc_id == "gdrive-abc123-0"
    assert chunks[0].metadata["source_type"] == "gdrive"
    assert chunks[0].metadata["file_id"] == "abc123"
    assert chunks[0].metadata["owner"] == "Test Owner"


def test_normalise_drive_empty_text_returns_empty():
    """normalise_drive returns [] for empty or whitespace-only text."""
    meta = {"id": "z1", "name": "Empty", "mimeType": "text/plain"}
    assert normalise_drive(meta, "") == []
    assert normalise_drive(meta, "   \n  ") == []


# ---------------------------------------------------------------------------
# Binary extractor integration tests
# ---------------------------------------------------------------------------

def _make_docx_bytes() -> bytes:
    import io
    from docx import Document
    doc = Document()
    doc.add_paragraph("Quarterly budget review")
    table = doc.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "Revenue"
    table.rows[0].cells[1].text = "Expenses"
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def test_fetch_text_docx_via_get_media(tmp_path):
    """DOCX file: _fetch_text fetches via get_media and extracts text.
    Via backfill_drive it upserts a gdrive-<id>-0 chunk."""
    docx_bytes = _make_docx_bytes()
    DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

    # _fetch_text unit check
    meta = {"id": "d1", "mimeType": DOCX_MIME}
    svc = FakeDriveService(media={"d1": docx_bytes})
    text = _fetch_text(svc, meta)
    assert text is not None
    assert "Quarterly budget review" in text
    assert "Revenue" in text

    # Integration: backfill_drive upserts the chunk
    store = _store(tmp_path)
    fmeta = {
        "id": "d1",
        "name": "Budget.docx",
        "mimeType": DOCX_MIME,
        "modifiedTime": "2026-05-01T10:00:00Z",
        "owners": [{"displayName": "Sam"}],
    }
    svc2 = FakeDriveService(
        media={"d1": docx_bytes},
        file_list=[fmeta],
    )
    processed = backfill_drive(svc2, store, "2026-01-01T00:00:00Z")
    assert processed == 1
    chunk = store.get_chunk("gdrive-d1-0")
    assert chunk is not None
    assert "Quarterly budget review" in chunk["text"]


def test_fetch_text_sheets_export_csv():
    """Google Sheets file: _fetch_text uses export(mimeType='text/csv'); text returned."""
    SHEETS_MIME = "application/vnd.google-apps.spreadsheet"
    csv_bytes = b"Month,Revenue\nJanuary,50000\nFebruary,62000\n"

    meta = {"id": "s1", "mimeType": SHEETS_MIME}
    svc = FakeDriveService(exports={"s1": csv_bytes})
    text = _fetch_text(svc, meta)
    assert text is not None
    assert "Month" in text
    assert "January" in text
    assert "50000" in text
