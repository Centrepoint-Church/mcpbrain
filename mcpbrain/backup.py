"""Store snapshot (Phase 5, Task 5.1).

Produces a single-file snapshot of the derived store. The store runs with
journal_mode=WAL, so committed writes can live in the `-wal` sidecar and a bare
copy of the main `.sqlite3` file alone can MISS them. snapshot() therefore uses
VACUUM INTO, which reads through the WAL under one consistent read transaction
and needs no exclusive checkpoint — see snapshot()'s own docstring for why the
previous checkpoint-then-copy approach was both blockable and tearable.

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

import errno
import logging
import os
import shutil
import sqlite3
import struct
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from mcpbrain.store import _open_db

log = logging.getLogger(__name__)

# Framed archive format. THREE shapes are readable; only v3 is ever written.
#
# A single Fernet token authenticates the whole artifact, but producing or
# reading one requires holding all of it -- plus several transient copies --
# in memory. At multi-GB snapshot sizes that OOMs the daemon: live incident
# 2026-08-04, a 4.24GB snapshot on a 16GB box. Hence framing.
#
#   archive  := MAGIC || frame+
#   frame    := uint32be len(token) || token
#   token v3 := Fernet(16-byte archive_id || uint32be index || uint8 is_final || chunk)
#   token v2 := Fernet(                      uint32be index || uint8 is_final || chunk)
#
# Fernet has no AAD, so everything binding a frame to its place goes INSIDE the
# authenticated plaintext. Both framed shapes therefore detect dropping,
# duplicating, reordering and truncating frames rather than silently yielding a
# short or scrambled store.
#
# WHAT v3 ADDS, AND WHAT v2 DOES NOT HAVE. The ordinal alone is not enough. The
# daily cadence reuses ONE escrow key, and that key lives in the
# all-members-readable fleet folder, so "two archives under the same key" is the
# steady state. With only (index, is_final) authenticated -- i.e. in v2 --
# frames 0..k of archive A followed by frames k+1..final of archive B form a run
# with correct indices and a correct final flag: a chimera store the reader
# accepts, assemblable with NO key at all. v3's random per-archive id, required
# constant across every frame of a read, closes that. It is generated per
# _FrameWriter and never derived from content or key (a derived id that two
# identical archives would share re-opens the splice).
#
# v2 GENUINELY CANNOT PROVIDE THAT, and is read-only legacy: its frames bind
# ORDER ONLY, and the reader does not and cannot enforce archive identity on
# them. Do not describe v2 as splice-protected. It stays readable because real
# v2 archives exist off-machine -- the author box held a 4.2GB v2 snapshot with
# retain=7 more on the Shared Drive -- and losing the ability to read them would
# silently invalidate every existing backup.
#
# WHY THE MAGIC HAD TO CHANGE. Both framed headers are self-describing only via
# the magic: a v2 archive read with the v3 header struct consumes 21 bytes of
# header where the frame carries 5, which MISPARSES rather than fails. Adding
# the archive id under the v2 magic did exactly that to the real archives above.
# So: a header change gets a NEW MAGIC, always, and an unrecognised magic is an
# explicit error (UnsupportedArchive) rather than a fallback guess. All magics
# are the same length so the reader can dispatch on one fixed-size read.
#
# Archives written before v2 lack a magic entirely and are a single Fernet token
# over the whole file; they are still read as one token, identified by Fernet's
# own version-byte prefix (see decrypt_file).
_ARCHIVE_MAGIC_V3 = b"MCPBRAIN-ENC-v3\n"
_ARCHIVE_MAGIC_V2 = b"MCPBRAIN-ENC-v2\n"   # legacy, READ-ONLY: no archive id
_ARCHIVE_MAGIC = _ARCHIVE_MAGIC_V3         # what new archives are written as
_MAGIC_LEN = len(_ARCHIVE_MAGIC_V3)
assert len(_ARCHIVE_MAGIC_V2) == _MAGIC_LEN, "magics must be one fixed length"
# urlsafe-base64 of Fernet's 0x80 version byte + a 32-bit-range timestamp. Every
# Fernet token starts with this, and no framed archive can (the magics above are
# ASCII "MCPBRAIN-..."), so it identifies a pre-v2 single-token archive.
_FERNET_PREFIX = b"gAAAAA"
_ENCRYPT_CHUNK = 8 * 1024 * 1024
_ARCHIVE_ID_LEN = 16
_FRAME_LEN = struct.Struct(">I")           # length prefix of each frame
# v3: (archive_id, index, is_final) inside each token
_FRAME_HEADER = struct.Struct(f">{_ARCHIVE_ID_LEN}sIB")
_FRAME_HEADER_V2 = struct.Struct(">IB")    # v2 legacy: (index, is_final)


class UnsupportedArchive(ValueError):
    """The artifact is not an archive shape this build knows how to read.

    A distinct type, not InvalidToken: InvalidToken means "this IS one of our
    archives and it failed authentication", which a caller may reasonably read
    as a wrong key or tampering. This means "I do not know what this file is",
    and it exists so an unrecognised format can never again be silently
    misparsed as a known one.
    """


def snapshot(store_path, out_path) -> Path:
    """Produce a single-file snapshot of the derived store at store_path.

    Uses VACUUM INTO, which builds the output from one consistent read
    transaction. That choice is load-bearing in three ways:

    1. It needs no exclusive checkpoint. PRAGMA wal_checkpoint(TRUNCATE) blocks
       until there is no writer AND every reader is on the newest snapshot, so
       a single held read transaction blocks it absolutely — measured busy=1 on
       6 of 6 attempts with checkpointed_frames=0, against brain_graph reads
       that run 6.3s median on the live store and outlive the 5000ms
       busy_timeout. Removing the need for the checkpoint removes that class.
    2. It cannot be torn. The previous implementation checkpointed and then
       shutil.copy2'd the main DB file, during which any connection's
       wal_autocheckpoint could write pages into that file mid-copy. _bulk_lock
       does not cover the daemon's control-API threads, which is where routed
       tool writes execute. A busy=0 result never made the following copy safe.
    3. It excludes free pages, which is what keeps the artifact small once
       per-file enrich_payloads keying frees ~11.3GB onto the freelist.

    Raises before writing anything if the destination cannot be cleared, and
    unlinks a partial destination if the copy fails, so a returned path always
    reflects a complete artifact. Accepts str or Path for both arguments.
    """
    store_path = Path(store_path)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # VACUUM INTO refuses outright if the output exists ("output file already
    # exists"), so clearing is required, not defensive. The sidecars matter
    # too: a stale -wal left beside a previous artifact would be applied over
    # the fresh one the first time it is opened.
    _clear_artifact(out_path)

    # Write-mode connection. mode=ro would be a better fit for a read-only
    # operation, but a read-only connection cannot create the -shm file when
    # nothing else has the DB open — which is exactly how bin/repair.py and
    # bin/consolidate.py run, with the daemon stopped.
    db = _open_db(store_path, read_only=False)
    try:
        db.execute("VACUUM INTO ?", (str(out_path),))
    except BaseException:
        _clear_artifact(out_path)
        raise
    finally:
        db.close()

    try:
        _verify_artifact(out_path)
    except BaseException:
        _clear_artifact(out_path)
        raise
    return out_path


def _clear_artifact(out_path: Path) -> None:
    """Remove an artifact path and any sidecars left beside it."""
    for p in (out_path, Path(f"{out_path}-wal"), Path(f"{out_path}-shm")):
        p.unlink(missing_ok=True)


_VERIFY_SAMPLE = 20


def _verify_artifact(out_path) -> None:
    """Check that the rebuilt artifact's vector index still resolves.

    Deliberately narrow. It probes the ARTIFACT ALONE and asserts an internal
    invariant — every sampled embedded chunk's rowid resolves to a vector of
    uniform, non-zero length. It does NOT compare against the source: the
    daemon writes throughout a multi-minute rebuild, so any source-vs-artifact
    count or KNN comparison would be legitimately unequal and flaky. The
    stronger source-equality check belongs in the test suite and the live gate,
    where the store is quiescent.

    It is also NOT an integrity_check: that re-reads the whole artifact and is
    not the best detector of the hazard this mechanism actually introduces,
    which is a vec0 shadow table not surviving the rebuild.

    Silent no-op when the store has no embedded chunks or no vec0 table — the
    probe has nothing to say, and bin/repair.py snapshots stores that may
    already be broken. A probe that raised there would block the very safety
    copy it exists to protect.
    """
    db = _open_db(out_path, read_only=False)
    try:
        try:
            rowids = [r[0] for r in db.execute(
                "SELECT rowid FROM chunks WHERE embedded=1 "
                "ORDER BY rowid LIMIT ?", (_VERIFY_SAMPLE,))]
        except sqlite3.DatabaseError:
            return                      # no chunks table: nothing to verify
        if not rowids:
            return

        lengths = set()
        for rid in rowids:
            try:
                row = db.execute(
                    "SELECT embedding FROM vec_chunks WHERE rowid=?", (rid,)).fetchone()
            except sqlite3.DatabaseError as exc:
                raise RuntimeError(
                    f"snapshot artifact {out_path}: vector lookup failed for "
                    f"chunk rowid {rid} ({exc}); the rebuild did not preserve "
                    "the vec0 index") from exc
            if row is None or not row[0]:
                raise RuntimeError(
                    f"snapshot artifact {out_path}: chunk rowid {rid} is marked "
                    "embedded but its vector does not resolve; the rebuild did "
                    "not preserve the vec0 index")
            lengths.add(len(bytes(row[0])))

        if len(lengths) != 1:
            raise RuntimeError(
                f"snapshot artifact {out_path}: sampled vectors have differing "
                f"lengths {sorted(lengths)}; the rebuild did not preserve the "
                "vec0 index")
    finally:
        db.close()


def generate_escrow_key() -> bytes:
    """Generate a new admin-escrow key.

    Returns a urlsafe-base64-encoded 32-byte key suitable for Fernet. This is
    the org-held key an admin uses to recover a user's backup. It is generated
    here and supplied to the encrypt/decrypt functions by the caller — this
    module never reads, stores, or hardcodes a real key.
    """
    return Fernet.generate_key()


class _FrameWriter:
    """A write-only file object that emits a v3 framed archive as it is fed.

    v3 is the ONLY shape written; v2 is read-only legacy (see the format notes).

    Exists so a producer can be encrypted *as it runs* — notably
    ``make_encrypted_snapshot``, which pipes a streaming tar straight in rather
    than first materialising a whole plaintext bundle on disk.

    Buffers just over one chunk: ``close()`` must always have something left to
    emit as the FINAL frame, because the end-of-stream flag is what makes
    truncation detectable, and a streaming producer cannot know which frame is
    last until it stops writing.
    """

    def __init__(self, fh, key: bytes, chunk_size: int = _ENCRYPT_CHUNK):
        self._fh = fh
        self._fernet = Fernet(key)
        self._chunk = chunk_size
        self._buf = bytearray()
        self._index = 0
        self._closed = False
        # Random, per-archive, never derived: see the archive_id note above.
        self._archive_id = os.urandom(_ARCHIVE_ID_LEN)
        fh.write(_ARCHIVE_MAGIC)

    def write(self, data) -> int:
        self._buf += data
        # Strictly greater: never drain the buffer to empty here, or an input
        # that is an exact multiple of the chunk size would leave close() with
        # nothing to flag as final.
        while len(self._buf) > self._chunk:
            self._emit(bytes(self._buf[:self._chunk]), final=False)
            del self._buf[:self._chunk]
        return len(data)

    def _emit(self, payload: bytes, *, final: bool) -> None:
        token = self._fernet.encrypt(
            _FRAME_HEADER.pack(self._archive_id, self._index,
                               1 if final else 0) + payload)
        self._fh.write(_FRAME_LEN.pack(len(token)))
        self._fh.write(token)
        self._index += 1

    def close(self) -> None:
        """Emit the FINAL frame. Only ever called on a clean exit."""
        if self._closed:
            return
        self._closed = True
        self._emit(bytes(self._buf), final=True)
        self._buf.clear()

    def abandon(self) -> None:
        """Give up without emitting the final frame, leaving a SHORT archive.

        The end-of-stream flag is the only thing that makes truncation
        detectable, so an aborted archive must not carry one -- see
        open_encrypted. Marks itself closed so a later stray close() cannot
        retroactively bless the partial artifact.
        """
        self._closed = True
        self._buf.clear()

    # tarfile/gzip poke at these on the wrapped object.
    def flush(self) -> None:
        pass

    def writable(self) -> bool:
        return True

    @property
    def closed(self) -> bool:
        return self._closed


@contextmanager
def open_encrypted(out_path, key: bytes, *, chunk_size: int = _ENCRYPT_CHUNK):
    """Context manager yielding a writable, encrypting file object at out_path.

    The archive is only well-formed once the block exits CLEANLY (that is when
    the final frame is written), so treat an artifact from an aborted block as
    garbage — ``decrypt_file`` will reject it as truncated, which is the point.

    That guarantee is why ``close()`` is in an ``else`` and not a ``finally``.
    Closing unconditionally emitted the final frame even when the body raised,
    which turned an abort for any non-I/O reason (a records file removed
    mid-``tar.add``, a git operation in the records repo, ``KeyboardInterrupt``)
    into a complete, correctly-indexed, final-flagged archive wrapping a
    TRUNCATED tar.gz — an artifact ``decrypt_file`` accepted and a restore would
    happily unpack. Catching ``BaseException`` is deliberate: KeyboardInterrupt
    and SystemExit are exactly the aborts that must not produce a valid archive.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("wb") as fh:
        writer = _FrameWriter(fh, key, chunk_size)
        try:
            yield writer
        except BaseException:
            writer.abandon()
            raise
        else:
            writer.close()


