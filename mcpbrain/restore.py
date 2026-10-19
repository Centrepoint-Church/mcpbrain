"""Recovery path: restore the brain from the newest restore point on Drive.

Every entry point here (`mcpbrain restore`, `restore --auto`, `restore
--check`, and the wizard's /api/backup/auto via run_restore_auto) goes through
backup_restore: the newest restore point by capture time across the
incremental chains and every legacy folder, verified before it is swapped in,
with the records repo and config.json restored alongside the store.
"""
from __future__ import annotations

import logging
import os
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# The daemon-running probe's connect timeout. Loopback answers or refuses
# at once; this only bounds a pathological local stack.
_DAEMON_PROBE_TIMEOUT_S = 0.5


class DaemonRunningError(RuntimeError):
    """A separate-process restore was refused because the daemon is up."""


def daemon_running(home: str) -> bool:
    """True when something accepts a TCP connection on 127.0.0.1:<control_port>.

    A raw connect, NOT ControlClient.status(): that maps 401/503 (a daemon that
    is up but gated, or a token mismatch) to DaemonUnavailable, which would
    read a live daemon as stopped. A missing or garbage control_port file
    means not running."""
    try:
        port = int((Path(home) / "control_port").read_text().strip())
    except (OSError, ValueError):
        return False
    if not 0 < port < 65536:
        return False
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=_DAEMON_PROBE_TIMEOUT_S):
            return True
    except OSError:
        return False


def refuse_if_daemon_running(home: str) -> None:
    """Raise DaemonRunningError when the daemon is up.

    A restore from a SEPARATE process cannot be made safe against a live
    daemon: any daemon connection opened before the busy-checked checkpoint
    that writes after os.replace appends frames to the -wal/-shm the old and
    new files share by name, and when it closes the restored store is
    silently rolled back. The daemon's own restores (start-up, the wizard's
    /api/backup/auto) pass `swap` and run behind its restoring gate instead.
    --force does not bypass this: it answers a different question
    (overwriting a non-empty store)."""
    if daemon_running(home):
        raise DaemonRunningError(
            "the mcpbrain daemon is running, and restoring under it can silently "
            "lose the restore. Stop it first with "
            "`launchctl bootout gui/$(id -u)/com.mcpbrain` (not `launchctl stop`: "
            "KeepAlive relaunches it), run the restore, then start it again with "
            "`launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.mcpbrain.plist`.")


class PartialRestore(RuntimeError):
    """The store was swapped in, but the records repo / config.json could not
    be placed. The store now HAS content, so this is not a failed restore to
    retry (the empty-store rule would refuse it): it is reported, and the
    records can be recovered by hand or by `mcpbrain restore --force` with the
    daemon stopped."""

    def __init__(self, msg: str, path: Path | None = None):
        super().__init__(msg)
        self.path = path


def _restore_newest(home: str, drive_service, key: bytes, swap):
    """backup_restore.restore_latest into the live store path, records repo and
    config.json. `swap(stage)` is the caller's store swap (the daemon's gated
    one); None is backup.swap_in_store, only safe with the daemon STOPPED
    (run_restore/run_restore_auto refuse otherwise).

    Raises NoRestorePoint as RuntimeError, and PartialRestore when the store
    swapped but the records/config placement after it failed (R24)."""
    from mcpbrain import backup as _backup, backup_restore, config as _cfg
    p = Path(_cfg.store_path())
    swapped: list[bool] = []

    def _swap(stage, dest):
        if swap is None:
            _backup.swap_in_store(stage, dest)
        else:
            swap(stage)
        swapped.append(True)

    try:
        return backup_restore.restore_latest(
            home, drive_service, key, dest_store=p,
            records_dir=_cfg.records_dir(home),
            config_path=str(Path(home) / "config.json"),
            swap=_swap, budget_s=None)
    except backup_restore.NoRestorePoint as exc:
        raise RuntimeError(str(exc)) from exc
    except Exception as exc:
        if not swapped:
            raise
        msg = (f"the store was restored to {p}, but the records repo / config.json "
               f"could not be placed: {type(exc).__name__}: {exc}")
        log.error("restore partly done: %s", msg)
        raise PartialRestore(msg, p) from exc


