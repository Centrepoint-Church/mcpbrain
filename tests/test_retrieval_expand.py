from mcpbrain import retrieval_expand as rx
from mcpbrain import config as _config

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


def test_expand_large_file_span_stitch_inserts_gap_marker_for_noncontiguous_runs():
    # Two hit indices far apart produce two disjoint windows; a gap marker must
    # separate the runs so the stitched text isn't presented as contiguous.
    files = {"f1": [{"doc_id": f"gdrive-f1-{i}", "text": f"p{i}",
                     "metadata": {"chunk_index": i}, "idx": i} for i in range(50)]}
    g = {"kind": "file", "key": "f1", "hit_indices": [5, 40], "rep_doc_id": "gdrive-f1-5"}
    out = rx.expand_parent(_FakeStore(files=files), g, window_n=1, short_doc_max_chunks=15)
    # window ±1 around 5 => 4,5,6; around 40 => 39,40,41; disjoint -> gap marker
    assert out == "p4\n\np5\n\np6\n\n[…]\n\np39\n\np40\n\np41"
    assert "[…]" in out


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
    out = rx.expand_hits(store, hits, max_parents=2, char_budget=10_000)
    assert len(out) == 2
    assert {h["doc_id"] for h in out} == {"gdrive-fa-0", "gdrive-fb-0"}


def test_expand_hits_respects_char_budget_dropping_lowest_rank():
    chunks, files = {}, {}
    hits = []
    for i, fid in enumerate(["fa", "fb"]):
        doc = f"gdrive-{fid}-0"
        meta = {"file_id": fid, "chunk_index": 0}
        big = "x" * 3000
        chunks[doc] = {"doc_id": doc, "text": big, "metadata": meta, "memory_tier": ""}
        files[fid] = [{"doc_id": doc, "text": big, "metadata": meta, "idx": 0}]
        hits.append({"doc_id": doc, "score": 1.0 - i, "distance": 0.1, "text": big})
    store = _StoreWithMeta(chunks, files=files)
    # budget 4000 chars: first parent (3000) fits; second (3000 more) would push
    # used to 6000 > budget, so it's dropped entirely (lower-ranked parent).
    out = rx.expand_hits(store, hits, max_parents=5, char_budget=4000)
    assert [h["doc_id"] for h in out] == ["gdrive-fa-0"]


def test_expand_hits_truncates_huge_first_parent_to_char_budget():
    # Regression: expand_hits used to admit the FIRST accumulated parent whole
    # regardless of size (the budget check was skipped for an empty
    # accumulator) — a single huge result (e.g. 27k chars) could reach the
    # consumer. The first parent must now be bound to char_budget too.
    chunks, files = {}, {}
    doc = "gdrive-fa-0"
    meta = {"file_id": "fa", "chunk_index": 0}
    huge = "y" * 27_000
    chunks[doc] = {"doc_id": doc, "text": huge, "metadata": meta, "memory_tier": ""}
    files["fa"] = [{"doc_id": doc, "text": huge, "metadata": meta, "idx": 0}]
    hits = [{"doc_id": doc, "score": 1.0, "distance": 0.1, "text": huge}]
    store = _StoreWithMeta(chunks, files=files)
    out = rx.expand_hits(store, hits, max_parents=5, char_budget=4000)
    assert len(out) == 1
    assert len(out[0]["text"]) == 4000


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
    out = rx.expand_hits(store, hits, max_parents=5, char_budget=10_000)
    # _head_tail puts even-index items (0,2,4→fa,fc,fe) in head, odd (1,3→fb,fd) in tail reversed
    # Expected: head + tail[::-1] = [fa,fc,fe,fd,fb]
    assert [h["doc_id"] for h in out] == [
        "gdrive-fa-0", "gdrive-fc-0", "gdrive-fe-0", "gdrive-fd-0", "gdrive-fb-0"
    ]


def test_maybe_expand_passthrough_when_expand_false(monkeypatch, tmp_path):
    monkeypatch.setattr(_config, "retrieval_expand_enabled", lambda home: True)
    hits = [{"doc_id": "d1", "score": 1.0, "distance": 0.1, "text": "x"}]
    assert rx.maybe_expand(_StoreWithMeta({}), hits, home=str(tmp_path), expand=False) is hits


def test_maybe_expand_passthrough_when_flag_off(monkeypatch, tmp_path):
    monkeypatch.setattr(_config, "retrieval_expand_enabled", lambda home: False)
    hits = [{"doc_id": "d1", "score": 1.0, "distance": 0.1, "text": "x"}]
    assert rx.maybe_expand(_StoreWithMeta({}), hits, home=str(tmp_path), expand=True) is hits


def test_maybe_expand_stitches_when_both_on(monkeypatch, tmp_path):
    monkeypatch.setattr(_config, "retrieval_expand_enabled", lambda home: True)
    doc = "gdrive-f1-0"
    meta = {"file_id": "f1", "chunk_index": 0}
    chunks = {doc: {"doc_id": doc, "text": "page0", "metadata": meta, "memory_tier": ""}}
    files = {"f1": [{"doc_id": doc, "text": "page0", "metadata": meta, "idx": 0}]}
    store = _StoreWithMeta(chunks, files=files)
    hits = [{"doc_id": doc, "score": 1.0, "distance": 0.1, "text": "page0"}]
    out = rx.maybe_expand(store, hits, home=str(tmp_path), expand=True)
    assert out and out[0]["doc_id"] == doc  # went through expand_hits (grouped by file)


def test_config_retrieval_expand_defaults_off(tmp_path):
    assert _config.retrieval_expand_enabled(str(tmp_path)) is False
    assert _config.expand_params(str(tmp_path)) == {
        "window_n": 3, "short_doc_max_chunks": 15, "max_parents": 5, "char_budget": 4000}