def encrypt_file(in_path, out_path, key: bytes, *,
                 chunk_size: int = _ENCRYPT_CHUNK) -> Path:
    """Encrypt in_path -> out_path with the Fernet escrow key. Returns out_path.

    Streams the input in ``chunk_size`` frames (see the format notes above), so
    peak memory is one chunk rather than the whole artifact. ``chunk_size`` is
    a knob for tests; callers should leave the default.
    """
    in_path = Path(in_path)
    with in_path.open("rb") as src, \
            open_encrypted(out_path, key, chunk_size=chunk_size) as dst:
        shutil.copyfileobj(src, dst, chunk_size)
    return Path(out_path)


def _require_free_space(work_dir, out_path, store_path) -> None:
    """Refuse to start a snapshot that cannot fit. Raises OSError(ENOSPC).

    make_encrypted_snapshot's dominant cost is one full copy of the store in
    the temp dir; the encrypted artifact then lands at out_path. Neither is
    small — on the live store that is ~11.9GB and ~4.2GB. Running the system
    disk to zero takes down far more than the backup (2026-08-03: 57 failures
    in a day, each an ENOSPC part-way through, each leaving an orphaned work
    dir). Checking up front converts that into one clean, logged, backed-off
    failure per interval.

    The output ceiling is deliberately generous: on the live store the
    compressed+encrypted artifact is ~36% of the raw store, so 50% leaves room
    for a less compressible corpus without demanding space we will not use.
    """
    store_bytes = Path(store_path).stat().st_size
    temp_need = int(store_bytes * 1.15)   # the store copy, plus slack
    out_need = int(store_bytes * 0.5)     # compressed+encrypted ceiling
    out_dir = Path(out_path).parent

    def _check(p, need):
        # shutil.disk_usage, NOT os.statvfs: statvfs does not exist on Windows,
        # so it raised AttributeError before any work — and since this runs
        # first, the daemon's broad `except Exception` swallowed it and backed
        # the cadence off one interval, meaning Windows installs never backed
        # up at all and nothing surfaced. disk_usage is cross-platform and
        # .free is the same number (f_bavail * f_frsize on POSIX).
        free = shutil.disk_usage(str(p)).free
        if free < need:
            raise OSError(
                errno.ENOSPC,
                f"snapshot needs ~{need // 1024**2}MB free at {p} but only "
                f"{free // 1024**2}MB is available; skipping this backup")

    try:
        same_device = os.stat(work_dir).st_dev == os.stat(out_dir).st_dev
    except OSError:
        same_device = False
    if same_device:
        _check(work_dir, temp_need + out_need)
    else:
        _check(work_dir, temp_need)
        _check(out_dir, out_need)


