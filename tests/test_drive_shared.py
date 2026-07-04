from mcpbrain.sync.drive import (
    list_shared_drives, normalise_drive, _file_content_hash,
)
from mcpbrain.org_contracts import DRIVE_ID_META_KEY

from mcpbrain import ingest_cache
from mcpbrain.org_contracts import FleetPin
from mcpbrain.store import Store
from mcpbrain.sync.drive import sync_shared_drive
from tests.helpers.org_fleet import LocalDirFleetStorage
from tests.test_drive_sync import FakeDriveService, _gdoc_change

PIN = FleetPin(embed_model="bge-small", dim=4, chunker_version="v1",
               enrich_logic_floor=1, fleet_secret="s3cret")


def _store(tmp_path, name="a.sqlite3"):
    s = Store(tmp_path / name, dim=4)
    s.init()
    return s


class _Req:
    def __init__(self, result):
        self._r = result

    def execute(self):
        return self._r


class _Drives:
    def __init__(self, pages):
        self._pages = pages
        self._i = 0

    def list(self, **_kw):
        page = self._pages[self._i]
        self._i = min(self._i + 1, len(self._pages) - 1)
        return _Req(page)


class _DriveOnlyService:
    def __init__(self, pages):
        self._drives = _Drives(pages)

    def drives(self):
        return self._drives


def test_list_shared_drives_paginates():
    svc = _DriveOnlyService([
        {"drives": [{"id": "D1", "name": "Ops"}], "nextPageToken": "p2"},
        {"drives": [{"id": "D2", "name": "Finance"}]},
    ])
    ds = list_shared_drives(svc)
    assert [d["id"] for d in ds] == ["D1", "D2"]


def test_normalise_drive_stamps_drive_id():
    fm = {"id": "FID", "name": "Doc", "mimeType": "application/vnd.google-apps.document",
          "modifiedTime": "2026-05-01T10:00:00Z", "owners": [{"displayName": "X"}]}
    chunks = normalise_drive(fm, "hello world", drive_id="D1")
    assert chunks and chunks[0].metadata[DRIVE_ID_META_KEY] == "D1"
    # My-Drive path (no drive_id) leaves the key absent
    chunks2 = normalise_drive(fm, "hello world")
    assert DRIVE_ID_META_KEY not in chunks2[0].metadata


def test_file_content_hash_prefers_md5_then_stable_for_native():
    assert _file_content_hash({"id": "F", "md5Checksum": "abc"}) == "abc"
    a = _file_content_hash({"id": "F", "version": "7", "modifiedTime": "T"})
    b = _file_content_hash({"id": "F", "version": "7", "modifiedTime": "T"})
    c = _file_content_hash({"id": "F", "version": "8", "modifiedTime": "T"})
    assert a == b and a != c and len(a) == 64        # deterministic sha256


def test_sync_shared_drive_bootstrap_sets_cursor(tmp_path):
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    svc = FakeDriveService(start_token="500")
    out = sync_shared_drive(svc, s, "D1", fleet_storage=fs, pin=PIN)
    assert out["processed"] == 0
    assert s.get_cursor("drive:D1") == "500"


def test_sync_shared_drive_miss_extracts_and_records_for_publish(tmp_path):
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    s.set_cursor("drive:D1", "100")
    svc = FakeDriveService(
        initial_cursor="100",
        pages=[{"changes": [_gdoc_change("FID")], "newStartPageToken": "101"}],
        exports={"FID": b"the quick brown fox jumps"})
    out = sync_shared_drive(svc, s, "D1", fleet_storage=fs, pin=PIN)
    assert out["processed"] == 1
    assert ("FID", out["miss"][0][1]) == out["miss"][0]      # (file_id, content_hash)
    ch = s.get_chunk("gdrive-FID-0")
    assert ch["metadata"]["drive_id"] == "D1"
    assert s.get_cursor("drive:D1") == "101"


