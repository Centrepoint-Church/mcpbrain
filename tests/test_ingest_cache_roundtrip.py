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
