"""Tests for mcpbrain.dashboard — data layer for the local dashboard."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


from mcpbrain import dashboard

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PERTH = timezone(timedelta(hours=8))


def _today() -> str:
    return datetime.now(_PERTH).date().isoformat()


def _yesterday() -> str:
    d = datetime.now(_PERTH).date() - timedelta(days=1)
    return d.isoformat()


def _tomorrow() -> str:
    d = datetime.now(_PERTH).date() + timedelta(days=1)
    return d.isoformat()


def _make_db(path: Path) -> None:
    """Seed a minimal actions table in a temp SQLite DB."""
    with sqlite3.connect(str(path)) as db:
        db.execute("""
            CREATE TABLE actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                owner TEXT DEFAULT '',
                status TEXT DEFAULT 'open',
                deadline TEXT DEFAULT '',
                org TEXT DEFAULT '',
                project_id TEXT DEFAULT '',
                action_type TEXT DEFAULT 'next',
                waiting_on TEXT,
                source TEXT DEFAULT 'email',
                resolved_by TEXT DEFAULT '',
                resolved_at TEXT DEFAULT '',
                updated_at TEXT DEFAULT ''
            )
        """)
        today = _today()
        yesterday = _yesterday()
        tomorrow = _tomorrow()

        db.executemany(
            "INSERT INTO actions(text, status, deadline, org, action_type, waiting_on, source) "
            "VALUES(?,?,?,?,?,?,?)",
            [
                # overdue
                ("Overdue task A", "open", yesterday, "Acme", "next", None, "email"),
                # due today
                ("Due today task B", "open", today, "ACC", "scheduled", None, "manual"),
                # upcoming with deadline
                ("Upcoming task C", "open", tomorrow, "", "next", None, "email"),
                # upcoming without deadline (no deadline = upcoming)
                ("No deadline task D", "open", "", "", "next", None, "email"),
                # blocked (waiting_on set) — also overdue
                ("Blocked overdue task E", "open", yesterday, "", "next", "Alice", "email"),
                # done — should NOT appear
                ("Done task F", "done", today, "", "next", None, "email"),
                # blocked with no deadline (upcoming + blocked)
                ("Blocked no-dl task G", "open", "", "", "next", "Bob", "email"),
            ],
        )


class FakeStore:
    """Minimal store stub with a _path attribute."""

    def __init__(self, path: Path):
        self._path = path


# ---------------------------------------------------------------------------
# actions_today
# ---------------------------------------------------------------------------

class TestActionsTodayBucketing:
    def test_overdue_contains_past_deadline(self, tmp_path):
        db_path = tmp_path / "brain.sqlite3"
        _make_db(db_path)
        store = FakeStore(db_path)
        result = dashboard.actions_today(store)
        texts = [a["text"] for a in result["overdue"]]
        assert "Overdue task A" in texts
        assert "Blocked overdue task E" in texts

    def test_due_today_contains_today_deadline(self, tmp_path):
        db_path = tmp_path / "brain.sqlite3"
        _make_db(db_path)
        store = FakeStore(db_path)
        result = dashboard.actions_today(store)
        texts = [a["text"] for a in result["due_today"]]
        assert "Due today task B" in texts

    def test_upcoming_contains_future_and_empty_deadline(self, tmp_path):
        db_path = tmp_path / "brain.sqlite3"
        _make_db(db_path)
        store = FakeStore(db_path)
        result = dashboard.actions_today(store)
        texts = [a["text"] for a in result["upcoming"]]
        assert "Upcoming task C" in texts
        assert "No deadline task D" in texts
        assert "Blocked no-dl task G" in texts

    def test_blocked_contains_waiting_on_actions(self, tmp_path):
        db_path = tmp_path / "brain.sqlite3"
        _make_db(db_path)
        store = FakeStore(db_path)
        result = dashboard.actions_today(store)
        texts = [a["text"] for a in result["blocked"]]
        assert "Blocked overdue task E" in texts
        assert "Blocked no-dl task G" in texts

    def test_done_actions_excluded_from_all_buckets(self, tmp_path):
        db_path = tmp_path / "brain.sqlite3"
        _make_db(db_path)
        store = FakeStore(db_path)
        result = dashboard.actions_today(store)
        all_actions = (
            result["overdue"] + result["due_today"] +
            result["upcoming"] + result["blocked"]
        )
        texts = [a["text"] for a in all_actions]
        assert "Done task F" not in texts

    def test_blocked_can_overlap_with_overdue(self, tmp_path):
        db_path = tmp_path / "brain.sqlite3"
        _make_db(db_path)
        store = FakeStore(db_path)
        result = dashboard.actions_today(store)
        # "Blocked overdue task E" is in both overdue and blocked
        overdue_texts = [a["text"] for a in result["overdue"]]
        blocked_texts = [a["text"] for a in result["blocked"]]
        assert "Blocked overdue task E" in overdue_texts
        assert "Blocked overdue task E" in blocked_texts

    def test_action_dict_shape(self, tmp_path):
        db_path = tmp_path / "brain.sqlite3"
        _make_db(db_path)
        store = FakeStore(db_path)
        result = dashboard.actions_today(store)
        for bucket in result.values():
            for action in bucket:
                assert set(action.keys()) == {
                    "id", "text", "deadline", "org", "project_id",
                    "action_type", "waiting_on", "source",
                }

    def test_overdue_sorted_oldest_first(self, tmp_path):
        db_path = tmp_path / "brain.sqlite3"
        two_days_ago = (datetime.now(_PERTH).date() - timedelta(days=2)).isoformat()
        with sqlite3.connect(str(db_path)) as db:
            db.execute("""
                CREATE TABLE actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT, owner TEXT DEFAULT '', status TEXT DEFAULT 'open',
                    deadline TEXT DEFAULT '', org TEXT DEFAULT '',
                    project_id TEXT DEFAULT '', action_type TEXT DEFAULT 'next',
                    waiting_on TEXT, source TEXT DEFAULT 'email',
                    resolved_at TEXT DEFAULT '', updated_at TEXT DEFAULT ''
                )
            """)
            db.execute(
                "INSERT INTO actions(text, status, deadline) VALUES(?,?,?)",
                ("Older", "open", two_days_ago)
            )
            db.execute(
                "INSERT INTO actions(text, status, deadline) VALUES(?,?,?)",
                ("Less old", "open", _yesterday())
            )
        store = FakeStore(db_path)
        result = dashboard.actions_today(store)
        overdue = result["overdue"]
        assert len(overdue) == 2
        assert overdue[0]["text"] == "Older"

    def test_upcoming_no_deadline_sorted_last(self, tmp_path):
        db_path = tmp_path / "brain.sqlite3"
        with sqlite3.connect(str(db_path)) as db:
            db.execute("""
                CREATE TABLE actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT, owner TEXT DEFAULT '', status TEXT DEFAULT 'open',
                    deadline TEXT DEFAULT '', org TEXT DEFAULT '',
                    project_id TEXT DEFAULT '', action_type TEXT DEFAULT 'next',
                    waiting_on TEXT, source TEXT DEFAULT 'email',
                    resolved_at TEXT DEFAULT '', updated_at TEXT DEFAULT ''
                )
            """)
            db.execute(
                "INSERT INTO actions(text, status, deadline) VALUES(?,?,?)",
                ("No deadline", "open", "")
            )
            db.execute(
                "INSERT INTO actions(text, status, deadline) VALUES(?,?,?)",
                ("Has deadline", "open", _tomorrow())
            )
        store = FakeStore(db_path)
        result = dashboard.actions_today(store)
        upcoming = result["upcoming"]
        assert len(upcoming) == 2
        assert upcoming[0]["text"] == "Has deadline"
        assert upcoming[1]["text"] == "No deadline"

    def test_missing_table_returns_empty_buckets(self, tmp_path):
        """If the actions table doesn't exist, degrade gracefully."""
        db_path = tmp_path / "empty.sqlite3"
        with sqlite3.connect(str(db_path)) as db:
            db.execute("CREATE TABLE meta(k TEXT, v TEXT)")
        store = FakeStore(db_path)
        result = dashboard.actions_today(store)
        assert result == {"overdue": [], "due_today": [], "upcoming": [], "blocked": []}

    def test_action_type_fallback_no_column(self, tmp_path):
        """If action_type column doesn't exist, action_type defaults to 'next'."""
        db_path = tmp_path / "no_at.sqlite3"
        with sqlite3.connect(str(db_path)) as db:
            # Table without action_type column
            db.execute("""
                CREATE TABLE actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT, owner TEXT DEFAULT '', status TEXT DEFAULT 'open',
                    deadline TEXT DEFAULT '', org TEXT DEFAULT '',
                    project_id TEXT DEFAULT '',
                    waiting_on TEXT, source TEXT DEFAULT 'email',
                    resolved_at TEXT DEFAULT '', updated_at TEXT DEFAULT ''
                )
            """)
            db.execute(
                "INSERT INTO actions(text, status, deadline) VALUES(?,?,?)",
                ("Task without action_type", "open", _tomorrow())
            )
        store = FakeStore(db_path)
        result = dashboard.actions_today(store)
        upcoming = result["upcoming"]
        assert len(upcoming) == 1
        assert upcoming[0]["action_type"] == "next"


