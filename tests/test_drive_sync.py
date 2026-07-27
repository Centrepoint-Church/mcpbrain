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
        # Instrumentation for tests that need to assert de-dup: how many times
        # export()/get_media() was actually called per fileId.
        self.export_calls: dict[str, int] = {}
        self.get_media_calls: dict[str, int] = {}

    def export(self, fileId, mimeType):
        # Mirror the real Drive v3 API: files.export does NOT accept
        # supportsAllDrives. Passing it would raise TypeError here (as in prod),
        # so this signature guards against the kwarg being re-added.
        self.export_calls[fileId] = self.export_calls.get(fileId, 0) + 1
        if fileId in self._raise:
            return _Req(raise_exc=self._raise[fileId])
        return _Req(self._exports.get(fileId, b""))

    def get_media(self, fileId, supportsAllDrives=None):
        assert supportsAllDrives is True, (
            "get_media() must pass supportsAllDrives=True — required by the real "
            "Drive v3 API for files inside a Shared Drive"
        )
        self.get_media_calls[fileId] = self.get_media_calls.get(fileId, 0) + 1
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


# ---------------------------------------------------------------------------
# Task 2 duty-cycle fix: budget-interrupted mid-upsert must checkpoint safely
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


def test_budget_interrupted_mid_upsert_resumes_without_skip_or_duplicate(tmp_path):
    """Mirrors test_gmail_sync.py's checkpoint-resume test for the Drive delta
    path. `newStartPageToken` is only emitted once ALL pages are consumed, so
    (like Gmail's historyId) advancing the cursor before every pending file
    is durably upserted would silently skip whatever wasn't reached yet.

    Verifies: (1) an interrupted run upserts only the files it reached and
    leaves the cursor at its OLD value; (2) a follow-up call with no budget
    completes the resume — f1 (already durably done in the first call) is
    genuinely SKIPPED in the upsert loop via the persisted `drive:resume_ids`
    set, only f2 is newly processed — and the cursor advances to the true
    newStartPageToken. (Unlike Gmail, the PAGINATION loop's own `_fetch_text`
    calls are not gated on the resume set here — see sync_drive's docstring
    for why that's an accepted, documented trade-off — so f1's export IS
    still re-fetched during pagination on resume; only the upsert/count is
    genuinely incremental.)
    """
    store = _store(tmp_path)
    store.set_cursor("drive", "100")

    pages = [
        _page(
            [_gdoc_change("f1", "Doc One"), _gdoc_change("f2", "Doc Two")],
            new_start_page_token="105",
        )
    ]
    svc = FakeDriveService(
        pages=pages,
        exports={"f1": b"first document content, long enough to matter",
                "f2": b"second document content, long enough to matter"},
    )

    # Call 1: pagination check before the (only) page (not expired -> both
    # f1/f2 fetched+buffered into `pending` during that one page). Call 2:
    # upsert-loop check before f1 (not expired -> f1 upserted). Call 3:
    # upsert-loop check before f2 (expired -> stop; f2 never upserted).
    budget = _FakeBudget(expire_after_calls=2)
    result = sync_drive(svc, store, budget=budget)

    assert result == 1, "only the file(s) upserted before budget expiry should count"
    assert store.get_cursor("drive") == "100", (
        "cursor must NOT advance on a partial run — an early advance would "
        "silently and permanently skip f2 (newStartPageToken is only valid "
        "once every pending file from this delta window is durable)"
    )
    assert store.get_chunk("gdrive-f1-0") is not None
    assert store.get_chunk("gdrive-f2-0") is None

    # Resume: cursor unchanged, so pagination re-lists the SAME page (and
    # re-fetches f1's/f2's text — see docstring), but the upsert loop must
    # SKIP f1 (already resumed) and process only f2.
    result2 = sync_drive(svc, store, budget=None)

    assert result2 == 1, "only the genuinely new file (f2) should be counted/upserted on resume"
    assert store.get_cursor("drive") == "105"
    assert store.get_chunk("gdrive-f1-0") is not None
    assert store.get_chunk("gdrive-f2-0") is not None
    assert store.get_cursor("drive:resume_ids") == "[]", "resume set must be cleared once the round closes"

    # No duplicate-visible-effect regardless: f1 stayed one row (upsert keyed
    # on doc_id), never inserted twice.
    with store._connect() as db:
        count = db.execute(
            "SELECT COUNT(*) FROM chunks WHERE doc_id='gdrive-f1-0'"
        ).fetchone()[0]
    assert count == 1


