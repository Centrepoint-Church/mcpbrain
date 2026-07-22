from mcpbrain import retrieval_expand as rx

def test_parent_key_prefers_thread_then_file_then_doc():
    assert rx.parent_key({"thread_id": "t1", "file_id": "f1"}, "d0") == ("thread", "t1")
    assert rx.parent_key({"file_id": "f1", "chunk_index": 7}, "gdrive-f1-7") == ("file", "f1")
    assert rx.parent_key({}, "note-9") == ("chunk", "note-9")

def test_group_by_parent_orders_by_best_rank_and_collects_indices():
    hits = [
        {"doc_id": "gdrive-f1-5", "score": 1.0, "metadata": {"file_id": "f1", "chunk_index": 5}},
        {"doc_id": "gmail-t1-0", "score": 0.9, "metadata": {"thread_id": "t1"}},
        {"doc_id": "gdrive-f1-6", "score": 0.8, "metadata": {"file_id": "f1", "chunk_index": 6}},
    ]
    groups = rx.group_by_parent(hits)
    assert [(g["kind"], g["key"]) for g in groups] == [("file", "f1"), ("thread", "t1")]
    assert groups[0]["hit_indices"] == [5, 6]
    assert groups[0]["rep_doc_id"] == "gdrive-f1-5"


class _FakeStore:
    def __init__(self, threads=None, files=None):
        self._threads = threads or {}
        self._files = files or {}
    def thread_chunks(self, tid):
        return self._threads.get(tid, [])
    def chunks_for_file(self, fid):
        return self._files.get(fid, [])

def test_expand_thread_stuffs_whole_thread_in_date_order():
    store = _FakeStore(threads={"t1": [
        {"doc_id": "m2", "text": "second", "metadata": {"date": "2026-02-02"}},
        {"doc_id": "m1", "text": "first",  "metadata": {"date": "2026-01-01"}},
    ]})
    g = {"kind": "thread", "key": "t1", "hit_indices": [], "rep_doc_id": "m1"}
    out = rx.expand_parent(store, g, window_n=3, short_doc_max_chunks=15)
    assert out == "first\n\nsecond"

def test_expand_short_file_returns_whole_doc():
    files = {"f1": [{"doc_id": f"gdrive-f1-{i}", "text": f"p{i}",
                     "metadata": {"chunk_index": i}, "idx": i} for i in range(3)]}
    g = {"kind": "file", "key": "f1", "hit_indices": [1], "rep_doc_id": "gdrive-f1-1"}
    out = rx.expand_parent(_FakeStore(files=files), g, window_n=3, short_doc_max_chunks=15)
    assert out == "p0\n\np1\n\np2"

def test_expand_large_file_span_stitches_window_only():
    files = {"f1": [{"doc_id": f"gdrive-f1-{i}", "text": f"p{i}",
                     "metadata": {"chunk_index": i}, "idx": i} for i in range(50)]}
    g = {"kind": "file", "key": "f1", "hit_indices": [10], "rep_doc_id": "gdrive-f1-10"}
    out = rx.expand_parent(_FakeStore(files=files), g, window_n=2, short_doc_max_chunks=15)
    # window ±2 around idx 10 => 8,9,10,11,12
    assert out == "p8\n\np9\n\np10\n\np11\n\np12"