# ---------------------------------------------------------------------------
# calendar_today
# ---------------------------------------------------------------------------

class TestCalendarTodayNoService:
    def test_no_calendar_service_returns_empty(self, tmp_path):
        with mock.patch("mcpbrain.auth.build_google_services", return_value={}):
            result = dashboard.calendar_today(str(tmp_path))
        assert result == []

    def test_credential_error_returns_empty(self, tmp_path):
        with mock.patch(
            "mcpbrain.auth.build_google_services",
            side_effect=RuntimeError("No token"),
        ):
            result = dashboard.calendar_today(str(tmp_path))
        assert result == []


class TestCalendarTodayWithEvents:
    def _make_service(self, items: list) -> mock.MagicMock:
        service = mock.MagicMock()
        service.events.return_value.list.return_value.execute.return_value = {
            "items": items
        }
        return service

    def test_timed_event_parsed(self, tmp_path):
        today = datetime.now(_PERTH).date()
        # 09:00 Perth time = 01:00 UTC
        start_dt = datetime(today.year, today.month, today.day, 9, 0, 0,
                            tzinfo=_PERTH)
        end_dt = datetime(today.year, today.month, today.day, 10, 0, 0,
                          tzinfo=_PERTH)

        items = [{
            "id": "evt1",
            "summary": "Morning meeting",
            "start": {"dateTime": start_dt.isoformat()},
            "end": {"dateTime": end_dt.isoformat()},
            "location": "Room A",
        }]
        svc = self._make_service(items)

        with mock.patch("mcpbrain.auth.build_google_services",
                        return_value={"calendar_service": svc}):
            result = dashboard.calendar_today(str(tmp_path))

        assert len(result) == 1
        evt = result[0]
        assert evt["id"] == "evt1"
        assert evt["title"] == "Morning meeting"
        assert evt["start"] == "09:00"
        assert evt["end"] == "10:00"
        assert evt["all_day"] is False
        assert evt["location"] == "Room A"
        assert evt["has_pack"] is False

    def test_all_day_event_parsed(self, tmp_path):
        today = datetime.now(_PERTH).date()

        items = [{
            "id": "evt2",
            "summary": "Public Holiday",
            "start": {"date": today.isoformat()},
            "end": {"date": today.isoformat()},
        }]
        svc = self._make_service(items)

        with mock.patch("mcpbrain.auth.build_google_services",
                        return_value={"calendar_service": svc}):
            result = dashboard.calendar_today(str(tmp_path))

        assert len(result) == 1
        evt = result[0]
        assert evt["all_day"] is True
        assert evt["start"] == ""
        assert evt["end"] == ""
        assert evt["has_pack"] is False

    def test_attendees_extracted_excluding_self_and_resources(self, tmp_path):
        today = datetime.now(_PERTH).date()
        start_dt = datetime(today.year, today.month, today.day, 9, 0, 0,
                            tzinfo=_PERTH)
        end_dt = datetime(today.year, today.month, today.day, 10, 0, 0,
                          tzinfo=_PERTH)
        items = [{
            "id": "evt3",
            "summary": "1:1",
            "start": {"dateTime": start_dt.isoformat()},
            "end": {"dateTime": end_dt.isoformat()},
            "attendees": [
                {"email": "alice@x.com", "displayName": "Alice"},
                {"email": "bob@x.com"},  # no displayName -> falls back to email
                {"email": "room@resource.calendar.google.com", "resource": True},
                {"email": "sam@x.com", "self": True, "displayName": "Sam"},
            ],
        }]
        svc = self._make_service(items)
        with mock.patch("mcpbrain.auth.build_google_services",
                        return_value={"calendar_service": svc}):
            result = dashboard.calendar_today(str(tmp_path))
        # Resources (rooms) and the owner (self) are excluded; displayName wins,
        # email is the fallback.
        assert result[0]["attendees"] == ["Alice", "bob@x.com"]

    def test_no_attendees_defaults_to_empty_list(self, tmp_path):
        today = datetime.now(_PERTH).date()
        start_dt = datetime(today.year, today.month, today.day, 9, 0, 0,
                            tzinfo=_PERTH)
        end_dt = datetime(today.year, today.month, today.day, 10, 0, 0,
                          tzinfo=_PERTH)
        items = [{
            "id": "evt4",
            "summary": "Solo block",
            "start": {"dateTime": start_dt.isoformat()},
            "end": {"dateTime": end_dt.isoformat()},
        }]
        svc = self._make_service(items)
        with mock.patch("mcpbrain.auth.build_google_services",
                        return_value={"calendar_service": svc}):
            result = dashboard.calendar_today(str(tmp_path))
        assert result[0]["attendees"] == []

    def test_mixed_timed_and_all_day(self, tmp_path):
        today = datetime.now(_PERTH).date()
        start_dt = datetime(today.year, today.month, today.day, 14, 30, 0,
                            tzinfo=_PERTH)
        end_dt = datetime(today.year, today.month, today.day, 15, 0, 0,
                          tzinfo=_PERTH)

        items = [
            {
                "id": "timed",
                "summary": "Timed event",
                "start": {"dateTime": start_dt.isoformat()},
                "end": {"dateTime": end_dt.isoformat()},
            },
            {
                "id": "allday",
                "summary": "All day event",
                "start": {"date": today.isoformat()},
                "end": {"date": today.isoformat()},
            },
        ]
        svc = self._make_service(items)

        with mock.patch("mcpbrain.auth.build_google_services",
                        return_value={"calendar_service": svc}):
            result = dashboard.calendar_today(str(tmp_path))

        assert len(result) == 2
        timed = next(e for e in result if e["id"] == "timed")
        allday = next(e for e in result if e["id"] == "allday")
        assert timed["all_day"] is False
        assert allday["all_day"] is True

    def test_event_without_summary_uses_fallback(self, tmp_path):
        today = datetime.now(_PERTH).date()
        items = [{
            "id": "nosummary",
            "start": {"date": today.isoformat()},
            "end": {"date": today.isoformat()},
        }]
        svc = self._make_service(items)
        with mock.patch("mcpbrain.auth.build_google_services",
                        return_value={"calendar_service": svc}):
            result = dashboard.calendar_today(str(tmp_path))
        assert result[0]["title"] == "(no title)"


