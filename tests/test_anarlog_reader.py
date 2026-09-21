import sqlite3
import pytest
from mcpbrain.sync import anarlog


def _make_db(path, *, version="20260909160300", drop_cols=()):
    """Build a real SQLite file shaped like anarlog's app.db."""
    db = sqlite3.connect(str(path))
    db.execute("CREATE TABLE _sqlx_migrations(version TEXT, description TEXT)")
    db.execute("INSERT INTO _sqlx_migrations VALUES(?, 'x')", (version,))
    sess_cols = ["id TEXT PRIMARY KEY", "title TEXT", "updated_at TEXT",
                 "deleted_at TEXT", "started_at TEXT", "event_id TEXT",
                 "series_id TEXT", "external_provider TEXT"]
    sess_cols = [c for c in sess_cols if c.split()[0] not in drop_cols]
    db.execute(f"CREATE TABLE sessions({','.join(sess_cols)})")
    db.execute("CREATE TABLE session_documents(session_id TEXT, kind TEXT, "
               "body TEXT, body_format TEXT, deleted_at TEXT)")
    db.execute("CREATE TABLE transcripts(session_id TEXT, words_json TEXT, "
               "deleted_at TEXT)")
    db.commit()
    return db


def test_schema_status_ok_on_pinned_version(tmp_path):
    p = tmp_path / "app.db"
    _make_db(p).close()
    with anarlog.connect_ro(str(p)) as db:
        ok, version, missing = anarlog.schema_status(db)
    assert ok is True
    assert version == "20260909160300"
    assert missing == []


def test_schema_status_ok_on_new_version_with_valid_columns(tmp_path):
    p = tmp_path / "app.db"
    _make_db(p, version="20270101000000").close()
    with anarlog.connect_ro(str(p)) as db:
        ok, version, missing = anarlog.schema_status(db)
    assert ok is True
    assert version == "20270101000000"


def test_schema_status_reports_missing_column(tmp_path):
    p = tmp_path / "app.db"
    _make_db(p, version="20270101000000", drop_cols=("series_id",)).close()
    with anarlog.connect_ro(str(p)) as db:
        ok, _version, missing = anarlog.schema_status(db)
    assert ok is False
    assert "sessions.series_id" in missing


def test_connect_ro_refuses_writes(tmp_path):
    p = tmp_path / "app.db"
    _make_db(p).close()
    with anarlog.connect_ro(str(p)) as db:
        with pytest.raises(sqlite3.OperationalError):
            db.execute("INSERT INTO sessions(id) VALUES('x')")


def test_changed_sessions_uses_inclusive_boundary(tmp_path):
    p = tmp_path / "app.db"
    db = _make_db(p)
    db.executemany(
        "INSERT INTO sessions(id,title,updated_at,deleted_at) VALUES(?,?,?,NULL)",
        [("a", "A", "2026-09-17T01:00:00Z"), ("b", "B", "2026-09-17T02:00:00Z")])
    db.commit(); db.close()
    with anarlog.connect_ro(str(p)) as conn:
        rows = anarlog.changed_sessions(conn, "2026-09-17T02:00:00Z", 10)
    # >= not >: the boundary row is re-read, never skipped.
    assert [r["id"] for r in rows] == ["b"]


def test_changed_sessions_flags_deleted(tmp_path):
    p = tmp_path / "app.db"
    db = _make_db(p)
    db.execute("INSERT INTO sessions(id,title,updated_at,deleted_at) "
               "VALUES('a','A','2026-09-17T01:00:00Z','2026-09-17T01:00:00Z')")
    db.commit(); db.close()
    with anarlog.connect_ro(str(p)) as conn:
        rows = anarlog.changed_sessions(conn, "", 10)
    assert rows[0]["deleted"] is True
