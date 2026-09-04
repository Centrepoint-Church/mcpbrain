"""Tests for mcpbrain.sync.drive — fake service, no network."""

import threading

from mcpbrain.store import Store
from mcpbrain.sync.drive import (
    backfill_drive, discover_drive, handle_drive_item, normalise_drive, _fetch_text,
)


# ---------------------------------------------------------------------------
# Fake Drive service
# ---------------------------------------------------------------------------

class _Req:
    def __init__(self, result=None, raise_exc=None):
        self._r = result
        self._e = raise_exc

    def execute(self, num_retries=0):
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
    def __init__(self, exports=None, media=None, export_raises=None, file_list=None,
                by_id=None):
        self._exports = exports or {}
        self._media = media or {}
        self._raise = export_raises or {}
        # file_list: list of file metadata dicts returned by files().list()
        self._file_list = file_list or []
        # by_id: fileId -> metadata dict, served by files().get() -- the
        # re-fetch handle_drive_item makes (the sync_queue row only carries
        # ref_id/version, not the full metadata blob discovery saw).
        self._by_id = by_id or {}
        # Instrumentation for tests that need to assert de-dup: how many times
        # export()/get_media() was actually called per fileId.
        self.export_calls: dict[str, int] = {}
        self.get_media_calls: dict[str, int] = {}

    def get(self, fileId, supportsAllDrives=None, fields=None):
        if fileId in self._by_id:
            return _Req(self._by_id[fileId])
        for f in self._file_list:
            if f.get("id") == fileId:
                return _Req(f)
        # Tolerant fallback: a minimal but usable metadata dict, so a test
        # that never registered this fileId still gets something plausible
        # rather than a KeyError deep inside handle_drive_item.
        return _Req({"id": fileId, "name": fileId, "mimeType": "application/octet-stream"})

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
        pages = kw.get("pages")
        self._changes = _Changes(start, pages, initial_cursor=initial)
        # Build a fileId -> metadata map from every change's embedded "file"
        # dict across all pages, so handle_drive_item's own files().get()
        # re-fetch (the sync_queue row only carries ref_id/version) can be
        # served without every test having to pass file metadata twice.
        # Explicit files_by_id entries win over anything derived from pages.
        file_meta: dict = {}
        for page in (pages or []):
            for ch in page.get("changes", []):
                f = ch.get("file")
                if f and f.get("id"):
                    file_meta[f["id"]] = f
        for f in (kw.get("file_list") or []):
            if f.get("id"):
                file_meta.setdefault(f["id"], f)
        file_meta.update(kw.get("files_by_id") or {})
        self._files = _Files(
            kw.get("exports"),
            kw.get("media"),
            kw.get("export_raises"),
            kw.get("file_list"),
            by_id=file_meta,
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
    discover_drive returns 0, cursor is set to "100", no chunks upserted."""
    store = _store(tmp_path)
    svc = FakeDriveService(start_token="100")

    result = discover_drive(svc, store)

    assert result == 0
    assert store.get_cursor("drive") == "100"
    assert store.unembedded_chunks() == []


def test_delta_google_doc_exported_and_upserted(tmp_path):
    """Delta run: cursor "100", one Google Doc change is enqueued (discover)
    and, once worked, exported and upserted (handle)."""
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

    n = discover_drive(svc, store)
    assert n == 1
    assert store.get_cursor("drive") == "105"

    row = store.due_sync_items(limit=10, now="2099-01-01T00:00:00")[0]
    handle_drive_item(svc, store, row)

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

    discover_drive(svc, store)
    row = store.due_sync_items(limit=10, now="2099-01-01T00:00:00")[0]
    handle_drive_item(svc, store, row)

    chunk = store.get_chunk("gdrive-f2-0")
    assert chunk is not None
    assert "meeting notes" in chunk["text"]


def test_removed_change_enqueued_as_remove_and_deletes_nothing_new(tmp_path):
    """A change with removed=True is enqueued as a 'remove' event; working it
    deletes any existing chunks (none here) rather than upserting."""
    store = _store(tmp_path)
    store.set_cursor("drive", "100")

    removed_change = {"fileId": "f3", "removed": True}
    pages = [
        _page([removed_change], new_start_page_token="107")
    ]
    svc = FakeDriveService(pages=pages)

    discover_drive(svc, store)
    row = store.due_sync_items(limit=10, now="2099-01-01T00:00:00")[0]
    assert row["event"] == "remove"

    handle_drive_item(svc, store, row)
    assert store.get_chunk("gdrive-f3-0") is None


def test_unsupported_mime_skipped(tmp_path):
    """image/png file: _fetch_text returns None via fetch_content, so
    handle_drive_item writes nothing."""
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

    discover_drive(svc, store)
    row = store.due_sync_items(limit=10, now="2099-01-01T00:00:00")[0]
    handle_drive_item(svc, store, row)

    assert store.get_chunk("gdrive-f4-0") is None


def test_pagination_processes_all(tmp_path):
    """Two pages: page 0 has nextPageToken -> page 1; page 1 has newStartPageToken.
    Both files are enqueued and, once worked, upserted; cursor equals the last
    page's cursor."""
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

    n = discover_drive(svc, store)

    assert n == 2
    assert store.get_cursor("drive") == "200"
    for row in store.due_sync_items(limit=10, now="2099-01-01T00:00:00"):
        handle_drive_item(svc, store, row)
    assert store.get_chunk("gdrive-fa-0") is not None
    assert store.get_chunk("gdrive-fb-0") is not None


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

# ---------------------------------------------------------------------------
# Gate 3 / Task 4: fetch_content, folder_path, upsert_file_chunks
# ---------------------------------------------------------------------------

def test_an_unsupported_drive_type_is_recorded_rather_than_silently_dropped():
    """A2: .pptx, .doc, .pages, images and .zip all returned None from
    _fetch_text with no chunk, no stub and no log line."""
    from mcpbrain.sync import drive

    class _Store:
        def __init__(self):
            self.changes = []

        def record_change(self, kind, ref_id="", summary=""):
            self.changes.append((kind, ref_id, summary))

    store = _Store()
    fmeta = {"id": "f-1", "name": "Deck.key",
             "mimeType": "application/x-iwork-keynote-sffkey"}

    assert drive.fetch_content(object(), fmeta, store=store) is None
    assert store.changes and store.changes[0][0] == "ingest_skip"
    assert "unsupported_mime" in store.changes[0][2]


def test_a_supported_type_that_extracts_to_nothing_is_recorded_distinctly(monkeypatch):
    """B7: eight `except Exception: return ""` sites make a corrupt DOCX
    indistinguishable from an unsupported type. They must not share a bucket."""
    from mcpbrain.sync import drive

    class _Store:
        def __init__(self):
            self.changes = []

        def record_change(self, kind, ref_id="", summary=""):
            self.changes.append((kind, ref_id, summary))

    store = _Store()
    monkeypatch.setattr(drive, "_fetch_text", lambda service, meta: "")
    fmeta = {"id": "f-2", "name": "Broken.docx",
             "mimeType": "application/vnd.openxmlformats-officedocument."
                         "wordprocessingml.document"}

    drive.fetch_content(object(), fmeta, store=store)

    assert [s.split(":")[0] for _k, _r, s in store.changes] == ["extraction_empty"]


def test_folder_path_is_resolved_and_cached():
    """C5: embed.contextual_prefix reads metadata['folder_path'] and
    normalise_drive never wrote it, so every Drive contextual prefix has been
    missing its folder context — dead provenance in a default-ON feature."""
    from mcpbrain.sync import drive

    calls: list = []

    class _Service:
        def files(self):
            return self

        def get(self, fileId, fields, supportsAllDrives=None):
            calls.append(fileId)
            self._fid = fileId
            return self

        def execute(self, num_retries=0):
            return {"folder-1": {"id": "folder-1", "name": "Budgets",
                                 "parents": ["folder-0"]},
                    "folder-0": {"id": "folder-0", "name": "Finance",
                                 "parents": []}}[self._fid]

    cache: dict = {}
    fmeta = {"id": "f1", "name": "Budget.xlsx", "parents": ["folder-1"]}

    assert drive.folder_path(_Service(), fmeta, cache) == "Finance/Budgets"
    drive.folder_path(_Service(), fmeta, cache)
    assert calls == ["folder-1", "folder-0"], "the second call must hit the cache"


def test_a_shrinking_document_drops_its_orphaned_chunks(tmp_path):
    """B5: Drive writes gdrive-<fid>-<i> for i in 0..n-1 and only ever upserts.
    Nothing deleted indices n..m left by a previous, longer version, so deleted
    paragraphs stayed searchable indefinitely and were re-fed to expansion as
    current content."""
    from mcpbrain.store import Store
    from mcpbrain.sync.drive import normalise_drive, upsert_file_chunks

    store = Store(tmp_path / "b.sqlite3", dim=4)
    store.init()
    fmeta = {"id": "f1", "name": "Notes.txt", "mimeType": "text/plain"}

    long_text = "\n\n".join(f"Para {i} " + "word " * 400 for i in range(5))
    upsert_file_chunks(store, normalise_drive(fmeta, long_text), file_id="f1")
    assert len(store.doc_ids_for_file("f1")) >= 3

    upsert_file_chunks(store, normalise_drive(fmeta, "Para 0 " + "word " * 100),
                       file_id="f1")

    assert store.doc_ids_for_file("f1") == ["gdrive-f1-0"], "stale chunks survived"


def test_upserting_an_unchanged_document_deletes_nothing(tmp_path):
    from mcpbrain.store import Store
    from mcpbrain.sync.drive import normalise_drive, upsert_file_chunks

    store = Store(tmp_path / "b.sqlite3", dim=4)
    store.init()
    fmeta = {"id": "f1", "name": "Notes.txt", "mimeType": "text/plain"}
    text = "\n\n".join(f"Para {i} " + "word " * 400 for i in range(5))

    upsert_file_chunks(store, normalise_drive(fmeta, text), file_id="f1")
    first = sorted(store.doc_ids_for_file("f1"))
    upsert_file_chunks(store, normalise_drive(fmeta, text), file_id="f1")

    assert sorted(store.doc_ids_for_file("f1")) == first


# ---------------------------------------------------------------------------
# Post-approval review finding: fetch_content's per-file record_skip floods
# change_log. Aggregated over a sync round via a `report` dict instead.
# ---------------------------------------------------------------------------

def test_fetch_content_report_tallies_instead_of_writing_immediately():
    """Unit-level: passing `report=` must switch fetch_content from an
    immediate store.record_change per call to tallying {(kind, mime): count}
    in the caller-owned dict, with nothing written to the store at all."""
    from mcpbrain.sync import drive

    class _Store:
        def __init__(self):
            self.changes = []

        def record_change(self, kind, ref_id="", summary=""):
            self.changes.append((kind, ref_id, summary))

    store = _Store()
    report: dict = {}
    fmeta_png = {"id": "f-1", "name": "a.png", "mimeType": "image/png"}
    fmeta_jpg = {"id": "f-2", "name": "b.jpg", "mimeType": "image/jpeg"}

    for _ in range(3):
        drive.fetch_content(object(), fmeta_png, store=store, report=report)
    drive.fetch_content(object(), fmeta_jpg, store=store, report=report)

    assert store.changes == [], "report= must suppress the immediate write"
    assert report == {("unsupported_mime", "image/png"): 3,
                      ("unsupported_mime", "image/jpeg"): 1}


def test_flush_skip_report_emits_one_bounded_row_per_kind():
    """flush_skip_report must turn a multi-mime tally into one change_log row
    per `kind`, with the per-mime breakdown folded into the detail text —
    not one row per (kind, mime) and definitely not one row per file."""
    from mcpbrain.sync import drive

    class _Store:
        def __init__(self):
            self.changes = []

        def record_change(self, kind, ref_id="", summary=""):
            self.changes.append((kind, ref_id, summary))

    store = _Store()
    report = {("unsupported_mime", "image/png"): 270,
              ("unsupported_mime", "image/jpeg"): 70,
              ("extraction_empty", "application/pdf"): 2}

    drive.flush_skip_report(store, report)

    assert len(store.changes) == 2, "one row per kind, not per (kind, mime)"
    by_kind = {c[2].split(":")[0]: c[2] for c in store.changes}
    assert "drive_unsupported_mime" in by_kind
    assert "270" in by_kind["drive_unsupported_mime"]
    assert "70" in by_kind["drive_unsupported_mime"]
    assert "image/png" in by_kind["drive_unsupported_mime"]
    assert "drive_extraction_empty" in by_kind
    assert "2" in by_kind["drive_extraction_empty"]


def test_flush_skip_report_is_a_noop_on_an_empty_report():
    from mcpbrain.sync import drive

    class _Store:
        def __init__(self):
            self.changes = []

        def record_change(self, kind, ref_id="", summary=""):
            self.changes.append((kind, ref_id, summary))

    store = _Store()
    drive.flush_skip_report(store, {})
    assert store.changes == []


def test_backfill_drive_also_aggregates_skips_across_its_bounded_window(tmp_path):
    """Same finding, backfill_drive path: it advances no cursor and re-lists
    the same window every call, so per-file recording would re-flood on every
    single re-run. Confirms the aggregation applies there too."""
    store = _store(tmp_path)

    n = 12
    file_list = [
        {"id": f"img{i}", "name": f"photo{i}.png", "mimeType": "image/png",
         "modifiedTime": "2026-05-01T10:00:00Z", "owners": []}
        for i in range(n)
    ]
    svc = FakeDriveService(file_list=file_list)

    processed = backfill_drive(svc, store, "2020-01-01T00:00:00Z")

    assert processed == 0
    skip_rows = [c for c in store.recent_changes(limit=1000) if c["change_type"] == "ingest_skip"]
    assert len(skip_rows) == 1, f"expected one aggregated row, got {len(skip_rows)}"
    assert str(n) in skip_rows[0]["summary"]


# ---------------------------------------------------------------------------
# I9: a PARTIAL extraction must not trigger the B5 orphan-delete sweep.
# ---------------------------------------------------------------------------

def test_a_partial_extraction_does_not_delete_the_chunks_it_never_reached(tmp_path):
    """I9: extract_tables_from_xlsx / _xls / extract_text_from_pptx keep whatever
    they had when an exception hits mid-iteration (better than nothing). But
    upsert_file_chunks read the SHORT chunk list as evidence that the document had
    SHRUNK and deleted the higher-index "orphans" — so a transient failure on
    sheet 3 of 5 permanently deleted sheets 3-5's previously-good chunks, and
    nothing re-triggers extraction for a file whose metadata never changes again.
    A logged warning became irreversible content loss."""
    from mcpbrain.store import Store
    from mcpbrain.sync.drive import normalise_drive, upsert_file_chunks

    store = Store(tmp_path / "b.sqlite3", dim=4)
    store.init()
    fmeta = {"id": "f1", "name": "Budget.xlsx", "mimeType": "text/plain"}

    full = "\n\n".join(f"Sheet {i} " + "word " * 400 for i in range(5))
    upsert_file_chunks(store, normalise_drive(fmeta, full), file_id="f1")
    before = sorted(store.doc_ids_for_file("f1"))
    assert len(before) >= 3

    # The next round's extraction dies after sheet 1: a much shorter document.
    partial_chunks = normalise_drive(fmeta, "Sheet 0 " + "word " * 100)
    deleted = upsert_file_chunks(store, partial_chunks, file_id="f1", partial=True)

    assert deleted == 0
    assert sorted(store.doc_ids_for_file("f1")) == before, (
        "a partial extraction deleted the chunks it never reached"
    )


def test_a_complete_short_extraction_still_sweeps_orphans(tmp_path):
    """The discriminator: partial=False keeps B5's behaviour exactly."""
    from mcpbrain.store import Store
    from mcpbrain.sync.drive import normalise_drive, upsert_file_chunks

    store = Store(tmp_path / "b.sqlite3", dim=4)
    store.init()
    fmeta = {"id": "f1", "name": "Notes.txt", "mimeType": "text/plain"}

    full = "\n\n".join(f"Para {i} " + "word " * 400 for i in range(5))
    upsert_file_chunks(store, normalise_drive(fmeta, full), file_id="f1")

    upsert_file_chunks(store, normalise_drive(fmeta, "Para 0 " + "word " * 100),
                       file_id="f1", partial=False)

    assert store.doc_ids_for_file("f1") == ["gdrive-f1-0"]


def test_fetch_content_marks_a_partial_table_extraction(monkeypatch):
    """The signal has to survive the trip from the extractor to Content.partial,
    or the call sites can't act on it."""
    from mcpbrain.sync import drive
    from mcpbrain.sync.extractors import PartialTables
    from mcpbrain.sync.tabular import Table

    xlsx = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    class _Media:
        def execute(self, num_retries=0):
            return b"fake"

    class _Files:
        def get_media(self, **kw):
            return _Media()

    class _Svc:
        def files(self):
            return _Files()

    monkeypatch.setattr(
        drive, "extract_tables_from_xlsx",
        lambda data, char_budget: PartialTables(
            [Table(sheet="S1", header=["a"], rows=[["1"]], rows_total=1)]))

    content = drive.fetch_content(_Svc(), {"id": "f1", "name": "b.xlsx",
                                           "mimeType": xlsx})

    assert content is not None and content.partial is True
    assert len(content.tables) == 1


def test_fetch_content_does_not_mark_a_complete_extraction(monkeypatch):
    from mcpbrain.sync import drive
    from mcpbrain.sync.tabular import Table

    xlsx = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    class _Media:
        def execute(self, num_retries=0):
            return b"fake"

    class _Files:
        def get_media(self, **kw):
            return _Media()

    class _Svc:
        def files(self):
            return _Files()

    monkeypatch.setattr(
        drive, "extract_tables_from_xlsx",
        lambda data, char_budget: [Table(sheet="S1", header=["a"], rows=[["1"]],
                                         rows_total=1)])

    content = drive.fetch_content(_Svc(), {"id": "f1", "name": "b.xlsx",
                                           "mimeType": xlsx})

    assert content is not None and content.partial is False


def _partial_publish_harness(tmp_path, monkeypatch, *, partial: bool):
    """_cache_first_extract_one over a forced cache MISS, so the local-extraction
    path runs and its (file_id, content_hash) publish tuple is observable."""
    from mcpbrain import ingest_cache
    from mcpbrain.store import Store
    from mcpbrain.sync import drive

    store = Store(tmp_path / "b.sqlite3", dim=4)
    store.init()
    monkeypatch.setattr(ingest_cache, "try_import", lambda *a, **kw: False)
    monkeypatch.setattr(
        drive, "fetch_content",
        lambda *a, **kw: drive.Content(text="Para 0 " + "word " * 200,
                                       partial=partial))
    monkeypatch.setattr(drive, "folder_path", lambda *a, **kw: "")

    fmeta = {"id": "f1", "name": "Budget.xlsx", "mimeType": "text/plain",
             "md5Checksum": "abc123"}
    processed, miss = drive._cache_first_extract_one(
        object(), store, object(), "drv1", fmeta, {})
    return store, processed, miss


def test_a_partial_extraction_is_not_published_to_the_ingest_cache(tmp_path, monkeypatch):
    """The miss tuple _cache_first_extract_one returns is what the caller
    publishes as the fleet-wide ingest-cache artifact for that content hash. I9
    stopped a partial extraction from deleting chunks locally, but still published
    it — so a truncated document propagated to every other install under a hash
    that says it is complete, and would not self-heal until the file changed."""
    store, processed, miss = _partial_publish_harness(
        tmp_path, monkeypatch, partial=True)

    assert miss is None, "a truncated extraction was published fleet-wide"
    assert processed is True, (
        "the file WAS indexed locally — only the cache publish is suppressed")
    assert store.doc_ids_for_file("f1"), "local chunks must still be written"


def test_a_complete_extraction_is_still_published_to_the_ingest_cache(tmp_path, monkeypatch):
    """The discriminator: the normal path must keep publishing, or every install
    re-extracts every shared-drive file forever."""
    _store, processed, miss = _partial_publish_harness(
        tmp_path, monkeypatch, partial=False)

    assert processed is True
    assert miss is not None and miss[0] == "f1"


def test_the_aggregated_skip_row_names_the_source_it_came_from():
    """Please-fix minor: flush_skip_report passed ref_id="", so the aggregated
    rows could not be traced to the drive that produced them. Mirrors
    sync/gmail.py's reviewed pattern of passing `source` as ref_id."""
    from mcpbrain.sync import drive

    class _Store:
        def __init__(self):
            self.changes = []

        def record_change(self, kind, ref_id="", summary=""):
            self.changes.append((kind, ref_id, summary))

    store = _Store()
    drive.flush_skip_report(store, {("unsupported_mime", "image/png"): 3},
                            source="drive:DRIVE123")

    assert store.changes[0][1] == "drive:DRIVE123"


def test_reingest_files_replaces_a_files_chunks_from_a_fresh_fetch(tmp_path, monkeypatch):
    """There is no targeted re-ingest path: backfill_drive filters on
    modifiedTime, and the delta sync only sees CHANGED files — so a file whose
    content is fine but whose CHUNKING is out of date can never be revisited.
    455 clipped spreadsheets and 9,351 legacy files need exactly that."""
    from mcpbrain.store import Store
    from mcpbrain.sync import drive

    store = Store(tmp_path / "b.sqlite3", dim=4)
    store.init()
    # Pre-existing chunks from the old chunker: more of them, and stale text.
    for i in range(4):
        store.upsert_chunk(f"gdrive-f1-{i}", f"|  |  | old {i} |", f"h{i}",
                           {"source_type": "gdrive", "file_id": "f1",
                            "chunk_index": i})

    class _Service:
        def files(self):
            return self

        def get(self, fileId, fields=None, supportsAllDrives=None):
            self._fid = fileId
            return self

        def get_media(self, fileId, supportsAllDrives=None):
            return self

        def execute(self, num_retries=0):
            return {"id": "f1", "name": "Notes.txt", "mimeType": "text/plain",
                    "modifiedTime": "2026-07-01T00:00:00Z", "parents": []}

    monkeypatch.setattr(drive, "_fetch_text",
                        lambda service, meta: "Recovered prose content.")

    summary = drive.reingest_files(_Service(), store, ["f1"])

    assert summary["files"] == 1
    remaining = sorted(store.doc_ids_for_file("f1"))
    assert remaining == ["gdrive-f1-0"], f"stale chunks survived: {remaining}"
    assert "Recovered" in store.get_chunk("gdrive-f1-0")["text"]


def test_a_file_that_genuinely_extracts_to_nothing_stops_being_selected(tmp_path, monkeypatch):
    """Live non-convergence bug, measured on the real store: 465
    `extraction_empty` change_log rows across 10 files in a 41-minute window —
    ~46 re-fetches of each — and all 10 were still in stale_chunker_file_ids
    afterwards.

    Cause: `_reingest_one` returned "failed" for BOTH a deterministic "there is
    nothing in this file" and a transient network error, so a file that yields
    zero chunks never got chunker_version stamped and the selector returned it
    forever. Verified against the real files: they are genuinely EMPTY
    spreadsheets — the old extractor emitted 41 chars of sheet names
    ('Sheet: Sheet1\\nSheet: Sheet2...'), and the new one correctly declines
    them. The extraction is right; only the convergence was wrong.

    Same shape as drain.py's _EMPTY_ATTEMPT_CAP, which exists to "bound the
    re-extract loop for genuinely content-empty docs".
    """
    from mcpbrain.chunking import CHUNKER_VERSION
    from mcpbrain.store import Store
    from mcpbrain.sync import drive

    store = Store(tmp_path / "b.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("gdrive-empty1-0", "Sheet: Sheet1\nSheet: Sheet2", "h1",
                       {"source_type": "gdrive", "file_id": "empty1",
                        "chunk_index": 0})
    assert [d["id"] for d in store.stale_chunker_ids(table_version=CHUNKER_VERSION, other_version=CHUNKER_VERSION, limit=10)] == ["empty1"]

    class _Service:
        def files(self):
            return self

        def get(self, fileId=None, **kw):
            return self

        def get_media(self, fileId=None, **kw):
            return self

        def execute(self, num_retries=0):
            return {"id": "empty1", "name": "Guest Coffee Vouchers.xlsx",
                    "mimeType": "text/plain", "parents": []}

    # Deterministically no content — the real case is an .xlsx with no non-empty
    # rows, which yields zero Tables.
    monkeypatch.setattr(drive, "_fetch_text", lambda service, meta: "")

    summary = drive.reingest_files(_Service(), store, ["empty1"])

    assert summary["empty"] == 1, f"expected an 'empty' outcome, got {summary}"
    assert store.stale_chunker_ids(table_version=CHUNKER_VERSION, other_version=CHUNKER_VERSION, limit=10) == [], (
        "the file is still selected, so reingest-stale will re-fetch it forever"
    )
    meta = store.get_chunk("gdrive-empty1-0")["metadata"]
    assert meta["chunker_version"] == CHUNKER_VERSION
    assert meta["reextract_empty"] is True, (
        "the fact that re-extraction found nothing must be recorded on the data, "
        "not just inferred from the absence of new chunks"
    )
    assert "Sheet: Sheet1" in store.get_chunk("gdrive-empty1-0")["text"], (
        "the old chunks are the best available content and must be kept"
    )


def test_a_transient_failure_does_NOT_stop_the_file_being_selected(tmp_path, monkeypatch):
    """The discriminator, and the reason this is not just 'stamp everything':
    a 429/503/timeout must stay retryable. Conflating the two is what caused
    the loop in the first place — in the other direction."""
    from mcpbrain.chunking import CHUNKER_VERSION
    from mcpbrain.store import Store
    from mcpbrain.sync import drive

    store = Store(tmp_path / "b.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("gdrive-flaky-0", "real content here", "h1",
                       {"source_type": "gdrive", "file_id": "flaky",
                        "chunk_index": 0})

    class _Service:
        def files(self):
            return self

        def get(self, fileId=None, **kw):
            return self

        def execute(self, num_retries=0):
            return {"id": "flaky", "name": "F.txt", "mimeType": "text/plain",
                    "parents": []}

    def _boom(service, meta):
        raise RuntimeError("503 backend error")

    monkeypatch.setattr(drive, "_fetch_text", _boom)

    summary = drive.reingest_files(_Service(), store, ["flaky"])

    assert summary["failed"] == 1
    assert summary["empty"] == 0
    assert [d["id"] for d in store.stale_chunker_ids(table_version=CHUNKER_VERSION, other_version=CHUNKER_VERSION, limit=10)] == ["flaky"], (
        "a transient failure must remain retryable"
    )


def test_reingest_files_skips_a_file_that_no_longer_exists(tmp_path):
    """A file deleted from Drive since it was chunked must not abort the run or
    delete its chunks — that is the removal path's job, not the repair's."""
    from googleapiclient.errors import HttpError

    from mcpbrain.store import Store
    from mcpbrain.sync import drive

    store = Store(tmp_path / "b.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("gdrive-gone-0", "text", "h", {"source_type": "gdrive",
                                                      "file_id": "gone"})

    class _Resp:
        status = 404
        reason = "Not Found"

    class _Service:
        def files(self):
            return self

        def get(self, **kw):
            return self

        def execute(self, num_retries=0):
            raise HttpError(_Resp(), b"not found")

    summary = drive.reingest_files(_Service(), store, ["gone"])

    assert summary["missing"] == 1
    assert summary["files"] == 0
    assert store.get_chunk("gdrive-gone-0") is not None


