import sqlite3

from mcpbrain.store import Store, _meta_extract

# The five expression indexes init() builds over chunks.metadata, and the
# _meta_extract path each one keys on. Kept here so the re-init drift test
# below covers the whole set rather than one hand-picked index.
_EXPR_INDEXES = (
    ("idx_chunks_msgid", "$.message_id"),
    ("idx_chunks_fileid", "$.file_id"),
    ("idx_chunks_eventid", "$.event_id"),
    ("idx_chunks_threadid", "$.thread_id"),
)


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


def test_meta_extract_is_json_extract_on_every_sqlite():
    """_meta_extract MUST emit json_extract regardless of SQLite version.

    Not jsonb_extract, and not version-dependent. `CREATE INDEX IF NOT EXISTS`
    keys on the index NAME, so an already-built index is never replaced when
    this fragment changes — every installed store's five expression indexes are
    built on json_extract, and SQLite does not match a jsonb_extract query
    against a json_extract index. A version-dependent fragment here therefore
    kills those indexes (full SCAN chunks, the 0.7.105 outage) on any machine
    whose SQLite crosses the 3.45 line. See test_reinit_on_existing_store_*.
    """
    frag = _meta_extract("$.message_id")
    assert frag == "json_extract(metadata,'$.message_id')", frag
    assert "jsonb_extract" not in frag


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


def _materialise_legacy_store(tmp_path, *, rows: int = 5000) -> Store:
    """A store as it exists on a real machine BEFORE this branch ships.

    init()'d, populated, and with the five metadata expression indexes built on
    the literal pre-branch expression (`json_extract`) — written out by hand
    rather than via _meta_extract, so this fixture stays pinned to what is
    actually on disk in the field even if _meta_extract is changed again.
    """
    p = tmp_path / "legacy.sqlite3"
    s = Store(str(p), dim=4)
    s.init()
    with s._connect(write=True) as db:
        for name, path in _EXPR_INDEXES:
            db.execute(f"DROP INDEX IF EXISTS {name}")
            db.execute(
                f"CREATE INDEX {name} ON chunks(json_extract(metadata,'{path}'))"
            )
        db.execute("DROP INDEX IF EXISTS idx_chunks_inbound_date")
        db.execute(
            "CREATE INDEX idx_chunks_inbound_date ON chunks(COALESCE("
            "json_extract(metadata,'$.date'),json_extract(metadata,'$.date_iso')))"
        )
        for i in range(rows):
            db.execute(
                "INSERT INTO chunks(doc_id, text, metadata) VALUES(?,?,?)",
                (f"d{i}", "t",
                 '{"message_id":"m%d","thread_id":"t%d","file_id":"f%d",'
                 '"event_id":"e%d","date":"2026-01-01"}' % (i, i, i, i)),
            )
    return s


def _plan(store, sql: str) -> str:
    with store._connect() as db:
        rows = db.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
    return " | ".join(str(cell) for r in rows for cell in tuple(r))


def test_reinit_on_existing_store_keeps_the_expression_indexes_live(tmp_path):
    """Re-running init() on an ALREADY-init'd store must not orphan its indexes.

    The structural gap that let the jsonb regression through: every index test
    built a FRESH store, where the index DDL and the query text necessarily
    agree because both come from the same _meta_extract call in the same
    process. Real machines are the other case — the indexes were created by an
    EARLIER version of the code and `CREATE INDEX IF NOT EXISTS` keys on the
    index NAME, so init() silently leaves the old expression in place while the
    queries move on. Verified: under a jsonb_extract _meta_extract this test
    reports `SCAN chunks` for both message_id and thread_id.
    """
    s = _materialise_legacy_store(tmp_path)

    # The daemon restarts on the new code: init() runs again against the store
    # that already exists. It must not leave a query planning as a full scan.
    s.init()

    for path, index in (("$.message_id", "idx_chunks_msgid"),
                        ("$.thread_id", "idx_chunks_threadid"),
                        ("$.file_id", "idx_chunks_fileid")):
        plan = _plan(
            s, f"SELECT doc_id FROM chunks WHERE {_meta_extract(path)} = 'x'"
        )
        assert "SEARCH" in plan and index in plan, f"{path}: {plan}"
        assert "SCAN chunks" not in plan, f"{path}: {plan}"


def test_reinit_on_existing_store_leaves_index_ddl_unchanged(tmp_path):
    """And the reason it holds: init() does NOT rewrite an existing index.

    Pins the mechanism, so a future change that makes the query text diverge
    from the persisted DDL fails here with an explicit diff rather than as a
    mysterious slowdown in production.
    """
    s = _materialise_legacy_store(tmp_path, rows=10)
    s.init()
    with s._connect() as db:
        ddl = {r[0]: r[1] for r in db.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index' "
            "AND name IN ('idx_chunks_msgid','idx_chunks_fileid',"
            "'idx_chunks_eventid','idx_chunks_threadid','idx_chunks_inbound_date')"
        ).fetchall()}
    assert len(ddl) == 5, ddl
    for name, path in _EXPR_INDEXES:
        assert "jsonb_extract" not in ddl[name], ddl[name]
        assert _meta_extract(path) in ddl[name], (name, ddl[name])
    assert "jsonb_extract" not in ddl["idx_chunks_inbound_date"]


def test_inbound_date_range_uses_the_index_after_reinit(tmp_path):
    """The COALESCE(date,date_iso) range index — the inbound_chunks_since arm.

    Same drift class, but a two-arm COALESCE expression rather than a plain
    path, so it is checked separately.
    """
    s = _materialise_legacy_store(tmp_path)
    s.init()
    expr = f"COALESCE({_meta_extract('$.date')}, {_meta_extract('$.date_iso')})"
    plan = _plan(s, f"SELECT doc_id FROM chunks WHERE {expr} > '2020-01-01'")
    assert "idx_chunks_inbound_date" in plan, plan
    assert "SCAN chunks" not in plan, plan
