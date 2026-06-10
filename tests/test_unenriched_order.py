"""unenriched_chunks returns newest-synced first so backfill enriches recent history first."""
from mcpbrain.store import Store


def test_unenriched_chunks_newest_first(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4, read_only=False); s.init()
    # upsert three chunks in order c1, c2, c3 (ascending rowid = sync order)
    for cid in ("c1", "c2", "c3"):
        s.upsert_chunk(doc_id=f"gmail-{cid}-body-0", text=f"text {cid}",
                       content_hash=f"hash-{cid}", metadata={"thread_id": cid})
    ids = [c["doc_id"] for c in s.unenriched_chunks()]
    # newest-synced (c3) must come first
    assert ids.index("gmail-c3-body-0") < ids.index("gmail-c1-body-0")
