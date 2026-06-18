"""Recovery path: restore the brain store from the latest encrypted snapshot on Drive."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


def run_restore(home: str, *, key: bytes | None = None, force: bool = False) -> str:
    """Find the latest snapshot on Drive and restore it to the store path.

    Resolves the escrow key from config (backup.escrow_key) unless key is given.
    Raises ValueError if backup is not configured.
    Raises RuntimeError if the store already exists and force=False.
    Returns the path of the restored store as a string.
    """
    from mcpbrain import backup as _backup, auth as _auth, config as _cfg

    cfg = _cfg.read_config(home)
    backup_cfg = cfg.get("backup") or {}

    if key is None:
        stored_key = backup_cfg.get("escrow_key", "")
        if not stored_key:
            raise ValueError("Backup not configured. Run the install skill and enable backup.")
        key = stored_key.encode()

    shared_drive_id = backup_cfg.get("shared_drive_id", "")
    user_id = backup_cfg.get("user_id") or _cfg.owner_email(home)

    creds = _auth.load_credentials()
    drive_service = _auth.build_service("drive", "v3", creds)

    file_id = _backup.find_latest_snapshot(drive_service, shared_drive_id, user_id)
    if file_id is None:
        raise RuntimeError("No snapshot found on Drive.")

    store_path = _cfg.store_path()
    p = Path(store_path)
    if p.exists() and p.stat().st_size > 0 and not force:
        raise RuntimeError(
            f"Store already exists at {p}. Use --force to overwrite, or delete it first."
        )

    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".restore-", suffix=".enc")
    if hasattr(os, "fchmod"):
        os.fchmod(fd, 0o600)
    os.close(fd)
    tmp_path = Path(tmp)
    try:
        _backup.download_snapshot(drive_service, file_id, tmp_path)
        # Restore the full bundle: store + the local records repo + config.json
        # (a bare-store legacy artifact restores the store only — restore()
        # detects the shape). records_dir/config_path are ignored when the
        # artifact doesn't carry them.
        _backup.restore(
            tmp_path, p, key,
            records_dir=_cfg.records_dir(home),
            config_path=str(Path(home) / "config.json"))
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass

    return str(p)


def _download_escrow_key(drive_service, escrow_folder_id: str, user_email: str) -> bytes | None:
    """Download <user_email>.key from the escrow folder, or None if absent.

    The key is uploaded by backup_setup at enable time. Fetching it from the
    Shared Drive (which the user already has access to) means recovery needs no
    manually-entered key — the user just signs in with Google.
    """
    from mcpbrain import backup as _backup
    name = f"{user_email}.key"
    try:
        # Parent-based query: the escrow folder is nested inside a Shared Drive,
        # so its id is NOT a valid driveId — query by parent with corpora=allDrives.
        files = _backup._list_in_drives(
            drive_service,
            f"name = '{name}' and trashed = false and '{escrow_folder_id}' in parents",
            fields="files(id, name)")
    except Exception:  # noqa: BLE001 — best-effort detection; treat errors as "no key"
        return None
    if not files:
        return None
    import tempfile
    fd, tmp = tempfile.mkstemp(prefix=".escrowkey-")
    os.close(fd)
    tmp_path = Path(tmp)
    try:
        _backup.download_snapshot(drive_service, files[0]["id"], tmp_path)
        return tmp_path.read_bytes()
    except Exception:  # noqa: BLE001
        return None
    finally:
        tmp_path.unlink(missing_ok=True)


def _escrow_folder(home: str) -> str:
    """The escrow FOLDER id for the auto-restore convention.

    Prefers fleet.escrow_folder_id, else the baked-in org default (so detection
    works on a fresh machine before the wizard writes config). Deliberately does
    NOT read backup.shared_drive_id — in legacy configs that is the Shared Drive
    ROOT (the daemon's drive-root upload target), not the nested escrow folder.
    """
    from mcpbrain import config as _cfg, org_defaults
    cfg = _cfg.read_config(home)
    return (
        (cfg.get("fleet") or {}).get("escrow_folder_id")
        or org_defaults.ESCROW_FOLDER_ID
    )


def store_has_content(store_path: str | None = None) -> bool:
    """True if the local store holds real synced content (not just empty schema).

    The daemon creates ``brain.sqlite3`` and initializes every table on startup —
    which `mcpbrain setup` does *before* the wizard runs — so file existence/size
    can't tell a fresh install from a populated brain (the file always has a
    SQLite header + empty tables). Instead, check the primary content table
    (``chunks``) for any row. Read-only connection so it never disturbs the daemon.

    Conservative on ambiguity: if the DB exists but can't be read (locked badly,
    corrupt), returns True so auto-restore never clobbers a real brain. A missing
    file or missing ``chunks`` table means a fresh store → False.
    """
    from mcpbrain import config as _cfg
    p = Path(store_path or _cfg.store_path())
    if not p.exists() or p.stat().st_size == 0:
        return False
    import sqlite3
    # Plain (not mode=ro) connection: the store is WAL, so this is a concurrent
    # reader alongside the daemon's writer and a SELECT writes nothing. mode=ro
    # has a WAL gotcha (can fail to open a hot WAL) that would wrongly read as
    # "unreadable". p.exists() above means connect() won't create a new file.
    try:
        con = sqlite3.connect(str(p), timeout=5)
    except sqlite3.Error:
        return True
    try:
        con.execute("PRAGMA busy_timeout=5000")
        return con.execute("SELECT 1 FROM chunks LIMIT 1").fetchone() is not None
    except sqlite3.OperationalError as exc:
        # "no such table" → fresh/uninitialized store → empty. Any other
        # operational error (locked, etc.) is ambiguous → conservatively treat as
        # populated so auto-restore never clobbers a real brain.
        return "no such table" not in str(exc).lower()
    except sqlite3.Error:
        return True
    finally:
        con.close()


def account_email(home: str) -> str:
    """The email the escrow backup is keyed by, resolved for the restore path.

    Order: saved ``owner_email`` → the connected **Google account** (the
    ``google_account`` sidecar the daemon writes on sign-in) → legacy
    ``backup.user_id``. The Google-account fallback is what makes auto-restore
    work right after sign-in: on a fresh machine the user hasn't filled the
    profile step yet (so owner_email is blank), but they ARE signed in — and the
    escrow is keyed by that Google email. Without this, restore wrongly waited
    until the user manually entered + saved their email. Returns "" if unknown.
    """
    from pathlib import Path
    from mcpbrain import config as _cfg
    email = (_cfg.owner_email(home) or "").strip()
    if email:
        return email
    try:
        sidecar = (Path(home) / "google_account").read_text().strip()
        if sidecar:
            return sidecar
    except OSError:
        pass
    return ((_cfg.read_config(home).get("backup") or {}).get("user_id") or "").strip()


def detect_restorable(home: str, drive_service) -> dict:
    """Report whether an existing backup can be restored for this account.

    Resolves the escrow folder (config → org default) and the user's email, then
    checks the escrow folder for both the user's escrow key and a snapshot.
    Returns {available, snapshot_id, has_key, user_email, escrow_folder_id}.
    Never raises — degrades to available=False.
    """
    from mcpbrain import backup as _backup
    # The escrow convention is email-keyed (snapshot subfolder <email>/ and
    # <email>.key). Use the signed-in account email (owner_email → Google account
    # → legacy backup.user_id) so restore works before the profile step is saved.
    user_email = account_email(home)
    if not user_email:
        return {"available": False, "reason": "no account signed in yet"}
    folder = _escrow_folder(home)
    key = _download_escrow_key(drive_service, folder, user_email)
    snapshot_id = None
    try:
        # Snapshots live in <escrow folder>/<user_email>/ — parent-based lookup
        # (the escrow folder is nested in a Shared Drive, not a Shared Drive root).
        snapshot_id = _backup.find_latest_in_subfolder(drive_service, folder, user_email)
    except Exception:  # noqa: BLE001
        snapshot_id = None
    return {
        "available": bool(key and snapshot_id),
        "snapshot_id": snapshot_id,
        "has_key": bool(key),
        "user_email": user_email,
        "escrow_folder_id": folder,
    }


def run_restore_auto(home: str, *, force: bool = False, drive_service=None) -> str:
    """Detect and restore the latest backup, fetching the escrow key from Drive.

    The turnkey recovery path for a fresh machine: after Google sign-in, this
    needs no key and no folder IDs (org defaults) — it finds the user's key +
    snapshot in the escrow folder and restores the full bundle (store + records
    + config). Raises RuntimeError when nothing is restorable.
    """
    from mcpbrain import auth as _auth, backup as _backup, config as _cfg

    if drive_service is None:
        creds = _auth.load_credentials()
        drive_service = _auth.build_service("drive", "v3", creds)

    info = detect_restorable(home, drive_service)
    if not info["available"]:
        raise RuntimeError(
            "No restorable backup found for this account "
            f"({info.get('user_email') or 'unknown'})."
        )
    key = _download_escrow_key(drive_service, info["escrow_folder_id"], info["user_email"])

    store_path = _cfg.store_path()
    p = Path(store_path)
    if p.exists() and p.stat().st_size > 0 and not force:
        raise RuntimeError(
            f"Store already exists at {p}. Use --force to overwrite, or delete it first."
        )
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".restore-", suffix=".enc")
    if hasattr(os, "fchmod"):
        os.fchmod(fd, 0o600)
    os.close(fd)
    tmp_path = Path(tmp)
    try:
        _backup.download_snapshot(drive_service, info["snapshot_id"], tmp_path)
        _backup.restore(
            tmp_path, p, key,
            records_dir=_cfg.records_dir(home),
            config_path=str(Path(home) / "config.json"))
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass
    return str(p)


def run_restore_main(argv=None) -> None:
    """CLI entry point: mcpbrain restore [--key <base64>] [--force] [--auto] [--check]."""
    import argparse
    parser = argparse.ArgumentParser(prog="mcpbrain restore",
                                     description="Restore the brain from Drive.")
    parser.add_argument("--key", help="Escrow key in base64; overrides config")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite an existing store")
    parser.add_argument("--auto", action="store_true",
                        help="Detect + restore automatically (key fetched from the org escrow folder)")
    parser.add_argument("--check", action="store_true",
                        help="Only report whether a restorable backup exists; restore nothing")
    ns = parser.parse_args(argv or [])

    from mcpbrain import auth as _auth, config as _cfg
    home = str(_cfg.app_dir())

    try:
        if ns.check:
            creds = _auth.load_credentials()
            drive_service = _auth.build_service("drive", "v3", creds)
            info = detect_restorable(home, drive_service)
            if info["available"]:
                print(f"Restorable backup found for {info['user_email']} "
                      f"(snapshot {info['snapshot_id']}). Run: mcpbrain restore --auto")
            else:
                print("No restorable backup found for this account.")
            sys.exit(0)
        if ns.auto:
            restored = run_restore_auto(home, force=ns.force)
        else:
            key = ns.key.encode() if ns.key else None
            restored = run_restore(home, key=key, force=ns.force)
        print(f"Restored: {restored}")
        sys.exit(0)
    except Exception as exc:
        print(f"Restore failed: {exc}", file=sys.stderr)
        sys.exit(1)