def test_reingest_files_isolates_a_non_404_httperror_from_metadata_fetch(tmp_path, monkeypatch):
    """Only a 404 (the file is genuinely gone) is special-cased as `missing`.
    A 403/429/5xx from files().get() itself -- all realistic across a
    9,351-file batch -- is a per-file failure like any other and must not
    escape the loop and abort the whole run; the next file must still be
    reached and processed."""
    from googleapiclient.errors import HttpError

    from mcpbrain.store import Store
    from mcpbrain.sync import drive

    store = Store(tmp_path / "b.sqlite3", dim=4)
    store.init()

    class _Resp:
        status = 429
        reason = "Too Many Requests"

    class _Service:
        def files(self):
            return self

        def get(self, fileId=None, **kw):
            self._fid = fileId
            return self

        def execute(self, num_retries=0):
            if self._fid == "throttled":
                raise HttpError(_Resp(), b"rate limited")
            return {"id": self._fid, "name": f"{self._fid}.txt",
                    "mimeType": "text/plain", "parents": []}

    monkeypatch.setattr(drive, "_fetch_text", lambda service, meta: "fine content")

    summary = drive.reingest_files(_Service(), store, ["ok1", "throttled", "ok2"])

    assert summary["files"] == 2, "a non-404 HttpError must not abort the run"
    assert summary["failed"] == 1
    assert summary["missing"] == 0