def test_sync_shared_drive_cache_hit_skips_extraction(tmp_path):
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    s.set_cursor("drive:D1", "100")
    # pre-publish an artifact for FID's current version so try_import hits
    src = _store(tmp_path, "src.sqlite3")
    src.import_cached_chunk("gdrive-FID-0", "cached body", "c0",
                            {"source_type": "gdrive", "file_id": "FID", "chunk_index": 0}, [0.5]*4)
    fm = _gdoc_change("FID")["file"]
    from mcpbrain.sync.drive import _file_content_hash
    ch = _file_content_hash(fm)
    ingest_cache.publish_file(src, fs, "D1", "FID", ch, PIN)
    svc = FakeDriveService(
        initial_cursor="100",
        pages=[{"changes": [_gdoc_change("FID")], "newStartPageToken": "101"}],
        exports={"FID": b"DIFFERENT - must NOT be extracted"})
    out = sync_shared_drive(svc, s, "D1", fleet_storage=fs, pin=PIN)
    assert out["processed"] == 1 and out["miss"] == []          # imported from cache
    assert s.get_chunk("gdrive-FID-0")["text"] == "cached body"  # not the export bytes


def test_sync_shared_drive_removal_purges_local_and_artifact(tmp_path):
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    s.import_cached_chunk("gdrive-FID-0", "a", "c", {"file_id": "FID", "drive_id": "D1"}, [0.0]*4)
    ingest_cache.publish_file(s, fs, "D1", "FID", "vX", PIN)
    s.set_cursor("drive:D1", "100")
    svc = FakeDriveService(
        initial_cursor="100",
        pages=[{"changes": [{"fileId": "FID", "removed": True}], "newStartPageToken": "101"}])
    sync_shared_drive(svc, s, "D1", fleet_storage=fs, pin=PIN)
    assert s.get_chunk("gdrive-FID-0") is None
    assert fs.list_paths(ingest_cache.CACHE_DIR + "/") == []


def test_sync_shared_drives_enumerates_and_returns_storages(tmp_path):
    s = _store(tmp_path)
    s.set_cursor("drive:D1", "100")
    svc = FakeDriveService(
        shared_drives=[{"id": "D1", "name": "Ops"}],
        initial_cursor="100",
        pages=[{"changes": [_gdoc_change("FID")], "newStartPageToken": "101"}],
        exports={"FID": b"body text here"})
    storages = {}

    def factory(drive_id):
        storages.setdefault(drive_id, LocalDirFleetStorage(tmp_path / drive_id))
        return storages[drive_id]

    out = sync_shared_drives(svc, s, pin=PIN, storage_factory=factory)
    assert set(out) >= {"D1"}
    assert out["D1"]["processed"] == 1
    assert out["D1"]["storage"] is storages["D1"]
    assert out["_revoked"] == []


def test_file_content_hash_degenerate_metadata_forces_cache_miss(caplog):
    """When BOTH md5Checksum and version/modifiedTime are absent, the function
    must not degrade to a constant hash (sha256("|")) — that would mean the
    file's cache entry NEVER invalidates even after the file changes
    (permanent silent staleness). Instead it must force a perpetual cache
    miss: successive calls for the SAME degenerate metadata must produce
    DIFFERENT hashes (so a hash computed this cycle can never match one
    stored from a previous cycle, including for this very file)."""
    meta = {"id": "NOVERSION"}
    with caplog.at_level("INFO"):
        h1 = _file_content_hash(meta)
        h2 = _file_content_hash(meta)
    assert h1 != h2
    import hashlib as _hashlib
    assert h1 != _hashlib.sha256(b"|").hexdigest()
    assert h2 != _hashlib.sha256(b"|").hexdigest()
    assert any("NOVERSION" in rec.message for rec in caplog.records)


def test_sync_shared_drive_isolates_per_file_extraction_failure(tmp_path, caplog):
    """One poison file raising during fetch/extract must not abort the whole
    drive's cycle: the good file still gets processed and the cursor still
    advances (so the poison file isn't retried forever, blocking the drive)."""
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    s.set_cursor("drive:D1", "100")
    svc = FakeDriveService(
        initial_cursor="100",
        pages=[{
            "changes": [_gdoc_change("BAD"), _gdoc_change("GOOD")],
            "newStartPageToken": "101",
        }],
        exports={"GOOD": b"good file content here"},
        export_raises={"BAD": RuntimeError("corrupt export")},
    )
    with caplog.at_level("WARNING"):
        out = sync_shared_drive(svc, s, "D1", fleet_storage=fs, pin=PIN)

    assert out["processed"] == 1
    assert s.get_chunk("gdrive-GOOD-0") is not None
    assert s.get_chunk("gdrive-BAD-0") is None
    # Cursor still advances — the poison file must not block the drive forever.
    assert s.get_cursor("drive:D1") == "101"
    assert any("BAD" in rec.message for rec in caplog.records)
    assert any(rec.levelname == "WARNING" for rec in caplog.records)


