import json
import logging
import sqlite3
import pytest
from mcpbrain.store import Store
from mcpbrain.sync import anarlog


def _anarlog_db(path, sessions, *, version="20260909160300"):
    db = sqlite3.connect(str(path))
    db.execute("CREATE TABLE _sqlx_migrations(version TEXT, description TEXT)")
    db.execute("INSERT INTO _sqlx_migrations VALUES(?, 'x')", (version,))
    # Columns match the real anarlog schema established in
    # tests/test_anarlog_reader.py (Task 1) and required by anarlog.py's
    # _REQUIRED_COLUMNS / read_session -- the brief's own fixture omitted
    # sessions.created_at, session_documents.updated_at and
    # transcripts.updated_at, which read_session's SQL selects/orders by.
    db.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY, title TEXT, "
               "updated_at TEXT, deleted_at TEXT, started_at TEXT, "
               "created_at TEXT, event_id TEXT, external_event_id TEXT, "
               "series_id TEXT, external_provider TEXT)")
    db.execute("CREATE TABLE session_documents(session_id TEXT, kind TEXT, "
               "body TEXT, body_format TEXT, deleted_at TEXT, updated_at TEXT)")
    db.execute("CREATE TABLE transcripts(session_id TEXT, words_json TEXT, "
               "deleted_at TEXT, updated_at TEXT)")
    for s in sessions:
        db.execute("INSERT INTO sessions(id,title,updated_at,deleted_at,"
                   "started_at,event_id,external_event_id,series_id,"
                   "external_provider) VALUES(?,?,?,?,?,?,?,?,?)",
                   (s["id"], s.get("title", ""), s["updated_at"],
                    s.get("deleted_at"), s.get("started_at", ""),
                    s.get("event_id", ""), s.get("external_event_id", ""),
                    s.get("series_id", ""), s.get("external_provider", "")))
        if s.get("summary"):
            db.execute("INSERT INTO session_documents VALUES(?,?,?,?,NULL,NULL)",
                       (s["id"], "summary", json.dumps({"type": "doc", "content": [
                           {"type": "paragraph", "content": [
                               {"type": "text", "text": s["summary"]}]}]}),
                        "prosemirror_json"))
        if s.get("transcript"):
            db.execute("INSERT INTO transcripts VALUES(?,?,NULL,NULL)",
                       (s["id"], json.dumps([{"id": "w:0",
                                              "text": s["transcript"]}])))
    db.commit(); db.close()


def _store(tmp_path):
    # Store(...) does NOT create its schema; .init() does. Without it the very
    # first query fails with "no such table".
    s = Store(str(tmp_path / "brain.sqlite3"), dim=8)
    s.init()
    return s


def test_discover_enqueues_and_advances_cursor(tmp_path):
    p = tmp_path / "app.db"
    _anarlog_db(p, [{"id": "a", "updated_at": "2026-09-17T01:00:00Z",
                     "summary": "hello"}])
    s = _store(tmp_path)
    n = anarlog.discover_anarlog(s, db_path=str(p))
    assert n == 1
    assert s.get_cursor("anarlog") == "2026-09-17T01:00:00Z"


def test_discover_stops_when_a_required_column_is_missing(tmp_path):
    p = tmp_path / "app.db"
    db = sqlite3.connect(str(p))
    db.execute("CREATE TABLE _sqlx_migrations(version TEXT, description TEXT)")
    db.execute("INSERT INTO _sqlx_migrations VALUES('20270101000000','x')")
    db.execute("CREATE TABLE sessions(id TEXT, updated_at TEXT)")  # missing cols
    db.execute("CREATE TABLE session_documents(session_id TEXT)")
    db.execute("CREATE TABLE transcripts(session_id TEXT)")
    db.commit(); db.close()
    s = _store(tmp_path)
    with pytest.raises(RuntimeError) as exc:
        anarlog.discover_anarlog(s, db_path=str(p))
    assert "sessions." in str(exc.value)


