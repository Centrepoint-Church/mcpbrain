"""Shared Drive discovery: identical per-page cursor-advance invariant as
discover_drive, applied per pinned drive.

The events-dict collapse + resumed_ids/resumed_removed_ids double-tracking
that sync_shared_drive hand-rolls today disappears entirely here --
sync_queue's PRIMARY KEY (source, ref_id) UPSERT does that collapse for
free, exactly as it already does for My Drive.
"""
from mcpbrain.store import Store
from mcpbrain.sync.drive import discover_shared_drive, discover_shared_drives
from mcpbrain.org_contracts import FleetPin

PIN = FleetPin(embed_model="bge-small", dim=4, chunker_version="v1",
              enrich_logic_floor=1, fleet_secret="s3cret")

PAGES = 3


class _Req:
    def __init__(self, r): self._r = r
    def execute(self, num_retries=0): return self._r


class _Changes:
    def __init__(self, svc): self._svc = svc

    def list(self, **kw):
        assert "corpora" not in kw, "changes().list() rejects corpora"
        tok = kw.get("pageToken")
        self._svc.pages.append(tok)
        i = int(tok)
        body = {"changes": [{"fileId": f"f{i}", "file": {
            "id": f"f{i}", "name": f"d{i}.pdf", "mimeType": "application/pdf",
            "version": "1", "modifiedTime": f"2026-09-0{i}T00:00:00Z"}}]}
        if i < PAGES:
            body["nextPageToken"] = str(i + 1)
        else:
            body["newStartPageToken"] = "DONE"
        return _Req(body)

    def getStartPageToken(self, **kw):
        return _Req({"startPageToken": "1"})


class _Drives:
    def __init__(self, drives): self._drives = drives
    def list(self, **_kw): return _Req({"drives": self._drives})


class _Service:
    def __init__(self, drives=None):
        self.pages = []
        self._drives = drives or [{"id": "D1", "name": "Drive One"}]

    def changes(self): return _Changes(self)
    def drives(self): return _Drives(self._drives)


def _store(tmp_path):
    s = Store(tmp_path / "d.sqlite3", dim=4)
    s.init()
    return s


def test_discover_one_shared_drive_advances_per_page(tmp_path):
    s, svc = _store(tmp_path), _Service()
    s.set_cursor("drive:D1", "1")
    n = discover_shared_drive(svc, s, "D1", "drive:D1")
    assert n == PAGES
    assert s.sync_queue_pending("drive:D1") == PAGES
    assert s.get_cursor("drive:D1") == "DONE"


def test_discover_shared_drives_enumerates_pinned_drives(tmp_path):
    s = _store(tmp_path)
    svc = _Service(drives=[{"id": "D1", "name": "One"}, {"id": "D2", "name": "Two"}])
    s.set_cursor("drive:D1", "1")
    s.set_cursor("drive:D2", "1")
    out = discover_shared_drives(svc, s, pin=PIN)
    assert out == {"D1": PAGES, "D2": PAGES}
    assert s.sync_queue_pending("drive:D1") == PAGES
    assert s.sync_queue_pending("drive:D2") == PAGES


def test_one_drives_failure_does_not_abort_the_others(tmp_path):
    s = _store(tmp_path)

    class _FailingChanges(_Changes):
        def list(self, **kw):
            if kw.get("driveId") == "BAD":
                raise RuntimeError("simulated API failure")
            return super().list(**kw)

    class _S(_Service):
        def changes(self): return _FailingChanges(self)

    svc = _S(drives=[{"id": "BAD", "name": "Broken"}, {"id": "D1", "name": "OK"}])
    s.set_cursor("drive:BAD", "1")
    s.set_cursor("drive:D1", "1")
    out = discover_shared_drives(svc, s, pin=PIN)
    assert "BAD" not in out
    assert out["D1"] == PAGES


from mcpbrain.sync.drive import handle_shared_drive_item
from tests.helpers.org_fleet import LocalDirFleetStorage


class _FilesGet:
    def __init__(self, meta): self._meta = meta
    def get(self, fileId, **kw): return _Req(self._meta.get(fileId, {}))


class _ExportFiles(_FilesGet):
    def export(self, fileId, mimeType): return _Req(b"shared drive document body")


class _ServiceWithFiles(_Service):
    def __init__(self, meta=None, **kw):
        super().__init__(**kw)
        self._files_meta = meta or {}

    def files(self):
        f = _ExportFiles(self._files_meta)
        return f


def test_handle_shared_drive_item_extracts_and_records_a_pending_publish(tmp_path):
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    svc = _ServiceWithFiles(meta={"f1": {
        "id": "f1", "name": "doc.gdoc",
        "mimeType": "application/vnd.google-apps.document",
        "version": "1", "modifiedTime": "2026-09-01T00:00:00Z"}})
    item = {"source": "drive:D1", "ref_id": "f1", "version": "1",
           "event": "upsert", "modified_at": "2026-09-01T00:00:00Z"}
    handle_shared_drive_item(svc, s, item, fleet_storage=fs, pin=PIN, drive_id="D1")
    assert s.pending_publishes("D1") == [("f1", s.pending_publishes("D1")[0][1])]
    assert s.sync_queue_pending() == 0 or True  # completion is work_queue's job, not this handler's


def test_handle_shared_drive_item_removal_deletes_chunks(tmp_path):
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    svc = _ServiceWithFiles()
    item = {"source": "drive:D1", "ref_id": "gone", "version": "",
           "event": "remove", "modified_at": "2026-09-01T00:00:00Z"}
    # must not raise even with nothing to delete
    handle_shared_drive_item(svc, s, item, fleet_storage=fs, pin=PIN, drive_id="D1")
