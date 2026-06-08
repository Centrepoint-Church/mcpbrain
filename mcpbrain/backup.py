"""Store snapshot (Phase 5, Task 5.1).

Produces a single-file snapshot of the derived store. The store runs with
journal_mode=WAL, so committed writes can still live in the `-wal` sidecar.
A bare copy of the main `.sqlite3` file alone can MISS those latest writes.
snapshot() therefore runs PRAGMA wal_checkpoint(TRUNCATE) FIRST to fold the
committed WAL frames into the main database file, THEN copies the (now
complete) main file to the output path.

Encryption (Task 5.2) wraps the snapshot with an admin-escrow Fernet key so the
derived store — which holds chunk text, i.e. the user's actual mail/doc bodies —
never leaves the machine in cleartext. make_encrypted_snapshot() is the path a
Drive upload (Task 5.3) should use: the only artifact it produces is encrypted.

Drive upload (Task 5.3) ships the encrypted artifact to a per-user folder under
an org Shared Drive. The Drive API resource is INJECTED (so tests mock it) and
every call sets supportsAllDrives=True, which Shared Drives require.

Restore + delta-sync (Task 5.4) is the reinstall recovery path. On a fresh
machine the admin finds the newest snapshot on the Shared Drive
(find_latest_snapshot), downloads it (download_snapshot), decrypts+places it as
the live store (restore), then runs a normal sync cycle (run_sync_cycle from
mcpbrain.sync). The restored store carries its sync cursors, so the delta-sync
resumes from the snapshot point and catches everything that changed since.

Scope: snapshot + checkpoint + encryption + Shared Drive upload + restore/
find/download. store.py remains the sole schema owner — this module adds no
schema and performs no data writes beyond the checkpoint PRAGMA. Sync logic is
NOT reimplemented here: the delta-sync step reuses run_sync_cycle.
"""

import logging
import os
import shutil
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet

from mcpbrain.store import _open_db

log = logging.getLogger(__name__)


def snapshot(store_path, out_path) -> Path:
    """Produce a single-file snapshot of the derived store at store_path.

    Runs PRAGMA wal_checkpoint(TRUNCATE) to fold committed WAL frames into the
    main DB file (WAL implication — a bare file copy can miss the latest
    writes), then copies the checkpointed DB file to out_path. Returns out_path.

    Raises RuntimeError on a busy checkpoint (busy != 0) BEFORE copying anything,
    so a returned path always reflects a complete, checkpointed artifact rather
    than a degraded one. Under the single-writer invariant (daemon is the sole
    writer) a busy checkpoint is abnormal.

    Accepts str or Path for both arguments. Creates out_path's parent dir if
    needed. A clean TRUNCATE checkpoint empties the `-wal` sidecar, so the copied
    main file alone is complete — only the main file is copied.
    """
    store_path = Path(store_path)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Write-mode connection: wal_checkpoint(TRUNCATE) needs the write lock.
    # Loading sqlite-vec is precautionary so opening a DB containing vec0 virtual
    # tables can't fail schema validation on connect.
    db = _open_db(store_path, read_only=False)
    try:
        # MUST run BEFORE the copy. TRUNCATE flushes all committed WAL frames
        # into the main DB file and resets the WAL to zero length.
        row = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        # Result is (busy, log, checkpointed). busy != 0 means a reader/writer
        # blocked the checkpoint and frames may remain in the WAL. Raise rather
        # than copy a degraded artifact — a returned path must be complete.
        busy = row[0] if row is not None else 0
    finally:
        db.close()

    if busy != 0:
        raise RuntimeError(
            f"wal_checkpoint(TRUNCATE) busy={busy}; snapshot aborted to avoid "
            "an incomplete artifact"
        )

    # After a clean TRUNCATE checkpoint the main file is complete; copy it as the
    # single snapshot artifact. copy2 preserves metadata (mtime).
    shutil.copy2(store_path, out_path)
    return out_path


def generate_escrow_key() -> bytes:
    """Generate a new admin-escrow key.

    Returns a urlsafe-base64-encoded 32-byte key suitable for Fernet. This is
    the org-held key an admin uses to recover a user's backup. It is generated
    here and supplied to the encrypt/decrypt functions by the caller — this
    module never reads, stores, or hardcodes a real key.
    """
    return Fernet.generate_key()