def _read_frames(src, dst, fernet, header: struct.Struct, *,
                 bind_archive_id: bool) -> None:
    """Stream one framed archive's frames from `src` into `dst`.

    Shared by the v3 and v2 read paths; `header` is that version's plaintext
    header struct and `bind_archive_id` says whether it carries an archive id to
    enforce. Both versions get identical ordering/completeness checks -- only
    the splice check is version-dependent, because only v3 has the field.

    Raises InvalidToken on any frame that is short, out of order, from another
    archive (v3), or on a run that does not end in a final frame.
    """
    expected_index = 0
    archive_id = None
    saw_final = False
    while True:
        raw_len = src.read(_FRAME_LEN.size)
        if not raw_len:
            break
        if saw_final or len(raw_len) != _FRAME_LEN.size:
            raise InvalidToken()  # trailing garbage / short length prefix
        (size,) = _FRAME_LEN.unpack(raw_len)
        token = src.read(size)
        if len(token) != size:
            raise InvalidToken()  # frame cut short
        plain = fernet.decrypt(token)
        if len(plain) < header.size:
            raise InvalidToken()  # too short to be a frame of this version
        fields = header.unpack(plain[:header.size])
        if bind_archive_id:
            frame_id, index, final_flag = fields
            if archive_id is None:
                archive_id = frame_id
            elif frame_id != archive_id:
                # A frame from a DIFFERENT archive under the same key: the
                # cross-archive splice. Indices and the final flag can both line
                # up, so this is the only check that catches it.
                raise InvalidToken()
        else:
            index, final_flag = fields
        if index != expected_index:
            raise InvalidToken()  # dropped / duplicated / reordered
        dst.write(plain[header.size:])
        expected_index += 1
        saw_final = bool(final_flag)
    if not saw_final:
        raise InvalidToken()  # truncated before the end of the stream


