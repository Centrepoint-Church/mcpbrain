"""Drive discovery enqueues and advances PER PAGE.

The 0.7.123 livelock: newStartPageToken arrives only on the feed's LAST page,
so a round that could not reach it never advanced the cursor and re-walked the
same prefix forever -- five weeks, on the author's store. Advancing per page
removes the need to reach the end at all.
"""
from mcpbrain.store import Store
from mcpbrain.sync.drive import discover_drive

PAGES = 4


class _Req:
    def __init__(self, r): self._r = r
    def execute(self, num_retries=0): return self._r


class _Changes:
    def __init__(self, svc): self._svc = svc

    def list(self, **kw):
        tok = kw.get("pageToken")
        self._svc.pages.append(tok)
        if tok == "DONE":
            return _Req({"changes": [], "newStartPageToken": "DONE"})
        i = int(tok)
        body = {"changes": [{"fileId": f"f{i}", "file": {
            "id": f"f{i}", "name": f"d{i}.pdf", "mimeType": "application/pdf",
            "version": "1", "modifiedTime": f"2026-09-0{i}T00:00:00Z"}}]}
        if i < PAGES:
            body["nextPageToken"] = str(i + 1)
        else:
            body["newStartPageToken"] = "DONE"
        return _Req(body)

    def getStartPageToken(self, **kw): return _Req({"startPageToken": "1"})


class _Service:
    def __init__(self): self.pages = []
    def changes(self): return _Changes(self)


class _OneShotBudget:
    def __init__(self, allow=1): self._left = allow
    def expired(self):
        if self._left > 0:
            self._left -= 1
            return False
        return True


def _store(tmp_path):
    s = Store(tmp_path / "d.sqlite3", dim=4)
    s.init()
    return s


def test_discovery_advances_the_cursor_after_every_page(tmp_path):
    s, svc = _store(tmp_path), _Service()
    s.set_cursor("drive", "1")
    discover_drive(svc, s, budget=_OneShotBudget(1))
    assert s.get_cursor("drive") != "1", "cursor did not advance after one page"
    assert s.sync_queue_pending("drive") == 1


def test_discovery_never_rewalks_a_page(tmp_path):
    """The livelock's signature was page 1, over and over."""
    s, svc = _store(tmp_path), _Service()
    s.set_cursor("drive", "1")
    for _ in range(4):
        discover_drive(svc, s, budget=_OneShotBudget(1))
    assert svc.pages.count("1") == 1, f"page 1 re-walked: {svc.pages}"


def test_discovery_reaches_the_feed_head(tmp_path):
    s, svc = _store(tmp_path), _Service()
    s.set_cursor("drive", "1")
    for _ in range(PAGES + 2):
        discover_drive(svc, s, budget=_OneShotBudget(1))
    assert s.get_cursor("drive") == "DONE"
    assert s.sync_queue_pending("drive") == PAGES


def test_removal_is_enqueued_as_a_remove_event(tmp_path):
    s = _store(tmp_path)

    class _Removed(_Changes):
        def list(self, **kw):
            return _Req({"changes": [{"fileId": "gone", "removed": True}],
                         "newStartPageToken": "DONE"})

    class _S(_Service):
        def changes(self): return _Removed(self)

    s.set_cursor("drive", "1")
    discover_drive(_S(), s)
    row = s.due_sync_items(limit=10, now="2099-01-01T00:00:00")[0]
    assert row["event"] == "remove" and row["ref_id"] == "gone"
