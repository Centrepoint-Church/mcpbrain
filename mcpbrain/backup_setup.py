"""Backup enable flow: adopt or create the escrow key (Drive is canonical), write config."""
from __future__ import annotations

from mcpbrain import backup, config

# see mcpbrain.backup._NUM_RETRIES for the full rationale. The escrow upload
# here is a MediaInMemoryUpload (a fixed in-memory byte buffer, not a
# resumable stream), so it has none of _MEDIA_NUM_RETRIES' "a retried chunk
# cannot re-seek" problem — a retry just resends the same bytes. This flow
# runs during `mcpbrain setup`, where a transient Errno-49-class blip on the
# very first Drive call would otherwise fail backup-enable outright.
_NUM_RETRIES = 5


class EscrowKeyMismatch(RuntimeError):
    """The escrow key on Drive differs from this install's local key. Backup
    stays off: the Drive key is the one every existing backup is encrypted
    with, and it is never overwritten (R27)."""


def enable_backup(home: str, *, drive_service, user_id: str) -> dict:
    """Enable backup for the user. Returns the updated config dict.

    The escrow key on Drive (<escrow folder>/<email>.key) is CANONICAL and is
    never overwritten -- every existing backup is encrypted with it:
      - Drive has a key, no local key   -> adopt it (written to config);
      - Drive has a key equal to local  -> no-op;
      - Drive has a key, local differs  -> EscrowKeyMismatch, nothing changed;
      - Drive definitively has no key   -> upload the local key, or a new one
        (create, never update).
    A key lookup/download that FAILS raises (restore.EscrowKeyUnavailable,
    chained to the cause) -- it is never read as "no key", which would
    generate a new one. Idempotent.
    """
    from mcpbrain.restore import _download_escrow_key, account_email
    cur = config.read_config(home)
    existing = (cur.get("backup") or {}).get("escrow_key")
    local_key = existing.encode() if isinstance(existing, str) and existing else None

    escrow_folder = _resolve_shared_drive(drive_service, home=home)
    # restore reads <escrow folder>/<email>.key, so name the key by email
    # (user_id only when no email is known yet).
    person = (account_email(home) or user_id).strip().lower()
    drive_key = _download_escrow_key(drive_service, escrow_folder, person)
    if drive_key is not None:
        drive_key = drive_key.strip()
        if local_key is None:
            key_bytes = drive_key            # adopt the canonical key
        elif local_key.strip() == drive_key:
            key_bytes = local_key.strip()
        else:
            raise EscrowKeyMismatch(
                f"the escrow key on Drive ({person}.key) differs from this "
                "install's backup key. Backup was left off and nothing was changed: "
                "every existing backup needs the Drive key. Ask an admin before "
                "replacing either key.")
    else:
        key_bytes = local_key or backup.generate_escrow_key()
        _escrow_key_to_drive(drive_service, person, key_bytes, folder_id=escrow_folder)
    # backup.shared_drive_id must be a Shared Drive id; the escrow folder id is
    # not one (Drive rejects it as driveId), so store the folder's drive.
    shared_drive_id = _drive_id_of(drive_service, escrow_folder)

    # Fernet.generate_key() returns urlsafe-base64-encoded bytes; decode to str.
    key_str = key_bytes.decode() if isinstance(key_bytes, bytes) else key_bytes
    return config.write_config(home, {
        "backup": {
            "escrow_key": key_str,
            "shared_drive_id": shared_drive_id,
            "user_id": user_id,
            "interval_s": 3600,
            "retain": 7,
        }
    })


def _drive_id_of(drive_service, folder_id: str) -> str:
    """The Shared Drive id that holds ``folder_id``."""
    meta = drive_service.files().get(
        fileId=folder_id, fields="driveId", supportsAllDrives=True,
    ).execute(num_retries=_NUM_RETRIES)
    drive_id = (meta or {}).get("driveId")
    if not drive_id:
        raise RuntimeError(
            f"escrow folder {folder_id!r} is not inside a Shared Drive")
    return drive_id


def _resolve_shared_drive(drive_service, *, home: str) -> str:
    """Return the escrow folder ID (Shared Drive subfolder).

    Previously this searched/created a personal-Drive 'mcpbrain-escrow' folder
    — a bug, because escrow keys then landed on the user's personal Drive
    instead of the org Shared Drive.

    Resolves via the single source of truth ``restore._escrow_folder``:
    ``fleet.escrow_folder_id`` if set, else the tenant profile's
    escrow_folder_id. The profile fallback matters during the wizard's
    automatic backup-enable, which runs *before* the wizard writes the fleet
    folder IDs to config — without the fallback, enable_backup raised and
    first-run backup always failed.
    """
    from mcpbrain.restore import _escrow_folder
    folder_id = _escrow_folder(home)
    if not folder_id:
        raise RuntimeError(
            "No escrow folder configured (fleet.escrow_folder_id and no org "
            "default) — run mcpbrain setup to configure backup escrow."
        )
    return folder_id


def _escrow_key_to_drive(drive_service, user_id: str, key: bytes,
                         *, folder_id: str) -> None:
    """Create <user_id>.key in the Shared-Drive escrow folder. Never updates
    an existing one: raises EscrowKeyMismatch if it already exists."""
    from googleapiclient.http import MediaInMemoryUpload

    name = f"{user_id}.key"
    existing = []
    page_token = None
    while True:
        resp = drive_service.files().list(
            q=f"name='{name}' and '{folder_id}' in parents and trashed=false",
            fields="nextPageToken, files(id)",
            pageSize=1000,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            pageToken=page_token,
        ).execute(num_retries=_NUM_RETRIES)
        existing.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    if existing:
        # Never overwrite an escrowed key (R27): every existing backup is
        # encrypted with it. enable_backup only gets here when Drive had none,
        # so this is a race with another install -- refuse.
        raise EscrowKeyMismatch(
            f"an escrow key {name} appeared on Drive while enabling backup; "
            "it was not overwritten. Try again.")
    media = MediaInMemoryUpload(key, mimetype="application/octet-stream")
    meta = {"name": name, "parents": [folder_id]}
    drive_service.files().create(
        body=meta, media_body=media, fields="id",
        supportsAllDrives=True).execute(num_retries=_NUM_RETRIES)
