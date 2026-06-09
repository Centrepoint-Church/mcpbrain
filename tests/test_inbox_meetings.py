# tests/test_inbox_meetings.py
import sqlite3
from datetime import datetime, timedelta, timezone

from mcpbrain import dashboard

_PERTH = timezone(timedelta(hours=8))

def _today():
    return datetime.now(_PERTH).date().isoformat()

def _days_ago(n):
    return (datetime.now(_PERTH).date() - timedelta(days=n)).isoformat()

class FakeStore:
    def __init__(self, path):
        self._path = path

def _make_inbox_db(path):
    with sqlite3.connect(str(path)) as db:
        db.execute("""CREATE TABLE email_context(
            message_id TEXT PRIMARY KEY, subject TEXT DEFAULT '',
            sender TEXT DEFAULT '', sender_email TEXT DEFAULT '',
            date_iso TEXT DEFAULT '', org TEXT DEFAULT '',
            content_type TEXT DEFAULT '', summary TEXT DEFAULT '',
            reply_needed INTEGER DEFAULT 0, reply_reason TEXT DEFAULT '')""")
        db.executemany(
            "INSERT INTO email_context VALUES(?,?,?,?,?,?,?,?,?,?)",
            [
                ("m1","Budget Q2","Alice","alice@x.com",_days_ago(1),"Centrepoint","update","Summary A",1,"needs reply"),
                ("m2","Request help","Bob","bob@x.com",_days_ago(2),"ACC","request","Summary B",0,""),
                ("m3","Decision made","Carol","carol@x.com",_days_ago(3),"Centrepoint","decision","Summary C",0,""),
                ("m4","FYI update","Dave","dave@x.com",_days_ago(1),"","fyi","Summary D",0,""),
                ("m5","Old request","Eve","eve@x.com",_days_ago(10),"","request","Summary E",0,""),
            ]
        )

def _make_packs_db(path):
    with sqlite3.connect(str(path)) as db:
        db.execute("""CREATE TABLE meeting_packs(
            event_id TEXT PRIMARY KEY, event_title TEXT,
            event_date TEXT, pack_text TEXT, attendees TEXT,
            built_at TEXT, cowork_session TEXT)""")
        db.execute("INSERT INTO meeting_packs VALUES('evt_packed','Board Meeting','2026-06-06','## Pack','[]','2026-06-06','test')")


class TestInboxToday:
    def test_returns_reply_needed(self, tmp_path):
        db_path = tmp_path / "brain.sqlite3"
        _make_inbox_db(db_path)
        result = dashboard.inbox_today(FakeStore(db_path))
        ids = [r["message_id"] for r in result]
        assert "m1" in ids

    def test_returns_request_type(self, tmp_path):
        db_path = tmp_path / "brain.sqlite3"
        _make_inbox_db(db_path)
        result = dashboard.inbox_today(FakeStore(db_path))
        ids = [r["message_id"] for r in result]
        assert "m2" in ids

    def test_excludes_fyi(self, tmp_path):
        db_path = tmp_path / "brain.sqlite3"
        _make_inbox_db(db_path)
        result = dashboard.inbox_today(FakeStore(db_path))
        ids = [r["message_id"] for r in result]
        assert "m4" not in ids

    def test_excludes_too_old(self, tmp_path):
        db_path = tmp_path / "brain.sqlite3"
        _make_inbox_db(db_path)
        result = dashboard.inbox_today(FakeStore(db_path))
        ids = [r["message_id"] for r in result]
        assert "m5" not in ids

    def test_missing_table_returns_empty(self, tmp_path):
        db_path = tmp_path / "empty.sqlite3"
        with sqlite3.connect(str(db_path)) as db:
            db.execute("CREATE TABLE meta(k TEXT, v TEXT)")
        result = dashboard.inbox_today(FakeStore(db_path))
        assert result == []

    def test_dict_has_expected_keys(self, tmp_path):
        db_path = tmp_path / "brain.sqlite3"
        _make_inbox_db(db_path)
        result = dashboard.inbox_today(FakeStore(db_path))
        assert len(result) > 0
        for item in result:
            assert "message_id" in item
            assert "subject" in item
            assert "sender" in item
            assert "date_iso" in item
            assert "org" in item
            assert "content_type" in item
            assert "summary" in item
            assert "reply_needed" in item


class TestAnnotateMeetingPacks:
    def test_has_pack_true_when_pack_exists(self, tmp_path):
        db_path = tmp_path / "brain.sqlite3"
        _make_packs_db(db_path)
        events = [
            {"id": "evt_packed", "title": "Board Meeting", "start": "09:00",
             "end": "10:00", "all_day": False, "location": "", "has_pack": False},
            {"id": "evt_nope", "title": "Standup", "start": "09:30",
             "end": "09:45", "all_day": False, "location": "", "has_pack": False},
        ]
        result = dashboard.annotate_meeting_packs(FakeStore(db_path), events)
        by_id = {e["id"]: e for e in result}
        assert by_id["evt_packed"]["has_pack"] is True
        assert by_id["evt_nope"]["has_pack"] is False

    def test_empty_events_returns_empty(self, tmp_path):
        db_path = tmp_path / "brain.sqlite3"
        _make_packs_db(db_path)
        result = dashboard.annotate_meeting_packs(FakeStore(db_path), [])
        assert result == []

    def test_degrades_if_no_meeting_packs_table(self, tmp_path):
        db_path = tmp_path / "brain.sqlite3"
        with sqlite3.connect(str(db_path)) as db:
            db.execute("CREATE TABLE meta(k TEXT, v TEXT)")
        events = [{"id": "e1", "title": "T", "start": "", "end": "",
                   "all_day": True, "location": "", "has_pack": False}]
        result = dashboard.annotate_meeting_packs(FakeStore(db_path), events)
        assert result[0]["has_pack"] is False
