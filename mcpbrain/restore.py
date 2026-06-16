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
        _backup.restore(tmp_path, p, key)
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass

    return str(p)


def run_restore_main(argv=None) -> None:
    """CLI entry point: mcpbrain restore [--key <base64>] [--force]."""
    import argparse
    parser = argparse.ArgumentParser(prog="mcpbrain restore",
                                     description="Restore the brain store from Drive.")
    parser.add_argument("--key", help="Escrow key in base64; overrides config")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite an existing store")
    ns = parser.parse_args(argv or [])

    from mcpbrain import config as _cfg
    home = str(_cfg.app_dir())
    key = ns.key.encode() if ns.key else None

    try:
        restored = run_restore(home, key=key, force=ns.force)
        print(f"Restored: {restored}")
        sys.exit(0)
    except Exception as exc:
        print(f"Restore failed: {exc}", file=sys.stderr)
        sys.exit(1)
