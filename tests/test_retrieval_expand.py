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