def decrypt_file(in_path, out_path, key: bytes) -> Path:
    """Decrypt in_path -> out_path with the Fernet escrow key. Returns out_path.

    Reads all three archive shapes, dispatching on the leading magic:
      - v3 framed (what this build writes): streamed, bounded memory, archive-id
        enforced so a cross-archive splice is rejected;
      - v2 framed (LEGACY, read-only): streamed the same way, order and
        completeness enforced, but archive identity is NOT enforced because v2
        frames do not carry it. Real v2 archives exist off-machine, so this path
        must stay;
      - pre-v2: a single Fernet token over the whole file, identified by
        Fernet's own version-byte prefix. Must be held in memory to
        authenticate — unavoidable, and bounded by the smaller sizes that format
        was ever written at.

    Anything else raises UnsupportedArchive. That refusal is the point: a v2
    archive read with v3's header struct misparses instead of failing, so an
    unrecognised magic must never fall back to a guess.

    Raises cryptography.fernet.InvalidToken if the key is wrong, the ciphertext
    was tampered with, or the frame sequence is not a complete, in-order run of
    ONE archive's frames ending in a final frame.
    """
    in_path = Path(in_path)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    fernet = Fernet(key)
    with in_path.open("rb") as src:
        magic = src.read(_MAGIC_LEN)
        if magic == _ARCHIVE_MAGIC_V3:
            header, bind = _FRAME_HEADER, True
        elif magic == _ARCHIVE_MAGIC_V2:
            log.info("%s is a legacy v2 archive: frames bind order only, so it "
                     "predates cross-archive splice protection (v3 adds it). "
                     "Reading it anyway.", in_path.name)
            header, bind = _FRAME_HEADER_V2, False
        elif magic.startswith(_FERNET_PREFIX):
            # Pre-v2: no magic at all, the whole file is one token.
            out_path.write_bytes(fernet.decrypt(magic + src.read()))
            return out_path
        else:
            raise UnsupportedArchive(
                f"{in_path.name}: unsupported archive format (leading bytes "
                f"{magic[:_MAGIC_LEN]!r}); expected {_ARCHIVE_MAGIC_V3!r}, "
                f"{_ARCHIVE_MAGIC_V2!r}, or a pre-v2 single Fernet token")

        with out_path.open("wb") as dst:
            _read_frames(src, dst, fernet, header, bind_archive_id=bind)
    return out_path


