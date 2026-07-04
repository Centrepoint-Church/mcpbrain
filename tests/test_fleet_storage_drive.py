"""DriveFleetStorage against an in-memory Drive double (no network)."""
import itertools

from mcpbrain.fleet_storage import DriveFleetStorage
from mcpbrain.org_contracts import FleetStorage

FOLDER_MIME = "application/vnd.google-apps.folder"


class _Req:
    def __init__(self, fn):
        self._fn = fn

    def execute(self, **_kw):
        return self._fn()


class FakeDrive:
    """Minimal Drive files() double: create/list/get_media/update/delete over an
    in-memory node table keyed by id, with (name, parent) lookups."""

    def __init__(self):
        self.nodes = {}   # id -> {id,name,mimeType,parents,data}
        self._ids = ("id%d" % i for i in itertools.count(1))

    def files(self):
        return self

    # -- create ----------------------------------------------------------
    def create(self, body=None, media_body=None, fields=None, supportsAllDrives=None):
        def _do():
            nid = next(self._ids)
            self.nodes[nid] = {
                "id": nid, "name": body["name"],
                "mimeType": body.get("mimeType", "application/octet-stream"),
                "parents": list(body.get("parents", [])),
                "data": media_body.stream().getvalue() if media_body is not None else b"",
            }
            return {"id": nid}
        return _Req(_do)

    # -- list ------------------------------------------------------------
    def list(self, q=None, fields=None, pageSize=None, pageToken=None,
             supportsAllDrives=None, includeItemsFromAllDrives=None,
             corpora=None, driveId=None):
        def _do():
            # supports "'<parent>' in parents" + "name = '<n>'" + mimeType filters
            import re
            parent = None
            m = re.search(r"'([^']+)' in parents", q or "")
            if m:
                parent = m.group(1)
            name = None
            m = re.search(r"name\s*=\s*'([^']+)'", q or "")
            if m:
                name = m.group(1)
            want_folder = FOLDER_MIME in (q or "")
            files = []
            for n in self.nodes.values():
                if parent is not None and parent not in n["parents"]:
                    continue
                if name is not None and n["name"] != name:
                    continue
                if want_folder and n["mimeType"] != FOLDER_MIME:
                    continue
                files.append({"id": n["id"], "name": n["name"],
                              "mimeType": n["mimeType"], "modifiedTime": ""})
            return {"files": files}
        return _Req(_do)

    def get_media(self, fileId=None, supportsAllDrives=None):
        return _Req(lambda: self.nodes[fileId]["data"])

    def update(self, fileId=None, media_body=None, supportsAllDrives=None):
        def _do():
            self.nodes[fileId]["data"] = media_body.stream().getvalue()
            return {"id": fileId}
        return _Req(_do)

    def delete(self, fileId=None, supportsAllDrives=None):
        def _do():
            self.nodes.pop(fileId, None)
            return {}
        return _Req(_do)


def test_satisfies_protocol():
    assert isinstance(DriveFleetStorage(FakeDrive(), "ROOT"), FleetStorage)


def test_put_get_roundtrip_nested_path():
    fs = DriveFleetStorage(FakeDrive(), "ROOT")
    fs.put_bytes(".mcpbrain-cache/FID.hash.pf.mbc.gz", b"payload")
    assert fs.get_bytes(".mcpbrain-cache/FID.hash.pf.mbc.gz") == b"payload"


def test_get_missing_returns_none():
    fs = DriveFleetStorage(FakeDrive(), "ROOT")
    assert fs.get_bytes(".mcpbrain-cache/nope.mbc.gz") is None


def test_put_overwrites_existing():
    drive = FakeDrive()
    fs = DriveFleetStorage(drive, "ROOT")
    fs.put_bytes("a/b.bin", b"one")
    fs.put_bytes("a/b.bin", b"two")
    assert fs.get_bytes("a/b.bin") == b"two"
    # only one leaf node named b.bin (update, not duplicate-create)
    leaves = [n for n in drive.nodes.values() if n["name"] == "b.bin"]
    assert len(leaves) == 1


def test_list_paths_by_prefix_sorted():
    fs = DriveFleetStorage(FakeDrive(), "ROOT")
    fs.put_bytes(".mcpbrain-cache/B.mbc.gz", b"b")
    fs.put_bytes(".mcpbrain-cache/A.mbc.gz", b"a")
    fs.put_bytes("other/x.bin", b"x")
    assert fs.list_paths(".mcpbrain-cache/") == [
        ".mcpbrain-cache/A.mbc.gz", ".mcpbrain-cache/B.mbc.gz"]


def test_delete_is_idempotent():
    fs = DriveFleetStorage(FakeDrive(), "ROOT")
    fs.put_bytes("a/x.bin", b"1")
    fs.delete("a/x.bin")
    fs.delete("a/x.bin")
    assert fs.get_bytes("a/x.bin") is None


def test_list_shared_drives_is_re_exported():
    from mcpbrain import fleet_storage
    from mcpbrain.sync.drive import list_shared_drives as canonical
    assert fleet_storage.list_shared_drives is canonical


def test_fleet_folder_storage_uses_config_folder_id(tmp_path):
    from mcpbrain import config, fleet_storage
    config.write_config(str(tmp_path), {"fleet": {"folder_id": "FLEETFOLDER"}})
    drive = FakeDrive()
    fs = fleet_storage.fleet_folder_storage(str(tmp_path), drive_service=drive)
    assert isinstance(fs, FleetStorage)
    assert fs._root == "FLEETFOLDER"
    # round-trips against the fleet folder root
    fs.put_bytes("org-graph/manifest.json", b'{"version":1}')
    assert fs.get_bytes("org-graph/manifest.json") == b'{"version":1}'


def test_fleet_folder_storage_falls_back_to_org_default(tmp_path):
    from mcpbrain import fleet_storage, org_defaults
    fs = fleet_storage.fleet_folder_storage(str(tmp_path), drive_service=FakeDrive())
    assert fs is not None and fs._root == org_defaults.FLEET_FOLDER_ID


def test_fleet_folder_storage_none_without_service(tmp_path):
    from mcpbrain import config, fleet_storage
    config.write_config(str(tmp_path), {"fleet": {"folder_id": "FLEETFOLDER"}})
    assert fleet_storage.fleet_folder_storage(str(tmp_path), drive_service=None) is None


def test_drive_cache_storage_roots_at_drive_and_uses_cache_prefix(tmp_path):
    from mcpbrain import fleet_storage, ingest_cache
    drive = FakeDrive()
    fs = fleet_storage.drive_cache_storage(drive, "D1")
    assert isinstance(fs, FleetStorage) and fs._root == "D1"
    # ingest_cache addresses the .mcpbrain-cache/ subfolder; no double-prefix
    fs.put_bytes(f"{ingest_cache.CACHE_DIR}/FID.h.pf.mbc.gz", b"payload")
    assert fs.get_bytes(f"{ingest_cache.CACHE_DIR}/FID.h.pf.mbc.gz") == b"payload"
    names = [n["name"] for n in drive.nodes.values()]
    assert ingest_cache.CACHE_DIR in names          # exactly one cache folder, at the root