def test_budget_interrupted_across_many_cycles_eventually_completes(tmp_path):
    """Critical-B reproduction, My-Drive variant (adversarial review, Task 2
    round 3): a delta bigger than one budget's worth of files must not
    livelock. Drives a 7-file delta through repeated budget-truncated calls
    (2 files' worth of upsert capacity each) and asserts the cursor
    eventually reaches the true final newStartPageToken and every file is
    durably upserted exactly once."""
    store = _store(tmp_path)
    store.set_cursor("drive", "100")

    n = 7
    changes = [_gdoc_change(f"f{i}", f"Doc {i}") for i in range(1, n + 1)]
    exports = {f"f{i}": f"document body number {i}, long enough to matter".encode()
              for i in range(1, n + 1)}
    pages = [_page(changes, new_start_page_token="200")]
    svc = FakeDriveService(pages=pages, exports=exports)

    per_call_capacity = 2
    max_cycles = 20
    for _cycle in range(max_cycles):
        if store.get_cursor("drive") != "100":
            break
        budget = _FakeBudget(expire_after_calls=1 + per_call_capacity)
        sync_drive(svc, store, budget=budget)
    else:
        raise AssertionError(
            f"cursor never advanced past the original delta window after "
            f"{max_cycles} cycles — this is the livelock the fix targets"
        )

    assert store.get_cursor("drive") == "200"
    assert store.get_cursor("drive:resume_ids") == "[]"
    for i in range(1, n + 1):
        doc_id = f"gdrive-f{i}-0"
        assert store.get_chunk(doc_id) is not None, f"f{i} was never ingested"
        with store._connect() as db:
            count = db.execute(
                "SELECT COUNT(*) FROM chunks WHERE doc_id=?", (doc_id,)
            ).fetchone()[0]
        assert count == 1, f"f{i} produced more than one chunk row"


def test_file_edited_mid_round_is_picked_up_not_skipped(tmp_path):
    """New Critical found in adversarial review round 4: once a file's id
    landed in the resume set (round 3's fix), it was skipped for the REST OF
    THAT ROUND no matter what -- including if the file changed in between.
    The round then closed and the real cursor advanced PAST the file's
    change record, with nothing left to re-surface it until the file
    happened to change again after the cursor had already moved on.

    Reproduced directly before this fix (against the round-3 code): an
    edited file's stored text stayed at its pre-edit content forever after
    the round closed. Fixed by keying the resume set on id+version
    (_file_resume_key: md5Checksum, or version+modifiedTime), not bare id,
    so an edit produces a DIFFERENT key and is recognized as new work rather
    than matched against the stale resume entry.
    """
    store = _store(tmp_path)
    store.set_cursor("drive", "100")

    f1 = _gdoc_change("f1", "Doc One")
    f1["file"]["md5Checksum"] = "hash-v1"
    f2 = _gdoc_change("f2", "Doc Two")
    f2["file"]["md5Checksum"] = "hash-v1-f2"
    pages = [_page([f1, f2], new_start_page_token="200")]
    svc = FakeDriveService(pages=pages, exports={
        "f1": b"ORIGINAL f1 body content, long enough to matter",
        "f2": b"f2 body content, long enough to matter",
    })

    upsert_calls = []
    orig_upsert = store.upsert_chunk

    def spy_upsert(*a, **kw):
        upsert_calls.append(a[0])  # doc_id
        return orig_upsert(*a, **kw)

    store.upsert_chunk = spy_upsert

    # Call 1: budget cuts off right after f1 is processed with its ORIGINAL
    # content -- f1's (stale-version) key lands in the resume set.
    budget = _FakeBudget(expire_after_calls=2)
    sync_drive(svc, store, budget=budget)
    assert store.get_cursor("drive") == "100", "round must still be open"
    assert store.get_chunk("gdrive-f1-0")["text"].startswith("ORIGINAL")

    # f1 is edited in Drive (content AND version change) WHILE the round is
    # still open.
    svc._files._exports["f1"] = b"REVISED f1 body content, long enough to matter"
    f1_edited = _gdoc_change("f1", "Doc One")
    f1_edited["file"]["md5Checksum"] = "hash-v2"
    svc._changes._pages[0] = {"changes": [f1_edited, f2], "newStartPageToken": "200"}

    # Call 2: unbounded, completes the round.
    sync_drive(svc, store, budget=None)

    assert store.get_cursor("drive") == "200", "round must close"
    assert store.get_chunk("gdrive-f1-0")["text"].startswith("REVISED"), (
        "f1's edit must land -- the resume set must not have permanently "
        "skipped it just because its OLD id+version key was already "
        "resumed from call 1"
    )
    assert store.get_cursor("drive:resume_ids") == "[]"

    # f1 was upserted exactly twice total across the two calls (once with
    # its original content, once with the edit) -- proving the edit is
    # picked up exactly once per version, not repeatedly re-applied within
    # the same round nor silently dropped.
    assert upsert_calls.count("gdrive-f1-0") == 2
