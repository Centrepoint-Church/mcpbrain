"""A budget-truncated Drive round must resume its PAGING, not restart it.

Live livelock (found 2026-09-02, author's store): the `drive` cursor had not
advanced since 2026-07-29 -- five weeks. The changes() backlog was longer than
one CYCLE_BUDGET_S (60s) could page through, and the cursor advances only when
a round finishes uninterrupted:

    if new_start and not interrupted and pending_keys <= resumed_ids:

`newStartPageToken` is returned only on the feed's LAST page, so a round that
never reaches it has new_start=None AND interrupted=True -- two independent
reasons the cursor cannot move. Every cycle re-walked the same ~5,000-change
prefix and threw it away: ~25% of a core burned continuously, and no Drive
change ingested for five weeks.

The write loop already guarantees forward progress ("Guaranteeing one item per
call is what makes the round monotonic and the livelock impossible"). The
PAGING loop had no such guarantee. These tests pin it.
"""
from mcpbrain.org_contracts import FleetPin
from mcpbrain.store import Store
from mcpbrain.sync.drive import sync_drive, sync_shared_drive
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


def test_my_drive_paging_converges_across_budgeted_rounds(tmp_path):
    s, svc = _store(tmp_path), _Service()
    s.set_cursor("drive", "1")
    # One page per round -- the live shape, where the feed outruns the budget.
    for _ in range(PAGES + 2):
        sync_drive(svc, s, budget=_OneShotBudget(1))
    assert s.get_cursor("drive") == "DONE", (
        f"cursor stuck at {s.get_cursor('drive')!r}; "
        f"pages fetched: {svc.pages_fetched}")


def test_my_drive_does_not_refetch_page_one_every_round(tmp_path):
    """The livelock's signature: page 1 walked over and over."""
    s, svc = _store(tmp_path), _Service()
    s.set_cursor("drive", "1")
    for _ in range(4):
        sync_drive(svc, s, budget=_OneShotBudget(1))
    assert svc.pages_fetched.count("1") == 1, \
        f"page 1 re-walked {svc.pages_fetched.count('1')}x: {svc.pages_fetched}"


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


def test_completed_round_clears_paging_state(tmp_path):
    """A finished round leaves no resume crumbs to confuse the next one."""
    s, svc = _store(tmp_path), _Service()
    s.set_cursor("drive", "1")
    for _ in range(PAGES + 2):
        sync_drive(svc, s, budget=_OneShotBudget(1))
    assert s.get_cursor("drive") == "DONE"
    assert (s.get_cursor("drive:page_token") or "") == ""
