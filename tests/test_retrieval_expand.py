from mcpbrain import retrieval_expand as rx


def test_retrieval_expand_defaults_off(tmp_path):
    from mcpbrain import config
    assert config.retrieval_expand_enabled(str(tmp_path)) is False
    p = config.expand_params(str(tmp_path))
    assert p == {"window_n": 3, "short_doc_max_chunks": 15,
                 "max_parents": 5, "token_budget": 6000}


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


class _StoreWithMeta(_FakeStore):
    def __init__(self, chunks, **kw):
        super().__init__(**kw)
        self._chunks = chunks
    def get_chunk(self, doc_id):
        return self._chunks.get(doc_id)

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


def test_expand_hits_caps_parents_and_orders_head_tail():
    # 3 distinct short files; max_parents=2 keeps the top 2 by rank
    chunks, files = {}, {}
    hits = []
    for i, fid in enumerate(["fa", "fb", "fc"]):
        doc = f"gdrive-{fid}-0"
        meta = {"file_id": fid, "chunk_index": 0}
        chunks[doc] = {"doc_id": doc, "text": fid, "metadata": meta, "memory_tier": ""}
        files[fid] = [{"doc_id": doc, "text": fid, "metadata": meta, "idx": 0}]
        hits.append({"doc_id": doc, "score": 1.0 - i * 0.1, "distance": 0.1, "text": fid})
    store = _StoreWithMeta(chunks, files=files)
    out = rx.expand_hits(store, hits, max_parents=2, token_budget=10_000)
    assert len(out) == 2
    assert {h["doc_id"] for h in out} == {"gdrive-fa-0", "gdrive-fb-0"}


def test_expand_hits_respects_token_budget_dropping_lowest_rank():
    chunks, files = {}, {}
    hits = []
    for i, fid in enumerate(["fa", "fb"]):
        doc = f"gdrive-{fid}-0"
        meta = {"file_id": fid, "chunk_index": 0}
        big = "x" * 400
        chunks[doc] = {"doc_id": doc, "text": big, "metadata": meta, "memory_tier": ""}
        files[fid] = [{"doc_id": doc, "text": big, "metadata": meta, "idx": 0}]
        hits.append({"doc_id": doc, "score": 1.0 - i, "distance": 0.1, "text": big})
    store = _StoreWithMeta(chunks, files=files)
    # budget ~100 tokens ≈ 400 chars: only the top parent fits
    out = rx.expand_hits(store, hits, max_parents=5, token_budget=100)
    assert [h["doc_id"] for h in out] == ["gdrive-fa-0"]


def test_expand_hits_orders_head_tail_with_five_parents():
    # Test head-tail reordering with 5 distinct files (≥3 items triggers reordering)
    chunks, files = {}, {}
    hits = []
    for i, fid in enumerate(["fa", "fb", "fc", "fd", "fe"]):
        doc = f"gdrive-{fid}-0"
        meta = {"file_id": fid, "chunk_index": 0}
        chunks[doc] = {"doc_id": doc, "text": fid, "metadata": meta, "memory_tier": ""}
        files[fid] = [{"doc_id": doc, "text": fid, "metadata": meta, "idx": 0}]
        hits.append({"doc_id": doc, "score": 1.0 - i * 0.1, "distance": 0.1, "text": fid})
    store = _StoreWithMeta(chunks, files=files)
    out = rx.expand_hits(store, hits, max_parents=5, token_budget=10_000)
    # _head_tail puts even-index items (0,2,4→fa,fc,fe) in head, odd (1,3→fb,fd) in tail reversed
    # Expected: head + tail[::-1] = [fa,fc,fe,fd,fb]
    assert [h["doc_id"] for h in out] == [
        "gdrive-fa-0", "gdrive-fc-0", "gdrive-fe-0", "gdrive-fd-0", "gdrive-fb-0"
    ]