# Archive layout (the bundle format). A snapshot is the full system: the derived
# store, the local records repo (world-model + continuity + memory, incl. its git
# history), and config.json (identity/orgs/settings). The records repo is
# local-only (no git remote) — bundling it here is its only off-machine copy.
_SQLITE_MAGIC = b"SQLite format 3\x00"
_STORE_ARC = "store/brain.sqlite3"
_RECORDS_ARC = "records"
_CONFIG_ARC = "config.json"


def make_encrypted_snapshot(store_path, out_path, key: bytes, *,
                            records_dir=None, config_path=None) -> Path:
    """Snapshot the system, encrypt it to out_path, return out_path.

    When ``records_dir`` (a local git repo) or ``config_path`` is given and
    exists, the snapshot is a gzip tar bundling the checkpointed store, the whole
    records repo (including ``.git`` history), and config.json. With neither, it
    is a bare single-file store snapshot (raw sqlite). Either way the artifact is
    encrypted with the escrow key, and all intermediate cleartext is written to a
    private temp dir and removed in a finally — only the encrypted out_path
    remains.

    Peak temp usage is ONE copy of the store. The bundle used to be written to
    the temp dir in full and then encrypted in a second pass, which doubled that
    (~15GB on the live store) and is the mechanism behind the 2026-08-03 ENOSPC
    storm; the tar now streams straight into the encryptor. A pre-flight
    free-space check refuses outright rather than filling the disk part-way.
    """
    import tarfile

    store_path = Path(store_path)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    records_dir = Path(records_dir) if records_dir else None
    config_path = Path(config_path) if config_path else None
    bundle = bool((records_dir and records_dir.exists())
                  or (config_path and config_path.exists()))

    work = Path(tempfile.mkdtemp(prefix="mcpbrain-snap-"))
    try:
        # Before anything heavy, and before out_path is truncated.
        _require_free_space(work, out_path, store_path)
        if not bundle:
            plain = work / "store.sqlite3"
            snapshot(store_path, plain)
            encrypt_file(plain, out_path, key)
        else:
            store_snap = work / "brain.sqlite3"
            snapshot(store_path, store_snap)
            # "w|gz" is tarfile's STREAMING mode — it never seeks, so it can
            # write into a forward-only encrypting sink. The plaintext bundle
            # is therefore never materialised.
            with open_encrypted(out_path, key) as enc:
                with tarfile.open(fileobj=enc, mode="w|gz") as tar:
                    tar.add(store_snap, arcname=_STORE_ARC)
                    if records_dir and records_dir.exists():
                        tar.add(records_dir, arcname=_RECORDS_ARC)
                    if config_path and config_path.exists():
                        tar.add(config_path, arcname=_CONFIG_ARC)
    finally:
        # Promptly destroy all transient cleartext, regardless of outcome.
        shutil.rmtree(work, ignore_errors=True)
    return out_path


