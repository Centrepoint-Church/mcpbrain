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
                    chunker="v1", enrich=None, chunks=None, published_at="2026-07-03"):
    chunks = chunks or (CacheChunk(idx=0, text="hello", embedding_b64=_b64([0.1, 0.2, 0.3, 0.4]),
                                   metadata={"source_type": "gdrive", "file_id": file_id,
                                             "chunk_index": 0}),)
    art = CacheArtifact(file_id=file_id, content_hash=content_hash,
                        extraction_method="gdocs", chunker_version=chunker,
                        embed_model=embed_model, dim=dim, chunks=tuple(chunks),
                        enrich=enrich or {}, published_by="p@x.org", published_at=published_at)
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


def test_try_import_malformed_enrich_field_falls_back(tmp_path):
    """Regression: malformed enrich field (string instead of dict) must not raise.
    _import_artifact accesses art.enrich.get() at line 80, which raises AttributeError
    if enrich is a string. try_import must return False, not raise."""
    import gzip, json
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    # Hand-plant an artifact with a valid outer structure but malformed enrich field
    file_id, content_hash = "FID", "vhash1"
    fname = artifact_filename(file_id, content_hash, PIN.embed_model, PIN.dim, PIN.chunker_version)
    art_dict = {
        "file_id": file_id,
        "content_hash": content_hash,
        "extraction_method": "gdocs",
        "chunker_version": PIN.chunker_version,
        "embed_model": PIN.embed_model,
        "dim": PIN.dim,
        "chunks": [{
            "idx": 0,
            "text": "hello",
            "embedding_b64": _b64([0.1, 0.2, 0.3, 0.4]),
            "metadata": {"source_type": "gdrive", "file_id": file_id, "chunk_index": 0}
        }],
        "enrich": "corrupted-not-a-dict",  # <-- malformed: string instead of dict
        "published_by": "p@x.org",
        "published_at": "2026-07-03"
    }
    fs.put_bytes(f"{ingest_cache.CACHE_DIR}/{fname}", gzip.compress(json.dumps(art_dict).encode()))
    # Must return False (cache miss fallback), not raise AttributeError
    assert ingest_cache.try_import(s, fs, "D1", file_id, content_hash, PIN) is False


def test_try_import_hand_planted_content_hash_mismatch_inner_field(tmp_path):
    """Test that internal content_hash validation (line 121) rejects an artifact
    where the inner JSON field doesn't match the param, even when filename matches.
    This tests the validation branch that the existing test can't reach."""
    import gzip, json
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    file_id, content_hash = "FID", "vhash1"
    fname = artifact_filename(file_id, content_hash, PIN.embed_model, PIN.dim, PIN.chunker_version)
    # Hand-plant with correct filename path but WRONG inner content_hash field
    art_dict = {
        "file_id": file_id,
        "content_hash": "WRONG_HASH",  # <-- doesn't match the param
        "extraction_method": "gdocs",
        "chunker_version": PIN.chunker_version,
        "embed_model": PIN.embed_model,
        "dim": PIN.dim,
        "chunks": [{
            "idx": 0,
            "text": "hello",
            "embedding_b64": _b64([0.1, 0.2, 0.3, 0.4]),
            "metadata": {"source_type": "gdrive", "file_id": file_id, "chunk_index": 0}
        }],
        "enrich": {},
        "published_by": "p@x.org",
        "published_at": "2026-07-03"
    }
    fs.put_bytes(f"{ingest_cache.CACHE_DIR}/{fname}", gzip.compress(json.dumps(art_dict).encode()))
    # Should detect inner hash mismatch and return False
    assert ingest_cache.try_import(s, fs, "D1", file_id, content_hash, PIN) is False


def test_try_import_hand_planted_pipeline_mismatch_inner_field(tmp_path):
    """Test that internal pipeline validation (line 76-78) rejects an artifact
    where inner embed_model/dim don't match, even when filename matches."""
    import gzip, json
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    file_id, content_hash = "FID", "vhash1"
    fname = artifact_filename(file_id, content_hash, PIN.embed_model, PIN.dim, PIN.chunker_version)
    # Hand-plant with correct filename but WRONG inner embed_model
    art_dict = {
        "file_id": file_id,
        "content_hash": content_hash,
        "extraction_method": "gdocs",
        "chunker_version": PIN.chunker_version,
        "embed_model": "other-model",  # <-- doesn't match PIN
        "dim": PIN.dim,
        "chunks": [{
            "idx": 0,
            "text": "hello",
            "embedding_b64": _b64([0.1, 0.2, 0.3, 0.4]),
            "metadata": {"source_type": "gdrive", "file_id": file_id, "chunk_index": 0}
        }],
        "enrich": {},
        "published_by": "p@x.org",
        "published_at": "2026-07-03"
    }
    fs.put_bytes(f"{ingest_cache.CACHE_DIR}/{fname}", gzip.compress(json.dumps(art_dict).encode()))
    # Should detect pipeline mismatch and return False
    assert ingest_cache.try_import(s, fs, "D1", file_id, content_hash, PIN) is False


