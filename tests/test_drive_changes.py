"""Guards the shared-drive delta loop's service.changes().list() kwargs.

changes().list() rejects `corpora` (that kwarg is files.list-only) — passing it
raises TypeError in the real Drive v3 client, which would abort every shared
drive's sync. Test via the real public entry point (sync_shared_drive) with a
minimal fake service that records the kwargs it was called with.
"""
from mcpbrain.sync.drive import sync_shared_drive
from mcpbrain.org_contracts import FleetPin
from mcpbrain.store import Store
from tests.helpers.org_fleet import LocalDirFleetStorage

PIN = FleetPin(embed_model="bge-small", dim=4, chunker_version="v1",
               enrich_logic_floor=1, fleet_secret="s3cret")


def _store(tmp_path, name="a.sqlite3"):
    s = Store(tmp_path / name, dim=4)
    s.init()
    return s


class _Req:
    def __init__(self, result):
        self._r = result

    def execute(self):
        return self._r


class _CapturingChanges:
    """Records the kwargs passed to list() and returns an empty, terminal page."""

    def __init__(self, seen):
        self._seen = seen

    def list(self, **kw):
        self._seen.update(kw)
        return _Req({"changes": [], "newStartPageToken": "101"})


class _CapturingService:
    def __init__(self, seen):
        self._seen = seen

    def changes(self):
        return _CapturingChanges(self._seen)


def test_changes_list_omits_corpora(tmp_path):
    """changes().list() must not be passed corpora (the real API rejects it)."""
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    s.set_cursor("drive:D1", "100")  # skip bootstrap; go straight to the delta loop
    seen: dict = {}
    svc = _CapturingService(seen)
    sync_shared_drive(svc, s, "D1", fleet_storage=fs, pin=PIN)
    assert "corpora" not in seen
    assert seen.get("driveId") == "D1"
