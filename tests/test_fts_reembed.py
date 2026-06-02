"""
Regression test: re-embedding a chunk whose content has changed must not leave
a stale FTS5 row, causing double-counting or ghost hits in BM25 search.

Bug: the old external-content fts_chunks used INSERT without a prior DELETE,
so the second index_pending call for the same rowid appended a second FTS row.
Fix: switch to a self-contained FTS5 table and add DELETE-before-INSERT in
write_embedding.
"""

from mcpbrain.store import Store
from mcpbrain.index import index_pending
from tests.test_retrieval import FakeEmbedder


def test_reembedding_changed_chunk_leaves_one_fts_row(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()

    # First version of the chunk
    s.upsert_chunk("d1", "annual budget review", "h1", {})
    index_pending(s, FakeEmbedder())

    # Content changes -> upsert_chunk resets embedded=0
    s.upsert_chunk("d1", "volunteer roster schedule", "h2", {})
    index_pending(s, FakeEmbedder())

    # New term: exactly one hit for d1 (not two)
    new_hits = [doc for doc, _ in s.fts_search("roster", 10) if doc == "d1"]
    assert len(new_hits) == 1, (
        f"Expected exactly 1 FTS hit for 'roster' on d1, got {len(new_hits)}. "
        "Stale/duplicate FTS row likely present."
    )

    # Old term: gone (stale row removed)
    old_hits = [doc for doc, _ in s.fts_search("budget", 10) if doc == "d1"]
    assert old_hits == [], (
        f"Expected 0 FTS hits for 'budget' on d1 after content change, got {len(old_hits)}. "
        "Stale FTS row not cleaned up."
    )
