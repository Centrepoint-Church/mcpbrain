import sqlite3

from mcpbrain.store import Store, _meta_extract, jsonb_supported


def test_meta_extract_matches_index_expression(tmp_path):
    """The index and the query MUST use the same function or the index is dead."""
    p = tmp_path / "b.sqlite3"
    Store(str(p), dim=4).init()
    db = sqlite3.connect(p)
    idx = db.execute(
        "SELECT sql FROM sqlite_master WHERE name='idx_chunks_msgid'"
    ).fetchone()[0]
    db.close()
    assert _meta_extract("$.message_id") in idx


def test_message_id_lookup_uses_the_index(tmp_path):
    """SQLite's cost-based planner ties SCAN vs SEARCH at trivial table sizes
    (verified: a 1-row table SCANs even with the index present, identical for
    json_extract and jsonb_extract — a pre-existing PRAGMA optimize/analyze
    characteristic, not a jsonb-specific regression). Insert enough rows that
    the planner's own stats clearly favour the index, matching how the real
    corpus behaves.
    """
    p = tmp_path / "b.sqlite3"
    s = Store(str(p), dim=4)
    s.init()
    with s._connect(write=True) as db:
        for i in range(50):
            db.execute(
                "INSERT INTO chunks(doc_id, text, metadata) VALUES(?,?,?)",
                (f"d{i}", "t", '{"message_id":"m%d"}' % i),
            )
    with s._connect() as db:
        # Store._connect() sets row_factory=sqlite3.Row, whose __str__ does not
        # dump column values — read the actual plan text via tuple(), not str(row).
        plan = db.execute(
            f"EXPLAIN QUERY PLAN SELECT doc_id FROM chunks "
            f"WHERE {_meta_extract('$.message_id')} = 'm1'"
        ).fetchall()
    plan_text = [tuple(r) for r in plan]
    assert any("idx_chunks_msgid" in str(cell) for r in plan_text for cell in r), plan_text


def test_jsonb_supported_matches_runtime_version():
    parts = tuple(int(x) for x in sqlite3.sqlite_version.split(".")[:2])
    assert jsonb_supported() == (parts >= (3, 45))