def test_handle_writes_hot_summary_and_cold_transcript(tmp_path):
    p = tmp_path / "app.db"
    _anarlog_db(p, [{"id": "a", "title": "Staff",
                     "updated_at": "2026-09-17T01:00:00Z",
                     "summary": "We agreed.", "transcript": "Spoken words."}])
    s = _store(tmp_path)
    anarlog.handle_anarlog_item(s, {"event": "upsert", "ref_id": "a"},
                                db_path=str(p))
    ids = set(s.doc_ids_for_messages(["anarlog-a"]))
    assert "anarlog-a-summary-0" in ids
    assert "anarlog-a-transcript-0" in ids


def test_handle_remove_deletes_every_chunk_of_the_session(tmp_path):
    p = tmp_path / "app.db"
    _anarlog_db(p, [{"id": "a", "updated_at": "2026-09-17T01:00:00Z",
                     "summary": "We agreed.", "transcript": "Spoken."}])
    s = _store(tmp_path)
    anarlog.handle_anarlog_item(s, {"event": "upsert", "ref_id": "a"},
                                db_path=str(p))
    assert s.doc_ids_for_messages(["anarlog-a"]) != []
    anarlog.handle_anarlog_item(s, {"event": "remove", "ref_id": "a"},
                                db_path=str(p))
    assert s.doc_ids_for_messages(["anarlog-a"]) == []


def test_handle_remove_invalidates_local_relations_sourced_from_the_session(tmp_path):
    """Fix-round-1 root-cause fix: deleting a session must invalidate any
    local relation whose source_doc_id points at one of that session's
    chunks, same pattern drive.py's remove-event handlers already use
    (store.invalidate_local_relations_for_docs before store.delete_chunks).
    Without this, a relation extracted before deletion keeps pointing at a
    doc_id whose chunk row no longer exists -- provenance org_contrib can no
    longer verify, which the org_contrib-side fix now refuses, but the
    relation should never have been left live in the first place."""
    p = tmp_path / "app.db"
    _anarlog_db(p, [{"id": "a", "updated_at": "2026-09-17T01:00:00Z",
                     "summary": "We agreed."}])
    s = _store(tmp_path)
    anarlog.handle_anarlog_item(s, {"event": "upsert", "ref_id": "a"},
                                db_path=str(p))
    doc_id = s.doc_ids_for_messages(["anarlog-a"])[0]
    with s._connect() as db:
        db.execute("INSERT INTO entities(id,name,type,origin) VALUES('x','X','person','local')")
        db.execute("INSERT INTO entities(id,name,type,origin) VALUES('y','Y','org','local')")
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b,source_doc_id,origin) "
                   "VALUES('x','works_at','y',?,'local')", (doc_id,))
    anarlog.handle_anarlog_item(s, {"event": "remove", "ref_id": "a"},
                                db_path=str(p))
    with s._connect() as db:
        r = db.execute(
            "SELECT invalidated_at, superseded_reason FROM entity_relations "
            "WHERE entity_a='x'").fetchone()
    assert r["invalidated_at"] is not None
    # Fix-round-2: reason must be truthful, not the borrowed drive.py default
    # ("drive_revoked" on a meeting deletion would be a lie in an audit column).
    assert r["superseded_reason"] == "anarlog_session_removed"


