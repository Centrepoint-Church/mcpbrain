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

Uploads are no longer done here: the incremental layout (backup_outbox,
backup_uploader, drive_resumable) replaced the full-snapshot upload. This
module keeps the encryption, the legacy snapshot reader, the escrow-key
download and the store swap. The Drive API resource is INJECTED (so tests mock
it) and every call sets supportsAllDrives=True, which Shared Drives require.

Restore + delta-sync is the reinstall recovery path. Every restore entry point
goes through backup_restore.restore_latest (the newest restore point across the
incremental chains and these legacy single-file snapshots; `restore` below
decrypts+places a legacy one), then a normal sync cycle (run_sync_cycle from
mcpbrain.sync). The restored store carries its sync cursors, so the delta-sync
resumes from the snapshot point and catches everything that changed since.

Scope: snapshot + checkpoint + encryption + restore/download + store swap. store.py remains the sole schema owner — this module adds no
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


def snapshot(store_path, out_path, *, home=None) -> Path:
    """Produce a single-file snapshot of the derived store at store_path.

    `home`, if given, is forwarded to `_verify_artifact` so it can check
    `embed_skip_tabular` instead of falling back to its conservative,
    can't-check-the-flag exclusion. Optional and defaults to None so every
    existing caller that doesn't have a home handy keeps working unchanged.

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
    # bulk=True: VACUUM INTO reads the whole store through one connection --
    # a genuinely long-lived, throughput-sensitive "backup pass" (PR #25
    # finding 5), not the many-short-connections case the smaller default
    # protects.
    db = _open_db(store_path, read_only=False, bulk=True)
    try:
        db.execute("VACUUM INTO ?", (str(out_path),))
    except BaseException:
        _clear_artifact(out_path)
        raise
    finally:
        db.close()

    try:
        _verify_artifact(out_path, home=home)
    except BaseException:
        _clear_artifact(out_path)
        raise
    return out_path


def _clear_artifact(out_path: Path) -> None:
    """Remove an artifact path and any sidecars left beside it."""
    for p in (out_path, Path(f"{out_path}-wal"), Path(f"{out_path}-shm")):
        p.unlink(missing_ok=True)


_VERIFY_SAMPLE = 20


def _verify_artifact(out_path, *, home=None) -> None:
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

    Table chunks are the only deliberately vector-less shape in the
    codebase: with `embed_skip_tabular` on, index_pending calls
    `store.write_embedding(rowid, None)` for them (FTS row written,
    embedded=1 stamped, no vec_chunks insert), and
    `bin/cleanup_tabular_vectors.py` deletes existing oversize table vectors
    outright while deliberately leaving embedded=1 so nothing re-queues
    them. Sampling the lowest rowids with no exclusion would eventually hit
    one of those and raise — re-creating the "backup upload failing" outage
    this spec set out to fix, and blocking the sweep that repairs the
    tabular chunks in the first place.

    Excluding ALL table chunks unconditionally would be safe but overbroad:
    reading `embed_skip_tabular` from `home` (when given) narrows the
    exclusion to only the chunks that could plausibly be in a legitimate
    vector-less state --
      * every table chunk, if the flag is ON (the ongoing designed state), or
      * only chunks still below CHUNKER_VERSION, if the flag is OFF (the
        transient post-cleanup window before the version-bump sweep
        re-renders and re-embeds them for real).
    A table chunk already AT CHUNKER_VERSION with the flag OFF should have
    gotten a real vector from index_pending same as any other chunk, so a
    missing one there is genuine corruption and is not excluded.

    `home=None` (the default, used by every caller that doesn't have one
    handy, e.g. bin/repair.py's pre-apply safety snapshot when it can't
    cheaply derive one) falls back to the fully unconditional exclusion —
    the conservative, always-safe choice when the flag can't be checked.
    """
    exclude_current_version_tables = True
    current_version = None
    if home is not None:
        from mcpbrain import config
        from mcpbrain.chunking import CHUNKER_VERSION
        exclude_current_version_tables = config.embed_skip_tabular_enabled(home)
        current_version = CHUNKER_VERSION

    db = _open_db(out_path, read_only=False)
    try:
        has_chunks_table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chunks'"
        ).fetchone()
        if not has_chunks_table:
            return                      # no chunks table: nothing to verify

        if exclude_current_version_tables:
            table_exclusion = (
                "COALESCE(json_extract(metadata,'$.content_subtype'),'')='table'"
            )
            params: tuple = (_VERIFY_SAMPLE,)
        else:
            table_exclusion = (
                "(COALESCE(json_extract(metadata,'$.content_subtype'),'')='table' "
                " AND COALESCE(json_extract(metadata,'$.chunker_version'),0) < ?)"
            )
            params = (current_version, _VERIFY_SAMPLE)

        rowids = [r[0] for r in db.execute(
            f"SELECT rowid FROM chunks WHERE embedded=1 "
            f"  AND NOT ({table_exclusion}) "
            f"ORDER BY rowid LIMIT ?", params)]
        if not rowids:
            return

        has_vec_table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='vec_chunks'"
        ).fetchone()
        if not has_vec_table:
            return                      # no vec0 table: nothing to verify

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


