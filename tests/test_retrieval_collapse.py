"""One result per document (MaxP) — spec 2026-10-07 §3.2."""
import json

from mcpbrain.retrieval import _collapse_documents, _document_key, hybrid_search
from mcpbrain.store import Store


def _hit(doc_id, score, **meta):
    return {"doc_id": doc_id, "score": score, "metadata": meta}


def test_drive_chunks_of_one_file_share_a_key():
    a = _hit("gdrive-F1-0", 1.0, file_id="F1")
    b = _hit("gdrive-F1-3", 0.5, file_id="F1")
    assert _document_key(a) == _document_key(b)


def test_digest_groups_with_its_own_drive_file():
    raw = _hit("gdrive-F1-0", 1.0, file_id="F1")
    digest = _hit("enriched-F1", 0.9, file_id="F1")
    assert _document_key(raw) == _document_key(digest)


def test_gmail_groups_by_message_not_thread():
    m1 = _hit("gmail-M1-body-0", 1.0, thread_id="T1", message_id="M1")
    m2 = _hit("gmail-M2-body-0", 0.9, thread_id="T1", message_id="M2")
    assert _document_key(m1) != _document_key(m2)


def test_legacy_string_metadata_and_missing_file_id_do_not_raise():
    h = {"doc_id": "gdrive-F9-2", "score": 1.0, "metadata": json.dumps({"x": 1})}
    assert _document_key(h) == "gdrive-F9"
    assert _document_key({"doc_id": "note-abc", "score": 1.0}) == "note-abc"


def test_collapse_keeps_best_chunk_and_counts_the_rest():
    hits = [_hit("gdrive-F1-2", 1.0, file_id="F1"), _hit("gdrive-F2-0", 0.8, file_id="F2"),
            _hit("gdrive-F1-0", 0.6, file_id="F1")]
    out = _collapse_documents(hits)
    assert [h["doc_id"] for h in out] == ["gdrive-F1-2", "gdrive-F2-0"]
    assert [h["doc_hits"] for h in out] == [2, 1]


def test_collapse_when_every_hit_is_one_document():
    hits = [_hit(f"gdrive-F1-{i}", 1.0 - i / 10, file_id="F1") for i in range(5)]
    out = _collapse_documents(hits)
    assert len(out) == 1 and out[0]["doc_hits"] == 5


class _Emb:
    dim = 4

    def embed_passages(self, texts):
        return [[1.0, 0, 0, 0] if "budget" in t else [0, 1.0, 0, 0] for t in texts]

    def embed_query(self, text):
        return [1.0, 0, 0, 0] if "budget" in text else [0, 1.0, 0, 0]


def _seed(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    for i in range(4):
        s.upsert_chunk(f"gdrive-F1-{i}", f"budget section {i}", f"h1{i}", {"file_id": "F1"})
    s.upsert_chunk("gdrive-F2-0", "budget summary", "h2", {"file_id": "F2"})
    from mcpbrain.index import index_pending
    index_pending(s, _Emb())
    return s


def test_hybrid_search_collapse_returns_one_hit_per_document(tmp_path):
    s = _seed(tmp_path)
    out = hybrid_search(s, _Emb(), "budget", limit=5, collapse_documents=True)
    files = [h["metadata"]["file_id"] for h in out]
    assert sorted(files) == ["F1", "F2"]
    assert {h["metadata"]["file_id"]: h["doc_hits"] for h in out}["F1"] == 4


def test_hybrid_search_without_collapse_is_unchanged(tmp_path):
    s = _seed(tmp_path)
    out = hybrid_search(s, _Emb(), "budget", limit=5)
    assert len(out) == 5 and all("doc_hits" not in h for h in out)


def test_flag_defaults_on_and_honours_local_kill_switch(tmp_path):
    from mcpbrain import config
    assert config.retrieval_collapse_documents_enabled(str(tmp_path)) is True
    config.write_config(str(tmp_path), {"retrieval_collapse_documents": False})
    assert config.retrieval_collapse_documents_enabled(str(tmp_path)) is False


# -- digest-vs-raw horizon under the deep collapse pool (M1) -----------------

def _pool_with_sibling_at(pos, n=60):
    """A #1 digest for thread T, its only raw sibling at index `pos`, and
    unrelated filler everywhere else."""
    hits = [_hit("enriched-T", 1.0, thread_id="T")]
    for i in range(1, n):
        hits.append(_hit(f"gmail-X{i}-body-0", 1.0 - i / 100, thread_id=f"X{i}"))
    hits[pos] = _hit("gmail-T1-body-0", 1.0 - pos / 100, thread_id="T")
    return hits


def test_digest_survives_when_its_only_raw_sibling_is_beyond_limit_x2():
    from mcpbrain.retrieval import _dedupe_by_cluster
    hits = _pool_with_sibling_at(40)              # limit 10 -> horizon 20
    out = _dedupe_by_cluster(hits, limit=10)
    assert out[0]["doc_id"] == "enriched-T"
    # Collapse off (no limit) keeps today's whole-pool behaviour.
    assert _dedupe_by_cluster(hits)[0]["doc_id"] != "enriched-T"


def test_digest_is_still_dropped_when_its_raw_sibling_is_inside_limit_x2():
    from mcpbrain.retrieval import _dedupe_by_cluster
    hits = _pool_with_sibling_at(19)              # last position inside the horizon
    out = _dedupe_by_cluster(hits, limit=10)
    assert "enriched-T" not in [h["doc_id"] for h in out]
    assert "gmail-T1-body-0" in [h["doc_id"] for h in out]


def test_hybrid_search_threads_the_horizon_only_when_collapsing(tmp_path, monkeypatch):
    from mcpbrain import retrieval
    seen = []
    real = retrieval._dedupe_by_cluster

    def spy(hits, limit=None):
        seen.append(limit)
        return real(hits, limit)

    monkeypatch.setattr(retrieval, "_dedupe_by_cluster", spy)
    s = _seed(tmp_path)
    hybrid_search(s, _Emb(), "budget", limit=5, collapse_documents=True)
    hybrid_search(s, _Emb(), "budget", limit=5)
    assert seen == [5, None]