def test_stale_chunk_sweep_invalidates_local_relations_too(tmp_path):
    """The same fix, on the OTHER delete path: a note that shrinks from 2
    chunks to 1 drops the extra chunk via the stale-chunk sweep inside
    handle_anarlog_item's upsert branch, not via a remove event. A relation
    sourced from the dropped chunk must be invalidated there too.

    Re-syncs the SAME anarlog db (updated in place, not recreated --
    _anarlog_db creates _sqlx_migrations fresh each call and would collide
    with itself on the same path) with the transcript soft-deleted, which is
    exactly how anarlog itself marks a document gone (see
    _REQUIRED_COLUMNS/read_session's deleted_at filtering)."""
    p = tmp_path / "app.db"
    _anarlog_db(p, [{"id": "a", "updated_at": "2026-09-17T01:00:00Z",
                     "summary": "We agreed.", "transcript": "Spoken words."}])
    s = _store(tmp_path)
    anarlog.handle_anarlog_item(s, {"event": "upsert", "ref_id": "a"},
                                db_path=str(p))
    stale_doc_id = "anarlog-a-transcript-0"
    assert stale_doc_id in s.doc_ids_for_messages(["anarlog-a"])
    with s._connect() as db:
        db.execute("INSERT INTO entities(id,name,type,origin) VALUES('x','X','person','local')")
        db.execute("INSERT INTO entities(id,name,type,origin) VALUES('y','Y','org','local')")
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b,source_doc_id,origin) "
                   "VALUES('x','works_at','y',?,'local')", (stale_doc_id,))
    # Soft-delete the transcript in place and bump the session's updated_at
    # (mirrors a real edit) so re-handling shrinks the chunk set and the
    # sweep drops the transcript chunk as stale.
    raw = sqlite3.connect(str(p))
    raw.execute("UPDATE transcripts SET deleted_at=? WHERE session_id='a'",
               ("2026-09-17T02:00:00Z",))
    raw.execute("UPDATE sessions SET updated_at=? WHERE id='a'",
               ("2026-09-17T02:00:00Z",))
    raw.commit(); raw.close()
    anarlog.handle_anarlog_item(s, {"event": "upsert", "ref_id": "a"},
                                db_path=str(p))
    assert stale_doc_id not in s.doc_ids_for_messages(["anarlog-a"])
    with s._connect() as db:
        r = db.execute(
            "SELECT invalidated_at, superseded_reason FROM entity_relations "
            "WHERE entity_a='x'").fetchone()
    assert r["invalidated_at"] is not None
    # Fix-round-2: distinct from the session-removal reason -- the meeting
    # still exists here, only its content shrank.
    assert r["superseded_reason"] == "anarlog_note_shrank"


def test_session_removal_and_stale_sweep_record_different_reasons(tmp_path):
    """The two anarlog delete paths are genuinely different events (the
    meeting went away vs. the meeting shrank) and must stay distinguishable
    to anyone auditing entity_relations.superseded_reason later -- not just
    individually truthful, but truthful AND distinct from each other."""
    p1 = tmp_path / "removed.db"
    _anarlog_db(p1, [{"id": "r", "updated_at": "2026-09-17T01:00:00Z",
                      "summary": "We agreed."}])
    s = _store(tmp_path)
    anarlog.handle_anarlog_item(s, {"event": "upsert", "ref_id": "r"},
                                db_path=str(p1))
    removed_doc_id = s.doc_ids_for_messages(["anarlog-r"])[0]

    p2 = tmp_path / "shrank.db"
    _anarlog_db(p2, [{"id": "k", "updated_at": "2026-09-17T01:00:00Z",
                      "summary": "We agreed.", "transcript": "Spoken words."}])
    anarlog.handle_anarlog_item(s, {"event": "upsert", "ref_id": "k"},
                                db_path=str(p2))
    shrank_doc_id = "anarlog-k-transcript-0"
    assert shrank_doc_id in s.doc_ids_for_messages(["anarlog-k"])

    with s._connect() as db:
        db.execute("INSERT INTO entities(id,name,type,origin) VALUES('x','X','person','local')")
        db.execute("INSERT INTO entities(id,name,type,origin) VALUES('y','Y','org','local')")
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b,source_doc_id,origin) "
                   "VALUES('x','works_at','y',?,'local')", (removed_doc_id,))
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b,source_doc_id,origin) "
                   "VALUES('x','member_of','y',?,'local')", (shrank_doc_id,))

    # Session r is removed entirely.
    anarlog.handle_anarlog_item(s, {"event": "remove", "ref_id": "r"},
                                db_path=str(p1))
    # Session k's transcript is soft-deleted in place; re-handling shrinks it.
    raw = sqlite3.connect(str(p2))
    raw.execute("UPDATE transcripts SET deleted_at=? WHERE session_id='k'",
               ("2026-09-17T02:00:00Z",))
    raw.execute("UPDATE sessions SET updated_at=? WHERE id='k'",
               ("2026-09-17T02:00:00Z",))
    raw.commit(); raw.close()
    anarlog.handle_anarlog_item(s, {"event": "upsert", "ref_id": "k"},
                                db_path=str(p2))

    with s._connect() as db:
        removed_reason = db.execute(
            "SELECT superseded_reason FROM entity_relations WHERE entity_a='x' "
            "AND relation='works_at'").fetchone()["superseded_reason"]
        shrank_reason = db.execute(
            "SELECT superseded_reason FROM entity_relations WHERE entity_a='x' "
            "AND relation='member_of'").fetchone()["superseded_reason"]
    assert removed_reason == "anarlog_session_removed"
    assert shrank_reason == "anarlog_note_shrank"
    assert removed_reason != shrank_reason