def test_import_atomic_mid_artifact_corruption_writes_zero_chunks(tmp_path):
    """A mid-artifact corrupt vector (chunk 2 of 3) must result in NOTHING
    written to the store — not a partial 2-of-3 import. Regression for the
    non-atomic per-chunk-transaction bug in _import_artifact."""
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    chunks = (
        CacheChunk(idx=0, text="t0", embedding_b64=_b64([0.1, 0.2, 0.3, 0.4]),
                  metadata={"file_id": "FID", "chunk_index": 0}),
        CacheChunk(idx=1, text="t1", embedding_b64=_b64([0.5, 0.6, 0.7, 0.8]),
                  metadata={"file_id": "FID", "chunk_index": 1}),
        # wrong dim (3 floats, not 4) -> _decode_vec raises ValueError
        CacheChunk(idx=2, text="t2", embedding_b64=_b64([0.9, 1.0, 1.1]),
                  metadata={"file_id": "FID", "chunk_index": 2}),
    )
    _write_artifact(fs, "FID", "vhash1", chunks=chunks)
    assert ingest_cache.try_import(s, fs, "D1", "FID", "vhash1", PIN) is False
    assert s.get_chunk("gdrive-FID-0") is None
    assert s.get_chunk("gdrive-FID-1") is None
    assert s.get_chunk("gdrive-FID-2") is None


def test_import_artifact_store_write_failure_logged_as_warning_not_info(tmp_path, caplog):
    """A genuine store-write failure (disk full, locked db, ...) during the
    atomic import must be logged at WARNING with a distinguishable message —
    not treated identically to a corrupt-artifact info-log — so an operator
    can tell the two failure classes apart. Still returns False (fail-safe
    contract: never raise into the caller)."""
    import logging
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    _write_artifact(fs, "FID", "vhash1")

    def boom(rows):
        raise RuntimeError("disk full")
    s.import_cached_chunks = boom

    with caplog.at_level(logging.INFO, logger="mcpbrain.ingest_cache"):
        assert ingest_cache.try_import(s, fs, "D1", "FID", "vhash1", PIN) is False

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "expected a WARNING-level log for the store-write failure"
    assert "NOT a cache-corruption signal" in warnings[0].message
    # and it must not ALSO be logged at info as a generic corrupt-artifact fallback
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert not any("corrupt artifact" in r.message for r in infos)


def test_collect_chunks_is_drive_neutral(tmp_path):
    s = _store(tmp_path)
    s.import_cached_chunk("gdrive-FID-0", "hello", "c0",
                          {"file_id": "FID", "chunk_index": 0, "drive_id": "D1"}, [0.1, 0.2, 0.3, 0.4])
    ccs = ingest_cache.collect_chunks(s, "FID")
    assert len(ccs) == 1 and ccs[0].idx == 0 and ccs[0].text == "hello"
    assert "drive_id" not in ccs[0].metadata            # neutralised for byte-identical artifacts
    assert struct.unpack("<4f", base64.b64decode(ccs[0].embedding_b64)) == (
        struct.unpack("<4f", struct.pack("<4f", 0.1, 0.2, 0.3, 0.4)))


def test_bootstrap_drive_imports_newest_per_file(tmp_path):
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    # two files, and an older + newer version of FID (GC would normally drop the old one)
    _write_artifact(fs, "FID", "vhash1")
    _write_artifact(fs, "GID", "vhashG")
    # a stale-pipeline artifact must be ignored by bootstrap
    _write_artifact(fs, "HID", "vhashH", embed_model="old-model")
    summary = ingest_cache.bootstrap_drive(s, fs, "D1", PIN)
    assert summary["imported"] == 2 and summary["chunks"] == 2
    assert summary["cache_hits"] == 2               # C sums this across drives
    assert s.get_chunk("gdrive-FID-0")["metadata"]["drive_id"] == "D1"
    assert s.get_chunk("gdrive-HID-0") is None       # stale pipeline skipped


def test_bootstrap_drive_unpinned_is_noop(tmp_path):
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    _write_artifact(fs, "FID", "vhash1")
    out = ingest_cache.bootstrap_drive(s, fs, "D1", FleetPin())
    assert out["imported"] == 0 and out["cache_hits"] == 0


def test_bootstrap_drive_prefers_newest_content_version(tmp_path):
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    # Two artifacts for the same file_id but different content versions (different hashes, different timestamps)
    # Write an older artifact
    _write_artifact(fs, "FID", "old_hash", chunks=(
        CacheChunk(idx=0, text="old content here", embedding_b64=_b64([0.1, 0.2, 0.3, 0.4]),
                   metadata={"source_type": "gdrive", "file_id": "FID", "chunk_index": 0}),
    ), published_at="2026-07-01")
    # Write a newer artifact with different content and a later timestamp
    _write_artifact(fs, "FID", "new_hash", chunks=(
        CacheChunk(idx=0, text="new content here", embedding_b64=_b64([0.5, 0.6, 0.7, 0.8]),
                   metadata={"source_type": "gdrive", "file_id": "FID", "chunk_index": 0}),
    ), published_at="2026-07-03")
    # bootstrap_drive should import only the newer version
    summary = ingest_cache.bootstrap_drive(s, fs, "D1", PIN)
    assert summary["imported"] == 1 and summary["chunks"] == 1
    # Verify the imported chunk has the NEWER artifact's text, not the older one's
    ch = s.get_chunk("gdrive-FID-0")
    assert ch is not None and ch["text"] == "new content here"
    assert ch["metadata"]["drive_id"] == "D1"