def _live_bytes(store_path) -> int:
    """Bytes of LIVE data in the store — page_count minus freelist, times
    page_size.

    snapshot() uses VACUUM INTO, whose output contains only live pages, so this
    is what the temp copy will cost. The file's own size is the wrong number:
    re-keying enrich_payloads frees ~11.3GB onto the freelist without shrinking
    the file, and sizing from stat() would keep refusing backups that now fit.
    Falls back to the file size if the store doesn't exist or the PRAGMAs cannot
    be read — refusing to back up is worse than over-estimating.
    """
    store_path = Path(store_path)
    # Don't call sqlite3.connect if the path doesn't exist, since connect()
    # auto-creates an empty file immediately, and the PRAGMAs then succeed
    # against that empty DB (page_count=0, freelist=0), returning 0 -- which is
    # wrong and unsafe. Let stat().st_size raise FileNotFoundError naturally.
    if not store_path.exists():
        return store_path.stat().st_size
    try:
        db = _open_db(store_path, read_only=False)
    except sqlite3.DatabaseError:
        return store_path.stat().st_size
    try:
        page_size = db.execute("PRAGMA page_size").fetchone()[0]
        page_count = db.execute("PRAGMA page_count").fetchone()[0]
        freelist = db.execute("PRAGMA freelist_count").fetchone()[0]
    except sqlite3.DatabaseError:
        return store_path.stat().st_size
    finally:
        db.close()
    return max(0, page_count - freelist) * page_size


def _require_free_space(work_dir, out_path, store_path) -> None:
    """Refuse to start a snapshot that cannot fit. Raises OSError(ENOSPC).

    make_encrypted_snapshot's dominant cost is one snapshot of the store in the
    temp dir; the encrypted artifact then lands at out_path. Neither is small.
    Both are sized from LIVE data (see _live_bytes), not the store file: after
    the enrich_payloads re-key the file keeps ~11.3GB of freelist that
    VACUUM INTO never copies. Running the system disk to zero takes down far
    more than the backup (2026-08-03: 57 failures in a day, each an ENOSPC
    part-way through, each leaving an orphaned work dir). Checking up front
    converts that into one clean, logged, backed-off failure per interval.

    The output ceiling is deliberately generous: on the live store the
    compressed+encrypted artifact is ~36% of the raw store, so 50% leaves room
    for a less compressible corpus without demanding space we will not use.
    """
    store_bytes = _live_bytes(store_path)
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

    `config_path` doubles as the source of `home` for `_verify_artifact`'s
    `embed_skip_tabular` check (its parent directory) -- daemon.maybe_backup
    already passes `config_path=str(Path(home)/"config.json")`, so no new
    parameter is needed to thread it through.
    """
    import tarfile

    store_path = Path(store_path)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    records_dir = Path(records_dir) if records_dir else None
    config_path = Path(config_path) if config_path else None
    home = str(config_path.parent) if config_path else None
    bundle = bool((records_dir and records_dir.exists())
                  or (config_path and config_path.exists()))

    work = Path(tempfile.mkdtemp(prefix="mcpbrain-snap-"))
    try:
        # Before anything heavy, and before out_path is truncated.
        _require_free_space(work, out_path, store_path)
        if not bundle:
            plain = work / "store.sqlite3"
            snapshot(store_path, plain, home=home)
            encrypt_file(plain, out_path, key)
        else:
            store_snap = work / "brain.sqlite3"
            snapshot(store_path, store_snap, home=home)
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
    query structure. backup_location relies on this.
    """
    if "'" in user_id or "\\" in user_id:
        raise ValueError(
            f"user_id contains characters unsafe in a Drive query: {user_id!r}"
        )


