"""Where this person's backups live: ONE place, resolved here.

Layout: ``<drive root>/<person email, lower-case>/``. Older installs wrote
under ``<drive root>/<backup.user_id>/`` (the full-snapshot uploader) and the
escrow convention has ``<escrow folder>/<person>/``; those stay readable as
``legacy_folders`` so restore and prune can find what is already there.

``backup.shared_drive_id`` is meant to hold a Shared Drive id, but wizard
installs wrote the escrow FOLDER id there, which Drive rejects as ``driveId``.
``resolve`` accepts either and returns the real drive id.
"""
from __future__ import annotations

from dataclasses import dataclass

from mcpbrain import backup, config

_NUM_RETRIES = backup._NUM_RETRIES


@dataclass(frozen=True)
class Location:
    drive_id: str
    person: str
    folder_id: str | None
    legacy_folders: tuple[str, ...]


def _drive_id(drive_service, configured: str) -> str:
    """The Shared Drive id for ``configured`` (a drive id, or a folder in one)."""
    try:
        drive_service.drives().get(driveId=configured, fields="id").execute(
            num_retries=_NUM_RETRIES)
        return configured
    except Exception:
        pass
    meta = drive_service.files().get(
        fileId=configured, fields="driveId", supportsAllDrives=True,
    ).execute(num_retries=_NUM_RETRIES)
    drive_id = (meta or {}).get("driveId")
    if not drive_id:
        raise ValueError(
            f"backup.shared_drive_id {configured!r} is neither a Shared Drive "
            "nor a folder inside one")
    return drive_id


def _find_folder(drive_service, name: str, parent: str, *, drive_id: str | None):
    """Id of folder ``name`` directly under ``parent``, or None."""
    backup._guard_user_id(name)
    q = (f"name = '{name}' and mimeType = '{backup.FOLDER_MIME}' "
         f"and trashed = false and '{parent}' in parents")
    kwargs = dict(corpora="drive", driveId=drive_id) if drive_id else dict(
        corpora="allDrives")
    resp = drive_service.files().list(
        q=q, includeItemsFromAllDrives=True, supportsAllDrives=True,
        fields="files(id, name)", **kwargs,
    ).execute(num_retries=_NUM_RETRIES)
    files = resp.get("files") or []
    return files[0]["id"] if files else None


def resolve(home: str, drive_service, *, create: bool = False) -> Location:
    from mcpbrain import restore

    person = (restore.account_email(home) or "").strip().lower()
    if not person:
        raise ValueError("no account email known; cannot name the backup folder")
    cfg = config.read_config(home).get("backup") or {}
    escrow = restore._escrow_folder(home)
    # A fresh machine has no backup block until the wizard's auto-restore has
    # run, and that restore must find the backups first: fall back to the
    # escrow folder, whose drive is what enable_backup writes here anyway.
    configured = cfg.get("shared_drive_id") or escrow
    if not configured:
        raise ValueError("backup.shared_drive_id is not configured")
    drive_id = _drive_id(drive_service, configured)

    folder_id = _find_folder(drive_service, person, drive_id, drive_id=drive_id)
    if folder_id is None and create:
        folder_id = drive_service.files().create(
            body={"name": person, "mimeType": backup.FOLDER_MIME,
                  "parents": [drive_id]},
            supportsAllDrives=True, fields="id",
        ).execute(num_retries=_NUM_RETRIES)["id"]

    legacy: list[str] = []
    user_id = (cfg.get("user_id") or "").strip()
    if user_id:
        legacy.append(_find_folder(drive_service, user_id, drive_id,
                                   drive_id=drive_id))
    if escrow:
        legacy.append(_find_folder(drive_service, person, escrow, drive_id=None))
    seen: set[str] = set()
    out = []
    for fid in legacy:
        if fid and fid != folder_id and fid not in seen:
            seen.add(fid)
            out.append(fid)
    return Location(drive_id, person, folder_id, tuple(out))
