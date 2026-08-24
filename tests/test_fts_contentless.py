import sqlite3
import pytest
from mcpbrain.store import Store, fts5_supports_contentless


def test_fts_table_is_contentless_when_sqlite_supports_it(tmp_path):
    if not fts5_supports_contentless():
        pytest.skip("SQLite < 3.43")
    p = tmp_path / "b.sqlite3"
    Store(p, dim=4).init()
    db = sqlite3.connect(p)
    sql = db.execute(
        "SELECT sql FROM sqlite_master WHERE name='fts_chunks'").fetchone()[0]
    db.close()
    assert "content=''" in sql
    assert "contentless_delete=1" in sql


def test_contentless_fts_still_ranks_with_bm25(tmp_path):
    if not fts5_supports_contentless():
        pytest.skip("SQLite < 3.43")
    p = tmp_path / "b.sqlite3"
    s = Store(p, dim=4); s.init()
    with s._connect(write=True) as db:
        for i, txt in enumerate(["septic tank byford", "unrelated roster"]):
            db.execute("INSERT INTO chunks(doc_id, text, embedded) VALUES(?,?,1)",
                       (f"d{i}", txt))
            db.execute("INSERT INTO fts_chunks(rowid, text) VALUES(?,?)",
                       (db.execute("SELECT last_insert_rowid()").fetchone()[0], txt))
        rows = db.execute(
            "SELECT rowid, bm25(fts_chunks) FROM fts_chunks "
            "WHERE fts_chunks MATCH 'septic' ORDER BY 2"
        ).fetchall()
    assert len(rows) == 1


def test_deleting_a_chunk_does_not_error_on_contentless_fts(tmp_path):
    """contentless_delete=1 is what makes this legal; without it FTS5 raises."""
    if not fts5_supports_contentless():
        pytest.skip("SQLite < 3.43")
    p = tmp_path / "b.sqlite3"
    s = Store(p, dim=4); s.init()
    with s._connect(write=True) as db:
        db.execute("INSERT INTO chunks(doc_id, text, embedded) VALUES('d1','hello',1)")
        rid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.execute("INSERT INTO fts_chunks(rowid, text) VALUES(?,'hello')", (rid,))
        db.execute("DELETE FROM fts_chunks WHERE rowid=?", (rid,))
