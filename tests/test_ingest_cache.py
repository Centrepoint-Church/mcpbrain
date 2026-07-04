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


def test_try_import_rejects_contextual_retrieval_mismatch(tmp_path):
    """CR materially changes the vector and isn't in the pipeline fingerprint, so
    an artifact embedded with CR on must NOT be imported by a CR-off install
    (and vice-versa). The guard makes it a cache miss -> local fallback."""
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    _write_artifact(fs, "FID", "vhash1", enrich={"contextual_retrieval": True})
    # CR-off importer must reject the CR-on artifact.
    assert ingest_cache.try_import(s, fs, "D1", "FID", "vhash1", PIN,
                                   contextual_retrieval=False) is False
    assert s.get_chunk("gdrive-FID-0") is None
    # Matching CR imports fine.
    assert ingest_cache.try_import(s, fs, "D1", "FID", "vhash1", PIN,
                                   contextual_retrieval=True) is True


def test_publish_stamps_real_contextual_retrieval_flag(tmp_path):
    """publish_file must stamp the artifact with the publisher's REAL CR setting,
    not a hardcoded default, or the guard above compares against a wrong stamp."""
    import gzip, json
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    s.import_cached_chunk("gdrive-FID-0", "hi", "c0",
                          {"source_type": "gdrive", "file_id": "FID", "chunk_index": 0}, [0.1, 0.2, 0.3, 0.4])
    ok = ingest_cache.publish_file(s, fs, "D1", "FID", "vhash1", PIN,
                                   published_by="p@x.org", contextual_retrieval=True)
    assert ok
    fname = artifact_filename("FID", "vhash1", "bge-small", 4, "v1")
    art = CacheArtifact.from_dict(json.loads(gzip.decompress(
        fs.get_bytes(f"{ingest_cache.CACHE_DIR}/{fname}"))))
    assert art.enrich.get("contextual_retrieval") is True


class _RaisingGetBytesFleetStorage:
    """FleetStorage double whose get_bytes() raises a real I/O-style error,
    simulating a Drive API failure (not a None/corrupt-bytes cache miss)."""
    def __init__(self, wrapped):
        self.wrapped = wrapped

    def list_paths(self, prefix):
        return self.wrapped.list_paths(prefix)

    def put_bytes(self, path, data):
        return self.wrapped.put_bytes(path, data)

    def get_bytes(self, path):
        raise ConnectionError("simulated Drive API failure")

    def delete(self, path):
        return self.wrapped.delete(path)


def test_try_import_get_bytes_raising_falls_back_not_raises(tmp_path):
    """_load's fleet_storage.get_bytes() call must be guarded: every other
    fetch in this module treats failure as 'return None, log at info', but an
    unguarded get_bytes() exception would propagate out of try_import and (per
    sync/drive.py's per-drive handler) skip the WHOLE REMAINING FILE LIST for
    that drive that cycle — violating the module's never-raise contract."""
    s = _store(tmp_path)
    real_fs = LocalDirFleetStorage(tmp_path / "drv")
    _write_artifact(real_fs, "FID", "vhash1")
    fs = _RaisingGetBytesFleetStorage(real_fs)
    # must not raise; must fall back to a cache miss (False)
    assert ingest_cache.try_import(s, fs, "D1", "FID", "vhash1", PIN) is False


def test_try_import_miss_returns_false(tmp_path):
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    assert ingest_cache.try_import(s, fs, "D1", "FID", "vhash1", PIN) is False


def test_try_import_unpinned_returns_false(tmp_path):
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    _write_artifact(fs, "FID", "vhash1")
    assert ingest_cache.try_import(s, fs, "D1", "FID", "vhash1", FleetPin()) is False


