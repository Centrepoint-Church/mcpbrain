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

    shared_drive_id = _resolve_shared_drive(drive_service)
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


def _resolve_shared_drive(drive_service) -> str:
    """Find or create the 'mcpbrain-escrow' folder on Drive. Returns its file ID."""
    resp = drive_service.files().list(
        q="name='mcpbrain-escrow' and mimeType='application/vnd.google-apps.folder' and trashed=false",
        fields="files(id,name)",
        spaces="drive",
    ).execute()
    files = resp.get("files", [])
    if files:
        return files[0]["id"]
    # Create it
    meta = {"name": "mcpbrain-escrow", "mimeType": "application/vnd.google-apps.folder"}
    folder = drive_service.files().create(body=meta, fields="id").execute()
    return folder["id"]


def _escrow_key_to_drive(drive_service, user_id: str, key: bytes,
                         *, folder_id: str | None = None) -> None:
    """Upload <user_id>.key to the mcpbrain-escrow folder.

    folder_id: pre-resolved folder ID from _resolve_shared_drive; if None,
    resolves it internally (legacy path / direct calls in tests).
    """
    from googleapiclient.http import MediaInMemoryUpload

    if folder_id is None:
        folder_id = _resolve_shared_drive(drive_service)
    name = f"{user_id}.key"
    # Check if it already exists (idempotent update)
    resp = drive_service.files().list(
        q=f"name='{name}' and '{folder_id}' in parents and trashed=false",
        fields="files(id)",
    ).execute()
    existing = resp.get("files", [])
    media = MediaInMemoryUpload(key, mimetype="application/octet-stream")
    if existing:
        drive_service.files().update(fileId=existing[0]["id"], media_body=media).execute()
    else:
        meta = {"name": name, "parents": [folder_id]}
        drive_service.files().create(body=meta, media_body=media, fields="id").execute()
