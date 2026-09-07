"""Guards the shared-drive delta loop's service.changes().list() kwargs.

changes().list() rejects `corpora` (that kwarg is files.list-only) — passing it
raises TypeError in the real Drive v3 client, which would abort every shared
drive's sync. Test via the real public entry point (discover_shared_drive,
sync_shared_drive's replacement) with a minimal fake service that records the
kwargs it was called with.

Note: test_shared_drive_discovery.py's own `_Changes.list` fake also asserts
"corpora" not in kw inline (exercised by its discover_shared_drive/
discover_shared_drives tests), so this exact property already has coverage
there too -- this file is kept as a dedicated, clearly-named regression guard
for a defect class ("a silently-rejected kwarg breaks every shared drive's
sync") that is worth a standalone test rather than only an incidental
assertion buried in another file's fixture.
"""
from mcpbrain.sync.drive import discover_shared_drive
from mcpbrain.store import Store


def _store(tmp_path, name="a.sqlite3"):
    s = Store(tmp_path / name, dim=4)
    s.init()
    return s


class _Req:
    def __init__(self, result):
        self._r = result

    def execute(self, num_retries=0):
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
    s = _store(tmp_path)
    s.set_cursor("drive:D1", "100")  # skip bootstrap; go straight to the delta loop
    seen: dict = {}
    svc = _CapturingService(seen)
    discover_shared_drive(svc, s, "D1", "drive:D1")
    assert "corpora" not in seen
    assert seen.get("driveId") == "D1"
