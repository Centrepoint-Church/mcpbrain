import json
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
               "created_at TEXT, event_id TEXT, series_id TEXT, "
               "external_provider TEXT)")
    db.execute("CREATE TABLE session_documents(session_id TEXT, kind TEXT, "
               "body TEXT, body_format TEXT, deleted_at TEXT, updated_at TEXT)")
    db.execute("CREATE TABLE transcripts(session_id TEXT, words_json TEXT, "
               "deleted_at TEXT, updated_at TEXT)")
    for s in sessions:
        db.execute("INSERT INTO sessions(id,title,updated_at,deleted_at,"
                   "started_at,event_id,series_id) VALUES(?,?,?,?,?,?,?)",
                   (s["id"], s.get("title", ""), s["updated_at"],
                    s.get("deleted_at"), s.get("started_at", ""),
                    s.get("event_id", ""), s.get("series_id", "")))
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