def _is_transient_upload_error(exc) -> bool:
    """A chunk failure worth resuming: the network's fault or Drive's.

    The sync stack's classifier (rate limits, 502/503/504, DNS, resets,
    timeouts, broken pipes, Errno 49), plus a plain 500 -- for a resumable
    chunk, Google documents a 500 as "resume the upload", unlike a sync read
    where a file that always 500s must eventually be given up on. Lazy import:
    reflow_handler pulls in the sync stack."""
    from mcpbrain.sync.reflow_handler import _http_status, _is_transient
    return _is_transient(exc) or _http_status(exc) == 500


# Every Drive call in this module is a plain metadata list or a download --
# none re-sends a drained upload stream (the resumable upload lives in
# drive_resumable, which drives its own retries), so all of them retry.
# googleapiclient's own num_retries param already does randomized exponential backoff on exactly this error
# class (SSL errors, socket timeouts, ConnectionError, OSError generally —
# confirmed against googleapiclient.http._retry_request), so no new retry
# logic is written here, just the parameter every other call in this codebase
# that already retries (sync/gmail.py, sync/attachments.py) also passes.
_NUM_RETRIES = 5


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


def _default_downloader(fh, request):
    """Lazy default downloader factory — imports googleapiclient only when a
    download actually runs, so `import mcpbrain.backup` does not require the
    SDK."""
    from googleapiclient.http import MediaIoBaseDownload

    return MediaIoBaseDownload(fh, request)


# --- Folder-based Drive helpers (escrow folder lives INSIDE a Shared Drive) ---
# The escrow/fleet folders are plain folders nested in a Shared Drive, not Shared
# Drive roots — so their contents must be queried by parent with corpora=allDrives
# (a folder id is NOT a valid driveId).

def _list_in_drives(service, q: str, *, fields="files(id, name, createdTime, modifiedTime)") -> list[dict]:
    return service.files().list(
        q=q, spaces="drive", corpora="allDrives",
        includeItemsFromAllDrives=True, supportsAllDrives=True, fields=fields,
    ).execute(num_retries=_NUM_RETRIES).get("files", [])


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


class RestoreBusyError(TimeoutError):
    """The live store could not be checkpointed cleanly (another connection
    holds a read transaction), so swapping a restored snapshot in would risk a
    reader replaying stale WAL frames onto it. Refused; a TimeoutError so the
    daemon's classifier treats it as transient and retries."""


def _checkpoint_for_swap(path) -> None:
    """PRAGMA wal_checkpoint(TRUNCATE) on the live store; require busy == 0
    and every WAL frame checkpointed, else raise RestoreBusyError."""
    path = Path(path)
    if not path.exists():
        return
    con = sqlite3.connect(str(path), timeout=2.0)
    try:
        busy, log_frames, done = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    except sqlite3.Error as exc:
        raise RestoreBusyError(f"checkpoint failed: {exc}") from exc
    finally:
        con.close()
    if busy != 0 or log_frames != done:
        raise RestoreBusyError(
            f"store busy (busy={busy}, log={log_frames}, checkpointed={done})")


def swap_in_store(stage, dest, *, gate=None) -> None:
    """Replace the live store `dest` with the restored `stage` file -- the ONE
    way any restore touches a live store (daemon start-up, the wizard's
    /api/backup/auto, `mcpbrain restore`). Inside `gate` (the daemon's
    restoring gate), checkpoint the live WAL with busy == 0 required
    (RestoreBusyError otherwise, which callers treat as transient), then
    os.replace -- atomic, same filesystem. An empty stage (an artifact that
    carried no store) is refused and the live store left alone.

    Without a gate (a separate process: the CLI) this is only safe with the
    daemon STOPPED. busy == 0 proves no reader holds a snapshot at checkpoint
    time, not that no daemon connection is open: one opened earlier that writes
    after the replace appends to the -wal/-shm both files share by name, and
    when it closes the restored store is silently rolled back. restore.py
    refuses the CLI path while the daemon answers on its control port."""
    from contextlib import nullcontext
    stage, dest = Path(stage), Path(dest)
    if not stage.exists() or stage.stat().st_size == 0:
        raise ValueError("snapshot carried no store; live store left untouched")
    os.chmod(stage, 0o600)
    with (gate or nullcontext)():
        _checkpoint_for_swap(dest)
        os.replace(stage, dest)