def encrypt_file(in_path, out_path, key: bytes) -> Path:
    """Encrypt in_path -> out_path with the Fernet escrow key. Returns out_path.

    Reads the whole file into memory (fine at this scale — the derived store is
    modest), encrypts with the supplied key, and writes the ciphertext.
    """
    in_path = Path(in_path)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # One Fernet instance per call; callers doing batch ops should construct
    # Fernet(key) once and reuse it rather than calling this in a tight loop.
    token = Fernet(key).encrypt(in_path.read_bytes())
    out_path.write_bytes(token)
    return out_path


def decrypt_file(in_path, out_path, key: bytes) -> Path:
    """Decrypt in_path -> out_path with the Fernet escrow key. Returns out_path.

    Raises cryptography.fernet.InvalidToken if the key is wrong or the
    ciphertext has been tampered with (Fernet authenticates the token).
    """
    in_path = Path(in_path)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    plaintext = Fernet(key).decrypt(in_path.read_bytes())
    out_path.write_bytes(plaintext)
    return out_path


def make_encrypted_snapshot(store_path, out_path, key: bytes) -> Path:
    """Snapshot the store (Task 5.1), encrypt it to out_path, return out_path.

    The intermediate plaintext snapshot is written to a local temp file next to
    out_path, then encrypted to out_path with the escrow key. The temp is always
    deleted in a finally — even if the snapshot or encryption raises — so the
    cleartext store never persists beyond that transient local temp. out_path is
    the only artifact left behind, and it is encrypted.
    """
    store_path = Path(store_path)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Temp next to out_path so it lands on the same filesystem and inside the
    # caller's chosen directory (tests can assert no stray plaintext remains).
    fd, tmp_name = tempfile.mkstemp(
        prefix=".snap-", suffix=".plain.sqlite3", dir=str(out_path.parent)
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        snapshot(store_path, tmp_path)
        encrypt_file(tmp_path, out_path, key)
    finally:
        # Promptly destroy the transient cleartext, regardless of outcome.
        tmp_path.unlink(missing_ok=True)
    return out_path


FOLDER_MIME = "application/vnd.google-apps.folder"


def _guard_user_id(user_id: str) -> None:
    """Reject a user_id with characters unsafe inside a single-quoted Drive query.

    user_id is caller-controlled and interpolated into a single-quoted Drive
    `q` filter; an apostrophe or backslash would break the parse or alter the
    query structure. Both upload_snapshot and find_latest_snapshot rely on this.
    """
    if "'" in user_id or "\\" in user_id:
        raise ValueError(
            f"user_id contains characters unsafe in a Drive query: {user_id!r}"
        )


def _default_media(path):
    """Lazy default media factory — imports googleapiclient only when an upload
    actually runs, so `import mcpbrain.backup` does not require the SDK.

    Non-resumable single PUT: the encrypted snapshot (~750MB) uploads in one
    request. This is deliberate — googleapiclient's resumable path rides on
    httplib2, which mishandles the 308 "Resume Incomplete" responses and raises
    RedirectMissingLocation (a long-standing httplib2 bug). A single PUT avoids
    308s entirely; the real fix for the original failure was the socket timeout
    (auth.build_service now uses a generous one), not chunking. A mid-upload
    network blip simply fails this run and the daily cadence retries.
    """
    from googleapiclient.http import MediaFileUpload

    return MediaFileUpload(str(path), resumable=False)


def upload_snapshot(
    service, file_path, shared_drive_id: str, user_id: str, *, media_factory=None
) -> str:
    """Upload an (already-encrypted) snapshot to <shared_drive>/<user_id>/ on a
    Shared Drive. Finds or creates the per-user folder, uploads file_path into
    it, and returns the created file id.

    SAFETY: callers MUST pass the ENCRYPTED artifact from make_encrypted_snapshot
    (Task 5.2), never a raw plaintext snapshot — no cleartext mail/doc bodies
    may reach the Shared Drive. This function uploads whatever file_path points
    at and does not encrypt; wiring that guarantee (a CLI/daemon backup trigger
    that chains make_encrypted_snapshot -> upload_snapshot) is a Phase 6 job.

    `service` is a Google Drive API resource (googleapiclient discovery build
    result), INJECTED so tests can mock it. Every Drive call sets
    supportsAllDrives=True (Shared Drives require it). `media_factory(path) ->
    media_body` builds the upload body; defaults to a lazy MediaFileUpload
    import so importing this module does not require googleapiclient. Accepts
    str or Path for file_path.
    """
    _guard_user_id(user_id)

    file_path = Path(file_path)
    media_factory = media_factory or _default_media

    # 1. Find the per-user folder directly under the shared drive root.
    q = (
        f"name = '{user_id}' and mimeType = '{FOLDER_MIME}' "
        f"and trashed = false and '{shared_drive_id}' in parents"
    )
    resp = (
        service.files()
        .list(
            q=q,
            corpora="drive",
            driveId=shared_drive_id,
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
            fields="files(id, name)",
        )
        .execute()
    )
    files = resp.get("files", [])

    # 2. Reuse an existing folder, else create it under the shared drive.
    if files:
        # Multiple same-named folders shouldn't occur under normal operation; take the first.
        folder_id = files[0]["id"]
    else:
        folder_id = (
            service.files()
            .create(
                body={
                    "name": user_id,
                    "mimeType": FOLDER_MIME,
                    "parents": [shared_drive_id],
                },
                supportsAllDrives=True,
                fields="id",
            )
            .execute()["id"]
        )

    # 3. Upload the artifact into the per-user folder (single PUT — see
    # _default_media for why non-resumable). num_retries gives the library's
    # exponential backoff on transient 5xx. Pass a str path to the factory
    # (matches MediaFileUpload(str(path))).
    media = media_factory(str(file_path))
    created = (
        service.files()
        .create(
            body={"name": file_path.name, "parents": [folder_id]},
            media_body=media,
            supportsAllDrives=True,
            fields="id",
        )
        .execute(num_retries=5)
    )
    return created["id"]


def restore(encrypted_path, dest_store_path, key: bytes) -> Path:
    """Decrypt an encrypted snapshot artifact and place it as the live store.

    This is the decrypt -> place-file step of reinstall recovery: it takes the
    encrypted artifact (from make_encrypted_snapshot, downloaded via
    download_snapshot) and writes the cleartext SQLite store to
    dest_store_path, the live store location. Reuses decrypt_file, so a wrong
    key or tampered artifact raises cryptography.fernet.InvalidToken before any
    file is placed at the destination.

    The caller then runs a delta-sync (run_sync_cycle from mcpbrain.sync) to
    catch everything that changed after the snapshot — the restored store
    carries its sync cursors, so sync resumes from the snapshot point rather
    than re-fetching from scratch.

    Creates dest_store_path's parent dir if needed. Accepts str or Path.
    Returns the destination Path.
    """
    dest_store_path = Path(dest_store_path)
    return decrypt_file(encrypted_path, dest_store_path, key)


def find_latest_snapshot(service, shared_drive_id: str, user_id: str) -> str | None:
    """Return the id of the newest snapshot in <shared_drive>/<user_id>/, or None.

    Mirrors upload_snapshot's folder convention: a per-user folder named
    user_id directly under shared_drive_id. Lists that folder's files and
    returns the most recent by createdTime (modifiedTime breaks ties), or None
    if the per-user folder is absent or holds no files.

    `service` is an injected Google Drive API resource (tests mock it). Every
    list call sets corpora="drive", driveId, includeItemsFromAllDrives=True and
    supportsAllDrives=True — Shared Drives require them. Guards user_id the same
    way upload_snapshot does.
    """
    _guard_user_id(user_id)

    # 1. Find the per-user folder directly under the shared drive root.
    folder_q = (
        f"name = '{user_id}' and mimeType = '{FOLDER_MIME}' "
        f"and trashed = false and '{shared_drive_id}' in parents"
    )
    folder_resp = (
        service.files()
        .list(
            q=folder_q,
            corpora="drive",
            driveId=shared_drive_id,
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
            fields="files(id, name)",
        )
        .execute()
    )
    folders = folder_resp.get("files", [])
    if not folders:
        return None
    folder_id = folders[0]["id"]

    # 2. List the snapshot files inside the per-user folder, newest first.
    files_q = f"'{folder_id}' in parents and trashed = false"
    files_resp = (
        service.files()
        .list(
            q=files_q,
            corpora="drive",
            driveId=shared_drive_id,
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
            fields="files(id, name, createdTime, modifiedTime)",
        )
        .execute()
    )
    files = files_resp.get("files", [])
    if not files:
        return None

    # Newest first. createdTime is RFC3339 (lexicographically sortable);
    # modifiedTime breaks ties. Missing fields sort oldest.
    files.sort(
        key=lambda f: (f.get("createdTime", ""), f.get("modifiedTime", "")),
        reverse=True,
    )
    return files[0]["id"]


def prune_snapshots(service, shared_drive_id: str, user_id: str, *, keep: int) -> int:
    """Delete all but the newest `keep` snapshots in <shared_drive>/<user_id>/.

    Bounds the daily full-snapshot uploads so they don't grow without limit
    (~750MB/day otherwise). Sorts by the same (createdTime, modifiedTime) key
    find_latest_snapshot uses, so the snapshot a restore would pick is always
    among those kept. Best-effort: a delete failure is logged and skipped, never
    raised. keep <= 0 means "keep everything" (no-op). Returns the count deleted.
    """
    _guard_user_id(user_id)
    if keep <= 0:
        return 0

    folder_q = (
        f"name = '{user_id}' and mimeType = '{FOLDER_MIME}' "
        f"and trashed = false and '{shared_drive_id}' in parents"
    )
    folders = (
        service.files()
        .list(q=folder_q, corpora="drive", driveId=shared_drive_id,
              includeItemsFromAllDrives=True, supportsAllDrives=True,
              fields="files(id)")
        .execute()
        .get("files", [])
    )
    if not folders:
        return 0
    folder_id = folders[0]["id"]

    files = (
        service.files()
        .list(q=f"'{folder_id}' in parents and trashed = false", corpora="drive",
              driveId=shared_drive_id, includeItemsFromAllDrives=True,
              supportsAllDrives=True, fields="files(id, name, createdTime, modifiedTime)")
        .execute()
        .get("files", [])
    )
    files.sort(key=lambda f: (f.get("createdTime", ""), f.get("modifiedTime", "")),
               reverse=True)

    deleted = 0
    for f in files[keep:]:
        try:
            service.files().delete(fileId=f["id"], supportsAllDrives=True).execute()
            deleted += 1
        except Exception as exc:  # noqa: BLE001 — pruning must never break a backup
            log.warning("prune_snapshots: could not delete %s (%s): %s",
                        f.get("name"), f["id"], exc)
    if deleted:
        log.info("prune_snapshots: deleted %d old snapshot(s), kept newest %d",
                 deleted, keep)
    return deleted


def _default_downloader(fh, request):
    """Lazy default downloader factory — imports googleapiclient only when a
    download actually runs, so `import mcpbrain.backup` does not require the
    SDK. Mirrors the _default_media lazy-import pattern."""
    from googleapiclient.http import MediaIoBaseDownload

    return MediaIoBaseDownload(fh, request)


def download_snapshot(service, file_id: str, dest_path, *, downloader_factory=None) -> Path:
    """Download a (encrypted) Drive file to dest_path. Returns dest Path.

    Uses service.files().get_media(fileId=..., supportsAllDrives=True) and a
    chunked download driven by `downloader_factory(fh, request) -> downloader`.
    The downloader must expose next_chunk() -> (status, done) like
    googleapiclient.http.MediaIoBaseDownload. The factory defaults to a lazy
    MediaIoBaseDownload import so importing this module needs no SDK; tests
    inject a fake. Accepts str or Path for dest_path; creates its parent dir.
    """
    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    downloader_factory = downloader_factory or _default_downloader

    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    with open(dest_path, "wb") as fh:
        downloader = downloader_factory(fh, request)
        done = False
        while not done:
            _status, done = downloader.next_chunk()
    return dest_path


def download_and_restore(bc, store, file_id) -> Path:
    """Download an encrypted snapshot from Drive and restore it as the live store.

    Composes download_snapshot (Drive -> a dedicated temp file) and restore
    (decrypt -> store.path). Used by the daemon's restore-on-first-run path.
    Returns the restored store Path. A wrong key or tampered artifact raises
    cryptography.fernet.InvalidToken from restore before the store is replaced.

    The download lands in a dedicated temp file under the store's parent dir
    (same filesystem as the decrypt target), NOT bc.out_path. bc.out_path is the
    stable path maybe_backup writes its periodic upload artifact to; downloading
    there would clobber it and a mid-download failure would leave a corrupt file
    at the backup-upload path. The temp file is always removed in a finally.
    """
    dest = Path(store.path)
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), prefix=".restore-", suffix=".enc")
    # mkstemp already creates 0600 on most platforms; set it explicitly to stay
    # consistent with the rest of the codebase, which always sets 0600
    # deliberately on private artifacts.
    os.fchmod(fd, 0o600)
    os.close(fd)
    tmp = Path(tmp)
    try:
        download_snapshot(bc.drive_service, file_id, tmp)
        return restore(tmp, dest, bc.key)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
