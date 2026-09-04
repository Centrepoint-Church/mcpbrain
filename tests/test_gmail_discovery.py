"""Gmail discovery: same per-page cursor advance as Drive.

gmail.py's old docstring described this exact livelock in its own words -- a
budget covering fewer messages than the delta contains meant messages were
"PERMANENTLY never ingested". Gmail stayed healthy only because its daily delta
is small; the shape was identical.
"""
from mcpbrain.store import Store
from mcpbrain.sync.gmail import discover_gmail

PAGES = 3


class _Req:
    def __init__(self, r): self._r = r
    def execute(self, num_retries=0): return self._r


class _History:
    def __init__(self, svc): self._svc = svc

    def list(self, **kw):
        tok = kw.get("pageToken")
        self._svc.pages.append(tok)
        i = 1 if tok is None else int(tok)
        body = {"history": [{"messagesAdded": [{"message": {"id": f"m{i}"}}]}],
                "historyId": str(1000 + i)}
        if i < PAGES:
            body["nextPageToken"] = str(i + 1)
        return _Req(body)


class _Users:
    def __init__(self, svc): self._svc = svc
    def history(self): return _History(self._svc)
    def getProfile(self, userId=None): return _Req({"historyId": "1000"})


class _Service:
    def __init__(self): self.pages = []
    def users(self): return _Users(self)


class _OneShotBudget:
    def __init__(self, allow=1): self._left = allow
    def expired(self):
        if self._left > 0:
            self._left -= 1
            return False
        return True


def _store(tmp_path):
    s = Store(tmp_path / "g.sqlite3", dim=4)
    s.init()
    return s


def test_gmail_discovery_advances_per_page(tmp_path):
    s, svc = _store(tmp_path), _Service()
    s.set_cursor("gmail", "1000")
    discover_gmail(svc, s, budget=_OneShotBudget(1))
    assert s.sync_queue_pending("gmail") == 1
    assert s.get_cursor("gmail") != "1000"


def test_gmail_discovery_does_not_rewalk(tmp_path):
    s, svc = _store(tmp_path), _Service()
    s.set_cursor("gmail", "1000")
    for _ in range(3):
        discover_gmail(svc, s, budget=_OneShotBudget(1))
    assert svc.pages.count(None) == 1, f"first page re-walked: {svc.pages}"


def test_gmail_messages_are_enqueued_as_upserts(tmp_path):
    s, svc = _store(tmp_path), _Service()
    s.set_cursor("gmail", "1000")
    for _ in range(PAGES + 1):
        discover_gmail(svc, s)
    rows = s.due_sync_items(limit=10, now="2099-01-01T00:00:00")
    assert {r["ref_id"] for r in rows} == {"m1", "m2", "m3"}
    assert all(r["event"] == "upsert" for r in rows)