# ---------------------------------------------------------------------------
# clickup_today
# ---------------------------------------------------------------------------

class TestClickupToday:
    def test_passes_through_task_list(self, tmp_path):
        tasks = [
            {"id": "t1", "name": "Fix thing", "status": "open",
             "due_date": _today(), "url": "https://app.clickup.com/t/t1"},
        ]
        with mock.patch("mcpbrain.clickup.search_tasks", return_value=tasks) as m:
            result = dashboard.clickup_today(str(tmp_path))
        assert result == tasks
        m.assert_called_once_with(str(tmp_path), due_date_lte=_today())

    def test_empty_result_passthrough(self, tmp_path):
        with mock.patch("mcpbrain.clickup.search_tasks", return_value=[]):
            result = dashboard.clickup_today(str(tmp_path))
        assert result == []


# ---------------------------------------------------------------------------
# mark_done
# ---------------------------------------------------------------------------

class TestMarkDone:
    """mark_done routes through Store.set_action_status (post-Phase-1 review
    fix), so these tests use a real Store rather than the FakeStore path stub."""

    def _real_store(self, tmp_path):
        from mcpbrain.store import Store
        s = Store(tmp_path / "real.sqlite3", dim=4)
        s.init()
        return s

    def test_marks_open_action_done(self, tmp_path):
        store = self._real_store(tmp_path)
        action_id = store.add_unified_action(text="Overdue task A")

        assert dashboard.mark_done(store, action_id) is True

        acts = store.unified_actions(status="done")
        assert [a["id"] for a in acts] == [action_id]
        assert acts[0]["resolved_by"] == "dashboard"

    def test_returns_false_for_nonexistent_id(self, tmp_path):
        store = self._real_store(tmp_path)
        assert dashboard.mark_done(store, 99999) is False

    def test_returns_false_for_already_done(self, tmp_path):
        store = self._real_store(tmp_path)
        action_id = store.add_unified_action(text="Done task F", status="done")
        assert dashboard.mark_done(store, action_id) is False


