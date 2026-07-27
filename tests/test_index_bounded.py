"""index_pending must respect a limit and a budget, and resume cleanly."""
import json

from mcpbrain.budget import Budget
from mcpbrain.index import index_pending
from mcpbrain.store import Store


class _FakeEmbedder:
    dim = 4

    def embed_passages(self, texts):
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    def embed_query(self, text):
        return [0.1, 0.2, 0.3, 0.4]


def _store_with_pending(tmp_path, n):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    with s._connect(write=True) as db:
        for i in range(n):
            db.execute(
                "INSERT INTO chunks(doc_id,text,content_hash,metadata,embedded,enriched) "
                "VALUES (?,?,?,?,0,0)",
                (f"d-{i}", f"text {i}", f"h{i}", json.dumps({})),
            )
    return s


def test_unembedded_chunks_respects_limit(tmp_path):
    s = _store_with_pending(tmp_path, 50)
    assert len(s.unembedded_chunks(limit=10)) == 10
    assert len(s.unembedded_chunks()) == 50


def test_index_pending_stops_at_expired_budget(tmp_path):
    s = _store_with_pending(tmp_path, 200)
    spent = Budget(deadline_s=0.0)          # already expired
    done = index_pending(s, _FakeEmbedder(), batch_size=32, home=str(tmp_path),
                         budget=spent)
    assert done == 0, "an expired budget must embed nothing"


def test_index_pending_resumes_and_processes_every_item_exactly_once(tmp_path):
    """N bounded slices over a K-item backlog process exactly K items."""
    s = _store_with_pending(tmp_path, 100)
    total = 0
    for _ in range(20):                      # generous slice count
        done = index_pending(s, _FakeEmbedder(), batch_size=10,
                             home=str(tmp_path), max_items=10)
        total += done
        if done == 0:
            break
    assert total == 100
    assert s.unembedded_chunks() == []