def test_publish_file_includes_enrich_payload_when_present(tmp_path):
    import gzip, json
    from mcpbrain import ingest_cache
    from mcpbrain.org_contracts import CacheArtifact, artifact_filename
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    s.import_cached_chunk("gdrive-FID-0", "body", "vh1",
                          {"source_type": "gdrive", "file_id": "FID", "chunk_index": 0},
                          [0.1, 0.2, 0.3, 0.4])
    s.set_enrich_payload("gdrive-FID-0",
                         '{"thread_id":"gdrive-FID","org":"Acme","content_type":"reference","summary":"x","entities":[]}',
                         1)  # PIN.enrich_logic_floor == 1
    assert ingest_cache.publish_file(s, fs, "D1", "FID", "vh1", PIN, published_by="p@x.org")
    art = CacheArtifact.from_dict(json.loads(gzip.decompress(
        fs.get_bytes(f"{ingest_cache.CACHE_DIR}/{artifact_filename('FID','vh1','bge-small',4,'v1')}"))))
    assert art.enrich.get("logic_version") == 1
    assert art.enrich.get("extraction", {}).get("org") == "Acme"


def test_publish_file_omits_payload_when_unenriched(tmp_path):
    import gzip, json
    from mcpbrain import ingest_cache
    from mcpbrain.org_contracts import CacheArtifact, artifact_filename
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    s.import_cached_chunk("gdrive-FID-0", "body", "vh1",
                          {"source_type": "gdrive", "file_id": "FID", "chunk_index": 0},
                          [0.1, 0.2, 0.3, 0.4])
    # no set_enrich_payload -> no payload in the artifact
    ingest_cache.publish_file(s, fs, "D1", "FID", "vh1", PIN, published_by="p@x.org")
    art = CacheArtifact.from_dict(json.loads(gzip.decompress(
        fs.get_bytes(f"{ingest_cache.CACHE_DIR}/{artifact_filename('FID','vh1','bge-small',4,'v1')}"))))
    assert "extraction" not in art.enrich


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


def test_publish_is_byte_identical_regardless_of_metadata_key_order(tmp_path):
    """Two publishers whose extractors build metadata dict keys in different
    insertion orders must produce IDENTICAL gzip-decompressed bytes for
    logically-equal content — publish() must serialize with sort_keys so the
    module's documented byte-identical-artifacts guarantee actually holds."""
    import gzip
    from datetime import datetime, timezone
    from unittest.mock import patch

    fs_a = LocalDirFleetStorage(tmp_path / "drv_a")
    fs_b = LocalDirFleetStorage(tmp_path / "drv_b")
    frozen = datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc)

    chunk_a = CacheChunk(idx=0, text="hello", embedding_b64=_b64([0.1, 0.2, 0.3, 0.4]),
                        metadata={"file_id": "FID", "chunk_index": 0, "source_type": "gdrive"})
    chunk_b = CacheChunk(idx=0, text="hello", embedding_b64=_b64([0.1, 0.2, 0.3, 0.4]),
                        metadata={"source_type": "gdrive", "chunk_index": 0, "file_id": "FID"})

    with patch("mcpbrain.ingest_cache.datetime") as mock_dt:
        mock_dt.now.return_value = frozen
        ingest_cache.publish(None, fs_a, "D1", "FID", "vhash1", (chunk_a,), PIN,
                             enrich={"b": 1, "a": 2}, published_by="p@x.org")
        ingest_cache.publish(None, fs_b, "D1", "FID", "vhash1", (chunk_b,), PIN,
                             enrich={"a": 2, "b": 1}, published_by="p@x.org")

    path = ingest_cache._artifact_path("FID", "vhash1", PIN)
    bytes_a = gzip.decompress(fs_a.get_bytes(path))
    bytes_b = gzip.decompress(fs_b.get_bytes(path))
    assert bytes_a == bytes_b


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


def test_publish_stamps_contextual_retrieval_and_matching_flag_imports(tmp_path):
    """publish_file threads contextual_retrieval into the artifact's enrich
    block; try_import with the SAME flag value must succeed."""
    src, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    src.import_cached_chunk("gdrive-FID-0", "hello", "c0",
                            {"file_id": "FID", "chunk_index": 0}, [0.1, 0.2, 0.3, 0.4])
    ok = ingest_cache.publish_file(src, fs, "D1", "FID", "vhash1", PIN,
                                   contextual_retrieval=True)
    assert ok
    dst = _store(tmp_path, "dst.sqlite3")
    assert ingest_cache.try_import(dst, fs, "D1", "FID", "vhash1", PIN,
                                   contextual_retrieval=True) is True