def test_reprocessing_the_boundary_row_is_idempotent(tmp_path):
    p = tmp_path / "app.db"
    _anarlog_db(p, [{"id": "a", "updated_at": "2026-09-17T01:00:00Z",
                     "summary": "We agreed."}])
    s = _store(tmp_path)
    anarlog.handle_anarlog_item(s, {"event": "upsert", "ref_id": "a"},
                                db_path=str(p))
    first = sorted(s.doc_ids_for_messages(["anarlog-a"]))
    anarlog.handle_anarlog_item(s, {"event": "upsert", "ref_id": "a"},
                                db_path=str(p))
    assert sorted(s.doc_ids_for_messages(["anarlog-a"])) == first


def test_discover_raises_on_a_full_page_sharing_one_timestamp(tmp_path):
    # A full _DISCOVER_LIMIT page all sharing one updated_at would otherwise
    # advance the cursor to that exact timestamp, then the next cycle would
    # re-query `>= T LIMIT _DISCOVER_LIMIT` and get the SAME rows back --
    # silently starving any row beyond them sharing T, forever. This must
    # raise loudly instead (same failure class as the Drive paging livelock).
    p = tmp_path / "app.db"
    ts = "2026-09-17T01:00:00.000Z"
    sessions = [{"id": f"s{i}", "updated_at": ts, "summary": "x"}
                for i in range(anarlog._DISCOVER_LIMIT)]
    _anarlog_db(p, sessions)
    s = _store(tmp_path)
    with pytest.raises(RuntimeError) as exc:
        anarlog.discover_anarlog(s, db_path=str(p))
    assert ts in str(exc.value)


def test_discover_logs_schema_drift_once_across_two_calls(tmp_path, caplog):
    # _drift_logged is a module-level global with no reset, so a test that
    # exercises it must reset it itself (before AND after) or it becomes
    # order-dependent on whatever ran earlier/later in the suite.
    anarlog._drift_logged = False
    try:
        p = tmp_path / "app.db"
        _anarlog_db(p, [{"id": "a", "updated_at": "2026-09-17T01:00:00Z",
                         "summary": "hi"}], version="20270101000000")
        s = _store(tmp_path)
        with caplog.at_level(logging.INFO, logger="mcpbrain.sync.anarlog"):
            n1 = anarlog.discover_anarlog(s, db_path=str(p))
            # Drift alone must never stop the source -- only a missing
            # column does. Re-reading the same boundary row on the second
            # call is expected (the `>=` boundary), and drift must still log
            # only once across both calls.
            n2 = anarlog.discover_anarlog(s, db_path=str(p))
        assert n1 == 1
        assert n2 == 1
        assert caplog.text.count("differs from pinned") == 1
    finally:
        anarlog._drift_logged = False