def test_sync_shared_drive_dedups_repeated_fileid_in_one_delta(tmp_path):
    """The same fileId appearing twice within one delta (edited then re-edited)
    must be fetched/extracted/published exactly ONCE — not twice. The delta is
    collapsed to one ordered, deduplicated view keyed by fileId before any
    extraction happens."""
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    s.set_cursor("drive:D1", "100")
    svc = FakeDriveService(
        initial_cursor="100",
        pages=[{"changes": [_gdoc_change("FID"), _gdoc_change("FID")],
                "newStartPageToken": "101"}],
        exports={"FID": b"the quick brown fox jumps"})
    out = sync_shared_drive(svc, s, "D1", fleet_storage=fs, pin=PIN)
    assert out["processed"] == 1
    assert len(out["miss"]) == 1 and out["miss"][0][0] == "FID"
    # export() invoked once despite the fileId appearing twice in the delta
    assert svc._files.export_calls.get("FID") == 1
    assert s.get_chunk("gdrive-FID-0") is not None


def test_sync_shared_drive_change_then_removal_collapses_to_removal(tmp_path):
    """A change followed by a removal of the SAME file within one delta resolves
    to the file's final state at the cursor endpoint: REMOVED. Reasoning: Drive
    emits changes chronologically, so the last event (removal) is the truth; the
    file must NOT be extracted (no fetch, no chunk, no miss to publish) and any
    prior local copy + artifact is purged."""
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    # seed a prior local chunk + artifact so we can prove the removal purges it
    s.import_cached_chunk("gdrive-FID-0", "old body", "c",
                          {"file_id": "FID", "drive_id": "D1"}, [0.0]*4)
    ingest_cache.publish_file(s, fs, "D1", "FID", "vX", PIN)
    s.set_cursor("drive:D1", "100")
    svc = FakeDriveService(
        initial_cursor="100",
        pages=[{"changes": [_gdoc_change("FID"),
                            {"fileId": "FID", "removed": True}],
                "newStartPageToken": "101"}],
        exports={"FID": b"MUST NOT be extracted"})
    out = sync_shared_drive(svc, s, "D1", fleet_storage=fs, pin=PIN)
    assert out["processed"] == 0 and out["miss"] == []
    assert svc._files.export_calls.get("FID") is None      # never fetched
    assert s.get_chunk("gdrive-FID-0") is None             # purged
    assert fs.list_paths(ingest_cache.CACHE_DIR + "/") == []


def test_sync_shared_drive_removal_then_change_collapses_to_change(tmp_path):
    """Reverse ordering: a removal followed by a later change of the SAME file
    (deleted then restored + edited) resolves to the final state: CHANGED. The
    file is extracted normally and NOT purged."""
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    s.set_cursor("drive:D1", "100")
    svc = FakeDriveService(
        initial_cursor="100",
        pages=[{"changes": [{"fileId": "FID", "removed": True},
                            _gdoc_change("FID")],
                "newStartPageToken": "101"}],
        exports={"FID": b"the quick brown fox jumps"})
    out = sync_shared_drive(svc, s, "D1", fleet_storage=fs, pin=PIN)
    assert out["processed"] == 1
    assert len(out["miss"]) == 1 and out["miss"][0][0] == "FID"
    assert svc._files.export_calls.get("FID") == 1
    assert s.get_chunk("gdrive-FID-0") is not None