def test_reingest_files_is_bounded_and_reports_per_file_failures(tmp_path, monkeypatch):
    """One unreadable file in 9,351 must not end the run."""
    from mcpbrain.store import Store
    from mcpbrain.sync import drive

    store = Store(tmp_path / "b.sqlite3", dim=4)
    store.init()

    class _Service:
        def files(self):
            return self

        def get(self, fileId=None, **kw):
            self._fid = fileId
            return self

        def execute(self, num_retries=0):
            return {"id": self._fid, "name": f"{self._fid}.txt",
                    "mimeType": "text/plain", "parents": []}

    def _boom(service, meta):
        if meta["id"] == "bad":
            raise RuntimeError("extraction exploded")
        return "fine content"

    monkeypatch.setattr(drive, "_fetch_text", _boom)

    summary = drive.reingest_files(_Service(), store, ["ok1", "bad", "ok2"])

    assert summary["files"] == 2
    assert summary["failed"] == 1


def test_reingest_files_with_workers_uses_a_fresh_service_per_worker_thread(tmp_path, monkeypatch):
    """googleapiclient's Resource wraps a stateful httplib2.Http that is not
    safe to share across threads, so max_workers>1 must never hand the same
    service instance to two concurrent fetches. service_factory is called once
    per WORKER (not once per file) via a thread-local, and the `service`
    positional argument goes unused in this mode."""
    from mcpbrain.store import Store
    from mcpbrain.sync import drive

    store = Store(tmp_path / "b.sqlite3", dim=4)
    store.init()
    built = []
    lock = threading.Lock()

    class _Service:
        def __init__(self):
            with lock:
                built.append(self)

        def files(self):
            return self

        def get(self, fileId=None, **kw):
            self._fid = fileId
            return self

        def execute(self, num_retries=0):
            return {"id": self._fid, "name": f"{self._fid}.txt",
                    "mimeType": "text/plain", "parents": []}

    monkeypatch.setattr(drive, "_fetch_text",
                        lambda service, meta: f"content for {meta['id']}")

    file_ids = [f"f{i}" for i in range(8)]
    summary = drive.reingest_files(
        None, store, file_ids, max_workers=3, service_factory=_Service)

    assert summary["files"] == 8
    for fid in file_ids:
        assert f"content for {fid}" in store.get_chunk(f"gdrive-{fid}-0")["text"]
    # At most one service per worker (never one per file), and definitely more
    # than one overall -- proof no single instance was shared across threads.
    assert 1 < len(built) <= 3, (
        f"expected 2-3 distinct service instances (one per worker, reused "
        f"across that worker's files), got {len(built)}"
    )


