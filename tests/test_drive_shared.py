from mcpbrain.sync.drive import (
    list_shared_drives, normalise_drive, _file_content_hash,
)
from mcpbrain.org_contracts import DRIVE_ID_META_KEY


class _Req:
    def __init__(self, result):
        self._r = result

    def execute(self):
        return self._r


class _Drives:
    def __init__(self, pages):
        self._pages = pages
        self._i = 0

    def list(self, **_kw):
        page = self._pages[self._i]
        self._i = min(self._i + 1, len(self._pages) - 1)
        return _Req(page)


class _DriveOnlyService:
    def __init__(self, pages):
        self._drives = _Drives(pages)

    def drives(self):
        return self._drives


def test_list_shared_drives_paginates():
    svc = _DriveOnlyService([
        {"drives": [{"id": "D1", "name": "Ops"}], "nextPageToken": "p2"},
        {"drives": [{"id": "D2", "name": "Finance"}]},
    ])
    ds = list_shared_drives(svc)
    assert [d["id"] for d in ds] == ["D1", "D2"]


def test_normalise_drive_stamps_drive_id():
    fm = {"id": "FID", "name": "Doc", "mimeType": "application/vnd.google-apps.document",
          "modifiedTime": "2026-05-01T10:00:00Z", "owners": [{"displayName": "X"}]}
    chunks = normalise_drive(fm, "hello world", drive_id="D1")
    assert chunks and chunks[0].metadata[DRIVE_ID_META_KEY] == "D1"
    # My-Drive path (no drive_id) leaves the key absent
    chunks2 = normalise_drive(fm, "hello world")
    assert DRIVE_ID_META_KEY not in chunks2[0].metadata


def test_file_content_hash_prefers_md5_then_stable_for_native():
    assert _file_content_hash({"id": "F", "md5Checksum": "abc"}) == "abc"
    a = _file_content_hash({"id": "F", "version": "7", "modifiedTime": "T"})
    b = _file_content_hash({"id": "F", "version": "7", "modifiedTime": "T"})
    c = _file_content_hash({"id": "F", "version": "8", "modifiedTime": "T"})
    assert a == b and a != c and len(a) == 64        # deterministic sha256