def run_restore(home: str, *, key: bytes | None = None, force: bool = False,
                swap=None):
    """Restore the newest restore point on Drive to the store path (plus the
    records repo and config.json). Returns backup_restore.RestoreResult.

    Resolves the escrow key from config (backup.escrow_key) unless key is given.
    Raises ValueError if backup is not configured.
    Raises RuntimeError if the store already exists and force=False, or when
    nothing is restorable; PartialRestore when only the store was placed.

    With `swap` None (the `mcpbrain restore` CLI, a separate process) this is
    NOT safe while the daemon runs, so it raises DaemonRunningError before
    anything is written -- force or not.
    """
    from mcpbrain import auth as _auth, config as _cfg

    if swap is None:
        refuse_if_daemon_running(home)

    cfg = _cfg.read_config(home)
    backup_cfg = cfg.get("backup") or {}

    if key is None:
        stored_key = backup_cfg.get("escrow_key", "")
        if not stored_key:
            raise ValueError("Backup not configured. Run the install skill and enable backup.")
        key = stored_key.encode()

    p = Path(_cfg.store_path())
    if p.exists() and p.stat().st_size > 0 and not force:
        raise RuntimeError(
            f"Store already exists at {p}. Use --force to overwrite, or delete it first."
        )

    creds = _auth.load_credentials()
    drive_service = _auth.build_service("drive", "v3", creds,
                                        timeout_s=_auth.DEFAULT_HTTP_TIMEOUT_S)
    return _restore_newest(home, drive_service, key, swap)


class EscrowKeyUnavailable(RuntimeError):
    """The escrow key's lookup or download FAILED (network, Drive error). Not
    the same as "absent": a caller that treated it as absent would generate a
    new key and, before R27, overwrite the one every existing backup needs."""


def _download_escrow_key(drive_service, escrow_folder_id: str, user_email: str) -> bytes | None:
    """<user_email>.key from the escrow folder: its bytes, or None when Drive
    definitively has no such file. Any failure to list or download it raises
    EscrowKeyUnavailable (chained to the original error, so the transient
    classifier still sees the cause) -- never None.

    The key is uploaded by backup_setup at enable time. Fetching it from the
    Shared Drive (which the user already has access to) means recovery needs no
    manually-entered key — the user just signs in with Google.
    """
    import tempfile

    from mcpbrain import backup as _backup
    name = f"{user_email}.key"
    try:
        # Parent-based query: the escrow folder is nested inside a Shared Drive,
        # so its id is NOT a valid driveId — query by parent with corpora=allDrives.
        files = _backup._list_in_drives(
            drive_service,
            f"name = '{name}' and trashed = false and '{escrow_folder_id}' in parents",
            fields="files(id, name)")
    except Exception as exc:
        raise EscrowKeyUnavailable(
            f"could not look up the escrow key {name}: {type(exc).__name__}: {exc}") from exc
    if not files:
        return None
    fd, tmp = tempfile.mkstemp(prefix=".escrowkey-")
    os.close(fd)
    tmp_path = Path(tmp)
    try:
        _backup.download_snapshot(drive_service, files[0]["id"], tmp_path)
        key = tmp_path.read_bytes()
    except Exception as exc:
        raise EscrowKeyUnavailable(
            f"could not download the escrow key {name}: {type(exc).__name__}: {exc}") from exc
    finally:
        tmp_path.unlink(missing_ok=True)
    if not key.strip():
        raise EscrowKeyUnavailable(f"the escrow key {name} downloaded empty")
    return key