def test_reingest_files_with_workers_isolates_a_per_file_failure(tmp_path, monkeypatch):
    """Per-file isolation must hold in the concurrent path exactly as it does
    in the sequential one: one file's failure must not lose or block any
    other file's result."""
    from mcpbrain.store import Store
    from mcpbrain.sync import drive

    store = Store(tmp_path / "b.sqlite3", dim=4)
    store.init()

    class _Service:
        def files(self):
            return self

        def get(self, fileId=None, **kw):
            self._fid = fileId
            return self

        def execute(self, num_retries=0):
            return {"id": self._fid, "name": f"{self._fid}.txt",
                    "mimeType": "text/plain", "parents": []}

    def _boom(service, meta):
        if meta["id"] == "bad":
            raise RuntimeError("extraction exploded")
        return "fine content"

    monkeypatch.setattr(drive, "_fetch_text", _boom)

    summary = drive.reingest_files(
        None, store, ["ok1", "bad", "ok2", "ok3"],
        max_workers=4, service_factory=_Service)

    assert summary["files"] == 3
    assert summary["failed"] == 1


def test_reingest_refreshes_metadata_when_the_text_is_byte_identical(tmp_path, monkeypatch):
    """The convergence guarantee. store.upsert_chunk short-circuits on an
    unchanged content_hash and writes NOTHING — metadata included — so a legacy
    Drive file whose prose re-chunks identically (the common case: spec 2 changed
    empty/oversize emission and tabular routing, not prose boundaries) would
    never acquire `chunker_version`. stale_chunker_ids selects on exactly
    that field and orders by MIN(rowid), so `reingest-stale --apply --limit N`
    would re-fetch the same oldest N files forever, burning Drive quota with zero
    progress while reporting success."""
    from mcpbrain.chunking import CHUNKER_VERSION, content_hash
    from mcpbrain.store import Store
    from mcpbrain.sync import drive

    store = Store(tmp_path / "b.sqlite3", dim=4)
    store.init()
    body = "Recovered prose content that re-chunks byte-identically."
    # Pre-existing chunk from the old chunker: same text, same hash, no version.
    store.upsert_chunk("gdrive-f1-0", body, content_hash(body),
                       {"source_type": "gdrive", "file_id": "f1", "chunk_index": 0})
    assert [d["id"] for d in store.stale_chunker_ids(table_version=CHUNKER_VERSION, other_version=CHUNKER_VERSION, limit=10)] == ["f1"]

    class _Service:
        def files(self):
            return self

        def get(self, fileId=None, **kw):
            self._fid = fileId
            return self

        def execute(self, num_retries=0):
            return {"id": "f1", "name": "Notes.txt", "mimeType": "text/plain",
                    "modifiedTime": "2026-07-01T00:00:00Z", "parents": []}

    monkeypatch.setattr(drive, "_fetch_text", lambda service, meta: body)

    summary = drive.reingest_files(_Service(), store, ["f1"])

    assert summary["files"] == 1
    assert store.stale_chunker_ids(table_version=CHUNKER_VERSION, other_version=CHUNKER_VERSION, limit=10) == [], (
        "an unchanged-text file never left the stale set — reingest-stale would "
        "re-fetch it forever"
    )
    meta = store.get_chunk("gdrive-f1-0")["metadata"]
    assert meta["chunker_version"] == CHUNKER_VERSION


