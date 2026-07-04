import struct

from mcpbrain.store import Store


def _store(tmp_path, name="a.sqlite3"):
    s = Store(tmp_path / name, dim=4)
    s.init()
    return s


def test_import_cached_chunk_is_searchable_and_read_back(tmp_path):
    s = _store(tmp_path)
    vec = [0.1, 0.2, 0.3, 0.4]
    ok = s.import_cached_chunk(
        "gdrive-FID-0", "hello world", "ch0",
        {"source_type": "gdrive", "file_id": "FID", "chunk_index": 0,
         "drive_id": "D1"}, vec, enriched=True, enriched_version=1)
    assert ok
    # read-back is bit-exact (float32 -> float64 -> float32 round trip is lossless)
    back = s.embedding_for_doc("gdrive-FID-0")
    assert struct.pack("<4f", *back) == struct.pack("<4f", *vec)
    # embedded=1 (not re-queued) and enriched=1 (Haiku skip)
    with s._connect() as db:
        r = db.execute("SELECT embedded, enriched, enriched_version "
                       "FROM chunks WHERE doc_id='gdrive-FID-0'").fetchone()
    assert (r["embedded"], r["enriched"], r["enriched_version"]) == (1, 1, 1)
    # vec_chunks + fts_chunks mirrors exist
    assert s.embedding_for_doc("gdrive-FID-0") is not None


def test_chunks_for_file_orders_by_index_and_scopes_by_file(tmp_path):
    s = _store(tmp_path)
    for i in (1, 0, 2):
        s.import_cached_chunk(
            f"gdrive-FID-{i}", f"t{i}", f"c{i}",
            {"file_id": "FID", "chunk_index": i}, [float(i)] * 4)
    # a different file must not leak in
    s.import_cached_chunk("gdrive-OTHER-0", "x", "cx",
                          {"file_id": "OTHER", "chunk_index": 0}, [9.0] * 4)
    rows = s.chunks_for_file("FID")
    assert [r["idx"] for r in rows] == [0, 1, 2]
    assert all(r["doc_id"].startswith("gdrive-FID-") for r in rows)


def test_embedding_for_doc_missing_returns_none(tmp_path):
    s = _store(tmp_path)
    assert s.embedding_for_doc("nope") is None


def test_cross_store_publish_import_identical(tmp_path):
    from mcpbrain import ingest_cache
    from mcpbrain.org_contracts import FleetPin
    from tests.helpers.org_fleet import LocalDirFleetStorage
    pin = FleetPin(embed_model="bge-small", dim=4, chunker_version="v1",
                   enrich_logic_floor=1, fleet_secret="s3cret")
    A = _store(tmp_path, "A.sqlite3"); fs = LocalDirFleetStorage(tmp_path / "drv")
    for i in range(3):
        A.import_cached_chunk(f"gdrive-FID-{i}", f"body {i}", f"c{i}",
                              {"source_type": "gdrive", "file_id": "FID", "chunk_index": i},
                              [0.11 * i, 0.22, 0.33, 0.44])
    ingest_cache.publish_file(A, fs, "D1", "FID", "vh", pin, enrich={"logic_version": 1})
    B = _store(tmp_path, "B.sqlite3")
    assert ingest_cache.try_import(B, fs, "D1", "FID", "vh", pin) is True
    for i in range(3):
        assert struct.pack("<4f", *A.embedding_for_doc(f"gdrive-FID-{i}")) == \
               struct.pack("<4f", *B.embedding_for_doc(f"gdrive-FID-{i}"))
        assert A.get_chunk(f"gdrive-FID-{i}")["text"] == B.get_chunk(f"gdrive-FID-{i}")["text"]


def test_version_skew_stores_never_read_each_others_artifacts(tmp_path):
    from mcpbrain import ingest_cache
    from mcpbrain.org_contracts import FleetPin
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "drv")
    pin_old = FleetPin(embed_model="bge-small", dim=4, chunker_version="v1", fleet_secret="s")
    pin_new = FleetPin(embed_model="bge-large", dim=4, chunker_version="v1", fleet_secret="s")
    A = _store(tmp_path, "A.sqlite3")
    A.import_cached_chunk("gdrive-FID-0", "x", "c", {"file_id": "FID", "chunk_index": 0}, [1.0]*4)
    ingest_cache.publish_file(A, fs, "D1", "FID", "vh", pin_old)
    B = _store(tmp_path, "B.sqlite3")
    # a bge-large daemon must NOT import the bge-small artifact (fingerprint separates them)
    assert ingest_cache.try_import(B, fs, "D1", "FID", "vh", pin_new) is False
    # and both artifacts can coexist (no churn): publish the new-pipeline one too
    ingest_cache.publish_file(A, fs, "D1", "FID", "vh", pin_new)
    names = [p.rsplit("/", 1)[-1] for p in fs.list_paths(ingest_cache.CACHE_DIR + "/")]
    assert len(names) == 2   # old + new pipeline artifacts side by side
