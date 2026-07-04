"""E2E conftest — FakeGoogleService + shared fixtures."""
import json
from pathlib import Path

import pytest

from mcpbrain import config, orgs
from mcpbrain.store import Store

FIXTURES = Path(__file__).parent / "fixtures"


class _Req:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _GmailMessages:
    """service.users().messages().list/get — Gmail message list + fetch."""
    def __init__(self, threads_by_id: dict, all_ids: list):
        self._threads = threads_by_id
        self._all_ids = all_ids

    def list(self, userId, **kwargs):
        return _Req({"messages": [{"id": mid} for mid in self._all_ids]})

    def get(self, userId, id, format="full"):
        return _Req(self._threads[id])


class _GmailUsers:
    def __init__(self, messages):
        self._messages = messages

    def messages(self):
        return self._messages


class _CalendarEvents:
    """service.events().list — one page of calendar events."""
    def __init__(self, events: list):
        self._events = events

    def list(self, **kwargs):
        return _Req({"items": list(self._events)})


class _DriveFiles:
    """service.files().list/export/get_media — paginated across two pages.

    The first list() call returns page 1 + a nextPageToken; the second returns
    page 2 with no token. This exercises backfill_drive's real paging loop.
    """
    def __init__(self, files: list, content: dict):
        self._content = content
        mid = max(1, len(files) // 2)
        self._page1, self._page2 = files[:mid], files[mid:]

    def list(self, **kwargs):
        if kwargs.get("pageToken") is None:
            return _Req({"files": self._page1, "nextPageToken": "DRIVE_P2"})
        return _Req({"files": self._page2})

    def export(self, fileId, mimeType, **_kw):   # accept supportsAllDrives
        return _Req(self._content[fileId])

    def get_media(self, fileId, **_kw):          # accept supportsAllDrives
        return _Req(self._content[fileId])


class FakeGoogleService:
    """googleapiclient-shaped double covering Gmail, Calendar, and Drive.

    Each resource is its own object (real clients don't share a list() across
    resources), so backfill_gmail, backfill_calendar_window, and backfill_drive
    each hit the right signature:
        service.users().messages().list(userId=…)/get(…)
        service.events().list(calendarId=…, …)
        service.files().list(q=…, …)/export(fileId=…, mimeType=…)/get_media(fileId=…)
    Drive paginates across two pages so the real nextPageToken loop is covered.
    """

    def __init__(self, *, gmail_threads=None, gmail_ids=None,
                 calendar_events=None, drive_files=None, drive_content=None):
        self._gmail = _GmailUsers(_GmailMessages(gmail_threads or {}, gmail_ids or []))
        self._events = _CalendarEvents(calendar_events or [])
        self._files = _DriveFiles(drive_files or [], drive_content or {})

    def users(self):
        return self._gmail

    def events(self):
        return self._events

    def files(self):
        return self._files


@pytest.fixture
def e2e_home(tmp_path, monkeypatch):
    """Minimal MCPBRAIN_HOME for prepare tests.

    Points config.app_dir() at a tmp dir, seeds a config.json with an owner
    identity and one org whose domain matches the fixture sender (acme.org),
    and creates the enrich_queue / enrich_inbox dirs prepare expects.

    taxonomy_from_config is lru_cache'd, so the cache is cleared before and
    after so the tmp config is not leaked to other tests.
    """
    home = tmp_path / "home"
    home.mkdir()
    (home / "enrich_inbox").mkdir()
    (home / "enrich_queue").mkdir()
    monkeypatch.setenv("MCPBRAIN_HOME", str(home))
    config.write_config(str(home), {
        "owner_name": "Sam Admin",
        "owner_full_name": "Sam Admin",
        "owner_email": "sam@acme.org",
        "orgs": [
            {"name": "Acme", "domains": ["acme.org"], "aliases": []},
        ],
    })
    # Clear the lru_cache so taxonomy_from_config reads the tmp config.
    orgs.taxonomy_from_config.cache_clear()
    yield home
    # Restore: clear again so the tmp config doesn't bleed into later tests.
    orgs.taxonomy_from_config.cache_clear()


@pytest.fixture
def e2e_store(tmp_path):
    s = Store(tmp_path / "brain.db", dim=4)
    s.init()
    return s


@pytest.fixture
def fake_google():
    raw = json.loads((FIXTURES / "gmail_threads.json").read_text())
    threads_by_id = {}
    all_ids = []
    for thread in raw:
        for msg in thread:
            threads_by_id[msg["id"]] = msg
            all_ids.append(msg["id"])
    cal = json.loads((FIXTURES / "calendar_events.json").read_text())
    drive = json.loads((FIXTURES / "drive_files.json").read_text())
    return FakeGoogleService(
        gmail_threads=threads_by_id, gmail_ids=all_ids,
        calendar_events=cal,
        drive_files=drive["files"], drive_content=drive["content"],
    )