def test_reingest_keeps_the_drive_id_stamp_for_a_shared_drive_file(tmp_path, monkeypatch):
    """upsert_chunk REPLACES metadata wholesale, so a re-ingest that does not
    re-request driveId strips the chunk's drive_id stamp — and
    ingest_cache.purge_drive finds content to delete on access revocation via
    store.doc_ids_for_drive, i.e. by that stamp. Re-ingested content would
    silently survive a revocation forever."""
    from mcpbrain.store import Store
    from mcpbrain.sync import drive

    store = Store(tmp_path / "b.sqlite3", dim=4)
    store.init()

    class _Service:
        def files(self):
            return self

        def get(self, fileId=None, fields=None, **kw):
            self._fid, self._fields = fileId, fields
            return self

        def execute(self, num_retries=0):
            assert "driveId" in (self._fields or ""), (
                "driveId must be requested or the fetched metadata can never "
                f"carry it: {self._fields}"
            )
            return {"id": "f1", "name": "Policy.txt", "mimeType": "text/plain",
                    "modifiedTime": "2026-07-01T00:00:00Z", "parents": [],
                    "driveId": "SHARED1"}

    monkeypatch.setattr(drive, "_fetch_text",
                        lambda service, meta: "Shared drive policy text.")

    assert drive.reingest_files(_Service(), store, ["f1"])["files"] == 1

    assert store.doc_ids_for_drive("SHARED1") == ["gdrive-f1-0"]
    assert store.get_chunk("gdrive-f1-0")["metadata"]["drive_id"] == "SHARED1"