def test_try_import_contextual_retrieval_mismatch_falls_back(tmp_path):
    """A LOCAL install with contextual-retrieval OFF must not import an
    artifact published by an install with it ON — the flag materially
    changes the embedding vector and is not covered by pipeline_fingerprint."""
    src, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    src.import_cached_chunk("gdrive-FID-0", "hello", "c0",
                            {"file_id": "FID", "chunk_index": 0}, [0.1, 0.2, 0.3, 0.4])
    ingest_cache.publish_file(src, fs, "D1", "FID", "vhash1", PIN, contextual_retrieval=True)
    dst = _store(tmp_path, "dst.sqlite3")
    assert ingest_cache.try_import(dst, fs, "D1", "FID", "vhash1", PIN,
                                   contextual_retrieval=False) is False


def test_try_import_contextual_retrieval_default_none_is_backward_compatible(tmp_path):
    """Existing callers that don't pass contextual_retrieval (None = don't
    check) must keep working exactly as before, regardless of what flag value
    the artifact was published with."""
    src, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    src.import_cached_chunk("gdrive-FID-0", "hello", "c0",
                            {"file_id": "FID", "chunk_index": 0}, [0.1, 0.2, 0.3, 0.4])
    ingest_cache.publish_file(src, fs, "D1", "FID", "vhash1", PIN, contextual_retrieval=True)
    dst = _store(tmp_path, "dst.sqlite3")
    assert ingest_cache.try_import(dst, fs, "D1", "FID", "vhash1", PIN) is True


def test_publish_default_contextual_retrieval_is_false_backward_compatible(tmp_path):
    """Existing publish_file callers that don't pass contextual_retrieval must
    keep stamping the same effective default (False) — no behavior change."""
    src, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    src.import_cached_chunk("gdrive-FID-0", "hello", "c0",
                            {"file_id": "FID", "chunk_index": 0}, [0.1, 0.2, 0.3, 0.4])
    assert ingest_cache.publish_file(src, fs, "D1", "FID", "vhash1", PIN) is True
    dst = _store(tmp_path, "dst.sqlite3")
    assert ingest_cache.try_import(dst, fs, "D1", "FID", "vhash1", PIN,
                                   contextual_retrieval=False) is True


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


def test_import_applies_cached_enrichment_payload(tmp_path):
    import gzip, json, base64, struct
    from mcpbrain import ingest_cache
    from mcpbrain.org_contracts import CacheArtifact, CacheChunk, artifact_filename
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    vec = base64.b64encode(struct.pack("<4f", 0.1, 0.2, 0.3, 0.4)).decode()
    extraction = {"thread_id": "gdrive-FID", "org": "Acme", "content_type": "update",
                  "summary": "quarterly plan",
                  "entities": [{"name": "Joel Chelliah", "type": "person"}],
                  "relations": [], "actions": [], "topics": [],
                  "messages": [{"message_id": "gdrive-FID-0", "text": "Joel Chelliah owns the plan"}]}
    art = CacheArtifact(
        file_id="FID", content_hash="vh1", extraction_method="gdocs",
        chunker_version="v1", embed_model="bge-small", dim=4,
        chunks=(CacheChunk(idx=0, text="Joel Chelliah owns the plan", embedding_b64=vec,
                           metadata={"source_type": "gdrive", "file_id": "FID", "chunk_index": 0}),),
        enrich={"logic_version": 1, "extraction": extraction},
        published_by="p@x.org", published_at="2026-07-04")
    fs.put_bytes(f"{ingest_cache.CACHE_DIR}/{artifact_filename('FID','vh1','bge-small',4,'v1')}",
                 gzip.compress(json.dumps(art.to_dict()).encode()))
    assert ingest_cache.try_import(s, fs, "D1", "FID", "vh1", PIN) is True
    # chunk marked enriched (no local re-enrich) AND graph rows applied
    with s._connect() as db:
        r = db.execute("SELECT enriched FROM chunks WHERE doc_id='gdrive-FID-0'").fetchone()
        n_ent = db.execute("SELECT COUNT(*) c FROM entities").fetchone()["c"]
    assert r["enriched"] == 1
    assert n_ent >= 1                    # the payload's entity was applied to the graph
    # idempotent: a second import doesn't error or double-apply
    assert ingest_cache.try_import(s, fs, "D1", "FID", "vh1", PIN) in (True, False)


