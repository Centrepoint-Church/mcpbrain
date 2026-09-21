import sqlite3
import pytest
from mcpbrain.sync import anarlog


def _make_db(path, *, version="20260909160300", drop_cols=()):
    """Build a real SQLite file shaped like anarlog's app.db."""
    db = sqlite3.connect(str(path))
    db.execute("CREATE TABLE _sqlx_migrations(version TEXT, description TEXT)")
    db.execute("INSERT INTO _sqlx_migrations VALUES(?, 'x')", (version,))
    sess_cols = ["id TEXT PRIMARY KEY", "title TEXT", "updated_at TEXT",
                 "deleted_at TEXT", "started_at TEXT", "created_at TEXT",
                 "event_id TEXT", "external_event_id TEXT", "series_id TEXT",
                 "external_provider TEXT"]
    sess_cols = [c for c in sess_cols if c.split()[0] not in drop_cols]
    db.execute(f"CREATE TABLE sessions({','.join(sess_cols)})")
    db.execute("CREATE TABLE session_documents(session_id TEXT, kind TEXT, "
               "body TEXT, body_format TEXT, deleted_at TEXT, updated_at TEXT)")
    db.execute("CREATE TABLE transcripts(session_id TEXT, words_json TEXT, "
               "deleted_at TEXT, updated_at TEXT)")
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


# --- C1: the calendar event id is external_event_id, not event_id -----------

def _session_row(db, **cols):
    keys = ",".join(cols)
    db.execute(f"INSERT INTO sessions({keys}) VALUES({','.join('?' * len(cols))})",
               tuple(cols.values()))
    db.commit()


def test_read_session_takes_the_google_event_id_from_external_event_id(tmp_path):
    """C1a. `sessions.event_id` is anarlog's OWN foreign key into its `events`
    table (a UUID); the GOOGLE calendar event id — the key behind
    `cal-<event_id>` and meeting_packs — is `external_event_id`. Verified on
    the live DB: event_id 45871b62-9dc5-4dc5-9abf-a740f9be2703 vs
    external_event_id 747cpncv0d4f9mfqkaossch7rr."""
    p = tmp_path / "app.db"
    db = _make_db(p)
    _session_row(db, id="s1", title="Dana Okafor - Leave",
                 started_at="2026-09-07T00:00:00+00:00",
                 event_id="45871b62-9dc5-4dc5-9abf-a740f9be2703",
                 external_event_id="747cpncv0d4f9mfqkaossch7rr",
                 external_provider="google", series_id="")
    db.close()
    with anarlog.connect_ro(str(p)) as conn:
        session = anarlog.read_session(conn, "s1")
    assert session["event_id"] == "747cpncv0d4f9mfqkaossch7rr"
    assert session["anarlog_event_id"] == "45871b62-9dc5-4dc5-9abf-a740f9be2703"


def test_read_session_refuses_a_non_google_external_event_id(tmp_path):
    """M3: external_provider is READ, not merely declared. Both live imported
    meetings carry a GRANOLA uuid in external_event_id; stamping that as
    `event_id` would assert a Google calendar linkage that does not exist."""
    p = tmp_path / "app.db"
    db = _make_db(p)
    _session_row(db, id="s2", title="Northgate Trust Staff Meeting",
                 event_id="", external_event_id="176a93ab-08d9-489e-887d-a2b96b07147b",
                 external_provider="granola", series_id="")
    db.close()
    with anarlog.connect_ro(str(p)) as conn:
        session = anarlog.read_session(conn, "s2")
    assert session["event_id"] == ""
    assert session["external_provider"] == "granola"


def test_read_session_keeps_an_unlabelled_external_event_id(tmp_path):
    """An empty provider must not SILENTLY drop a real linkage — the stamped
    external_provider is what records that it was unverified."""
    p = tmp_path / "app.db"
    db = _make_db(p)
    _session_row(db, id="s3", event_id="", external_event_id="abc123",
                 external_provider="", series_id="")
    db.close()
    with anarlog.connect_ro(str(p)) as conn:
        session = anarlog.read_session(conn, "s3")
    assert session["event_id"] == "abc123"


def test_external_event_id_is_guarded_by_drift_detection(tmp_path):
    """It is read by SQL, so it must be in _REQUIRED_COLUMNS — the whole point
    of that map is that it lists exactly what read_session depends on."""
    assert "external_event_id" in anarlog._REQUIRED_COLUMNS["sessions"]
    p = tmp_path / "app.db"
    _make_db(p, version="20270101000000",
             drop_cols=("external_event_id",)).close()
    with anarlog.connect_ro(str(p)) as db:
        ok, _version, missing = anarlog.schema_status(db)
    assert ok is False
    assert "sessions.external_event_id" in missing