# ---------------------------------------------------------------------------
# assemble
# ---------------------------------------------------------------------------

class TestAssembleShape:
    def test_assemble_returns_all_four_keys(self, tmp_path, monkeypatch):
        db_path = tmp_path / "brain.sqlite3"
        _make_db(db_path)
        store = FakeStore(db_path)

        mock_actions = {"overdue": [], "due_today": [], "upcoming": [], "blocked": []}
        mock_cal = [{"id": "e1", "title": "Standup", "start": "09:00", "end": "09:30",
                     "all_day": False, "location": "", "has_pack": False}]
        mock_cu = [{"id": "t1", "name": "Task", "status": "open",
                    "due_date": _today(), "url": ""}]

        monkeypatch.setattr(dashboard, "actions_today", lambda s: mock_actions)
        monkeypatch.setattr(dashboard, "calendar_today", lambda h: mock_cal)
        monkeypatch.setattr(dashboard, "clickup_today", lambda h: mock_cu)
        monkeypatch.setattr(dashboard, "inbox_today", lambda s: [])

        result = dashboard.assemble(store, str(tmp_path))

        assert set(result.keys()) == {"actions", "calendar", "clickup", "inbox", "changes", "findings", "as_of"}
        assert result["actions"] == mock_actions
        assert result["calendar"] == mock_cal
        assert result["clickup"] == mock_cu
        assert isinstance(result["as_of"], str)
        assert len(result["as_of"]) > 0

    def test_assemble_degrades_on_calendar_error(self, tmp_path, monkeypatch):
        db_path = tmp_path / "brain.sqlite3"
        _make_db(db_path)
        store = FakeStore(db_path)

        monkeypatch.setattr(dashboard, "actions_today",
                            lambda s: {"overdue": [], "due_today": [], "upcoming": [], "blocked": []})
        monkeypatch.setattr(dashboard, "calendar_today",
                            mock.Mock(side_effect=RuntimeError("calendar down")))
        monkeypatch.setattr(dashboard, "clickup_today", lambda h: [])

        result = dashboard.assemble(store, str(tmp_path))
        assert result["calendar"] == []
        assert "as_of" in result

    def test_assemble_degrades_on_actions_error(self, tmp_path, monkeypatch):
        db_path = tmp_path / "brain.sqlite3"
        _make_db(db_path)
        store = FakeStore(db_path)

        monkeypatch.setattr(dashboard, "actions_today",
                            mock.Mock(side_effect=RuntimeError("db locked")))
        monkeypatch.setattr(dashboard, "calendar_today", lambda h: [])
        monkeypatch.setattr(dashboard, "clickup_today", lambda h: [])

        result = dashboard.assemble(store, str(tmp_path))
        assert result["actions"] == {
            "overdue": [], "due_today": [], "upcoming": [], "blocked": []
        }