def _escrow_folder(home: str) -> str | None:
    """The escrow FOLDER id for the auto-restore convention.

    Prefers fleet.escrow_folder_id, else this build's tenant profile (so detection
    works on a fresh machine before the wizard writes config). Deliberately does
    NOT read backup.shared_drive_id — in legacy configs that is the Shared Drive
    ROOT (the daemon's drive-root upload target), not the nested escrow folder.
    None when neither resolves.
    """
    from mcpbrain import config as _cfg, tenant
    cfg = _cfg.read_config(home)
    prof = tenant.profile()
    return (
        (cfg.get("fleet") or {}).get("escrow_folder_id")
        or (prof.escrow_folder_id if prof else None)
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


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _source(drive_service, cand, names: dict) -> str:
    """"chain" for a manifest; "legacy:<folder name>" for a single-file
    snapshot (the folder id when its name cannot be read). Display only."""
    if cand.kind == "manifest":
        return "chain"
    fid = cand.folder_id
    if fid not in names:
        try:
            from mcpbrain import backup as _backup
            meta = drive_service.files().get(
                fileId=fid, fields="name", supportsAllDrives=True,
            ).execute(num_retries=_backup._NUM_RETRIES)
            names[fid] = (meta or {}).get("name") or fid
        except Exception:  # noqa: BLE001 — a label, never a reason to fail
            names[fid] = fid
    return f"legacy:{names[fid]}"


def point_info(drive_service, cand) -> dict:
    """{"restore_point": ISO-8601 UTC capture time, "source": "chain" |
    "legacy:<folder>"} for one restore point (a backup_restore.Candidate)."""
    return {"restore_point": _iso(cand.captured_at),
            "source": _source(drive_service, cand, {})}


def _describe(drive_service, cands, limit: int) -> list[dict]:
    names: dict = {}
    return [{"restore_point": _iso(c.captured_at), "captured_at": c.captured_at,
             "source": _source(drive_service, c, names), "kind": c.kind,
             "label": c.label, "file_id": c.file_id} for c in cands[:limit]]


def detect_restorable(home: str, drive_service) -> dict:
    """Report whether an existing backup can be restored for this account.

    Resolves the escrow folder (config → tenant profile) and the user's email,
    fetches the user's escrow key, and lists the restore points
    (backup_restore.candidates: every chain's newest manifest and every legacy
    file, newest first). Returns {available, snapshot_id, has_key, user_email,
    escrow_folder_id, restore_point, source, candidates}: `snapshot_id` is the
    chosen point's file id, `restore_point` its capture time (ISO-8601 UTC),
    `source` "chain" or "legacy:<folder>", `candidates` the first 11 points
    described. Never raises -- degrades to available=False (with `reason`).
    """
    from mcpbrain import backup_restore
    # The escrow convention is email-keyed (<email>.key). Use the signed-in
    # account email (owner_email → Google account → legacy backup.user_id) so
    # restore works before the profile step is saved.
    user_email = account_email(home)
    if not user_email:
        return {"available": False, "reason": "no account signed in yet"}
    folder = _escrow_folder(home)
    if not folder:
        return {"available": False, "reason": "no escrow folder configured"}
    out = {"available": False, "snapshot_id": None, "has_key": False,
           "user_email": user_email, "escrow_folder_id": folder,
           "restore_point": None, "source": None, "candidates": []}
    try:
        key = _download_escrow_key(drive_service, folder, user_email)
    except EscrowKeyUnavailable as exc:
        log.warning("restore detection: %s", exc)
        out["reason"] = str(exc)
        return out
    out["has_key"] = bool(key)
    if not key:
        return out
    try:
        cands = backup_restore.candidates(home, drive_service, key)
    except Exception as exc:  # noqa: BLE001 — detection degrades, never raises
        log.warning("restore detection: could not list restore points: %s", exc)
        out["reason"] = f"{type(exc).__name__}: {exc}"
        return out
    if not cands:
        return out
    described = _describe(drive_service, cands, 11)
    out.update(available=True, snapshot_id=cands[0].file_id,
               restore_point=described[0]["restore_point"],
               source=described[0]["source"], candidates=described)
    return out


def run_restore_auto(home: str, *, force: bool = False, drive_service=None,
                     swap=None):
    """Detect and restore the newest restore point, fetching the escrow key
    from Drive. Returns backup_restore.RestoreResult.

    The turnkey recovery path for a fresh machine: after Google sign-in, this
    needs no key and no folder IDs (org defaults) — it finds the user's key in
    the escrow folder and restores the full bundle (store + records + config).
    Raises RuntimeError when nothing is restorable, PartialRestore when only
    the store was placed.

    With `swap` None (the `mcpbrain restore --auto` CLI, a separate process)
    this is NOT safe while the daemon runs, so it raises DaemonRunningError
    before anything is written -- force or not. The daemon's own caller
    (/api/backup/auto) passes its gated `swap`.
    """
    from mcpbrain import auth as _auth, config as _cfg

    if swap is None:
        refuse_if_daemon_running(home)

    if drive_service is None:
        creds = _auth.load_credentials()
        drive_service = _auth.build_service("drive", "v3", creds,
                                            timeout_s=_auth.DEFAULT_HTTP_TIMEOUT_S)

    info = detect_restorable(home, drive_service)
    if not info["available"]:
        raise RuntimeError(
            "No restorable backup found for this account "
            f"({info.get('user_email') or 'unknown'})."
            + (f" {info['reason']}" if info.get("reason") else "")
        )
    key = _download_escrow_key(drive_service, info["escrow_folder_id"], info["user_email"])
    if not key:
        raise RuntimeError("The escrow key could not be fetched from Drive; try again.")

    p = Path(_cfg.store_path())
    if p.exists() and p.stat().st_size > 0 and not force:
        raise RuntimeError(
            f"Store already exists at {p}. Use --force to overwrite, or delete it first."
        )
    return _restore_newest(home, drive_service, key, swap)


def _age(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 48 * 3600:
        return f"{int(seconds // 3600)} h ago"
    return f"{int(seconds // 86400)} d ago"


def _when(ts: float, now: float | None = None) -> str:
    """'2026-10-19 17:13 (3 h ago)', local time."""
    now = time.time() if now is None else now
    return (f"{datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M')} "
            f"({_age(now - ts)})")


def _print_result(result, drive_service) -> None:
    used = result.used
    print(f"Restored: {result.path}")
    print(f"Restore point: {_when(used.captured_at)} from "
          f"{_source(drive_service, used, {})} ({used.label})")
    for cand, why in result.fallbacks:
        print(f"  skipped {cand.label}: {why}")


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
            drive_service = _auth.build_service("drive", "v3", creds,
                                                timeout_s=_auth.DEFAULT_HTTP_TIMEOUT_S)
            info = detect_restorable(home, drive_service)
            if info["available"]:
                first, *others = info["candidates"]
                print(f"Restore point for {info['user_email']}: "
                      f"{_when(first['captured_at'])} from {first['source']} "
                      f"({first['label']})")
                if others:
                    print("Older restore points (used only if a newer one fails):")
                    for c in others[:10]:
                        print(f"  {_when(c['captured_at'])}  {c['source']}  {c['label']}")
                print("Stop the daemon first (launchctl bootout gui/$(id -u)/com.mcpbrain), "
                      "then run: mcpbrain restore --auto")
            else:
                print("No restorable backup found for this account."
                      + (f" ({info['reason']})" if info.get("reason") else ""))
            sys.exit(0)
        if ns.auto:
            refuse_if_daemon_running(home)       # before any Drive call
            creds = _auth.load_credentials()
            drive_service = _auth.build_service("drive", "v3", creds,
                                                timeout_s=_auth.DEFAULT_HTTP_TIMEOUT_S)
            result = run_restore_auto(home, force=ns.force, drive_service=drive_service)
        else:
            key = ns.key.encode() if ns.key else None
            result = run_restore(home, key=key, force=ns.force)
            drive_service = None
        _print_result(result, drive_service)
        sys.exit(0)
    except PartialRestore as exc:
        print(f"Restore partly done: {exc}. The store is restored; the records repo "
              "and config.json were left as they were.", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"Restore failed: {exc}", file=sys.stderr)
        sys.exit(1)
