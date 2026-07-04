"""Production FleetStorage over Google Drive (spec §A4).

DriveFleetStorage maps '/'-separated relative paths onto a Drive folder tree under
a root (a Shared Drive id or a folder id). Subsystem A uses one instance per Shared
Drive (root = driveId) for the in-drive `.mcpbrain-cache/`; B/C use it over the
fleet folder — both only ever through the FleetStorage protocol.

All Drive calls set supportsAllDrives=True (Shared Drives require it), matching the
mechanism backup.py / fleet.py already rely on, and run through _exec(), which
activates the client library's exponential backoff (num_retries=5) on transient
5xx/429/quota errors and logs a warning if a call still ultimately fails — this is
the sole production transport for an unattended daemon feature. googleapiclient is
imported lazily so importing this module does not require the SDK.
"""
from __future__ import annotations

import logging
import time

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

    def __init__(self, drive_service, folder_or_drive_id: str, *,
                 ensure_folder_retry_attempts: int = 3,
                 ensure_folder_retry_backoff: float = 0.05):
        self._svc = drive_service
        self._root = folder_or_drive_id
        # Bounded retry knobs for _ensure_folder's post-create re-resolve.
        # Overridable so tests don't have to wait out real backoff.
        self._ensure_folder_retry_attempts = max(1, ensure_folder_retry_attempts)
        self._ensure_folder_retry_backoff = ensure_folder_retry_backoff
        # (parent_id, name) -> folder_id cache to avoid re-listing on every put.
        self._folder_cache: dict[tuple[str, str], str] = {}

    # -- Drive primitives ------------------------------------------------

    def _exec(self, request, *, context: str):
        """Run a Drive API request with the client library's retry/backoff
        activated (num_retries=5) for transient 5xx/429/quota errors. Logs a
        warning (rather than failing silently) when a call still ultimately
        raises after those retries are exhausted."""
        try:
            return request.execute(num_retries=5)
        except Exception:
            log.warning("fleet_storage: Drive operation failed (%s)", context, exc_info=True)
            raise

    def _find_child(self, parent_id: str, name: str, *, folder: bool):
        q = (f"name = '{_q_escape(name)}' and '{parent_id}' in parents "
             f"and trashed = false")
        if folder:
            q += f" and mimeType = '{_FOLDER_MIME}'"
        matches = []
        page_token = None
        while True:
            resp = self._exec(
                self._svc.files().list(
                    q=q, fields="nextPageToken, files(id,name,mimeType,modifiedTime)",
                    pageSize=100, supportsAllDrives=True,
                    includeItemsFromAllDrives=True, pageToken=page_token,
                ),
                context=f"list children of {parent_id!r} named {name!r}",
            )
            matches.extend(resp.get("files", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        if not matches:
            return None
        # Secondary sort key (id) makes the tie-break deterministic across processes
        # when modifiedTime ties or is missing (see Finding 2's race-mitigation use).
        matches.sort(key=lambda f: (f.get("modifiedTime", ""), f.get("id", "")), reverse=True)
        return matches[0]["id"]

    def _ensure_folder(self, parent_id: str, name: str) -> str:
        # Find-then-create is inherently racy: two concurrent instances can both
        # see no match and both create a folder, producing two duplicates under
        # the same parent (Drive does not enforce name uniqueness). We don't
        # trust our own create()'s id — instead we re-resolve via a fresh
        # _find_child call, which (now that it paginates and applies a fully
        # deterministic modifiedTime/id tie-break) will see ALL folders with
        # that name, including one a racing process just created, and every
        # racing instance converges on the SAME single folder id rather than
        # each trusting its own possibly-losing create call. This is a
        # mitigation, not a full fix: a genuinely adversarial two-process race
        # in the exact same instant can still occasionally observe inconsistent
        # results due to Drive's eventual consistency, but it turns the common
        # case from a guaranteed split-brain into convergence. The bounded
        # retry below tolerates the ordinary eventual-consistency lag between
        # a create() returning and that folder becoming visible to a
        # subsequent list() — only raising once retries are exhausted.
        key = (parent_id, name)
        if key in self._folder_cache:
            return self._folder_cache[key]
        fid = self._find_child(parent_id, name, folder=True)
        if fid is None:
            self._exec(
                self._svc.files().create(
                    body={"name": name, "mimeType": _FOLDER_MIME, "parents": [parent_id]},
                    fields="id", supportsAllDrives=True,
                ),
                context=f"create folder {name!r} under {parent_id!r}",
            )
            fid = None
            attempts = self._ensure_folder_retry_attempts
            for attempt in range(1, attempts + 1):
                fid = self._find_child(parent_id, name, folder=True)
                if fid is not None:
                    break
                if attempt < attempts:
                    log.info(
                        "fleet_storage: folder %r under %r not yet visible after "
                        "create (attempt %d/%d); retrying after a short "
                        "eventual-consistency backoff",
                        name, parent_id, attempt, attempts,
                    )
                    time.sleep(self._ensure_folder_retry_backoff)
            if fid is None:
                log.warning(
                    "fleet_storage: giving up resolving folder %r under parent "
                    "%r after %d attempts (Drive eventual consistency?)",
                    name, parent_id, attempts,
                )
                raise RuntimeError(
                    f"DriveFleetStorage: folder {name!r} under parent {parent_id!r} "
                    f"was created but not found on re-resolve after {attempts} "
                    f"attempts (Drive eventual consistency?)"
                )
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
            self._exec(
                self._svc.files().update(
                    fileId=existing, media_body=media, supportsAllDrives=True,
                ),
                context=f"update leaf {leaf!r} under {parent!r}",
            )
        else:
            self._exec(
                self._svc.files().create(
                    body={"name": leaf, "parents": [parent]},
                    media_body=media, fields="id", supportsAllDrives=True,
                ),
                context=f"create leaf {leaf!r} under {parent!r}",
            )

    def get_bytes(self, path: str) -> bytes | None:
        parent, leaf = self._resolve_file(path, create_parents=False)
        if parent is None:
            return None
        fid = self._find_child(parent, leaf, folder=False)
        if fid is None:
            return None
        raw = self._exec(
            self._svc.files().get_media(fileId=fid, supportsAllDrives=True),
            context=f"get_media {path!r}",
        )
        if not isinstance(raw, bytes):
            # Best-effort-stringifying a non-bytes response would silently
            # produce garbage that *looks* like a successful read for a
            # binary (gzip) payload. Surface it as an explicit failure so
            # callers' existing fail-safe fallback treats it as a fetch miss
            # rather than importing corrupted content.
            raise TypeError(
                f"DriveFleetStorage.get_bytes({path!r}): expected bytes from "
                f"get_media, got {type(raw).__name__}"
            )
        return raw

    def list_paths(self, prefix: str) -> list[str]:
        # Resolve the prefix's leading folder path first (targeted per-segment
        # lookups via _resolve_parent/_find_child), then walk only that subtree
        # instead of the whole drive. See fix note in the code review for why:
        # walking from self._root unconditionally made every publish/cache-miss
        # and sync-cycle sweep pay for a full-drive listing.
        folder_parts = [p for p in prefix.split("/")[:-1] if p]
        root = self._resolve_parent(folder_parts, create=False)
        if root is None:
            return []
        rel_prefix = "/".join(folder_parts) + "/" if folder_parts else ""
        out: list[str] = []

        def _walk(parent_id: str, rel: str):
            page_token = None
            while True:
                resp = self._exec(
                    self._svc.files().list(
                        q=f"'{parent_id}' in parents and trashed = false",
                        fields="nextPageToken, files(id,name,mimeType)", pageSize=1000,
                        supportsAllDrives=True, includeItemsFromAllDrives=True,
                        pageToken=page_token,
                    ),
                    context=f"list children of {parent_id!r} (walk)",
                )
                for f in resp.get("files", []):
                    child_rel = f"{rel}{f['name']}"
                    if f.get("mimeType") == _FOLDER_MIME:
                        _walk(f["id"], child_rel + "/")
                    else:
                        out.append(child_rel)
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break

        _walk(root, rel_prefix)
        return sorted(p for p in out if p.startswith(prefix))

    def delete(self, path: str) -> None:
        parent, leaf = self._resolve_file(path, create_parents=False)
        if parent is None:
            return
        fid = self._find_child(parent, leaf, folder=False)
        if fid is None:
            return
        self._exec(
            self._svc.files().delete(fileId=fid, supportsAllDrives=True),
            context=f"delete {path!r}",
        )


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