def sweep_orphan_snapshots(parent, *, max_age_s: float) -> int:
    """Remove stale `mcpbrain-snap-*` work dirs directly under `parent`.

    make_encrypted_snapshot's cleanup runs in a `finally`, which cannot fire
    when the process is killed mid-snapshot -- exactly what the daemon's
    watchdog does deliberately (os._exit on a detected stall/Windows handover).
    Orphaned work dirs then accumulate forever: ~24GB of them was found live
    under the OS temp dir (`tempfile.gettempdir()` -- typically
    `/var/folders/...` on macOS, NOT `$HOME`) on 2026-07-27, having filled the
    disk. Call this at daemon startup, and again periodically thereafter (see
    Daemon._backup_under_bulk_lock), against that same parent (the one
    `tempfile.mkdtemp(prefix="mcpbrain-snap-")` above actually uses).

    A directory is removed only when it is older than `max_age_s` (judged by
    mtime), so a snapshot that is genuinely still being written moments ago
    is left alone. Returns the number of directories ACTUALLY removed --
    `shutil.rmtree` is called without `ignore_errors` specifically so a
    partial/failed removal (e.g. a permissions error, or a stray symlink
    rmtree won't descend into) raises and is counted as a skip via the
    `except OSError` below, rather than being silently reported as removed.
    Best-effort overall: a directory that can't be listed/stat'd/removed
    (permissions, already gone, a race with an in-flight snapshot) is
    skipped rather than raised -- a failed sweep must never crash the daemon.
    """
    parent = Path(parent)
    try:
        candidates = list(parent.glob("mcpbrain-snap-*"))
    except OSError as exc:
        log.warning("snapshot orphan sweep: could not list %s: %s", parent, exc)
        return 0
    now = time.time()
    removed = 0
    for d in candidates:
        try:
            if not d.is_dir():
                continue
            age = now - d.stat().st_mtime
            if age < max_age_s:
                continue
            shutil.rmtree(d)
            removed += 1
        except OSError as exc:
            log.warning("snapshot orphan sweep: could not remove %s: %s", d, exc)
    return removed


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

    RESUMABLE. Google caps simple and multipart upload at **5 MB**; anything
    larger must go up resumably. This used to pass ``resumable=False`` on the
    theory that a single PUT dodged httplib2's 308 bug — but that made
    googleapiclient take the multipart path, which reads the whole artifact via
    ``getbytes(0, size)`` and flattens the entire request body into one
    ``io.BytesIO``. Once the store outgrew the "~750MB" this was sized for, a
    4.24 GB single PUT became multi-GB of allocation and a coin-flip on the
    wire: 97 failures against 52 successes between 2026-06-25 and 2026-08-04,
    arriving in storms (57 in one day) rather than steadily.

    Resumable fixes both halves: ``MediaFileUpload`` exposes a stream, so
    googleapiclient sends bounded ``_StreamSlice`` chunks instead of buffering
    the artifact, and each request is a legal size. The httplib2 308 bug is
    real, and is handled at the transport instead — see ``auth._google_http``,
    which un-registers 308 as a redirect code so googleapiclient's own
    ``_process_response`` sees it. ``execute()`` drives the chunk loop itself.

    Callers must still pass the long socket timeout
    (``timeout_s=DEFAULT_HTTP_TIMEOUT_S`` /
    ``drive_timeout_s=DEFAULT_HTTP_TIMEOUT_S``) to
    ``build_service``/``build_google_services``. A mid-upload network blip fails
    this run and the cadence retries.
    """
    from googleapiclient.http import MediaFileUpload

    return MediaFileUpload(str(path), resumable=True)


# num_retries for a RESUMABLE media create. Zero, deliberately.
#
# googleapiclient's media-retry loop cannot retry a streamed chunk correctly.
# `HttpRequest.next_chunk` builds one `_StreamSlice` and then loops
# `for retry_num in range(num_retries + 1)`, re-sending that SAME object;
# `_StreamSlice` seeks only in `__init__`, and once the first attempt has read
# the slice, `read()` computes `n = end - cur == 0` and returns b"". So a
# retried chunk puts 0 bytes on the wire against a `Content-Length` of up to
# 100MB, and the connection blocks until the socket timeout —
# `auth.DEFAULT_HTTP_TIMEOUT_S` (600s) for the Drive service.
#
# That cost lands in the worst possible place: `daemon.maybe_backup` runs on the
# cycle thread HOLDING the bulk lock, with `_backup_in_progress` set, which the
# watchdog's `_recover_from_stall` reads as "do not recover". One transient 5xx
# mid-upload would therefore wedge the daemon for ~10 minutes with the watchdog
# explicitly muzzled. This is a regression the non-resumable path did not have:
# it built `self.body` once, so its retries genuinely re-sent the bytes.
#
# The real backoff is the cadence: a failed backup is logged, backed off one
# interval, and retried from scratch. Raising this above 0 requires driving
# `next_chunk` by hand so each attempt re-seeks.
_MEDIA_NUM_RETRIES = 0


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

    # 3. Upload the artifact into the per-user folder (resumable, chunk-streamed
    # — see _default_media). execute() drives the chunk loop for a resumable
    # media body. Pass a str path to the factory (matches
    # MediaFileUpload(str(path))).
    #
    # num_retries=0 IS THE FIX, not an oversight — see _MEDIA_NUM_RETRIES.
    media = media_factory(str(file_path))
    created = (
        service.files()
        .create(
            body={"name": file_path.name, "parents": [folder_id]},
            media_body=media,
            supportsAllDrives=True,
            fields="id",
        )
        .execute(num_retries=_MEDIA_NUM_RETRIES)
    )
    return created["id"]


def restore(encrypted_path, dest_store_path, key: bytes, *,
            records_dir=None, config_path=None) -> Path:
    """Decrypt an encrypted snapshot and place the store (and bundled records +
    config, if present in the artifact and destinations are given).

    Handles both artifact shapes make_encrypted_snapshot can produce:
      - a bare sqlite store (raw) -> placed at dest_store_path;
      - a gzip-tar bundle -> store -> dest_store_path, records repo ->
        records_dir (replacing any existing dir, git history and all), and
        config.json -> config_path.

    Decryption happens first, so a wrong key or tampered artifact raises
    cryptography.fernet.InvalidToken before anything is written. Archive
    extraction uses the tarfile ``data`` filter, which refuses members that
    would escape the destination (path traversal / absolute paths).

    The caller then runs a delta-sync to catch changes after the snapshot — the
    restored store carries its sync cursors, so sync resumes from the snapshot
    point. Creates parent dirs as needed. Returns dest_store_path.
    """
    import tarfile

    dest_store_path = Path(dest_store_path)
    dest_store_path.parent.mkdir(parents=True, exist_ok=True)

    work = Path(tempfile.mkdtemp(prefix="mcpbrain-restore-"))
    try:
        # Decrypt to a temp file FIRST. Streaming keeps peak memory to one
        # frame instead of several times the artifact — restore runs when the
        # machine is already in trouble. Writing to temp (not the destination)
        # preserves the guarantee that a wrong key or tampered/truncated
        # archive raises before anything at the destination is touched.
        arc = work / "bundle.tar.gz"
        decrypt_file(encrypted_path, arc, key)

        with arc.open("rb") as fh:
            head = fh.read(len(_SQLITE_MAGIC))
        if head == _SQLITE_MAGIC:
            shutil.copy2(arc, dest_store_path)  # bare store snapshot
            return dest_store_path

        xroot = work / "x"
        xroot.mkdir()
        with tarfile.open(arc, "r:gz") as tar:
            tar.extractall(xroot, filter="data")  # data filter blocks traversal
        store_src = xroot / _STORE_ARC
        if store_src.exists():
            shutil.copy2(store_src, dest_store_path)
        rec_src = xroot / _RECORDS_ARC
        if records_dir and rec_src.is_dir():
            dest_records = Path(records_dir)
            if dest_records.exists():
                shutil.rmtree(dest_records)
            shutil.copytree(rec_src, dest_records)
        cfg_src = xroot / _CONFIG_ARC
        if config_path and cfg_src.exists():
            cfg_dest = Path(config_path)
            cfg_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(cfg_src, cfg_dest)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return dest_store_path


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


# --- Folder-based Drive helpers (escrow folder lives INSIDE a Shared Drive) ---
# The escrow/fleet folders are plain folders nested in a Shared Drive, not Shared
# Drive roots — so their contents must be queried by parent with corpora=allDrives
# (a folder id is NOT a valid driveId). upload_snapshot/find_latest_snapshot above
# stay driveId-based for the legacy <shared-drive-root>/<user> convention.

def _list_in_drives(service, q: str, *, fields="files(id, name, createdTime, modifiedTime)") -> list[dict]:
    return service.files().list(
        q=q, spaces="drive", corpora="allDrives",
        includeItemsFromAllDrives=True, supportsAllDrives=True, fields=fields,
    ).execute().get("files", [])


def ensure_subfolder(service, parent_folder_id: str, name: str) -> str:
    """Return the id of subfolder `name` under parent_folder_id, creating it if absent."""
    found = _list_in_drives(
        service,
        f"name = '{name}' and mimeType = '{FOLDER_MIME}' and trashed = false "
        f"and '{parent_folder_id}' in parents",
        fields="files(id, name)")
    if found:
        return found[0]["id"]
    return service.files().create(
        body={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_folder_id]},
        supportsAllDrives=True, fields="id").execute()["id"]


def upload_to_folder(service, file_path, parent_folder_id: str, *,
                     name=None, media_factory=None) -> str:
    """Upload file_path directly into parent_folder_id (a folder anywhere, incl.
    inside a Shared Drive). Returns the created file id.

    num_retries=0 for the same reason upload_snapshot uses it — the media body
    is resumable and the library cannot re-seek a retried chunk. See
    _MEDIA_NUM_RETRIES.
    """
    file_path = Path(file_path)
    media = (media_factory or _default_media)(str(file_path))
    created = service.files().create(
        body={"name": name or file_path.name, "parents": [parent_folder_id]},
        media_body=media, supportsAllDrives=True, fields="id",
    ).execute(num_retries=_MEDIA_NUM_RETRIES)
    return created["id"]


def find_latest_in_subfolder(service, parent_folder_id: str, subfolder_name: str) -> str | None:
    """Newest non-folder file in <parent_folder_id>/<subfolder_name>/, or None.

    Parent-based (corpora=allDrives) so it works for a folder nested in a Shared
    Drive, where the folder id is not a valid driveId.
    """
    subs = _list_in_drives(
        service,
        f"name = '{subfolder_name}' and mimeType = '{FOLDER_MIME}' and trashed = false "
        f"and '{parent_folder_id}' in parents",
        fields="files(id, name)")
    if not subs:
        return None
    files = _list_in_drives(
        service,
        f"'{subs[0]['id']}' in parents and trashed = false and mimeType != '{FOLDER_MIME}'")
    if not files:
        return None
    files.sort(key=lambda f: (f.get("createdTime", ""), f.get("modifiedTime", "")),
               reverse=True)
    return files[0]["id"]


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
    if hasattr(os, "fchmod"):  # POSIX-only; mkstemp is already owner-only on Windows
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
