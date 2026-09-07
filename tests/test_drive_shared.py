from mcpbrain.sync.drive import (
    list_shared_drives, normalise_drive, _file_content_hash,
)
from mcpbrain.org_contracts import DRIVE_ID_META_KEY

from mcpbrain import ingest_cache
from mcpbrain.org_contracts import FleetPin
from mcpbrain.store import Store
from mcpbrain.sync.drive import discover_shared_drive, handle_shared_drive_item
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

    def execute(self, num_retries=0):
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


def test_discover_shared_drive_bootstrap_sets_cursor(tmp_path):
    """First-ever call for a pinned Shared Drive: no cursor yet, so
    discover_shared_drive must bootstrap via
    changes().getStartPageToken(driveId=..., supportsAllDrives=True) --
    a shared-drive-specific branch distinct from My Drive's driveId-less
    getStartPageToken() and, before this port, only exercised through the
    now-deleted sync_shared_drive (Task 2/3's own test file
    test_shared_drive_discovery.py always pre-seeds a cursor and never hits
    this branch)."""
    s = _store(tmp_path)
    svc = FakeDriveService(start_token="500")
    n = discover_shared_drive(svc, s, "D1", "drive:D1")
    assert n == 0
    assert s.get_cursor("drive:D1") == "500"


def test_handle_shared_drive_item_cache_hit_skips_extraction(tmp_path):
    """Cache-hit behavior through handle_shared_drive_item's shared
    _cache_first_extract_one call has no other coverage -- Task 3's own
    test file (test_shared_drive_discovery.py) only exercises the miss
    path. Ported from the deleted sync_shared_drive's equivalent test."""
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    # pre-publish an artifact for FID's current version so try_import hits
    src = _store(tmp_path, "src.sqlite3")
    src.import_cached_chunk("gdrive-FID-0", "cached body", "c0",
                            {"source_type": "gdrive", "file_id": "FID", "chunk_index": 0}, [0.5]*4)
    fm = _gdoc_change("FID")["file"]
    ch = _file_content_hash(fm)
    ingest_cache.publish_file(src, fs, "D1", "FID", ch, PIN)
    svc = FakeDriveService(files_by_id={"FID": fm},
                           exports={"FID": b"DIFFERENT - must NOT be extracted"})
    item = {"ref_id": "FID", "version": "", "event": "upsert",
           "modified_at": "2026-05-01T10:00:00Z"}
    handle_shared_drive_item(svc, s, item, fleet_storage=fs, pin=PIN, drive_id="D1")
    assert s.get_chunk("gdrive-FID-0")["text"] == "cached body"  # not the export bytes
    assert s.pending_publishes("D1") == []          # nothing new to publish


def test_handle_shared_drive_item_removal_purges_local_and_artifact(tmp_path):
    """The removal branch's actual effect -- an EXISTING local chunk deleted
    and its published artifact GC'd via remove_file_artifacts -- has no
    other coverage: Task 3's own removal test
    (test_handle_shared_drive_item_removal_deletes_chunks) only asserts the
    call doesn't raise when there is nothing to delete. Ported from the
    deleted sync_shared_drive's equivalent test.

    Also covers the final-review fix: a durable
    shared_drive_pending_publish row for the SAME file (seeded via the real
    store.record_pending_publish, as if an earlier cycle extracted this file
    with a cache miss but never got to publish it) must be cleared too --
    otherwise publish_file would retry it forever with nothing left to
    collect (its chunks are gone)."""
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    s.import_cached_chunk("gdrive-FID-0", "a", "c", {"file_id": "FID", "drive_id": "D1"}, [0.0]*4)
    ingest_cache.publish_file(s, fs, "D1", "FID", "vX", PIN)
    s.record_pending_publish("D1", "FID", "vX")
    item = {"ref_id": "FID", "version": "", "event": "remove",
           "modified_at": "2026-05-01T10:00:00Z"}
    handle_shared_drive_item(FakeDriveService(), s, item, fleet_storage=fs, pin=PIN, drive_id="D1")
    assert s.get_chunk("gdrive-FID-0") is None
    assert fs.list_paths(ingest_cache.CACHE_DIR + "/") == []
    assert s.pending_publishes("D1") == []


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


def test_handle_shared_drive_item_herd_race_recheck_import_hits(tmp_path, monkeypatch):
    """Herd-race re-check branch: the FIRST try_import (before fetch) misses, but
    the SECOND (after fetch, right before extraction) HITS because a concurrent
    daemon published the artifact while we were fetching. The file must count as
    a cache hit (nothing new to publish) and NOT be extracted locally. Ported
    from the deleted sync_shared_drive's equivalent test -- handle_shared_drive_item
    calls the same shared _cache_first_extract_one, but nothing else exercises
    this branch through the new path."""
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    calls = {"n": 0}

    def fake_try_import(*_a, **_k):
        calls["n"] += 1
        return calls["n"] >= 2          # first call miss, second (+) hit

    monkeypatch.setattr(ingest_cache, "try_import", fake_try_import)
    fm = _gdoc_change("FID")["file"]
    svc = FakeDriveService(files_by_id={"FID": fm},
                           exports={"FID": b"the quick brown fox jumps"})
    item = {"ref_id": "FID", "version": "", "event": "upsert",
           "modified_at": "2026-05-01T10:00:00Z"}
    handle_shared_drive_item(svc, s, item, fleet_storage=fs, pin=PIN, drive_id="D1")
    assert calls["n"] == 2                          # both try_import calls exercised
    assert svc._files.export_calls.get("FID") == 1  # fetch happened before the hit
    assert s.get_chunk("gdrive-FID-0") is None       # no local extraction occurred
    assert s.pending_publishes("D1") == []           # re-check hit -> nothing to publish


def test_handle_shared_drive_item_does_not_sweep_unchanged_artifacts(tmp_path):
    """A queued work item only ever concerns the ONE file it names -- handling
    it must never sweep the ingest cache for the rest of the drive. Ported
    from the deleted sync_shared_drives' equivalent test: that fleet-wide,
    delta-based reasoning still applies here (handle_shared_drive_item, like
    the deleted function, never calls sweep_drive), and this ports it to the
    finer per-item grain the new architecture actually works at."""
    s = _store(tmp_path)
    fs = LocalDirFleetStorage(tmp_path / "D1")

    # Pre-seed a cache artifact for UNTOUCHED, a file this item does NOT
    # mention at all.
    src = _store(tmp_path, "src.sqlite3")
    src.import_cached_chunk("gdrive-UNTOUCHED-0", "untouched body", "cU",
                            {"source_type": "gdrive", "file_id": "UNTOUCHED",
                             "chunk_index": 0}, [0.5] * 4)
    ingest_cache.publish_file(src, fs, "D1", "UNTOUCHED", "vU", PIN)
    assert fs.list_paths(ingest_cache.CACHE_DIR + "/") != []

    fm = _gdoc_change("FID")["file"]
    svc = FakeDriveService(files_by_id={"FID": fm}, exports={"FID": b"body text here"})
    item = {"ref_id": "FID", "version": "", "event": "upsert",
           "modified_at": "2026-05-01T10:00:00Z"}
    handle_shared_drive_item(svc, s, item, fleet_storage=fs, pin=PIN, drive_id="D1")

    # UNTOUCHED's artifact must still be present — it was never named by
    # this item.
    assert fs.list_paths(ingest_cache.CACHE_DIR + "/") != []


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
