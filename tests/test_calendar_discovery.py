"""Calendar discovery reuses _list_events (which already pages to completion in
memory) and advances the cursor only when it completes uninterrupted. Rows are
enqueued regardless, so an interruption never loses work -- only re-lists."""
from mcpbrain.store import Store
from mcpbrain.sync.calendar import discover_calendar


class _Req:
    def __init__(self, r): self._r = r
    def execute(self, num_retries=0): return self._r


class _Events:
    """One page of 3 events, syncToken on request."""
    def __init__(self, svc): self._svc = svc

    def list(self, **kw):
        self._svc.calls.append(kw.get("pageToken"))
        return _Req({
            "items": [
                {"id": "e1", "status": "confirmed", "updated": "2026-09-01T00:00:00Z"},
                {"id": "e2", "status": "confirmed", "updated": "2026-09-02T00:00:00Z"},
                {"id": "e3", "status": "cancelled", "updated": "2026-09-03T00:00:00Z"},
            ],
            "nextSyncToken": "SYNCED",
        })


class _Service:
    def __init__(self): self.calls = []
    def events(self): return _Events(self)


def _store(tmp_path):
    s = Store(tmp_path / "c.sqlite3", dim=4)
    s.init()
    return s


def test_calendar_discovery_enqueues_and_advances_when_uninterrupted(tmp_path):
    s, svc = _store(tmp_path), _Service()
    s.set_cursor("calendar", "TOK0")
    n = discover_calendar(svc, s)
    assert n == 3
    assert s.sync_queue_pending("calendar") == 3
    assert s.get_cursor("calendar") == "SYNCED"


def test_cancelled_event_is_a_remove(tmp_path):
    s, svc = _store(tmp_path), _Service()
    s.set_cursor("calendar", "TOK0")
    discover_calendar(svc, s)
    rows = {r["ref_id"]: r for r in s.due_sync_items(limit=10, now="2099-01-01T00:00:00")}
    assert rows["e3"]["event"] == "remove"
    assert rows["e1"]["event"] == "upsert"


def test_version_is_the_updated_field_not_etag(tmp_path):
    """The proven-correct dedup key from _event_resume_key: id + updated."""
    s, svc = _store(tmp_path), _Service()
    s.set_cursor("calendar", "TOK0")
    discover_calendar(svc, s)
    rows = {r["ref_id"]: r for r in s.due_sync_items(limit=10, now="2099-01-01T00:00:00")}
    assert rows["e1"]["version"] == "2026-09-01T00:00:00Z"


def test_interrupted_call_enqueues_but_does_not_advance_cursor(tmp_path, monkeypatch):
    import mcpbrain.sync.calendar as cal
    s, svc = _store(tmp_path), _Service()
    s.set_cursor("calendar", "TOK0")

    def fake_list_events(service, calendar_id, sync_token, time_min, time_max, *,
                         budget=None):
        return ([{"id": "e1", "status": "confirmed",
                  "updated": "2026-09-01T00:00:00Z"}], None, True)

    monkeypatch.setattr(cal, "_list_events", fake_list_events)
    discover_calendar(svc, s)
    assert s.sync_queue_pending("calendar") == 1, "interrupted call must still enqueue"
    assert s.get_cursor("calendar") == "TOK0", "cursor must not advance when interrupted"
