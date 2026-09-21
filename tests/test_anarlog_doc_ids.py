from mcpbrain.store import Store


def _store(tmp_path):
    # Store(...) does NOT create its schema; .init() does. Without it the very
    # first query fails with "no such table".
    s = Store(str(tmp_path / "brain.sqlite3"), dim=8)
    s.init()
    return s


def test_resolves_all_subtypes_of_one_session(tmp_path):
    s = _store(tmp_path)
    meta = {"source_type": "anarlog", "session_id": "sess-1"}
    s.upsert_chunk("anarlog-sess-1-summary-0", "sum", "h1",
                   {**meta, "content_subtype": "summary"})
    s.upsert_chunk("anarlog-sess-1-note-0", "note", "h2",
                   {**meta, "content_subtype": "note"})
    s.upsert_chunk("anarlog-sess-1-transcript-0", "tx", "h3",
                   {**meta, "content_subtype": "transcript"})
    s.upsert_chunk("anarlog-sess-2-summary-0", "other", "h4",
                   {"source_type": "anarlog", "session_id": "sess-2"})

    got = set(s.doc_ids_for_messages(["anarlog-sess-1"]))
    assert got == {"anarlog-sess-1-summary-0", "anarlog-sess-1-note-0",
                   "anarlog-sess-1-transcript-0"}


def test_unprefixed_id_does_not_match_a_session(tmp_path):
    s = _store(tmp_path)
    s.upsert_chunk("anarlog-sess-1-summary-0", "sum", "h1",
                   {"source_type": "anarlog", "session_id": "sess-1"})
    # A bare session id must not resolve: the anarlog identity is always
    # prefixed, exactly like the calendar arm.
    assert s.doc_ids_for_messages(["sess-1"]) == []


def test_session_arm_uses_the_index_on_an_existing_store(tmp_path):
    """Re-init an ALREADY-initialised store, then check the plan.

    Mirrors test_metadata_jsonb's re-init test: CREATE INDEX IF NOT EXISTS keys
    on the index NAME, so a fresh-store test would pass even if the DDL and the
    query expression had drifted apart.
    """
    path = str(tmp_path / "brain.sqlite3")
    Store(path, dim=8).init()   # first init creates the index
    s = Store(path, dim=8)
    s.init()                    # re-init on an ALREADY-initialised store
    sql = s._doc_ids_query(1)
    with s._connect() as db:
        plan = "\n".join(
            str(r[3]) for r in db.execute(f"EXPLAIN QUERY PLAN {sql}",
                                          ["x", "x", "x", "x", "x"]).fetchall())
    assert "idx_chunks_sessionid" in plan
    assert "SCAN chunks" not in plan
