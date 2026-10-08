"""The +x<N> extraction_version stamp guard, and the mixed-fleet guarantee.

The +s split stamp (0.7.138) stopped pre-split chunks being laundered into
the +s fingerprint. The +x<N> fingerprint had the same hole: a pending
publish recorded by an older block extractor (chunks stamped
extraction_version 1, or not at all) was published under the CURRENT +x<N>
fingerprint, and a peer imported it as current output.
"""
import base64
import gzip
import json
import struct

from mcpbrain import ingest_cache
from mcpbrain.org_contracts import (DRIVE_ID_META_KEY, CacheArtifact, CacheChunk,
                                    FleetPin, artifact_filename)
from mcpbrain.store import Store
from mcpbrain.sync.blocks import extraction_version
from tests.helpers.org_fleet import LocalDirFleetStorage

PIN = FleetPin(embed_model="bge-small", dim=4, chunker_version="v1",
               enrich_logic_floor=1, fleet_secret="s3cret")
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XV = extraction_version(DOCX)


def _store(tmp_path, name="a.sqlite3"):
    s = Store(tmp_path / name, dim=4)
    s.init()
    return s


def _b64(vec):
    return base64.b64encode(struct.pack(f"<{len(vec)}f", *vec)).decode("ascii")


def _base():
    return ingest_cache.effective_chunker_version(PIN)


def _write_raw_artifact(fs, file_id, content_hash, *, chunker, xv=None,
                        published_at="2026-10-07"):
    meta = {"source_type": "gdrive", "file_id": file_id, "chunk_index": 0,
            "chunk_total": 1, "mime_type": DOCX}
    if xv is not None:
        meta["extraction_version"] = xv
    chunks = (CacheChunk(idx=0, text="docx text", embedding_b64=_b64([0.1, 0.2, 0.3, 0.4]),
                         metadata=meta),)
    art = CacheArtifact(file_id=file_id, content_hash=content_hash,
                        extraction_method="docx", chunker_version=chunker,
                        embed_model="bge-small", dim=4, chunks=chunks,
                        enrich={}, published_by="p@x.org", published_at=published_at)
    fname = artifact_filename(file_id, content_hash, "bge-small", 4, chunker)
    path = f"{ingest_cache.CACHE_DIR}/{fname}"
    fs.put_bytes(path, gzip.compress(json.dumps(art.to_dict()).encode()))
    return path


def _local_docx(store, fid, xv):
    md = {"source_type": "gdrive", "file_id": fid, "chunk_index": 0, "chunk_total": 1,
          DRIVE_ID_META_KEY: "D1", "mime_type": DOCX}
    if xv is not None:
        md["extraction_version"] = xv
    store.import_cached_chunk(f"gdrive-{fid}-0", "docx text", "ch0", md, [0.1, 0.2, 0.3, 0.4])


# -- the predicate ------------------------------------------------------------

def test_stamp_predicate():
    ok = ingest_cache._carries_extraction_stamp
    assert XV >= 2
    assert ok([{"extraction_version": XV}], DOCX) is True
    assert ok([{"extraction_version": XV + 1}], DOCX) is True
    assert ok([{"extraction_version": XV}, {"extraction_version": XV - 1}], DOCX) is False
    assert ok([{}], DOCX) is False
    assert ok([], DOCX) is False
    # A MIME with no EXTRACTION_VERSIONS entry has nothing to prove.
    assert ok([{}], "text/html") is True
    assert ok([], "") is True


def test_malformed_extraction_version_does_not_raise():
    ok = ingest_cache._carries_extraction_stamp
    assert ok([{"extraction_version": "two"}], DOCX) is False
    assert ok([{"extraction_version": [2]}], DOCX) is False
    assert ok(["not a dict"], DOCX) is False
    assert ok([None], DOCX) is False
    assert ok(None, DOCX) is False


# -- write path ---------------------------------------------------------------

def test_old_stamped_pending_publish_is_not_published_under_the_current_x(tmp_path):
    """A v1-stamped docx (recorded before the EXTRACTION_VERSIONS bump) lands
    under the base fingerprint it belongs to, never under +x<current>."""
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    A = _store(tmp_path, "A.sqlite3")
    _local_docx(A, "W1", xv=1)
    assert ingest_cache.publish_file(A, fs, "D1", "W1", "md5a", PIN) is True
    names = [p.rsplit("/", 1)[-1] for p in fs.list_paths(ingest_cache.CACHE_DIR + "/")]
    assert names == [artifact_filename("W1", "md5a", "bge-small", 4, _base())]
    B = _store(tmp_path, "B.sqlite3")
    assert ingest_cache.try_import(B, fs, "D1", "W1", "md5a", PIN, mime=DOCX) is False


