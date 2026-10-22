"""Shared Drives that hold mcpbrain's OWN infrastructure, which it must never ingest.

The backup drive (encrypted segments, manifests), the fleet folder's drive
(escrow keys, org-config.json, per-user status, the central ingest cache,
contributions, the org graph) and the escrow folder's drive are ordinary Shared
Drives to the Drive API, so the shared-drive sync would otherwise enumerate them
like any document source: live (2026-10-22) the backup drive's own 2 KB status
file had been chunked, embedded and published back into
`ingest-cache/<that drive>/` eight times.

Three configured ids name that infrastructure: `backup.shared_drive_id`
(a drive id, or -- wizard installs -- a folder in one), the fleet folder
(`fleet_folder_storage.fleet_folder_id`) and the escrow folder
(`restore._escrow_folder`). A drive id is recognised for free when it is in
this cycle's `drives().list` enumeration; a folder id costs one
`files().get(fields=driveId)` the first time, then its answer is cached in the
store's meta table. A failed lookup excludes nothing for that id this cycle
(blocking every drive on an unprovable negative would stop legitimate sync),
logs once per process, and is retried next cycle.

No name backstop: the drive's NAME is chosen by each organisation, not by
mcpbrain, so any literal would be one tenant's choice baked into the source.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_META_PREFIX = "infra_drive:"
# Cached answer for an id that is NOT inside any Shared Drive (a My Drive
# folder): nothing to exclude, and no reason to ask Drive again every cycle.
_NOT_SHARED = "-"
_NUM_RETRIES = 5

_warned: set[str] = set()


def configured_ids(home) -> list[str]:
    """The configured ids that name mcpbrain infrastructure, deduplicated, in
    a stable order. Any of them may be a Shared Drive id or a folder id."""
    from mcpbrain import config, restore
    from mcpbrain.fleet_storage import fleet_folder_id

    out: list[str] = []
    candidates = [
        (config.read_config(home).get("backup") or {}).get("shared_drive_id"),
        fleet_folder_id(home),
        restore._escrow_folder(home),
    ]
    for cid in candidates:
        if isinstance(cid, str) and cid.strip() and cid.strip() not in out:
            out.append(cid.strip())
    return out


def _lookup_drive_id(drive_service, cid: str) -> str | None:
    """The Shared Drive containing ``cid`` (itself, for a drive's root id), or
    None for an id outside every Shared Drive. Raises on a failed call."""
    meta = drive_service.files().get(
        fileId=cid, fields="driveId", supportsAllDrives=True,
    ).execute(num_retries=_NUM_RETRIES)
    did = (meta or {}).get("driveId") if isinstance(meta, dict) else None
    return did if isinstance(did, str) and did else None


def resolve(home, store, drive_service, *, listed_drive_ids=()) -> set[str]:
    """Shared Drive ids hosting mcpbrain infrastructure. Never raises."""
    listed = set(listed_drive_ids or ())
    out: set[str] = set()
    try:
        ids = configured_ids(home)
    except Exception as exc:  # noqa: BLE001 — a broken config excludes nothing
        log.warning("infra_drives: could not read the configured ids: %s", exc)
        return out
    for cid in ids:
        if cid in listed:          # a drive id, proven by this cycle's listing
            out.add(cid)
            continue
        key = _META_PREFIX + cid
        cached = None
        if store is not None:
            try:
                cached = store.get_meta(key)
            except Exception:  # noqa: BLE001 — fall through to a lookup
                cached = None
        if cached:
            if cached != _NOT_SHARED:
                out.add(cached)
            continue
        if drive_service is None:
            continue
        try:
            did = _lookup_drive_id(drive_service, cid)
        except Exception as exc:  # noqa: BLE001 — retried next cycle
            if cid not in _warned:
                _warned.add(cid)
                log.warning(
                    "infra_drives: could not resolve which Shared Drive holds "
                    "%s (will retry each cycle; not excluded until it resolves): %s",
                    cid, exc)
            continue
        _warned.discard(cid)
        if store is not None:
            try:
                store.set_meta(key, did or _NOT_SHARED)
            except Exception:  # noqa: BLE001 — the cache is an optimisation
                pass
        if did:
            out.add(did)
    return out
