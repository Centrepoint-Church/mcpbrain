import gzip
import json
import struct
import tempfile
from pathlib import Path

from mcpbrain import ingest_cache
from mcpbrain.chunking import CHUNKER_VERSION
from mcpbrain.org_contracts import FleetPin, artifact_filename
from mcpbrain.store import Store
from tests.helpers.org_fleet import LocalDirFleetStorage

# chunker_version matches the code's own constant, which is what a caught-up org
# pin carries. A pin that LAGS the code is a real case with its own behaviour
# (ingest_cache floors it — see
# test_a_pin_lagging_the_code_does_not_key_artifacts_on_the_stale_version), but
# the fixture for every other test here should not be silently exercising it:
# the tests below build expected artifact filenames from PIN directly, so a
# lagging fixture would make them assert against a fingerprint the module never
# uses.
PIN = FleetPin(embed_model="bge-small", dim=4, chunker_version=str(CHUNKER_VERSION),
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


def _import_one_chunk_artifact(store, *, file_id, text, content_hash="v1"):
    """Build a one-chunk artifact for `file_id`/`text` and import it straight
    into `store`. Thin wrapper over the same publish/try_import round-trip
    test_publish_then_import_roundtrips already exercises (seed a throwaway
    source store, publish_file it to a throwaway fleet dir, try_import into
    `store`) rather than a second, hand-rolled artifact-construction path."""
    with tempfile.TemporaryDirectory() as d:
        src = _store(Path(d), "src.sqlite3")
        fs = LocalDirFleetStorage(Path(d) / "drv")
        src.import_cached_chunk(
            f"gdrive-{file_id}-0", text, "c0",
            {"source_type": "gdrive", "file_id": file_id, "chunk_index": 0},
            [0.0, 0.0, 0.0, 0.0])
        assert ingest_cache.publish_file(src, fs, "D1", file_id, content_hash, PIN,
                                         published_by="test")
        assert ingest_cache.try_import(store, fs, "D1", file_id, content_hash, PIN)


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
    # must NOT be deleted (version-skew guarantee: pipelines coexist)
    stale = artifact_filename("FID", "vhash1", "old-model", 4, "v1")
    cur = artifact_filename("FID", "vhash1", PIN.embed_model, PIN.dim, PIN.chunker_version)
    fs.put_bytes(f"{ingest_cache.CACHE_DIR}/{stale}", b"x")
    fs.put_bytes(f"{ingest_cache.CACHE_DIR}/{cur}", b"y")
    removed = ingest_cache.gc_superseded(fs, "D1", "FID", "vhash1", PIN)
    # gc_superseded only removes same-pipeline artifacts with stale content hashes;
    # stale-pipeline artifacts coexist (see spec A2 version-skew guarantee)
    assert removed == 0
    remaining = sorted([p.rsplit("/", 1)[-1] for p in fs.list_paths(ingest_cache.CACHE_DIR + "/")])
    assert remaining == sorted([stale, cur])


class ListPathsSpyFleetStorage:
    """FleetStorage wrapper that counts list_paths() calls, to prove
    gc_superseded_batch lists the cache folder exactly once per call
    regardless of how many files are in the batch."""
    def __init__(self, wrapped):
        self.wrapped = wrapped
        self.list_paths_calls = 0

    def list_paths(self, prefix):
        self.list_paths_calls += 1
        return self.wrapped.list_paths(prefix)

    def put_bytes(self, path, data):
        return self.wrapped.put_bytes(path, data)

    def get_bytes(self, path):
        return self.wrapped.get_bytes(path)

    def delete(self, path):
        return self.wrapped.delete(path)


def test_gc_superseded_batch_lists_cache_folder_once_across_many_files(tmp_path):
    """Seed several files' worth of artifacts (current, stale-hash, and
    stale-pipeline), call gc_superseded_batch once with a keep_map covering
    ALL of them, and assert: (1) list_paths is called exactly once (not once
    per file — the O(n^2) bug this replaces), (2) the correct stale artifacts
    across the whole batch are removed, (3) current + other-pipeline artifacts
    survive."""
    real_fs = LocalDirFleetStorage(tmp_path / "drv")
    fs = ListPathsSpyFleetStorage(real_fs)

    # File FID: one stale content-hash version + one current version.
    fid_stale = artifact_filename("FID", "vOLD", PIN.embed_model, PIN.dim, PIN.chunker_version)
    fid_cur = artifact_filename("FID", "vNEW", PIN.embed_model, PIN.dim, PIN.chunker_version)
    real_fs.put_bytes(f"{ingest_cache.CACHE_DIR}/{fid_stale}", b"old")
    real_fs.put_bytes(f"{ingest_cache.CACHE_DIR}/{fid_cur}", b"new")

    # File GID: one stale content-hash version + one current version.
    gid_stale = artifact_filename("GID", "vOLD", PIN.embed_model, PIN.dim, PIN.chunker_version)
    gid_cur = artifact_filename("GID", "vNEW", PIN.embed_model, PIN.dim, PIN.chunker_version)
    real_fs.put_bytes(f"{ingest_cache.CACHE_DIR}/{gid_stale}", b"old")
    real_fs.put_bytes(f"{ingest_cache.CACHE_DIR}/{gid_cur}", b"new")

    # File HID: a stale-pipeline artifact (different embed model) must survive
    # even though its content hash differs from keep_map — other pipelines coexist.
    hid_other_pipeline = artifact_filename("HID", "vOLD", "old-model", PIN.dim, PIN.chunker_version)
    real_fs.put_bytes(f"{ingest_cache.CACHE_DIR}/{hid_other_pipeline}", b"other-pipeline")

    keep_map = {"FID": "vNEW", "GID": "vNEW"}
    removed = ingest_cache.gc_superseded_batch(fs, "D1", keep_map, PIN)

    assert fs.list_paths_calls == 1
    assert removed == 2

    remaining = {p.rsplit("/", 1)[-1] for p in real_fs.list_paths(ingest_cache.CACHE_DIR + "/")}
    assert fid_stale not in remaining
    assert gid_stale not in remaining
    assert fid_cur in remaining
    assert gid_cur in remaining
    assert hid_other_pipeline in remaining   # other-pipeline artifact survives


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


def test_gc_superseded_skips_unparseable_filename(tmp_path):
    """Unparseable filename in cache dir should not crash gc_superseded;
    it should be silently skipped and not affect the count."""
    fs = LocalDirFleetStorage(tmp_path / "drv")
    # Plant a valid artifact for FID
    valid = artifact_filename("FID", "vhash1", PIN.embed_model, PIN.dim, PIN.chunker_version)
    fs.put_bytes(f"{ingest_cache.CACHE_DIR}/{valid}", b"valid")
    # Plant an unparseable/stray filename that doesn't match the pattern
    fs.put_bytes(f"{ingest_cache.CACHE_DIR}/totally-invalid-name.txt", b"garbage")
    # gc_superseded should not crash, should skip the unparseable file,
    # and should still remove stale artifacts for FID if any exist
    removed = ingest_cache.gc_superseded(fs, "D1", "FID", "vhash1", PIN)
    # With one valid artifact (kept) and no stale ones, count should be 0
    assert removed == 0
    # The valid artifact should still exist
    remaining = fs.list_paths(ingest_cache.CACHE_DIR + "/")
    assert len(remaining) == 2  # valid + unparseable (unparseable is not deleted by gc)
    # The garbage file should still be there (not deleted by gc_superseded)
    assert any("totally-invalid" in p for p in remaining)


def test_sweep_drive_skips_unparseable_filename(tmp_path):
    """sweep_drive should skip unparseable filenames without crashing."""
    fs = LocalDirFleetStorage(tmp_path / "drv")
    # Plant valid artifacts for A, B
    for fid in ("A", "B"):
        fs.put_bytes(f"{ingest_cache.CACHE_DIR}/{artifact_filename(fid, 'v1', PIN.embed_model, PIN.dim, PIN.chunker_version)}", b"x")
    # Plant an unparseable filename
    fs.put_bytes(f"{ingest_cache.CACHE_DIR}/garbage.xyz", b"junk")
    # sweep_drive with only A as live should delete B but skip the garbage file
    removed = ingest_cache.sweep_drive(fs, {"A"})
    assert removed == 1  # only B deleted, garbage not counted
    remaining = fs.list_paths(ingest_cache.CACHE_DIR + "/")
    assert len(remaining) == 2  # A + garbage


class FailingFleetStorage:
    """Test double for FleetStorage that raises on delete() for a specific path."""
    def __init__(self, wrapped, fail_on_path=None):
        self.wrapped = wrapped
        self.fail_on_path = fail_on_path

    def list_paths(self, prefix):
        return self.wrapped.list_paths(prefix)

    def put_bytes(self, path, data):
        return self.wrapped.put_bytes(path, data)

    def get_bytes(self, path):
        return self.wrapped.get_bytes(path)

    def delete(self, path):
        if self.fail_on_path and path == self.fail_on_path:
            raise RuntimeError(f"Simulated delete failure for {path}")
        return self.wrapped.delete(path)


def test_gc_superseded_handles_delete_failure(tmp_path):
    """When delete() raises, gc_superseded should catch it, skip the item,
    and return count of only successfully deleted items."""
    real_fs = LocalDirFleetStorage(tmp_path / "drv")
    # Plant two stale artifacts for FID that will be deleted
    stale1 = artifact_filename("FID", "vOLD1", PIN.embed_model, PIN.dim, PIN.chunker_version)
    stale2 = artifact_filename("FID", "vOLD2", PIN.embed_model, PIN.dim, PIN.chunker_version)
    cur = artifact_filename("FID", "vNEW", PIN.embed_model, PIN.dim, PIN.chunker_version)
    real_fs.put_bytes(f"{ingest_cache.CACHE_DIR}/{stale1}", b"old1")
    real_fs.put_bytes(f"{ingest_cache.CACHE_DIR}/{stale2}", b"old2")
    real_fs.put_bytes(f"{ingest_cache.CACHE_DIR}/{cur}", b"new")

    # Wrap it to fail on stale1 deletion
    fail_path = f"{ingest_cache.CACHE_DIR}/{stale1}"
    fs = FailingFleetStorage(real_fs, fail_on_path=fail_path)

    # gc_superseded should not raise even though one delete fails
    removed = ingest_cache.gc_superseded(fs, "D1", "FID", "vNEW", PIN)
    # Should only count stale2 as removed (stale1 failed)
    assert removed == 1

    # stale2 should be gone, stale1 should still exist
    remaining = {p.rsplit('/', 1)[-1] for p in real_fs.list_paths(ingest_cache.CACHE_DIR + "/")}
    assert stale1 in remaining
    assert stale2 not in remaining
    assert cur in remaining


def test_sweep_drive_handles_delete_failure(tmp_path):
    """sweep_drive should handle delete() failures gracefully."""
    real_fs = LocalDirFleetStorage(tmp_path / "drv")
    for fid in ("A", "B", "C"):
        fs_path = f"{ingest_cache.CACHE_DIR}/{artifact_filename(fid, 'v1', PIN.embed_model, PIN.dim, PIN.chunker_version)}"
        real_fs.put_bytes(fs_path, b"x")

    # Wrap to fail on C's deletion
    fail_path = f"{ingest_cache.CACHE_DIR}/{artifact_filename('C', 'v1', PIN.embed_model, PIN.dim, PIN.chunker_version)}"
    fs = FailingFleetStorage(real_fs, fail_on_path=fail_path)

    # sweep with only A as live should delete B and C; C's delete will fail, B will succeed
    removed = ingest_cache.sweep_drive(fs, {"A"})
    # Should count only B as removed (C's delete failed)
    assert removed == 1

    remaining = {p.rsplit('.', 4)[0].rsplit('/', 1)[-1] for p in real_fs.list_paths(ingest_cache.CACHE_DIR + "/")}
    assert "A" in remaining
    assert "B" not in remaining  # B was deleted
    assert "C" in remaining  # C still exists because delete failed


def test_remove_file_artifacts_handles_delete_failure(tmp_path):
    """remove_file_artifacts should handle delete() failures gracefully."""
    real_fs = LocalDirFleetStorage(tmp_path / "drv")
    # Plant two artifacts for FID
    a1 = artifact_filename("FID", "v1", PIN.embed_model, PIN.dim, PIN.chunker_version)
    a2 = artifact_filename("FID", "v2", PIN.embed_model, PIN.dim, PIN.chunker_version)
    real_fs.put_bytes(f"{ingest_cache.CACHE_DIR}/{a1}", b"x")
    real_fs.put_bytes(f"{ingest_cache.CACHE_DIR}/{a2}", b"y")

    # Wrap to fail on a1's deletion
    fail_path = f"{ingest_cache.CACHE_DIR}/{a1}"
    fs = FailingFleetStorage(real_fs, fail_on_path=fail_path)

    # remove_file_artifacts should not raise
    removed = ingest_cache.remove_file_artifacts(fs, "FID")
    # Should count only a2 (a1's delete failed)
    assert removed == 1

    remaining = {p.rsplit('/', 1)[-1] for p in real_fs.list_paths(ingest_cache.CACHE_DIR + "/")}
    assert a1 in remaining
    assert a2 not in remaining


def test_a_cache_import_deletes_chunks_the_artifact_no_longer_has(tmp_path):
    """B5's remaining half. Spec 2 closed orphan-on-shrink for locally-extracted
    files (upsert_file_chunks) but deliberately skipped the cache-import path to
    avoid racing try_import's own transaction. So a shared-drive file that shrank
    and is served from cache still leaves indices n..m-1 searchable forever, and
    re-fed to expansion as current content.

    The sweep belongs INSIDE _import_artifact's transaction — the same one that
    writes the replacement rows — so there is no race to avoid."""
    from mcpbrain.store import Store

    store = Store(tmp_path / "b.sqlite3", dim=4)
    store.init()
    for i in range(4):
        store.upsert_chunk(f"gdrive-f1-{i}", f"old para {i}", f"h{i}",
                           {"source_type": "gdrive", "file_id": "f1",
                            "chunk_index": i})

    # Build a 1-chunk artifact for the same file and import it. Use this module's
    # existing artifact-construction helper rather than hand-rolling a second one.
    _import_one_chunk_artifact(store, file_id="f1", text="only para now")

    assert sorted(store.doc_ids_for_file("f1")) == ["gdrive-f1-0"], (
        "the cache import left the shrunk file's tail chunks behind"
    )


def test_a_cache_import_of_the_same_size_deletes_nothing(tmp_path):
    from mcpbrain.store import Store

    store = Store(tmp_path / "b.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("gdrive-f1-0", "para", "h0",
                       {"source_type": "gdrive", "file_id": "f1", "chunk_index": 0})

    _import_one_chunk_artifact(store, file_id="f1", text="para updated")

    assert sorted(store.doc_ids_for_file("f1")) == ["gdrive-f1-0"]


def test_a_pin_lagging_the_code_does_not_key_artifacts_on_the_stale_version(tmp_path):
    """A local chunker bump must invalidate stale fleet cache artifacts on its
    own. pipeline_fingerprint is keyed off the FLEET-DISTRIBUTED pin, and
    config.fleet_pin's code-version default only applies when the key is ABSENT
    — the live pin sets it explicitly ('v1'), so bumping CHUNKER_VERSION alone
    changed nothing and installs kept importing pre-bump artifacts silently.
    ingest_cache floors the pin at the local constant instead."""
    stale_pin = FleetPin(embed_model="bge-small", dim=4, chunker_version="v1",
                         enrich_logic_floor=1, fleet_secret="s3cret")

    assert ingest_cache.effective_chunker_version(stale_pin) == str(CHUNKER_VERSION)
    # ...so the artifact path is the CURRENT chunker's, not the stale pin's.
    assert ingest_cache._artifact_path("FID", "vhash1", stale_pin) != (
        f"{ingest_cache.CACHE_DIR}/"
        f"{artifact_filename('FID', 'vhash1', 'bge-small', 4, 'v1')}")
    assert ingest_cache._artifact_path("FID", "vhash1", stale_pin) == (
        f"{ingest_cache.CACHE_DIR}/"
        f"{artifact_filename('FID', 'vhash1', 'bge-small', 4, str(CHUNKER_VERSION))}")

    # An artifact published by a pre-bump install (stamped + filed under the old
    # chunker version) is not importable by this install at all: not at the
    # stale path (never looked at) and not through the gate either.
    src, fs = _store(tmp_path, "src.sqlite3"), LocalDirFleetStorage(tmp_path / "drv")
    _seed_file(src, "FID", n=1)
    old_art = ingest_cache.CacheArtifact(
        file_id="FID", content_hash="vhash1", extraction_method="gdocs",
        chunker_version="v1", embed_model="bge-small", dim=4,
        chunks=(ingest_cache.CacheChunk(
            idx=0, text="text 0",
            embedding_b64=ingest_cache._encode_vec([0.0, 0.0, 0.0, 0.0]),
            metadata={"source_type": "gdrive", "file_id": "FID", "chunk_index": 0}),),
        enrich={"logic_version": 1}, published_by="old@x.org",
        published_at="2026-07-01")
    stale_path = (f"{ingest_cache.CACHE_DIR}/"
                  f"{artifact_filename('FID', 'vhash1', 'bge-small', 4, 'v1')}")
    fs.put_bytes(stale_path, gzip.compress(
        json.dumps(old_art.to_dict()).encode("utf-8")))

    dst = _store(tmp_path, "dst.sqlite3")
    assert ingest_cache.try_import(dst, fs, "D1", "FID", "vhash1", stale_pin) is False
    assert ingest_cache._import_artifact(dst, "D1", old_art, stale_pin) is False, (
        "an artifact built by the superseded chunker must not pass the gate"
    )
    assert dst.doc_ids_for_file("FID") == []

    # And a fresh publish under the same lagging pin is importable — the floored
    # version is what gets stamped, so installs on this code agree with each other.
    assert ingest_cache.publish_file(src, fs, "D1", "FID", "vhash1", stale_pin,
                                     published_by="me@x.org")
    assert ingest_cache.try_import(dst, fs, "D1", "FID", "vhash1", stale_pin) is True
    assert dst.doc_ids_for_file("FID") == ["gdrive-FID-0"]