def test_unstamped_pending_publish_is_not_published_under_the_current_x(tmp_path):
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    A = _store(tmp_path, "A.sqlite3")
    _local_docx(A, "W1", xv=None)
    assert ingest_cache.publish_file(A, fs, "D1", "W1", "md5a", PIN) is True
    B = _store(tmp_path, "B.sqlite3")
    assert ingest_cache.try_import(B, fs, "D1", "W1", "md5a", PIN, mime=DOCX) is False
    assert ingest_cache.try_import(B, fs, "D1", "W1", "md5a", PIN) is True


def test_correctly_stamped_docx_round_trips(tmp_path):
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    A = _store(tmp_path, "A.sqlite3")
    _local_docx(A, "W1", xv=XV)
    assert ingest_cache.publish_file(A, fs, "D1", "W1", "md5a", PIN) is True
    B = _store(tmp_path, "B.sqlite3")
    assert ingest_cache.try_import(B, fs, "D1", "W1", "md5a", PIN, mime=DOCX) is True
    assert B.get_chunk("gdrive-W1-0")["metadata"]["extraction_version"] == XV


# -- read path ----------------------------------------------------------------

def test_mislabelled_current_x_artifact_is_refused_on_import(tmp_path):
    """Under the +x<current> fingerprint but holding older (or unstamped)
    chunks: a miss, exactly like the unstamped +s refusal."""
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    cur = ingest_cache.effective_chunker_version(PIN, DOCX)
    _write_raw_artifact(fs, "W1", "md5a", chunker=cur, xv=None)
    _write_raw_artifact(fs, "W2", "md5b", chunker=cur, xv=XV - 1)
    _write_raw_artifact(fs, "W3", "md5c", chunker=cur, xv="garbage")
    B = _store(tmp_path, "B.sqlite3")
    for fid, ch in (("W1", "md5a"), ("W2", "md5b"), ("W3", "md5c")):
        assert ingest_cache.try_import(B, fs, "D1", fid, ch, PIN, mime=DOCX) is False
        assert B.get_chunk(f"gdrive-{fid}-0") is None
    summary = ingest_cache.bootstrap_drive(B, fs, "D1", PIN)
    assert summary["imported"] == 0 and summary["skipped"] == 3


def test_stamped_current_x_artifact_imports_and_bootstraps(tmp_path):
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    cur = ingest_cache.effective_chunker_version(PIN, DOCX)
    _write_raw_artifact(fs, "W1", "md5a", chunker=cur, xv=XV)
    B = _store(tmp_path, "B.sqlite3")
    assert ingest_cache.try_import(B, fs, "D1", "W1", "md5a", PIN, mime=DOCX) is True
    C = _store(tmp_path, "C.sqlite3")
    assert ingest_cache.bootstrap_drive(C, fs, "D1", PIN)["imported"] == 1


# -- mixed fleet (M5) ---------------------------------------------------------

def test_older_x_artifact_from_a_lagging_peer_is_left_alone(tmp_path):
    """This install is at extraction v<XV>; a peer still on v<XV-1> code
    publishes +x<XV-1> for the same file. It is the peer's pipeline: never
    GC'd by us, never chosen by bootstrap, never served to try_import."""
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    old = f"{_base()}+x{XV - 1}"
    old_path = _write_raw_artifact(fs, "W1", "md5old", chunker=old, xv=XV - 1)
    # The same content hash too, so only the fingerprint can keep it apart.
    same_path = _write_raw_artifact(fs, "W1", "md5new", chunker=old, xv=XV - 1)

    assert ingest_cache.gc_superseded(fs, "D1", "W1", "md5new", PIN) == 0
    assert ingest_cache.gc_superseded_batch(fs, "D1", {"W1": "md5new"}, PIN) == 0
    assert ingest_cache.gc_superseded_batch(fs, "D1", {"W1": "md5other"}, PIN) == 0
    paths = set(fs.list_paths(ingest_cache.CACHE_DIR + "/"))
    assert old_path in paths and same_path in paths

    B = _store(tmp_path, "B.sqlite3")
    summary = ingest_cache.bootstrap_drive(B, fs, "D1", PIN)
    assert summary["imported"] == 0
    assert B.get_chunk("gdrive-W1-0") is None
    for ch in ("md5old", "md5new"):
        assert ingest_cache.try_import(B, fs, "D1", "W1", ch, PIN, mime=DOCX) is False
    assert B.get_chunk("gdrive-W1-0") is None