def test_import_below_floor_payload_falls_back_to_reenrich(tmp_path):
    # logic_version below the floor -> not applied, chunk left unenriched.
    import gzip, json, base64, struct
    from mcpbrain import ingest_cache
    from mcpbrain.org_contracts import CacheArtifact, CacheChunk, artifact_filename
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    vec = base64.b64encode(struct.pack("<4f", 0.1, 0.2, 0.3, 0.4)).decode()
    extraction = {"thread_id": "gdrive-FID", "org": "Acme", "content_type": "update",
                  "summary": "quarterly plan",
                  "entities": [{"name": "Joel Chelliah", "type": "person"}],
                  "relations": [], "actions": [], "topics": [],
                  "messages": [{"message_id": "gdrive-FID-0", "text": "Joel Chelliah owns the plan"}]}
    art = CacheArtifact(
        file_id="FID", content_hash="vh1", extraction_method="gdocs",
        chunker_version="v1", embed_model="bge-small", dim=4,
        chunks=(CacheChunk(idx=0, text="Joel Chelliah owns the plan", embedding_b64=vec,
                           metadata={"source_type": "gdrive", "file_id": "FID", "chunk_index": 0}),),
        enrich={"logic_version": 0, "extraction": extraction},
        published_by="p@x.org", published_at="2026-07-04")
    fs.put_bytes(f"{ingest_cache.CACHE_DIR}/{artifact_filename('FID','vh1','bge-small',4,'v1')}",
                 gzip.compress(json.dumps(art.to_dict()).encode()))
    assert ingest_cache.try_import(s, fs, "D1", "FID", "vh1", PIN) is True
    with s._connect() as db:
        r = db.execute("SELECT enriched FROM chunks WHERE doc_id='gdrive-FID-0'").fetchone()
        n_ent = db.execute("SELECT COUNT(*) c FROM entities").fetchone()["c"]
    assert r["enriched"] == 0
    assert n_ent == 0


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


def test_import_apply_coerces_float_idx_to_int_doc_id(tmp_path):
    """A peer artifact with a non-int idx (valid JSON, e.g. 0.0) must still apply
    against the real chunk doc_id (gdrive-FID-0), not gdrive-FID-0.0."""
    import gzip, json, base64, struct
    from mcpbrain import ingest_cache
    from mcpbrain.org_contracts import CacheArtifact, CacheChunk, artifact_filename
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    vec = base64.b64encode(struct.pack("<4f", 0.1, 0.2, 0.3, 0.4)).decode()
    extraction = {"thread_id": "gdrive-FID", "org": "Acme", "content_type": "update",
                  "summary": "s", "entities": [{"name": "Joel Chelliah", "type": "person"}],
                  "relations": [], "actions": [], "topics": [],
                  "messages": [{"message_id": "gdrive-FID-0", "text": "Joel Chelliah owns it"}]}
    # hand-write the artifact JSON with idx as a float to simulate a peer
    art = CacheArtifact(file_id="FID", content_hash="vh1", extraction_method="gdocs",
        chunker_version="v1", embed_model="bge-small", dim=4,
        chunks=(CacheChunk(idx=0, text="Joel Chelliah owns it", embedding_b64=vec,
                           metadata={"source_type":"gdrive","file_id":"FID","chunk_index":0}),),
        enrich={"logic_version": 1, "extraction": extraction},
        published_by="p@x.org", published_at="2026-07-04")
    d = art.to_dict(); d["chunks"][0]["idx"] = 0.0        # float idx from a peer
    fs.put_bytes(f"{ingest_cache.CACHE_DIR}/{artifact_filename('FID','vh1','bge-small',4,'v1')}",
                 gzip.compress(json.dumps(d).encode()))
    assert ingest_cache.try_import(s, fs, "D1", "FID", "vh1", PIN) is True
    assert s.get_chunk("gdrive-FID-0") is not None        # int doc_id, not 0.0
    with s._connect() as db:
        assert db.execute("SELECT COUNT(*) c FROM entities").fetchone()["c"] >= 1