def test_sync_shared_drives_does_not_sweep_unchanged_artifacts(tmp_path):
    """A per-cycle delta only ever contains the files that changed since the
    last cursor — it is never a complete file listing. sync_shared_drives must
    NOT sweep the cache off that partial set: an artifact for a file that was
    NOT touched by this cycle's delta must survive."""
    s = _store(tmp_path)
    s.set_cursor("drive:D1", "100")
    fs = LocalDirFleetStorage(tmp_path / "D1")

    # Pre-seed a cache artifact for UNTOUCHED, a file that will NOT appear
    # in this cycle's delta at all.
    src = _store(tmp_path, "src.sqlite3")
    src.import_cached_chunk("gdrive-UNTOUCHED-0", "untouched body", "cU",
                            {"source_type": "gdrive", "file_id": "UNTOUCHED",
                             "chunk_index": 0}, [0.5] * 4)
    ingest_cache.publish_file(src, fs, "D1", "UNTOUCHED", "vU", PIN)
    assert fs.list_paths(ingest_cache.CACHE_DIR + "/") != []

    # This cycle's delta only mentions a different file, FID.
    svc = FakeDriveService(
        shared_drives=[{"id": "D1", "name": "Ops"}],
        initial_cursor="100",
        pages=[{"changes": [_gdoc_change("FID")], "newStartPageToken": "101"}],
        exports={"FID": b"body text here"})

    sync_shared_drives(svc, s, pin=PIN, storage_factory=lambda d: fs)

    # UNTOUCHED's artifact must still be present — it was never in this
    # cycle's (necessarily partial) live_file_ids set.
    assert fs.list_paths(ingest_cache.CACHE_DIR + "/") != []


def test_sync_shared_drives_revokes_vanished_drive(tmp_path):
    from mcpbrain import ingest_cache
    s = _store(tmp_path)
    s.import_cached_chunk("gdrive-F1-0", "a", "c", {"file_id": "F1", "drive_id": "GONE"}, [0.0]*4)
    # seed the counter as if GONE was seen and has been absent (threshold-1) times
    ingest_cache.note_drive_presence(s, ["GONE"], threshold=2)  # GONE present, counter=0
    ingest_cache.note_drive_presence(s, [], threshold=2)        # GONE absent, counter=1
    svc = FakeDriveService(shared_drives=[])         # GONE no longer listed
    out = sync_shared_drives(svc, s, pin=PIN,
                             storage_factory=lambda d: LocalDirFleetStorage(tmp_path / d),
                             absence_threshold=2)
    assert out["_revoked"] == ["GONE"]
    assert s.doc_ids_for_drive("GONE") == []


from mcpbrain.sync.drive import sync_shared_drives   # noqa: E402  (import after helpers)


def test_backfill_shared_drive_cache_first(tmp_path):
    from mcpbrain.sync.drive import backfill_shared_drive
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    fm = {"id": "FID", "name": "Doc", "mimeType": "text/plain",
          "modifiedTime": "2026-05-01T10:00:00Z", "md5Checksum": "abc",
          "owners": [{"displayName": "X"}]}
    svc = FakeDriveService(file_list=[fm], media={"FID": b"backfilled body text"})
    out = backfill_shared_drive(svc, s, "D1", "2020-01-01T00:00:00Z",
                                fleet_storage=fs, pin=PIN)
    assert out["processed"] == 1
    assert out["miss"] == [("FID", "abc")]           # md5 is the content-version id
    assert s.get_chunk("gdrive-FID-0")["metadata"]["drive_id"] == "D1"
    # cursor untouched
    assert s.get_cursor("drive:D1") is None


def test_backfill_shared_drive_cache_hit_skips_extraction(tmp_path):
    from mcpbrain.sync.drive import backfill_shared_drive
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    # pre-publish an artifact for FID's current version so try_import hits
    src = _store(tmp_path, "src.sqlite3")
    src.import_cached_chunk("gdrive-FID-0", "cached body", "c0",
                            {"source_type": "gdrive", "file_id": "FID", "chunk_index": 0}, [0.5]*4)
    fm = {"id": "FID", "name": "Doc", "mimeType": "text/plain",
          "modifiedTime": "2026-05-01T10:00:00Z", "md5Checksum": "abc",
          "owners": [{"displayName": "X"}]}
    from mcpbrain.sync.drive import _file_content_hash
    ch = _file_content_hash(fm)
    ingest_cache.publish_file(src, fs, "D1", "FID", ch, PIN)
    svc = FakeDriveService(
        file_list=[fm],
        media={"FID": b"DIFFERENT - must NOT be extracted"})
    out = backfill_shared_drive(svc, s, "D1", "2020-01-01T00:00:00Z",
                                fleet_storage=fs, pin=PIN)
    assert out["processed"] == 1 and out["miss"] == []          # imported from cache
    assert s.get_chunk("gdrive-FID-0")["text"] == "cached body"  # not the export bytes