# ---------------------------------------------------------------------------
# actions_today owner filter
# ---------------------------------------------------------------------------

class TestActionsTodayOwnerFilter:
    def _db_with_owners(self, tmp_path) -> Path:
        db_path = tmp_path / "brain.sqlite3"
        with sqlite3.connect(str(db_path)) as db:
            db.execute("""
                CREATE TABLE actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT, owner TEXT DEFAULT '', status TEXT DEFAULT 'open',
                    deadline TEXT DEFAULT '', org TEXT DEFAULT '',
                    project_id TEXT DEFAULT '', action_type TEXT DEFAULT 'next',
                    waiting_on TEXT, source TEXT DEFAULT 'email',
                    resolved_by TEXT DEFAULT '', resolved_at TEXT DEFAULT '',
                    updated_at TEXT DEFAULT ''
                )
            """)
            db.executemany(
                "INSERT INTO actions(text, owner, status, deadline) VALUES(?,?,?,?)",
                [
                    ("Mine explicit", "Sam", "open", _tomorrow()),
                    ("Mine unowned", "", "open", _tomorrow()),
                    ("Someone else's", "Taryn", "open", _tomorrow()),
                    ("Mine case-insensitive", "SAM", "open", _tomorrow()),
                ],
            )
        return db_path

    def test_excludes_other_owners(self, tmp_path):
        store = FakeStore(self._db_with_owners(tmp_path))
        result = dashboard.actions_today(store, owner="Sam")
        texts = {a["text"] for a in result["upcoming"]}
        assert texts == {"Mine explicit", "Mine unowned", "Mine case-insensitive"}

    def test_none_owner_applies_no_filter(self, tmp_path):
        store = FakeStore(self._db_with_owners(tmp_path))
        result = dashboard.actions_today(store, owner=None)
        assert len(result["upcoming"]) == 4

    def test_assemble_resolves_owner_from_config(self, tmp_path):
        """assemble passes config.owner_name(home) through to actions_today."""
        import json
        store = FakeStore(self._db_with_owners(tmp_path))
        (tmp_path / "config.json").write_text(json.dumps({"owner_name": "Taryn"}))
        with mock.patch("mcpbrain.dashboard.calendar_today", return_value=[]), \
             mock.patch("mcpbrain.dashboard.clickup_today", return_value=[]):
            result = dashboard.assemble(store, str(tmp_path))
        texts = {a["text"] for a in result["actions"]["upcoming"]}
        # Taryn's view: her explicit action + the unowned one. Sam's are excluded.
        assert texts == {"Someone else's", "Mine unowned"}
