import base64
import struct

from mcpbrain import ingest_cache
from mcpbrain.org_contracts import FleetPin, CacheArtifact, CacheChunk, artifact_filename
from mcpbrain.store import Store
from tests.helpers.org_fleet import LocalDirFleetStorage

PIN = FleetPin(embed_model="bge-small", dim=4, chunker_version="v1",
               enrich_logic_floor=1, fleet_secret="s3cret")


def _store(tmp_path, name="a.sqlite3"):
    s = Store(tmp_path / name, dim=4)
    s.init()
    return s


def _b64(vec):
    return base64.b64encode(struct.pack(f"<{len(vec)}f", *vec)).decode("ascii")


def _write_artifact(fs, file_id, content_hash, *, dim=4, embed_model="bge-small",
                    chunker="v1", enrich=None, chunks=None):
    chunks = chunks or (CacheChunk(idx=0, text="hello", embedding_b64=_b64([0.1, 0.2, 0.3, 0.4]),
                                   metadata={"source_type": "gdrive", "file_id": file_id,
                                             "chunk_index": 0}),)
    art = CacheArtifact(file_id=file_id, content_hash=content_hash,
                        extraction_method="gdocs", chunker_version=chunker,
                        embed_model=embed_model, dim=dim, chunks=tuple(chunks),
                        enrich=enrich or {}, published_by="p@x.org", published_at="2026-07-03")
    import gzip, json
    fname = artifact_filename(file_id, content_hash, embed_model, dim, chunker)
    fs.put_bytes(f"{ingest_cache.CACHE_DIR}/{fname}", gzip.compress(json.dumps(art.to_dict()).encode()))


def test_try_import_hit_imports_chunks_and_stamps_drive_id(tmp_path):
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    _write_artifact(fs, "FID", "vhash1")
    assert ingest_cache.try_import(s, fs, "D1", "FID", "vhash1", PIN) is True
    ch = s.get_chunk("gdrive-FID-0")
    assert ch is not None and ch["metadata"]["drive_id"] == "D1"
    back = s.embedding_for_doc("gdrive-FID-0")
    assert struct.pack("<4f", *back) == struct.pack("<4f", 0.1, 0.2, 0.3, 0.4)


def test_try_import_miss_returns_false(tmp_path):
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    assert ingest_cache.try_import(s, fs, "D1", "FID", "vhash1", PIN) is False


def test_try_import_unpinned_returns_false(tmp_path):
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    _write_artifact(fs, "FID", "vhash1")
    assert ingest_cache.try_import(s, fs, "D1", "FID", "vhash1", FleetPin()) is False


def test_try_import_content_hash_mismatch_falls_back(tmp_path):
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    _write_artifact(fs, "FID", "vhash1")
    # a DIFFERENT file version -> the artifact for vhash1 must not be used for vhash2
    assert ingest_cache.try_import(s, fs, "D1", "FID", "vhash2", PIN) is False


def test_try_import_pipeline_mismatch_falls_back(tmp_path):
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    # artifact embedded with a different model -> filename fp differs, so it is not even found;
    # but a hand-planted file with mismatched inner fields must also be rejected.
    _write_artifact(fs, "FID", "vhash1", embed_model="other-model")
    assert ingest_cache.try_import(s, fs, "D1", "FID", "vhash1", PIN) is False


def test_try_import_corrupt_artifact_falls_back(tmp_path):
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    fname = artifact_filename("FID", "vhash1", PIN.embed_model, PIN.dim, PIN.chunker_version)
    fs.put_bytes(f"{ingest_cache.CACHE_DIR}/{fname}", b"not-gzip-json")
    assert ingest_cache.try_import(s, fs, "D1", "FID", "vhash1", PIN) is False


def test_try_import_marks_enriched_when_logic_gate_clears(tmp_path):
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    _write_artifact(fs, "FID", "vhash1", enrich={"logic_version": 9})
    assert ingest_cache.try_import(s, fs, "D1", "FID", "vhash1", PIN) is True
    with s._connect() as db:
        r = db.execute("SELECT enriched FROM chunks WHERE doc_id='gdrive-FID-0'").fetchone()
    assert r["enriched"] == 1


def test_try_import_no_enrich_block_leaves_unenriched(tmp_path):
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    _write_artifact(fs, "FID", "vhash1", enrich={})
    assert ingest_cache.try_import(s, fs, "D1", "FID", "vhash1", PIN) is True
    with s._connect() as db:
        r = db.execute("SELECT enriched FROM chunks WHERE doc_id='gdrive-FID-0'").fetchone()
    assert r["enriched"] == 0


def test_collect_chunks_is_drive_neutral(tmp_path):
    s = _store(tmp_path)
    s.import_cached_chunk("gdrive-FID-0", "hello", "c0",
                          {"file_id": "FID", "chunk_index": 0, "drive_id": "D1"}, [0.1, 0.2, 0.3, 0.4])
    ccs = ingest_cache.collect_chunks(s, "FID")
    assert len(ccs) == 1 and ccs[0].idx == 0 and ccs[0].text == "hello"
    assert "drive_id" not in ccs[0].metadata            # neutralised for byte-identical artifacts
    assert struct.unpack("<4f", base64.b64decode(ccs[0].embedding_b64)) == (
        struct.unpack("<4f", struct.pack("<4f", 0.1, 0.2, 0.3, 0.4)))
