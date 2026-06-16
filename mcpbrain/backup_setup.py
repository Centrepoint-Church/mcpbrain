"""Backup enable flow: generate escrow key, write to config, escrow copy to Drive."""
from __future__ import annotations

from mcpbrain import backup, config


def enable_backup(home: str, *, drive_service, user_id: str) -> dict:
    """Enable backup for the user.

    Generates a Fernet escrow key if not already set, writes it to config,
    and uploads a copy to the shared Drive folder for admin recovery.

    Idempotent: a second call with the same user_id keeps the existing key.
    Returns the updated config dict.
    """
    cur = config.read_config(home)
    existing_key = (cur.get("backup") or {}).get("escrow_key")

    if existing_key:
        # Key is already a urlsafe-base64 string; re-encode to bytes for escrow.
        key_bytes = existing_key.encode()
    else:
        key_bytes = backup.generate_escrow_key()

    shared_drive_id = _resolve_shared_drive(drive_service, home=home)
    _escrow_key_to_drive(drive_service, user_id, key_bytes, folder_id=shared_drive_id)

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


def _resolve_shared_drive(drive_service, *, home: str) -> str:
    """Return the configured escrow folder ID (Shared Drive subfolder).

    Previously this searched/created a personal-Drive 'mcpbrain-escrow' folder
    — a bug, because escrow keys then landed on the user's personal Drive
    instead of the org Shared Drive. The folder ID is now set during
    `mcpbrain setup` (wizard) as ``fleet.escrow_folder_id`` and read straight
    from config — no Drive search.
    """
    folder_id = (config.read_config(home).get("fleet") or {}).get("escrow_folder_id")
    if not folder_id:
        raise RuntimeError(
            "fleet.escrow_folder_id not set — run mcpbrain setup to configure backup escrow."
        )
    return folder_id


def _escrow_key_to_drive(drive_service, user_id: str, key: bytes,
                         *, folder_id: str) -> None:
    """Upload <user_id>.key to the Shared-Drive escrow folder (idempotent)."""
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
        ).execute()
        existing.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    media = MediaInMemoryUpload(key, mimetype="application/octet-stream")
    if existing:
        drive_service.files().update(
            fileId=existing[0]["id"], media_body=media, supportsAllDrives=True).execute()
    else:
        meta = {"name": name, "parents": [folder_id]}
        drive_service.files().create(
            body=meta, media_body=media, fields="id", supportsAllDrives=True).execute()
