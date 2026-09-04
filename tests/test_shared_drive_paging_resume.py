"""A budget-truncated shared-drive round must resume its PAGING, not restart it.

Split out of the deleted `test_drive_paging_resume.py` (2026-09-04 sync-queue
Task 9): that file's `sync_drive`/My-Drive tests were superseded by
`test_drive_discovery.py` once `discover_drive`/`handle_drive_item` replaced
`sync_drive` -- but `sync_shared_drive` was deliberately NOT migrated (no
`discover_shared_drive`/`handle_shared_drive_item` exists; see
`sync_shared_drive`'s own docstring in `mcpbrain/sync/drive.py`), so it still
needs this same per-round paging-resume guarantee, and nothing else in the
suite exercises it. Deleting this coverage alongside the My-Drive tests would
have silently dropped it.

Live livelock this guards against (found 2026-09-02, author's store): a
changes() backlog longer than one budget's worth of pages meant the cursor
never advanced -- `newStartPageToken` is only returned on the feed's LAST
page, so a round that never reaches it can never move the real cursor.
"""
from mcpbrain.org_contracts import FleetPin
from mcpbrain.store import Store
from mcpbrain.sync.drive import sync_shared_drive
from tests.helpers.org_fleet import LocalDirFleetStorage

PIN = FleetPin(embed_model="bge-small", dim=4, chunker_version="v1",
               enrich_logic_floor=1, fleet_secret="s3cret")

PAGES = 5  # feed length, in pages


class _OneShotBudget:
    """Expires after `allow` calls to expired() -- one page per round."""

    def __init__(self, allow=1):
        self._left = allow

    def expired(self):
        if self._left > 0:
            self._left -= 1
            return False
        return True


class _Req:
    def __init__(self, r):
        self._r = r

    def execute(self, num_retries=0):
        return self._r


class _Changes:
    """A PAGES-long feed of unsupported (skipped) files.

    Every change is an image: fetch_content returns None for those, so nothing
    is ever written and `pending` stays empty -- exactly the live shape, where
    the backlog was ~5,000 jpegs. newStartPageToken only on the final page.
    """

    def __init__(self, svc):
        self._svc = svc

    def list(self, **kw):
        tok = kw.get("pageToken")
        self._svc.pages_fetched.append(tok)
        if tok == "DONE":                     # feed caught up: empty terminal page
            return _Req({"changes": [], "newStartPageToken": "DONE"})
        idx = int(tok)
        body = {"changes": [{"fileId": f"f{idx}",
                             "file": {"id": f"f{idx}", "name": f"img{idx}.jpg",
                                      "mimeType": "image/jpeg", "version": "1"}}]}
        if idx < PAGES:
            body["nextPageToken"] = str(idx + 1)
        else:
            body["newStartPageToken"] = "DONE"
        return _Req(body)

    def getStartPageToken(self, **kw):
        return _Req({"startPageToken": "1"})


class _Service:
    def __init__(self):
        self.pages_fetched = []

    def changes(self):
        return _Changes(self)


def _store(tmp_path, name="d.sqlite3"):
    s = Store(tmp_path / name, dim=4)
    s.init()
    return s


def test_shared_drive_paging_converges(tmp_path):
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    svc = _Service()
    s.set_cursor("drive:D1", "1")
    for _ in range(PAGES + 2):
        sync_shared_drive(svc, s, "D1", fleet_storage=fs, pin=PIN,
                          budget=_OneShotBudget(1))
    assert s.get_cursor("drive:D1") == "DONE", \
        f"cursor stuck at {s.get_cursor('drive:D1')!r}"


def test_shared_drive_processes_a_partially_paged_round(tmp_path):
    """An interrupted round must still checkpoint what it paged.

    This is the half that kept the shared-drive cursor pinned: paging stopped
    early, ALL processing was skipped, so resumed_ids stayed empty and the
    paging offset could never advance.
    """
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    svc = _Service()
    s.set_cursor("drive:D1", "1")
    sync_shared_drive(svc, s, "D1", fleet_storage=fs, pin=PIN,
                      budget=_OneShotBudget(1))
    assert (s.get_cursor("drive:D1:page_token") or "") != "", \
        "an interrupted round left no paging progress"
