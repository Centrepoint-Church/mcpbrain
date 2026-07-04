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
