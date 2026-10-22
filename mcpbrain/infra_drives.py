"""Where mcpbrain's OWN infrastructure lives in Drive, so it is never ingested.

The backup tree (per-person folders of encrypted `chain-*` segments), the fleet
folder (escrow keys, org-config.json, per-user status, the central ingest
cache, contributions, the org graph) and the escrow folder are ordinary Drive
items to the API. Live (2026-10-22) the shared-drive sync had chunked three of
the fleet folder's JSON files and published them back into its own ingest cache.

Three configured ids name that infrastructure: `backup.shared_drive_id` (a
Shared Drive id, or -- wizard installs -- a folder in one), the fleet folder
(`fleet_storage.fleet_folder_id`) and the escrow folder
(`restore._escrow_folder`). From them this module derives a `Layout`:

* `folders[drive]` -- the infrastructure folder ids inside each Shared Drive:
  the configured folders, plus backup person folders found at the backup
  root (named after this person or the legacy `backup.user_id`, or any
  email-named folder that holds only `chain-*` folders / `.enc` / `.key`
  files). Files under them are skipped at ingest; `paths[drive]` are the same
  folders as `folder_path` strings, for matching chunks already stored.
* `dedicated` -- drives proven to hold NOTHING else: every item at the drive
  root is one of those folders. Only these are excluded from sync whole. A
  drive that also holds real documents (a fork that put its fleet folder
  inside an "Operations" drive) stays synced; only the subtrees are skipped.

The verdict is cached in meta for 7 days, and recomputed at once when the
configured ids change or a cached drive is missing from the cycle's listing.
A failed lookup never widens anything: an incomplete result keeps the last
cached layout (or, with none, uses what did resolve and is not cached), logs
once per process, and is retried next cycle.

No name backstop: the drive's NAME is each organisation's choice, not
mcpbrain's, so any literal would bake one tenant into the source.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

TTL = timedelta(days=7)
_LAYOUT_META = "infra_drives.layout"
_NUM_RETRIES = 5
_FOLDER_MIME = "application/vnd.google-apps.folder"
# Bounds on the dedication check: a drive whose root is bigger than this is
# not a dedicated infrastructure drive, and is not worth listing in full.
_MAX_ROOT_ITEMS = 200
_MAX_PERSON_PROBES = 50

_warned: set[str] = set()


@dataclass(frozen=True)
class Layout:
    dedicated: frozenset = frozenset()
    folders: dict = field(default_factory=dict)   # drive id -> frozenset(folder ids)
    paths: dict = field(default_factory=dict)     # drive id -> tuple(folder_path)
    checked_at: str = ""

    def skip_folders(self, drive_id) -> frozenset:
        return self.folders.get(drive_id, frozenset())

    def in_infra(self, drive_id, folder_path: str) -> bool:
        fp = folder_path or ""
        return any(fp == p or fp.startswith(p + "/")
                   for p in self.paths.get(drive_id, ()))

    def to_json(self, key) -> str:
        return json.dumps({
            "key": key, "checked_at": self.checked_at,
            "dedicated": sorted(self.dedicated),
            "folders": {d: sorted(v) for d, v in self.folders.items()},
            "paths": {d: list(v) for d, v in self.paths.items()},
        }, sort_keys=True)

    @classmethod
    def from_json(cls, raw):
        d = json.loads(raw)
        return d.get("key"), cls(
            dedicated=frozenset(d.get("dedicated") or ()),
            folders={k: frozenset(v) for k, v in (d.get("folders") or {}).items()},
            paths={k: tuple(v) for k, v in (d.get("paths") or {}).items()},
            checked_at=d.get("checked_at") or "")


def _configured(home) -> dict:
    from mcpbrain import config, restore
    from mcpbrain.fleet_storage import fleet_folder_id

    cfg = config.read_config(home)
    backup = cfg.get("backup") or {}

    def _s(v):
        return v.strip() if isinstance(v, str) and v.strip() else None

    people = {p for p in (_s((restore.account_email(home) or "").lower()),
                          _s(backup.get("user_id"))) if p}
    return {"backup": _s(backup.get("shared_drive_id")),
            "fleet": _s(fleet_folder_id(home)),
            "escrow": _s(restore._escrow_folder(home)),
            "people": sorted(people)}


def _drive_of(svc, cid):
    meta = svc.files().get(fileId=cid, fields="id,driveId",
                           supportsAllDrives=True).execute(num_retries=_NUM_RETRIES)
    did = (meta or {}).get("driveId") if isinstance(meta, dict) else None
    return did if isinstance(did, str) and did else None


def _children(svc, parent, drive_id, limit):
    out, token = [], None
    while True:
        resp = svc.files().list(
            q=f"'{parent}' in parents and trashed = false",
            fields="nextPageToken, files(id,name,mimeType)", pageSize=100,
            corpora="drive", driveId=drive_id, includeItemsFromAllDrives=True,
            supportsAllDrives=True, pageToken=token,
        ).execute(num_retries=_NUM_RETRIES)
        out.extend(resp.get("files") or [])
        token = resp.get("nextPageToken")
        if not token or len(out) > limit:
            return out


def _holds_only_backups(svc, folder_id, drive_id) -> bool:
    kids = _children(svc, folder_id, drive_id, _MAX_ROOT_ITEMS)
    if not kids or len(kids) > _MAX_ROOT_ITEMS:
        return False
    for k in kids:
        name = str(k.get("name") or "")
        if k.get("mimeType") == _FOLDER_MIME:
            if not name.startswith("chain-"):
                return False
        elif not (name.endswith(".enc") or name.endswith(".key")):
            return False
    return True


def _compute(svc, conf, listed: set, now) -> tuple[Layout, bool]:
    """(layout, complete). Every Drive failure is caught and makes the result
    incomplete; nothing is ever ADDED on a guess."""
    from mcpbrain.sync.drive import folder_path

    complete = True
    folders: dict[str, set] = {}
    backup_root_drive = None
    for role in ("backup", "fleet", "escrow"):
        cid = conf[role]
        if not cid:
            continue
        if cid in listed:
            if role == "backup":
                backup_root_drive = cid
            continue
        try:
            did = _drive_of(svc, cid)
        except Exception as exc:  # noqa: BLE001
            complete = False
            if cid not in _warned:
                _warned.add(cid)
                log.warning("infra_drives: could not resolve which Shared Drive "
                            "holds %s (retried each cycle): %s", cid, exc)
            continue
        if did:
            folders.setdefault(did, set()).add(cid)

    people = set(conf["people"])
    dedicated = set()
    for did in sorted(set(folders) | ({backup_root_drive} if backup_root_drive else set())):
        known = folders.setdefault(did, set())
        try:
            root = _children(svc, did, did, _MAX_ROOT_ITEMS)
        except Exception as exc:  # noqa: BLE001
            complete = False
            log.info("infra_drives: could not list the root of %s: %s", did, exc)
            continue
        other = len(root) > _MAX_ROOT_ITEMS
        probes = 0
        for item in root:
            fid, name = item.get("id"), str(item.get("name") or "")
            if fid in known:
                continue
            is_folder = item.get("mimeType") == _FOLDER_MIME
            if is_folder and did == backup_root_drive and name.lower() in people:
                known.add(fid)
                continue
            if is_folder and "@" in name and probes < _MAX_PERSON_PROBES:
                probes += 1
                try:
                    if _holds_only_backups(svc, fid, did):
                        known.add(fid)
                        continue
                except Exception:  # noqa: BLE001 — unproven means not infra
                    complete = False
            other = True
        if not other and known:
            dedicated.add(did)

    paths: dict[str, tuple] = {}
    cache: dict = {}
    for did, ids in folders.items():
        ps = []
        for fid in sorted(ids):
            p = folder_path(svc, {"parents": [fid]}, cache)
            if p:
                ps.append(p)
            else:
                complete = False
        paths[did] = tuple(sorted(set(ps)))
    layout = Layout(
        dedicated=frozenset(dedicated),
        folders={d: frozenset(v) for d, v in folders.items() if v},
        paths={d: v for d, v in paths.items() if v},
        checked_at=now.isoformat())
    return layout, complete


def resolve(home, store, drive_service, *, listed_drive_ids=(), now=None) -> Layout:
    """The current Layout. Never raises; an empty Layout excludes nothing."""
    now = now or datetime.now(timezone.utc)
    listed = set(listed_drive_ids or ())
    try:
        conf = _configured(home)
    except Exception as exc:  # noqa: BLE001
        log.warning("infra_drives: could not read the configured ids: %s", exc)
        return Layout()
    if not (conf["backup"] or conf["fleet"] or conf["escrow"]):
        return Layout()
    key = json.dumps(conf, sort_keys=True)

    cached = None
    if store is not None:
        try:
            raw = store.get_meta(_LAYOUT_META)
            if raw:
                ckey, cached = Layout.from_json(raw)
                if ckey != key:
                    cached = None
        except Exception:  # noqa: BLE001 — recompute
            cached = None
    if cached is not None:
        try:
            fresh = now - datetime.fromisoformat(cached.checked_at) < TTL
        except ValueError:
            fresh = False
        if fresh and set(cached.folders) <= listed:
            return cached

    if drive_service is None:
        return cached or Layout()
    try:
        layout, complete = _compute(drive_service, conf, listed, now)
    except Exception as exc:  # noqa: BLE001
        log.warning("infra_drives: layout check failed (retried next cycle): %s", exc)
        return cached or Layout()
    if not complete:
        return cached or layout
    if store is not None:
        try:
            store.set_meta(_LAYOUT_META, layout.to_json(key))
        except Exception:  # noqa: BLE001 — the cache is an optimisation
            pass
    return layout
