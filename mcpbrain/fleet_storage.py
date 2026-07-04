"""Production FleetStorage over Google Drive (spec §A4).

DriveFleetStorage maps '/'-separated relative paths onto a Drive folder tree under
a root (a Shared Drive id or a folder id). Subsystem A uses one instance per Shared
Drive (root = driveId) for the in-drive `.mcpbrain-cache/`; B/C use it over the
fleet folder — both only ever through the FleetStorage protocol.

All Drive calls set supportsAllDrives=True (Shared Drives require it), matching the
mechanism backup.py / fleet.py already rely on. googleapiclient is imported lazily
so importing this module does not require the SDK.
"""
from __future__ import annotations

import logging

# Re-exported so onboarding/curation code acquires ALL fleet transport (folder
# storage, per-drive cache storage, and drive enumeration) from one module.
from mcpbrain.sync.drive import list_shared_drives  # noqa: F401

log = logging.getLogger(__name__)

_FOLDER_MIME = "application/vnd.google-apps.folder"


def _q_escape(name: str) -> str:
    # Drive query strings are single-quoted; escape backslash then quote.
    return name.replace("\\", "\\\\").replace("'", "\\'")


class DriveFleetStorage:
    """A FleetStorage backed by a Google Drive folder subtree."""

    def __init__(self, drive_service, folder_or_drive_id: str):
        self._svc = drive_service
        self._root = folder_or_drive_id
        # (parent_id, name) -> folder_id cache to avoid re-listing on every put.
        self._folder_cache: dict[tuple[str, str], str] = {}

    # -- Drive primitives ------------------------------------------------

    def _find_child(self, parent_id: str, name: str, *, folder: bool):
        q = (f"name = '{_q_escape(name)}' and '{parent_id}' in parents "
             f"and trashed = false")
        if folder:
            q += f" and mimeType = '{_FOLDER_MIME}'"
        resp = self._svc.files().list(
            q=q, fields="files(id,name,mimeType,modifiedTime)",
            pageSize=100, supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        files = resp.get("files", [])
        if not files:
            return None
        files.sort(key=lambda f: f.get("modifiedTime", ""), reverse=True)
        return files[0]["id"]

    def _ensure_folder(self, parent_id: str, name: str) -> str:
        key = (parent_id, name)
        if key in self._folder_cache:
            return self._folder_cache[key]
        fid = self._find_child(parent_id, name, folder=True)
        if fid is None:
            fid = self._svc.files().create(
                body={"name": name, "mimeType": _FOLDER_MIME, "parents": [parent_id]},
                fields="id", supportsAllDrives=True,
            ).execute()["id"]
        self._folder_cache[key] = fid
        return fid

    def _resolve_parent(self, components: list[str], *, create: bool):
        parent = self._root
        for comp in components:
            if create:
                parent = self._ensure_folder(parent, comp)
            else:
                fid = self._find_child(parent, comp, folder=True)
                if fid is None:
                    return None
                parent = fid
        return parent

    def _resolve_file(self, path: str, *, create_parents: bool):
        parts = [p for p in path.split("/") if p]
        parent = self._resolve_parent(parts[:-1], create=create_parents)
        if parent is None:
            return None, None
        return parent, parts[-1]

    # -- FleetStorage protocol ------------------------------------------

    def put_bytes(self, path: str, data: bytes) -> None:
        from googleapiclient.http import MediaInMemoryUpload
        parent, leaf = self._resolve_file(path, create_parents=True)
        media = MediaInMemoryUpload(data, mimetype="application/octet-stream")
        existing = self._find_child(parent, leaf, folder=False)
        if existing:
            self._svc.files().update(
                fileId=existing, media_body=media, supportsAllDrives=True,
            ).execute()
        else:
            self._svc.files().create(
                body={"name": leaf, "parents": [parent]},
                media_body=media, fields="id", supportsAllDrives=True,
            ).execute()

    def get_bytes(self, path: str) -> bytes | None:
        parent, leaf = self._resolve_file(path, create_parents=False)
        if parent is None:
            return None
        fid = self._find_child(parent, leaf, folder=False)
        if fid is None:
            return None
        raw = self._svc.files().get_media(
            fileId=fid, supportsAllDrives=True).execute()
        return raw if isinstance(raw, bytes) else str(raw).encode("utf-8")

    def list_paths(self, prefix: str) -> list[str]:
        # Walk the subtree from root, building '/'-relative file paths, then filter.
        out: list[str] = []

        def _walk(parent_id: str, rel: str):
            resp = self._svc.files().list(
                q=f"'{parent_id}' in parents and trashed = false",
                fields="files(id,name,mimeType)", pageSize=1000,
                supportsAllDrives=True, includeItemsFromAllDrives=True,
            ).execute()
            for f in resp.get("files", []):
                child_rel = f"{rel}{f['name']}"
                if f.get("mimeType") == _FOLDER_MIME:
                    _walk(f["id"], child_rel + "/")
                else:
                    out.append(child_rel)

        _walk(self._root, "")
        return sorted(p for p in out if p.startswith(prefix))

    def delete(self, path: str) -> None:
        parent, leaf = self._resolve_file(path, create_parents=False)
        if parent is None:
            return
        fid = self._find_child(parent, leaf, folder=False)
        if fid is None:
            return
        self._svc.files().delete(fileId=fid, supportsAllDrives=True).execute()


# -- factories (the storage instances B and C acquire) ----------------------

def fleet_folder_storage(home, drive_service=None):
    """FleetStorage over the fleet FOLDER (spec: fleet folder / org-graph / contrib).

    This is the instance B's org cadences (contrib upload, curate, snapshot
    publish) and C's snapshot-import call — distinct from the per-shared-drive
    cache storages. Root is the configured fleet folder id, falling back to the
    bundled org default. Returns None when there is no drive_service or no folder
    id resolves (caller then runs fully local — existing degradation behaviour).
    """
    if drive_service is None:
        return None
    from mcpbrain import config, org_defaults
    folder_id = (config.read_config(home).get("fleet") or {}).get("folder_id") \
        or org_defaults.FLEET_FOLDER_ID
    if not folder_id:
        return None
    return DriveFleetStorage(drive_service, folder_id)


def drive_cache_storage(drive_service, drive_id):
    """FleetStorage for one Shared Drive's ingest cache (read/publish; C's per-drive
    bootstrap_drive). Rooted at the SHARED DRIVE ROOT — ingest_cache addresses the
    `.mcpbrain-cache/` subfolder via its CACHE_DIR path prefix, so rooting here (not
    at the cache folder) is required to avoid a doubled prefix."""
    return DriveFleetStorage(drive_service, drive_id)
