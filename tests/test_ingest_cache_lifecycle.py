import struct

from mcpbrain import ingest_cache
from mcpbrain.org_contracts import FleetPin, artifact_filename
from mcpbrain.store import Store
from tests.helpers.org_fleet import LocalDirFleetStorage

PIN = FleetPin(embed_model="bge-small", dim=4, chunker_version="v1",
               enrich_logic_floor=1, fleet_secret="s3cret")


def _store(tmp_path, name="a.sqlite3"):
    s = Store(tmp_path / name, dim=4)
    s.init()
    return s


def _seed_file(store, file_id, n=2):
    for i in range(n):
        store.import_cached_chunk(
            f"gdrive-{file_id}-{i}", f"text {i}", f"c{i}",
            {"source_type": "gdrive", "file_id": file_id, "chunk_index": i},
            [float(i)] * 4)


def test_publish_then_import_roundtrips(tmp_path):
    src, fs = _store(tmp_path, "src.sqlite3"), LocalDirFleetStorage(tmp_path / "drv")
    _seed_file(src, "FID", n=3)
    ok = ingest_cache.publish_file(src, fs, "D1", "FID", "vhash1", PIN,
                                   enrich={"logic_version": 1}, published_by="me@x.org")
    assert ok
    dst = _store(tmp_path, "dst.sqlite3")
    assert ingest_cache.try_import(dst, fs, "D1", "FID", "vhash1", PIN) is True
    for i in range(3):
        a = src.embedding_for_doc(f"gdrive-FID-{i}")
        b = dst.embedding_for_doc(f"gdrive-FID-{i}")
        assert struct.pack("<4f", *a) == struct.pack("<4f", *b)   # bitwise-identical


def test_publish_unpinned_is_noop(tmp_path):
    src, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    _seed_file(src, "FID")
    assert ingest_cache.publish_file(src, fs, "D1", "FID", "vhash1", FleetPin()) is False
    assert fs.list_paths(ingest_cache.CACHE_DIR + "/") == []


def test_publish_gcs_superseded_content_versions(tmp_path):
    src, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    _seed_file(src, "FID")
    ingest_cache.publish_file(src, fs, "D1", "FID", "vOLD", PIN)
    ingest_cache.publish_file(src, fs, "D1", "FID", "vNEW", PIN)   # supersedes vOLD
    names = [p.rsplit("/", 1)[-1] for p in fs.list_paths(ingest_cache.CACHE_DIR + "/")]
    assert any(n.startswith("FID.vNEW"[:16]) or "vNEW"[:12] in n for n in names)
    # exactly one artifact remains for FID
    assert len(names) == 1


def test_gc_superseded_drops_stale_pipeline(tmp_path):
    fs = LocalDirFleetStorage(tmp_path / "drv")
    # a stale-pipeline artifact (different embed model => different pf8)
    stale = artifact_filename("FID", "vhash1", "old-model", 4, "v1")
    cur = artifact_filename("FID", "vhash1", PIN.embed_model, PIN.dim, PIN.chunker_version)
    fs.put_bytes(f"{ingest_cache.CACHE_DIR}/{stale}", b"x")
    fs.put_bytes(f"{ingest_cache.CACHE_DIR}/{cur}", b"y")
    removed = ingest_cache.gc_superseded(fs, "D1", "FID", "vhash1", PIN)
    assert removed == 1
    remaining = [p.rsplit("/", 1)[-1] for p in fs.list_paths(ingest_cache.CACHE_DIR + "/")]
    assert remaining == [cur]


def test_sweep_and_remove_file_artifacts(tmp_path):
    fs = LocalDirFleetStorage(tmp_path / "drv")
    for fid in ("A", "B", "C"):
        fs.put_bytes(f"{ingest_cache.CACHE_DIR}/{artifact_filename(fid, 'v1', PIN.embed_model, PIN.dim, PIN.chunker_version)}", b"x")
    # sweep keeps only live files
    assert ingest_cache.sweep_drive(fs, {"A", "B"}) == 1
    assert {p.rsplit('.', 4)[0].rsplit('/', 1)[-1] for p in fs.list_paths(ingest_cache.CACHE_DIR + "/")} == {"A", "B"}
    # remove one file's artifacts explicitly
    assert ingest_cache.remove_file_artifacts(fs, "A") == 1
    assert ingest_cache.remove_file_artifacts(fs, "A") == 0