def test_a_file_gone_from_drive_also_stops_being_selected(tmp_path, monkeypatch):
    """The same non-convergence loop as the `empty` case, via the OTHER branch —
    found by running the real repair: `{'files': 8, 'missing': 14, 'empty': 10}`
    left exactly 14 files still selected, so every future run re-fetches and
    re-404s the same 14 forever.

    Leaving the CHUNKS alone on a 404 is still right: removal is the delta sync's
    job, and a 404 can be a permission change or a moved file rather than a
    deletion. But "don't delete the content" is not the same as "keep asking
    Drive about it forever". Stamping converges the repair selector without
    touching the data — if the file reappears, the Changes API re-ingests it on
    its own terms; if it is genuinely deleted, the delta sync's removal path
    deletes the chunks.
    """
    from googleapiclient.errors import HttpError

    from mcpbrain.chunking import CHUNKER_VERSION
    from mcpbrain.store import Store
    from mcpbrain.sync import drive

    store = Store(tmp_path / "b.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("gdrive-gone-0", "content worth keeping", "h1",
                       {"source_type": "gdrive", "file_id": "gone",
                        "chunk_index": 0})

    class _Resp:
        status = 404
        reason = "Not Found"

    class _Service:
        def files(self):
            return self

        def get(self, **kw):
            return self

        def execute(self, num_retries=0):
            raise HttpError(_Resp(), b"not found")

    summary = drive.reingest_files(_Service(), store, ["gone"])

    assert summary["missing"] == 1
    assert store.stale_chunker_ids(table_version=CHUNKER_VERSION, other_version=CHUNKER_VERSION, limit=10) == [], (
        "the 404'd file is still selected, so every run re-fetches it forever"
    )
    chunk = store.get_chunk("gdrive-gone-0")
    assert chunk is not None, "a 404 must NOT delete content — that is the delta sync's job"
    assert chunk["text"] == "content worth keeping"
    assert chunk["metadata"]["reextract_missing"] is True, (
        "why the stamp is there must be recorded, not inferred"
    )
