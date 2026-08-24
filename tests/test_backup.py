"""Tests for mcpbrain.backup — store snapshot via VACUUM INTO.

The store runs journal_mode=WAL. Committed writes can live in the -wal sidecar,
so a bare copy of the main .sqlite3 file can MISS the latest writes. snapshot()
uses VACUUM INTO, which reads through the WAL under one consistent read
transaction: it needs no exclusive checkpoint (which a single held reader
blocks absolutely) and cannot be torn by a concurrent autocheckpoint. The
latest-writes roundtrip test below is the behavioural proof: a freshly
committed row must survive the snapshot.
"""

import sqlite3
from pathlib import Path

import sqlite_vec

from mcpbrain.store import Store
from mcpbrain.index import index_pending
from mcpbrain.backup import (
    snapshot,
    generate_escrow_key,
    encrypt_file,
    decrypt_file,
    make_encrypted_snapshot,
    upload_snapshot,
    restore,
    find_latest_snapshot,
    download_snapshot,
)

# Reuse the keyword/semantic fake embedder so a vec row + fts row both exist.
from tests.test_retrieval import FakeEmbedder


def _raw_connect(path):
    """Open a raw read connection with sqlite_vec loaded (vec0 tables need it)."""
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    return db


def test_latest_writes_survive_snapshot(tmp_path):
    """The key WAL test: writes committed just before the snapshot must be
    present in the loaded snapshot. They live in the -wal sidecar until the
    checkpoint folds them into the main file, so this fails if the checkpoint
    were skipped and only the pre-WAL main file copied."""
    store = Store(tmp_path / "live.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("d-latest", "the annual budget review", "h1", {})
    store.upsert_entity("taryn-hamilton", "Taryn Hamilton", "person", org="Acme")
    store.set_cursor("gmail", "cursor-token-42")

    snap_path = snapshot(store.path, tmp_path / "snap.sqlite3")

    # Load the snapshot in a fresh Store (new connection) and confirm all three
    # WAL-resident writes folded in.
    loaded = Store(snap_path, dim=4)
    assert loaded.get_chunk("d-latest") is not None
    assert loaded.get_entity("taryn-hamilton") is not None
    assert loaded.get_cursor("gmail") == "cursor-token-42"


def test_vec_and_fts_survive_snapshot(tmp_path):
    """The vec0 + fts5 virtual-table data must be captured in the single
    artifact, so vec_knn and fts_search work against the loaded snapshot."""
    store = Store(tmp_path / "live.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("d-budget", "the annual budget review", "h1", {})
    store.upsert_chunk("d-roster", "the volunteer roster", "h2", {})
    index_pending(store, FakeEmbedder())

    snap_path = snapshot(store.path, tmp_path / "snap.sqlite3")

    loaded = Store(snap_path, dim=4)
    # vec0 query: "budget" embeds to [1,0,0,0], matching the budget chunk.
    knn = loaded.vec_knn(FakeEmbedder().embed_query("budget"), k=1)
    assert knn and knn[0][0] == "d-budget"
    # fts5 query
    fts = loaded.fts_search("roster", k=2)
    assert any(doc_id == "d-roster" for doc_id, _ in fts)

    # And the raw virtual-table rows exist in the single artifact.
    db = _raw_connect(str(snap_path))
    try:
        assert db.execute("SELECT count(*) FROM vec_chunks").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM fts_chunks").fetchone()[0] == 2
    finally:
        db.close()


def test_snapshot_returns_out_path_and_writes_nonempty_file(tmp_path):
    from pathlib import Path

    store = Store(tmp_path / "live.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("d1", "hello", "h1", {})

    out = tmp_path / "nested" / "dir" / "snap.sqlite3"
    result = snapshot(store.path, out)

    assert isinstance(result, Path)
    assert result == out
    assert out.exists()
    assert out.stat().st_size > 0


def test_snapshot_accepts_str_paths(tmp_path):
    store = Store(tmp_path / "live.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("d1", "hello", "h1", {})

    out = snapshot(str(store.path), str(tmp_path / "snap.sqlite3"))
    loaded = Store(out, dim=4)
    assert loaded.get_chunk("d1") is not None


def test_snapshot_succeeds_while_a_read_transaction_is_held(tmp_path):
    """Cause (R) from Finding 3: one open read transaction on an older snapshot
    blocks wal_checkpoint(TRUNCATE) absolutely — busy=1 on 6/6 live attempts,
    checkpointed_frames=0 every time. snapshot() must not depend on that
    checkpoint at all.

    NULL-INSTRUMENT GUARD: the WAL must be non-empty when the snapshot runs.
    With an empty WAL, TRUNCATE returns busy=0 regardless of concurrency, so
    this test could not go red — the exact failure that made the 2026-08-04
    idle measurement worthless.
    """
    from mcpbrain.store import _open_db

    store = Store(tmp_path / "live.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("d1", "before", "h1", {})

    reader = _open_db(store.path, read_only=False)
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM chunks").fetchone()  # pins the snapshot
    try:
        for i in range(50):
            store.upsert_chunk(f"d-{i}", f"body {i}", f"h-{i}", {})

        wal = Path(f"{store.path}-wal")
        assert wal.exists() and wal.stat().st_size > 0, (
            "NULL INSTRUMENT: the WAL is empty, so TRUNCATE would have returned "
            "busy=0 regardless and this test proves nothing")

        out = snapshot(store.path, tmp_path / "snap.sqlite3")
    finally:
        reader.rollback()
        reader.close()

    loaded = Store(out, dim=4)
    assert loaded.get_chunk("d1") is not None
    assert loaded.get_chunk("d-49") is not None


def test_snapshot_never_runs_an_exclusive_checkpoint(tmp_path, monkeypatch):
    """The inverse of the old test_checkpoint_runs_before_copy, which pinned
    the defect in place. wal_checkpoint(TRUNCATE) is what cause (R) blocks, so
    the backup path must not issue one at all."""
    import mcpbrain.backup as backup_mod

    store = Store(tmp_path / "live.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("d1", "hello", "h1", {})

    seen = []
    real_open_db = backup_mod._open_db

    def spy_open_db(*args, **kwargs):
        conn = real_open_db(*args, **kwargs)

        class _Proxy:
            def __getattr__(self, name):
                return getattr(conn, name)

            def execute(self, sql, *a, **k):
                seen.append(sql)
                return conn.execute(sql, *a, **k)

        return _Proxy()

    monkeypatch.setattr(backup_mod, "_open_db", spy_open_db)
    snapshot(store.path, tmp_path / "snap.sqlite3")

    assert not any("wal_checkpoint" in s.lower() for s in seen), (
        f"snapshot() issued a checkpoint: {seen}")
    assert any("vacuum into" in s.lower() for s in seen), (
        f"snapshot() did not use VACUUM INTO: {seen}")


def test_snapshot_leaves_no_partial_artifact_when_the_copy_fails(tmp_path, monkeypatch):
    """The old busy-abort contract, preserved under the new mechanism: a
    failure must leave nothing that looks like a successful artifact to
    make_encrypted_snapshot."""
    import sqlite3

    import pytest

    import mcpbrain.backup as backup_mod

    store = Store(tmp_path / "live.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("d1", "hello", "h1", {})

    out = tmp_path / "snap.sqlite3"
    real_open_db = backup_mod._open_db

    def spy_open_db(*args, **kwargs):
        conn = real_open_db(*args, **kwargs)

        class _Proxy:
            def __getattr__(self, name):
                return getattr(conn, name)

            def execute(self, sql, *a, **k):
                if "vacuum into" in sql.lower():
                    out.write_bytes(b"partial")   # a half-written artifact
                    raise sqlite3.OperationalError("disk I/O error")
                return conn.execute(sql, *a, **k)

        return _Proxy()

    monkeypatch.setattr(backup_mod, "_open_db", spy_open_db)

    with pytest.raises(sqlite3.OperationalError):
        snapshot(store.path, out)

    assert not out.exists(), "a partial artifact was left behind"


def test_snapshot_clears_a_pre_existing_destination_and_its_sidecars(tmp_path):
    """VACUUM INTO refuses to overwrite, and a stale -wal beside a previous
    artifact would be applied over the fresh one on first open."""
    store = Store(tmp_path / "live.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("d1", "hello", "h1", {})

    out = tmp_path / "snap.sqlite3"
    out.write_bytes(b"stale artifact")
    Path(f"{out}-wal").write_bytes(b"stale wal")
    Path(f"{out}-shm").write_bytes(b"stale shm")

    snapshot(store.path, out)

    assert Store(out, dim=4).get_chunk("d1") is not None
    assert not Path(f"{out}-wal").exists()
    assert not Path(f"{out}-shm").exists()


def test_snapshot_artifact_opens_and_ends_in_wal_mode(tmp_path):
    """VACUUM INTO writes a fresh DB whose header says rollback-journal, where
    copy2 preserved WAL. init() converts it on open; this pins that the restore
    path is unaffected."""
    from mcpbrain.store import _open_db

    store = Store(tmp_path / "live.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("d1", "hello", "h1", {})

    out = snapshot(store.path, tmp_path / "snap.sqlite3")

    db = _open_db(out, read_only=False)
    try:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        db.close()

    restored = Store(out, dim=4)
    restored.init()
    db = _open_db(out, read_only=False)
    try:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        db.close()
    assert restored.get_chunk("d1") is not None


def test_snapshot_is_consistent_under_a_concurrent_writer(tmp_path):
    """The torn-copy half. The old implementation checkpointed and then copied
    the DB file for minutes, during which any connection's wal_autocheckpoint
    could write pages into that file mid-copy. VACUUM INTO builds from one
    consistent read transaction and cannot be torn.

    The artifact is a point-in-time snapshot, so rows committed DURING it may
    or may not appear — what must hold is that everything committed BEFORE the
    call is present and the result is a valid database.
    """
    import threading

    from mcpbrain.store import _open_db

    store = Store(tmp_path / "live.sqlite3", dim=4)
    store.init()
    for i in range(200):
        store.upsert_chunk(f"pre-{i}", f"before {i}", f"hp{i}", {})

    stop = threading.Event()
    written = []

    def writer():
        i = 0
        while not stop.is_set():
            store.upsert_chunk(f"dur-{i}", f"during {i}", f"hd{i}", {})
            written.append(i)
            i += 1

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    try:
        while len(written) < 20:          # ensure the writer is genuinely live
            pass
        out = snapshot(store.path, tmp_path / "snap.sqlite3")
    finally:
        stop.set()
        t.join(timeout=10)

    assert written, "NULL INSTRUMENT: the writer never committed anything"

    db = _open_db(out, read_only=False)
    try:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        missing = [i for i in range(200) if db.execute(
            "SELECT 1 FROM chunks WHERE doc_id=?", (f"pre-{i}",)).fetchone() is None]
    finally:
        db.close()
    assert not missing, f"pre-snapshot rows missing from the artifact: {missing[:5]}"


# --- Task 5.2: encryption with an admin-escrow key ----------------------------

SQLITE_MAGIC = b"SQLite format 3\x00"


def test_generate_escrow_key_usable_by_fernet():
    """A generated key must be bytes and round-trip a payload through Fernet."""
    from cryptography.fernet import Fernet

    key = generate_escrow_key()
    assert isinstance(key, bytes)
    f = Fernet(key)
    payload = b"the annual budget review"
    assert f.decrypt(f.encrypt(payload)) == payload


def test_encrypt_decrypt_roundtrip_recovers_identical_bytes(tmp_path):
    key = generate_escrow_key()
    src = tmp_path / "plain.bin"
    original = b"mail body bytes \x00\x01\x02 with nulls and \xff high bytes"
    src.write_bytes(original)

    enc = encrypt_file(src, tmp_path / "cipher.bin", key)
    # Ciphertext must NOT equal the plaintext on disk.
    assert enc.read_bytes() != original

    dec = decrypt_file(enc, tmp_path / "back.bin", key)
    assert dec.read_bytes() == original


def test_decrypt_with_wrong_key_raises_invalid_token(tmp_path):
    from cryptography.fernet import InvalidToken

    import pytest

    key_a = generate_escrow_key()
    key_b = generate_escrow_key()
    src = tmp_path / "plain.bin"
    src.write_bytes(b"secret")

    enc = encrypt_file(src, tmp_path / "cipher.bin", key_a)
    with pytest.raises(InvalidToken):
        decrypt_file(enc, tmp_path / "back.bin", key_b)


def test_make_encrypted_snapshot_not_plaintext_and_roundtrips(tmp_path):
    """Build a real Store, take an encrypted snapshot, and assert:
    (a) the artifact is NOT plaintext sqlite (mail body not shipped in clear);
    (b) decrypting yields a loadable Store with the chunk + entity + cursor;
    (c) no leftover plaintext temp remains in the temp dir."""
    from pathlib import Path

    store = Store(tmp_path / "live.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("d-latest", "the annual budget review", "h1", {})
    store.upsert_entity("taryn-hamilton", "Taryn Hamilton", "person", org="Acme")
    store.set_cursor("gmail", "cursor-token-42")

    key = generate_escrow_key()
    out_dir = tmp_path / "enc_out"
    out = out_dir / "snap.enc"

    before = set(out_dir.iterdir()) if out_dir.exists() else set()

    result = make_encrypted_snapshot(store.path, out, key)

    assert isinstance(result, Path)
    assert result == out
    assert out.exists()

    # (a) Encrypted artifact must not begin with the SQLite magic header.
    head = out.read_bytes()[: len(SQLITE_MAGIC)]
    assert head != SQLITE_MAGIC, "artifact looks like plaintext sqlite — mail in clear"

    # (c) Only the encrypted artifact should sit in out_dir — no stray plaintext.
    after = set(out_dir.iterdir())
    new_files = after - before
    assert new_files == {out}, f"unexpected leftover files: {new_files - {out}}"

    # (b) Decrypt and load as a Store; confirm all three writes survived.
    dec = decrypt_file(out, tmp_path / "restored.sqlite3", key)
    loaded = Store(dec, dim=4)
    assert loaded.get_chunk("d-latest") is not None
    assert loaded.get_entity("taryn-hamilton") is not None
    assert loaded.get_cursor("gmail") == "cursor-token-42"


def test_make_encrypted_snapshot_accepts_str_paths(tmp_path):
    store = Store(tmp_path / "live.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("d1", "hello", "h1", {})

    key = generate_escrow_key()
    out = make_encrypted_snapshot(
        str(store.path), str(tmp_path / "snap.enc"), key
    )
    dec = decrypt_file(out, tmp_path / "restored.sqlite3", key)
    loaded = Store(dec, dim=4)
    assert loaded.get_chunk("d1") is not None


def test_tampered_ciphertext_raises_invalid_token(tmp_path):
    from cryptography.fernet import InvalidToken

    import pytest

    key = generate_escrow_key()
    src = tmp_path / "plain.bin"
    src.write_bytes(b"secret payload")
    enc = encrypt_file(src, tmp_path / "cipher.bin", key)

    raw = bytearray(enc.read_bytes())
    raw[-1] ^= 0x01  # flip a byte
    enc.write_bytes(bytes(raw))

    with pytest.raises(InvalidToken):
        decrypt_file(enc, tmp_path / "back.bin", key)


# --- streaming encryption (bounded memory) ------------------------------------
#
# Live incident 2026-08-04: encrypt_file did Fernet(key).encrypt(read_bytes()),
# holding the WHOLE artifact plus several Fernet copies in RAM. At 4.24GB on a
# 16GB box that meant swap exhaustion and an OOM kill mid-backup. restore() had
# the same shape, which is worse -- that is the emergency path. Encryption is
# now framed so memory is bounded by one chunk, while archives written by the
# old code must still decrypt (7 of them are live on Drive).


def _peak_bytes(fn):
    """Run fn() and return peak Python allocation during it."""
    import tracemalloc

    tracemalloc.start()
    try:
        fn()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return peak


def test_encrypt_file_does_not_buffer_the_whole_artifact(tmp_path):
    import os

    key = generate_escrow_key()
    src = tmp_path / "big.bin"
    src.write_bytes(os.urandom(4 * 1024 * 1024))  # 4MB

    peak = _peak_bytes(
        lambda: encrypt_file(src, tmp_path / "big.enc", key, chunk_size=64 * 1024))

    assert peak < 1_500_000, (
        f"peak {peak} bytes — encryption buffered the artifact instead of "
        "streaming it in chunks")


def test_decrypt_file_does_not_buffer_the_whole_artifact(tmp_path):
    import os

    key = generate_escrow_key()
    src = tmp_path / "big.bin"
    src.write_bytes(os.urandom(4 * 1024 * 1024))
    enc = encrypt_file(src, tmp_path / "big.enc", key, chunk_size=64 * 1024)

    peak = _peak_bytes(lambda: decrypt_file(enc, tmp_path / "back.bin", key))

    assert peak < 1_500_000, (
        f"peak {peak} bytes — decryption buffered the artifact instead of "
        "streaming it in chunks")


def test_multi_frame_archive_round_trips_exactly(tmp_path):
    import os

    key = generate_escrow_key()
    original = os.urandom(500_000)
    src = tmp_path / "plain.bin"
    src.write_bytes(original)

    enc = encrypt_file(src, tmp_path / "c.bin", key, chunk_size=4096)  # ~123 frames
    dec = decrypt_file(enc, tmp_path / "back.bin", key)

    assert dec.read_bytes() == original


def test_empty_file_round_trips(tmp_path):
    key = generate_escrow_key()
    src = tmp_path / "empty.bin"
    src.write_bytes(b"")

    enc = encrypt_file(src, tmp_path / "c.bin", key)
    dec = decrypt_file(enc, tmp_path / "back.bin", key)

    assert dec.read_bytes() == b""


def test_decrypt_reads_legacy_single_token_archives(tmp_path):
    """The 7 snapshots already on Drive were written as one Fernet token.

    Losing the ability to read them would silently invalidate every existing
    backup — the artifact format may change, but only additively.
    """
    from cryptography.fernet import Fernet

    key = generate_escrow_key()
    original = b"legacy snapshot bytes \x00\xff"
    legacy = tmp_path / "legacy.enc"
    legacy.write_bytes(Fernet(key).encrypt(original))

    dec = decrypt_file(legacy, tmp_path / "back.bin", key)

    assert dec.read_bytes() == original


def _write_v2_archive(path, payload, key, *, chunk_size=4096):
    """Write a LEGACY v2 framed archive (5-byte `>IB` header, no archive id).

    A fixture generator, deliberately independent of _FrameWriter: _FrameWriter
    only writes v3 now, so the legacy read path has to be tested against bytes
    built to the old spec rather than against the current writer.
    """
    import struct
    from pathlib import Path

    from cryptography.fernet import Fernet

    from mcpbrain.backup import _ARCHIVE_MAGIC_V2

    fernet = Fernet(key)
    frames = [payload[i:i + chunk_size]
              for i in range(0, len(payload), chunk_size)] or [b""]
    blob = bytearray(_ARCHIVE_MAGIC_V2)
    for i, part in enumerate(frames):
        token = fernet.encrypt(
            struct.pack(">IB", i, 1 if i == len(frames) - 1 else 0) + part)
        blob += struct.pack(">I", len(token)) + token
    Path(path).write_bytes(bytes(blob))
    return Path(path)


def test_decrypt_reads_legacy_v2_framed_archives(tmp_path):
    """v2 archives EXIST off-machine and must stay readable.

    The author box held a 4.2GB v2 snapshot (magic MCPBRAIN-ENC-v2\\n, 8MiB
    frames, `>IB` headers) with retain=7 more on the Shared Drive. Adding the
    archive id under the v2 magic MISPARSED all of them — 21 bytes of header
    consumed where the frame carries 5 — which is why v3 got its own magic and
    v2 keeps a read path.
    """
    key = generate_escrow_key()
    original = b"legacy v2 payload \x00\xff" * 900     # several 4KB frames
    arc = _write_v2_archive(tmp_path / "v2.enc", original, key)

    dec = decrypt_file(arc, tmp_path / "back.bin", key)

    assert dec.read_bytes() == original


def test_legacy_v2_archive_still_rejects_truncation_and_reordering(tmp_path):
    """v2 loses only the SPLICE check — order and completeness still hold.

    v2 frames bind order, just not archive identity, so the legacy path must
    keep enforcing what v2 can actually prove. A read path that waved these
    through would turn "old format" into "unchecked format".
    """
    from cryptography.fernet import InvalidToken

    import pytest

    key = generate_escrow_key()
    original = b"C" * 20_000
    arc = _write_v2_archive(tmp_path / "v2.enc", original, key)
    frames, magic = _split_frames(arc.read_bytes())
    assert len(frames) >= 3

    truncated = tmp_path / "v2-trunc.enc"
    truncated.write_bytes(magic + b"".join(frames[:-1]))   # drop the final frame
    with pytest.raises(InvalidToken):
        decrypt_file(truncated, tmp_path / "a.bin", key)

    swapped = tmp_path / "v2-swap.enc"
    reordered = list(frames)
    reordered[0], reordered[1] = reordered[1], reordered[0]
    swapped.write_bytes(magic + b"".join(reordered))
    with pytest.raises(InvalidToken):
        decrypt_file(swapped, tmp_path / "b.bin", key)


def test_unknown_magic_raises_unsupported_archive(tmp_path):
    """An unrecognised format must be REFUSED, never guessed at.

    This is the property whose absence caused the v2 misparse: the v2 magic was
    reused for a changed header, so the reader accepted the file and then
    consumed the wrong number of header bytes. Anything that is not a magic we
    know, and not a Fernet token, has to stop the read.
    """
    import pytest

    from mcpbrain.backup import UnsupportedArchive

    key = generate_escrow_key()
    bogus = tmp_path / "future.enc"
    bogus.write_bytes(b"MCPBRAIN-ENC-v9\n" + b"whatever follows")

    with pytest.raises(UnsupportedArchive) as ei:
        decrypt_file(bogus, tmp_path / "back.bin", key)
    assert "unsupported archive format" in str(ei.value)

    # Not a magic and not a Fernet token either -> same refusal, not InvalidToken.
    garbage = tmp_path / "garbage.enc"
    garbage.write_bytes(b"\x00" * 64)
    with pytest.raises(UnsupportedArchive):
        decrypt_file(garbage, tmp_path / "back2.bin", key)


def test_new_archives_are_written_as_v3(tmp_path):
    """The writer must emit the v3 magic — a header change needs a new magic."""
    from mcpbrain.backup import _ARCHIVE_MAGIC_V3

    key = generate_escrow_key()
    src = tmp_path / "plain.bin"
    src.write_bytes(b"x" * 100)
    enc = encrypt_file(src, tmp_path / "c.bin", key)

    assert enc.read_bytes()[:16] == _ARCHIVE_MAGIC_V3


def test_truncated_archive_is_rejected(tmp_path):
    """Dropping trailing frames must fail loudly, not yield a short store.

    A single Fernet token authenticated the whole artifact, so truncation was
    impossible to miss. Framing must not quietly lose that property: a half
    restored store is far worse than a failed restore.
    """
    from cryptography.fernet import InvalidToken

    import pytest

    key = generate_escrow_key()
    src = tmp_path / "plain.bin"
    src.write_bytes(b"A" * 40_000)
    enc = encrypt_file(src, tmp_path / "c.bin", key, chunk_size=4096)

    raw = enc.read_bytes()
    enc.write_bytes(raw[: len(raw) // 2])  # lop off the tail, whole frames and all

    with pytest.raises(InvalidToken):
        decrypt_file(enc, tmp_path / "back.bin", key)


def test_reordered_frames_are_rejected(tmp_path):
    """Swapping two frames must be detected — each frame binds its own index."""
    from cryptography.fernet import InvalidToken

    import pytest

    key = generate_escrow_key()
    src = tmp_path / "plain.bin"
    src.write_bytes(b"B" * 12_000)
    enc = encrypt_file(src, tmp_path / "c.bin", key, chunk_size=4096)

    frames, rest = _split_frames(enc.read_bytes())
    assert len(frames) >= 3, "test needs at least 3 frames to swap two"
    frames[0], frames[1] = frames[1], frames[0]
    enc.write_bytes(rest + b"".join(frames))

    with pytest.raises(InvalidToken):
        decrypt_file(enc, tmp_path / "back.bin", key)


def test_spliced_frames_from_two_v3_archives_are_rejected(tmp_path):
    """A chimera built from two v3 archives under the SAME key must be rejected.

    The daily cadence reuses one escrow key, so "two archives under one key" is
    the steady state, and the escrow key lives in an all-members-readable fleet
    folder. With only (index, is_final) inside the authenticated plaintext,
    frames 0..k of archive A followed by frames k+1..final of archive B form a
    perfectly ordered, final-flagged run — so splicing two stores together
    needed NO key at all. Each v3 frame must also bind the archive it belongs to.

    v2 cannot be held to this (its frames carry no archive id); that is exactly
    why v3 exists and why v2 is read-only legacy.
    """
    from cryptography.fernet import InvalidToken

    import pytest

    from mcpbrain.backup import _ARCHIVE_MAGIC_V3

    key = generate_escrow_key()
    a_src = tmp_path / "a.bin"
    b_src = tmp_path / "b.bin"
    a_src.write_bytes(b"A" * 20_000)
    b_src.write_bytes(b"B" * 20_000)
    a = encrypt_file(a_src, tmp_path / "a.enc", key, chunk_size=4096)
    b = encrypt_file(b_src, tmp_path / "b.enc", key, chunk_size=4096)

    a_frames, magic = _split_frames(a.read_bytes())
    b_frames, _ = _split_frames(b.read_bytes())
    assert magic == _ARCHIVE_MAGIC_V3, "this test is about the v3 guarantee"
    assert len(a_frames) == len(b_frames) >= 3
    # First half of A, second half of B: indices stay 0..n, final flag intact.
    cut = len(a_frames) // 2
    chimera = tmp_path / "chimera.enc"
    chimera.write_bytes(magic + b"".join(a_frames[:cut] + b_frames[cut:]))

    with pytest.raises(InvalidToken):
        decrypt_file(chimera, tmp_path / "back.bin", key)


def test_each_archive_gets_a_distinct_id(tmp_path):
    """Two archives of identical bytes under one key must not share an id.

    A constant or derived id would re-open the splice, so the id has to be
    random per archive.
    """
    key = generate_escrow_key()
    src = tmp_path / "same.bin"
    src.write_bytes(b"identical payload" * 100)
    one = encrypt_file(src, tmp_path / "one.enc", key)
    two = encrypt_file(src, tmp_path / "two.enc", key)

    assert _first_archive_id(one, key) != _first_archive_id(two, key)


def _first_archive_id(path, key):
    """The archive id bound into a framed archive's first frame."""
    from cryptography.fernet import Fernet

    from mcpbrain.backup import _FRAME_HEADER

    frames, _magic = _split_frames(path.read_bytes())
    plain = Fernet(key).decrypt(frames[0][4:])
    return _FRAME_HEADER.unpack(plain[:_FRAME_HEADER.size])[0]


def test_aborted_write_leaves_an_artifact_decrypt_rejects(tmp_path):
    """An abort mid-archive must NOT leave a valid-looking archive behind.

    open_encrypted's cleanup used to close the writer in a `finally`, and
    close() emits the final frame — so a body that raised for any non-I/O
    reason (a records file removed mid-`tar.add`, KeyboardInterrupt) produced a
    complete, correctly-indexed, final-flagged archive wrapping a TRUNCATED
    tar.gz, which decrypt_file happily accepted. The final frame must be the
    marker of a clean exit, nothing less.
    """
    from cryptography.fernet import InvalidToken

    import pytest

    from mcpbrain.backup import open_encrypted

    key = generate_escrow_key()
    out = tmp_path / "aborted.enc"

    class _Boom(Exception):
        pass

    with pytest.raises(_Boom):
        with open_encrypted(out, key, chunk_size=4096) as enc:
            enc.write(b"C" * 12_000)   # several whole frames land on disk
            raise _Boom("records file vanished mid-tar")

    assert out.exists(), "test needs the partial artifact to still be there"
    with pytest.raises(InvalidToken):
        decrypt_file(out, tmp_path / "back.bin", key)


def test_aborted_bundle_snapshot_leaves_an_artifact_decrypt_rejects(tmp_path,
                                                                   monkeypatch):
    """The same guarantee through the REAL producer, tarfile and all.

    This is the path that actually aborts in the field (a records file removed
    mid-`tar.add`, a git operation in the records repo), and the tarfile
    interaction is the subtle half: on an exception TarFile.__exit__ skips the
    end-of-archive blocks but _Stream still flushes the gzip trailer into the
    encrypting sink, so bytes keep arriving after the failure. The final frame
    must still never be emitted.
    """
    import tarfile

    from cryptography.fernet import InvalidToken

    import pytest

    store = Store(tmp_path / "live.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("d1", "x" * 5000, "h1", {})
    records = tmp_path / "records"
    records.mkdir()
    (records / "world.md").write_text("world model", encoding="utf-8")
    out = tmp_path / "snap.enc"
    key = generate_escrow_key()

    real_add = tarfile.TarFile.add
    calls = {"n": 0}

    def _add(self, *a, **kw):
        calls["n"] += 1
        if calls["n"] > 1:            # the store went in; the records repo dies
            raise OSError("records file vanished mid-tar")
        return real_add(self, *a, **kw)

    monkeypatch.setattr(tarfile.TarFile, "add", _add)

    with pytest.raises(OSError):
        make_encrypted_snapshot(store.path, out, key, records_dir=records)

    assert out.exists(), "test needs the partial artifact to still be there"
    with pytest.raises(InvalidToken):
        decrypt_file(out, tmp_path / "back.bin", key)


def test_restore_does_not_buffer_the_whole_artifact(tmp_path):
    """Restore is the emergency path — it must not OOM on a large snapshot.

    restore() decrypted the entire artifact into one bytes object and then
    wrote that back out again, so recovering a multi-GB snapshot needed several
    times its size in RAM at the exact moment the machine is least healthy.
    """
    import os

    from mcpbrain.backup import restore

    key = generate_escrow_key()
    store_src = tmp_path / "plain.sqlite3"
    store_src.write_bytes(SQLITE_MAGIC + os.urandom(4 * 1024 * 1024))
    enc = encrypt_file(store_src, tmp_path / "snap.enc", key, chunk_size=64 * 1024)

    dest = tmp_path / "restored.sqlite3"
    peak = _peak_bytes(lambda: restore(enc, dest, key))

    assert dest.read_bytes() == store_src.read_bytes()
    assert peak < 1_500_000, (
        f"peak {peak} bytes — restore buffered the whole artifact")


def _split_frames(blob):
    """Split a framed archive into (list_of_raw_frames, magic).

    Version-agnostic: the length prefixes are identical in v2 and v3 (only the
    plaintext header inside each token differs), and every magic is the same
    fixed length, so this works for either shape.
    """
    import struct

    from mcpbrain.backup import _MAGIC_LEN

    magic = blob[:_MAGIC_LEN]
    assert magic.startswith(b"MCPBRAIN-ENC-v"), magic
    pos = _MAGIC_LEN
    frames = []
    while pos < len(blob):
        (n,) = struct.unpack(">I", blob[pos:pos + 4])
        frames.append(blob[pos:pos + 4 + n])
        pos += 4 + n
    return frames, magic


# --- bounded temp disk during snapshot ----------------------------------------
#
# make_encrypted_snapshot wrote a full plaintext bundle.tar.gz next to the store
# copy, so peak temp was ~2x the store. On the live 11.9GB store that is ~15GB
# of transient cleartext, and it is the mechanism behind the 2026-08-03 ENOSPC
# storm (57 backups failed in a day with "No space left on device", each leaving
# another orphaned work dir). Streaming the tar straight into the encryptor
# removes the intermediate copy entirely; a pre-flight free-space check turns
# what is left into a clean, backed-off failure instead of a filled disk.


def _fat_store(path, mb):
    """A valid Store padded to roughly `mb` megabytes of INCOMPRESSIBLE data,
    so the tar.gz is about the same size as the store (a compressible filler
    would hide the second copy this test is looking for)."""
    import os
    import sqlite3

    store = Store(path, dim=4)
    store.init()
    store.upsert_chunk("d1", "hello", "h1", {})
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE IF NOT EXISTS _bulk (b BLOB)")
    for _ in range(mb):
        con.execute("INSERT INTO _bulk VALUES (?)", (os.urandom(1024 * 1024),))
    con.commit()
    con.close()
    return store


def _dir_bytes(d):
    from pathlib import Path as _P
    total = 0
    for p in _P(d).rglob("*"):
        try:
            total += p.stat().st_size
        except OSError:
            pass
    return total


def test_snapshot_never_materialises_a_plaintext_bundle(tmp_path, monkeypatch):
    """Peak temp usage must be about ONE copy of the store, not two."""
    import tempfile as _tf
    import threading

    from mcpbrain import backup as _bk

    mb = 16
    store = _fat_store(tmp_path / "fat.sqlite3", mb)
    records = tmp_path / "records"          # force the bundle path
    records.mkdir()
    (records / "note.md").write_text("hello")

    created = []
    real_mkdtemp = _tf.mkdtemp

    def _spy(*a, **k):
        d = real_mkdtemp(*a, **k)
        created.append(d)
        return d

    monkeypatch.setattr(_bk.tempfile, "mkdtemp", _spy)

    peak = {"v": 0}
    stop = threading.Event()

    def _sampler():
        while not stop.is_set():
            if created:
                peak["v"] = max(peak["v"], _dir_bytes(created[0]))
            stop.wait(0.002)

    t = threading.Thread(target=_sampler, daemon=True)
    t.start()
    try:
        out = make_encrypted_snapshot(
            store.path, tmp_path / "snap.enc", generate_escrow_key(),
            records_dir=records)
    finally:
        stop.set()
        t.join(timeout=5)

    assert out.exists()
    one_copy = mb * 1024 * 1024
    assert peak["v"] < one_copy * 1.5, (
        f"peak temp {peak['v'] / 1e6:.0f}MB for a {mb}MB store — a second full "
        "plaintext copy (bundle.tar.gz) was materialised alongside it")


def test_snapshot_bundle_still_round_trips_when_streamed(tmp_path):
    """Streaming the tar must not change what comes back out."""
    store = Store(tmp_path / "live.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("d-latest", "the annual budget review", "h1", {})
    records = tmp_path / "records"
    records.mkdir()
    (records / "world.md").write_text("world model contents")
    cfgp = tmp_path / "config.json"
    cfgp.write_text('{"owner_name": "Josh"}')

    key = generate_escrow_key()
    out = make_encrypted_snapshot(store.path, tmp_path / "snap.enc", key,
                                  records_dir=records, config_path=cfgp)

    from mcpbrain.backup import restore
    dest_records = tmp_path / "restored_records"
    dest_cfg = tmp_path / "restored_config.json"
    restore(out, tmp_path / "restored.sqlite3", key,
            records_dir=dest_records, config_path=dest_cfg)

    assert Store(tmp_path / "restored.sqlite3", dim=4).get_chunk("d-latest") is not None
    assert (dest_records / "world.md").read_text() == "world model contents"
    assert dest_cfg.read_text() == '{"owner_name": "Josh"}'


def test_snapshot_refuses_to_start_without_free_space(tmp_path, monkeypatch):
    """Fail fast and clean rather than filling the disk.

    On 2026-08-03 the retry storm ran the system disk to zero, which takes down
    far more than the backup. Checking first turns that into one logged, backed
    off failure per interval.
    """
    import errno

    import pytest

    from mcpbrain import backup as _bk

    store = Store(tmp_path / "live.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("d1", "x", "h1", {})
    out = tmp_path / "snap.enc"

    monkeypatch.setattr(_bk.shutil, "disk_usage",
                        lambda p: _FakeUsage(free=4096))  # 4KB free

    with pytest.raises(OSError) as ei:
        make_encrypted_snapshot(store.path, out, generate_escrow_key())

    assert ei.value.errno == errno.ENOSPC
    assert not out.exists(), "wrote a partial artifact despite refusing to start"


class _FakeUsage:
    """Stands in for shutil.disk_usage()'s named tuple (only .free is read)."""

    def __init__(self, free):
        self.free = free


def test_free_space_check_does_not_need_statvfs(tmp_path, monkeypatch):
    """The pre-flight check must work on Windows, which has no os.statvfs.

    _require_free_space ran os.statvfs, which does not exist on Windows at all.
    make_encrypted_snapshot calls it before any work, so on every Windows
    install the whole backup raised AttributeError, the daemon's broad
    `except Exception` swallowed it, the cadence backed off one interval — and
    backups never ran, forever, with nothing surfaced. The test suite could not
    catch it either, because the old test monkeypatched os.statvfs (itself an
    AttributeError on the platform that matters).
    """
    import os

    from mcpbrain import backup as _bk

    store = Store(tmp_path / "live.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("d1", "x", "h1", {})
    out = tmp_path / "snap.enc"

    # Simulate Windows: os.statvfs simply isn't there, and shutil.disk_usage is
    # the nt._getdiskfree-backed implementation that does not consult it. Any
    # os.statvfs call from backup.py's own code now raises AttributeError.
    monkeypatch.setattr(_bk.shutil, "disk_usage",
                        lambda p: _FakeUsage(free=10 ** 12))
    monkeypatch.delattr(os, "statvfs", raising=False)

    # Plenty of room -> the check passes and the snapshot completes.
    _bk.make_encrypted_snapshot(store.path, out, generate_escrow_key())
    assert out.exists() and out.stat().st_size > 0


# --- Task 5.3: upload encrypted snapshot to a Shared Drive --------------------


class _FakeCreate:
    """Records the kwargs of a single files().create call and returns a canned
    id from .execute(). The fake distinguishes a folder-create (body has
    mimeType == folder) from a file-upload create by inspecting the body."""

    def __init__(self, calls, canned_id, executes=None):
        self.calls = calls
        self.canned_id = canned_id
        self.executes = executes if executes is not None else []

    def execute(self, num_retries=0):
        self.executes.append(num_retries)
        return {"id": self.canned_id}


class _FakeList:
    def __init__(self, calls, canned, executes=None):
        self.calls = calls
        self.canned = canned
        self.executes = executes if executes is not None else []

    def execute(self, num_retries=0):
        self.executes.append(num_retries)
        return self.canned


class FakeFiles:
    """Mimics the chained googleapiclient pattern: service.files().list(**kw)
    .execute() and service.files().create(**kw).execute(). Records every call so
    tests can assert on kwargs, and branches its create response on whether the
    body is a folder (mimeType == application/vnd.google-apps.folder)."""

    FOLDER_MIME = "application/vnd.google-apps.folder"

    def __init__(self, list_response, folder_id="folder-new", file_id="file-123"):
        self.list_response = list_response
        self.folder_id = folder_id
        self.file_id = file_id
        self.list_calls = []
        self.create_calls = []
        # num_retries passed to each list(...).execute(), in call order.
        self.list_retries = []
        # num_retries passed to each create(...).execute(), in call order.
        self.execute_retries = []

    def list(self, **kw):
        self.list_calls.append(kw)
        return _FakeList(self.list_calls, self.list_response, executes=self.list_retries)

    def create(self, **kw):
        self.create_calls.append(kw)
        body = kw.get("body", {})
        if body.get("mimeType") == self.FOLDER_MIME:
            return _FakeCreate(self.create_calls, self.folder_id,
                               self.execute_retries)
        return _FakeCreate(self.create_calls, self.file_id, self.execute_retries)


class FakeService:
    def __init__(self, files):
        self._files = files

    def files(self):
        return self._files


def _fake_media(path):
    return ("MEDIA", path)


def test_upload_creates_folder_when_missing_then_uploads(tmp_path):
    src = tmp_path / "snap.enc"
    src.write_bytes(b"ciphertext")

    files = FakeFiles(list_response={"files": []})
    service = FakeService(files)

    result = upload_snapshot(
        service, src, "drive-XYZ", "sam", media_factory=_fake_media
    )

    # Folder was missing → exactly two create calls: folder, then file.
    folder_creates = [
        c
        for c in files.create_calls
        if c["body"].get("mimeType") == FakeFiles.FOLDER_MIME
    ]
    file_creates = [
        c
        for c in files.create_calls
        if c["body"].get("mimeType") != FakeFiles.FOLDER_MIME
    ]
    assert len(folder_creates) == 1
    assert len(file_creates) == 1

    fc = folder_creates[0]
    assert fc["body"]["name"] == "sam"
    assert fc["body"]["mimeType"] == FakeFiles.FOLDER_MIME
    assert fc["body"]["parents"] == ["drive-XYZ"]
    assert fc["supportsAllDrives"] is True

    up = file_creates[0]
    assert up["body"]["name"] == "snap.enc"
    assert up["body"]["parents"] == ["folder-new"]
    assert up["supportsAllDrives"] is True
    assert up.get("media_body") is not None

    assert result == "file-123"


def test_upload_does_not_ask_the_library_to_retry_media_chunks(tmp_path):
    """num_retries MUST be 0 now that the upload is resumable.

    googleapiclient's media-retry loop (HttpRequest.next_chunk:
    `for retry_num in range(num_retries + 1)`) re-sends the SAME _StreamSlice
    object, and _StreamSlice seeks only in __init__ — so a retried chunk reads
    b"" and puts 0 bytes on the wire against a Content-Length of up to 100MB.
    The connection then blocks until the socket timeout, which for the Drive
    service is 600s. maybe_backup runs on the cycle thread holding the bulk
    lock with _backup_in_progress set, which tells the watchdog to DEFER
    recovery — so one transient 5xx would wedge the daemon for ~10 minutes with
    the watchdog explicitly muzzled. The cadence retry is the real backoff.
    """
    src = tmp_path / "snap.enc"
    src.write_bytes(b"ciphertext")
    files = FakeFiles(list_response={"files": [{"id": "folder-1", "name": "sam"}]})

    upload_snapshot(FakeService(files), src, "drive-XYZ", "sam",
                    media_factory=_fake_media)

    assert files.execute_retries == [0], (
        "media create asked the library to retry chunks it cannot re-seek")


def test_upload_to_folder_does_not_ask_the_library_to_retry_media_chunks(tmp_path):
    """Same defect, same fix, on the folder-based upload path."""
    from mcpbrain.backup import upload_to_folder

    src = tmp_path / "cache.enc"
    src.write_bytes(b"ciphertext")
    files = FakeFiles(list_response={"files": []})

    upload_to_folder(FakeService(files), src, "folder-1",
                     media_factory=_fake_media)

    assert files.execute_retries == [0]


def test_upload_reuses_existing_folder_no_folder_create(tmp_path):
    src = tmp_path / "snap.enc"
    src.write_bytes(b"ciphertext")

    files = FakeFiles(
        list_response={"files": [{"id": "folder-existing", "name": "sam"}]}
    )
    service = FakeService(files)

    result = upload_snapshot(
        service, src, "drive-XYZ", "sam", media_factory=_fake_media
    )

    # No folder-create call — only the file upload create.
    folder_creates = [
        c
        for c in files.create_calls
        if c["body"].get("mimeType") == FakeFiles.FOLDER_MIME
    ]
    assert folder_creates == []
    assert len(files.create_calls) == 1

    up = files.create_calls[0]
    assert up["body"]["parents"] == ["folder-existing"]
    assert result == "file-123"


def test_upload_sets_supports_all_drives_on_every_call(tmp_path):
    src = tmp_path / "snap.enc"
    src.write_bytes(b"ciphertext")

    files = FakeFiles(list_response={"files": []})
    service = FakeService(files)

    upload_snapshot(service, src, "drive-XYZ", "sam", media_factory=_fake_media)

    # list + both create variants must set supportsAllDrives=True.
    for call in files.list_calls:
        assert call["supportsAllDrives"] is True
    for call in files.create_calls:
        assert call["supportsAllDrives"] is True
    # Sanity: there were two create calls (folder + file).
    assert len(files.create_calls) == 2


def test_upload_uses_injected_media_factory(tmp_path):
    src = tmp_path / "snap.enc"
    src.write_bytes(b"ciphertext")

    files = FakeFiles(list_response={"files": [{"id": "f-1", "name": "sam"}]})
    service = FakeService(files)

    upload_snapshot(service, src, "drive-XYZ", "sam", media_factory=_fake_media)

    up = files.create_calls[0]
    # The fake media tuple must have reached media_body — proof no real
    # MediaFileUpload import happened.
    assert up["media_body"] == ("MEDIA", str(src))


def test_upload_accepts_str_path_and_uses_basename(tmp_path):
    src = tmp_path / "nested" / "snap.enc"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"ciphertext")

    files = FakeFiles(list_response={"files": [{"id": "f-1", "name": "sam"}]})
    service = FakeService(files)

    upload_snapshot(
        service, str(src), "drive-XYZ", "sam", media_factory=_fake_media
    )

    up = files.create_calls[0]
    assert up["body"]["name"] == "snap.enc"


def test_upload_list_query_targets_per_user_folder_in_drive(tmp_path):
    src = tmp_path / "snap.enc"
    src.write_bytes(b"ciphertext")

    files = FakeFiles(list_response={"files": []})
    service = FakeService(files)

    upload_snapshot(service, src, "drive-XYZ", "sam", media_factory=_fake_media)

    lc = files.list_calls[0]
    assert "sam" in lc["q"]
    assert FakeFiles.FOLDER_MIME in lc["q"]
    assert "drive-XYZ" in lc["q"]
    assert lc["driveId"] == "drive-XYZ"
    assert lc["corpora"] == "drive"
    assert lc["includeItemsFromAllDrives"] is True


def test_upload_rejects_user_id_with_query_unsafe_chars(tmp_path):
    """A user_id containing an apostrophe (or backslash) would break the
    single-quoted Drive query, so upload_snapshot must reject it before any
    Drive call is made. A normal email user_id must still work."""
    import pytest

    src = tmp_path / "snap.enc"
    src.write_bytes(b"ciphertext")

    files = FakeFiles(list_response={"files": []})
    service = FakeService(files)

    with pytest.raises(ValueError, match="unsafe in a Drive query"):
        upload_snapshot(
            service, src, "drive-1", "o'brien@example.com", media_factory=_fake_media
        )
    # No Drive calls should have been made before the guard tripped.
    assert files.list_calls == []
    assert files.create_calls == []

    # A normal email user_id still uploads cleanly.
    result = upload_snapshot(
        service, src, "drive-1", "normal@example.com", media_factory=_fake_media
    )
    assert result == "file-123"


# --- Task 5.4: restore + find/download snapshot + delta-sync roundtrip --------

import base64

from mcpbrain.sync import run_sync_cycle


# Fake Gmail service reused from the test_gmail_sync.py / test_sync_cycle.py
# shape: users().getProfile / history().list / messages().get, all chained
# through .execute(). Kept minimal but real-shaped.

def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode()


def _plain_msg(mid, subject, sender, body):
    return {
        "id": mid,
        "threadId": "t-" + mid,
        "labelIds": ["INBOX"],
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": sender},
            ],
            "body": {"data": _b64(body)},
        },
    }


class _GReq:
    def __init__(self, result):
        self._r = result

    def execute(self):
        return self._r


class _GHistory:
    def __init__(self, pages, expected_start_history_id=None, recorded_start_history_ids=None):
        self._pages = pages
        # Gate the delta page on the restored cursor. When an expected value is
        # set, list() only returns the populated page if startHistoryId matches;
        # any other value (bootstrap "9999", None) yields an empty page so a
        # wrong cursor produces zero new chunks.
        self._expected_start_history_id = expected_start_history_id
        # Shared list so the test can assert the exact startHistoryId values the
        # delta path passed in (proves resume-from-restored-cursor).
        self.recorded_start_history_ids = (
            recorded_start_history_ids if recorded_start_history_ids is not None else []
        )

    def list(self, **kw):
        start = kw.get("startHistoryId")
        token = kw.get("pageToken")
        # Record only the first page of a delta (pageToken is None). Paged calls
        # reuse the same startHistoryId, so recording every call would inflate the
        # list and break the `== [SNAPSHOT_CURSOR]` assertion under multi-page sync.
        if token is None:
            self.recorded_start_history_ids.append(start)
        idx = 0 if token is None else int(token)
        if (
            self._expected_start_history_id is not None
            and start != self._expected_start_history_id
        ):
            # Wrong/None cursor — return an empty history page.
            return _GReq({"history": [], "historyId": start or "0"})
        return _GReq(self._pages[idx])


class _GMessages:
    def __init__(self, by_id):
        self._by_id = by_id

    def get(self, userId, id, format):
        return _GReq(self._by_id[id])


class _GUsers:
    def __init__(self, profile_hid, history, messages):
        self._p = profile_hid
        self._h = history
        self._m = messages
        # Count getProfile calls. A correct delta-sync from a non-None cursor
        # NEVER calls getProfile; only the bootstrap / 404-410 reset paths do.
        self.get_profile_calls = 0

    def getProfile(self, userId):
        self.get_profile_calls += 1
        return _GReq({"historyId": self._p, "emailAddress": "test@example.com"})

    def history(self):
        return self._h

    def messages(self):
        return self._m


class FakeGmailService:
    def __init__(
        self,
        profile_hid="1000",
        pages=None,
        messages=None,
        expected_start_history_id=None,
    ):
        self._history = _GHistory(
            pages or [], expected_start_history_id=expected_start_history_id
        )
        self._users = _GUsers(profile_hid, self._history, _GMessages(messages or {}))

    def users(self):
        return self._users

    @property
    def recorded_start_history_ids(self):
        return self._history.recorded_start_history_ids

    @property
    def get_profile_calls(self):
        return self._users.get_profile_calls


def _gmail_page(msg_ids, history_id, next_page_token=None):
    history = [
        {
            "id": f"h-{mid}",
            "messagesAdded": [{"message": {"id": mid, "labelIds": ["INBOX"]}}],
        }
        for mid in msg_ids
    ]
    page = {"history": history, "historyId": history_id}
    if next_page_token is not None:
        page["nextPageToken"] = next_page_token
    return page


def test_restore_decrypts_artifact_to_dest_and_opens_as_store(tmp_path):
    """Focused: restore() decrypts an encrypted artifact to the dest store path
    and the result opens as a valid Store with the original data."""
    from pathlib import Path

    store = Store(tmp_path / "live.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("d-budget", "the annual budget review", "h1", {})

    key = generate_escrow_key()
    enc = make_encrypted_snapshot(store.path, tmp_path / "snap.enc", key)

    dest = tmp_path / "restored" / "live.sqlite3"
    result = restore(enc, dest, key)

    assert isinstance(result, Path)
    assert result == dest
    assert dest.exists()
    loaded = Store(dest, dim=4)
    assert loaded.get_chunk("d-budget") is not None


def test_restore_handles_a_mixed_format_folder(tmp_path):
    """A snapshot folder holds BOTH formats — restore must handle either.

    retain=7 with a daily cadence means the Shared Drive carries up to 7 legacy
    v2 archives alongside new v3 ones for a week after this change. restore()
    itself is format-agnostic (it only inspects _SQLITE_MAGIC on the DECRYPTED
    output), so this pins that decrypt_file's magic dispatch is what carries it,
    for the real bundle shape (gzip tar) rather than a bare store.
    """
    import io
    import tarfile
    from pathlib import Path

    store = Store(tmp_path / "live.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("d-budget", "the annual budget review", "h1", {})
    records = tmp_path / "records"
    records.mkdir()
    (records / "world.md").write_text("world model contents", encoding="utf-8")
    cfg = tmp_path / "config.json"
    cfg.write_text('{"owner_name": "Josh"}', encoding="utf-8")
    key = generate_escrow_key()

    # --- v3: written by the current producer.
    v3 = make_encrypted_snapshot(store.path, tmp_path / "new.enc", key,
                                 records_dir=records, config_path=cfg)
    assert v3.read_bytes()[:16] == b"MCPBRAIN-ENC-v3\n"

    # --- v2: the same bundle shape, framed the legacy way.
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w|gz") as tar:
        tar.add(store.path, arcname="store/brain.sqlite3")
        tar.add(records, arcname="records")
        tar.add(cfg, arcname="config.json")
    v2 = _write_v2_archive(tmp_path / "old.enc", raw.getvalue(), key,
                           chunk_size=8192)
    assert v2.read_bytes()[:16] == b"MCPBRAIN-ENC-v2\n"

    for name, arc in (("v3", v3), ("v2", v2)):
        dest = tmp_path / name / "brain.sqlite3"
        dest_records = tmp_path / name / "records"
        dest_cfg = tmp_path / name / "config.json"
        result = restore(arc, dest, key,
                         records_dir=dest_records, config_path=dest_cfg)
        assert result == Path(dest), name
        assert Store(dest, dim=4).get_chunk("d-budget") is not None, name
        assert (dest_records / "world.md").read_text() == "world model contents", name
        assert dest_cfg.read_text() == '{"owner_name": "Josh"}', name


def test_restore_wrong_key_raises_before_overwriting_dest(tmp_path):
    """A wrong escrow key must raise InvalidToken (Fernet authenticates)."""
    from cryptography.fernet import InvalidToken

    import pytest

    store = Store(tmp_path / "live.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("d1", "hello", "h1", {})

    key = generate_escrow_key()
    enc = make_encrypted_snapshot(store.path, tmp_path / "snap.enc", key)

    dest = tmp_path / "out" / "live.sqlite3"
    with pytest.raises(InvalidToken):
        restore(enc, dest, generate_escrow_key())

    # Decrypt authenticates before any write, so no partial/corrupt store may be
    # left at the dest. (The parent dir may be created; the FILE must be absent.)
    assert not dest.exists(), "wrong-key restore must not leave a file at dest"


def test_snapshot_wipe_restore_delta_sync_roundtrip(tmp_path):
    """PHASE 5 EXIT: snapshot -> wipe -> restore -> delta-sync roundtrip.

    Proves the full reinstall recovery path:
      1. A live store holds an indexed chunk (vec + fts rows), a graph entity,
         and a Gmail sync cursor at historyId "1000" (the snapshot point).
      2. An encrypted snapshot is taken.
      3. The live store file (and any -wal/-shm) is wiped — the reinstall.
      4. restore() decrypts the snapshot back to the live path. A fresh Store
         recovers the chunk, vec/fts searchability, the entity, AND the cursor.
      5. DELTA-SYNC catches the gap: run_sync_cycle is given a fake Gmail
         service whose delta page (read from the RESTORED cursor) returns ONE
         NEW message dated after the snapshot, with a higher historyId "1042".
         We assert the new message's chunk is indexed/searchable AND the cursor
         advanced past the restored "1000" — proving sync resumed from the
         restored cursor and caught the post-snapshot change.
    """
    from tests.test_retrieval import FakeEmbedder

    emb = FakeEmbedder()  # dim == 4, keyword/semantic fake

    # 1. Build + populate the live store.
    live = tmp_path / "live.sqlite3"
    store = Store(live, dim=emb.dim)
    store.init()
    store.upsert_chunk("d-budget", "the annual budget review", "h1", {})
    store.upsert_entity(
        "taryn-hamilton", "Taryn Hamilton", "person", org="Acme"
    )
    index_pending(store, emb)  # vec + fts rows now exist
    SNAPSHOT_CURSOR = "1000"
    store.set_cursor("gmail", SNAPSHOT_CURSOR)

    # Sanity: searchable before snapshot.
    pre_knn = store.vec_knn(emb.embed_query("budget"), k=1)
    assert pre_knn and pre_knn[0][0] == "d-budget"

    # 2. Encrypted snapshot.
    key = generate_escrow_key()
    enc = make_encrypted_snapshot(live, tmp_path / "backup.enc", key)

    # 3. WIPE — simulate the reinstall: delete the store and any WAL sidecars.
    live.unlink()
    for sidecar in (
        live.with_name(live.name + "-wal"),
        live.with_name(live.name + "-shm"),
    ):
        if sidecar.exists():
            sidecar.unlink()
    assert not live.exists(), "store file must be gone after wipe"

    # 4. RESTORE — decrypt the snapshot back to the live path.
    restored_path = restore(enc, live, key)
    assert restored_path == live
    assert live.exists()

    store2 = Store(live, dim=emb.dim)
    # Chunk + entity + cursor all recovered.
    assert store2.get_chunk("d-budget") is not None
    assert store2.get_entity("taryn-hamilton") is not None
    assert store2.get_cursor("gmail") == SNAPSHOT_CURSOR
    # vec + fts searchability recovered.
    knn = store2.vec_knn(emb.embed_query("budget"), k=1)
    assert knn and knn[0][0] == "d-budget"
    fts = store2.fts_search("budget", k=2)
    assert any(doc_id == "d-budget" for doc_id, _ in fts)

    # 5. DELTA-SYNC — catch the gap. The fake Gmail service, reading the
    # RESTORED cursor "1000", returns ONE new message in a history page whose
    # historyId is "1042" (after the snapshot point). run_sync_cycle reuses the
    # real sync path (sync_gmail + index_pending).
    new_msg = _plain_msg(
        "m-new",
        "Post-snapshot roster",
        "ops@example.com",
        "the volunteer roster updated after the backup was taken",
    )
    pages = [_gmail_page(["m-new"], history_id="1042")]
    fake_gmail = FakeGmailService(
        profile_hid="9999",  # bootstrap value; delta path must NOT use it
        pages=pages,
        messages={"m-new": new_msg},
        # Belt-and-braces: the delta page only returns the new message when the
        # restored cursor "1000" is passed; a bootstrap/None cursor yields empty.
        expected_start_history_id=SNAPSHOT_CURSOR,
    )

    res = run_sync_cycle(store2, emb, gmail_service=fake_gmail)

    # Delta-sync must have resumed from the RESTORED cursor, not bootstrapped.
    # history.list was called with startHistoryId == "1000" (the restored
    # cursor); a regression that bootstrapped (getProfile -> "9999") or passed
    # None would fail here.
    assert fake_gmail.recorded_start_history_ids == [SNAPSHOT_CURSOR], (
        "delta-sync must call history.list with the restored cursor, got "
        f"{fake_gmail.recorded_start_history_ids}"
    )
    # The bootstrap path was NOT taken — a correct delta-sync from a non-None
    # cursor never calls getProfile.
    assert fake_gmail.get_profile_calls == 0, (
        "getProfile must not be called on a delta-sync from a restored cursor; "
        f"called {fake_gmail.get_profile_calls} time(s)"
    )

    assert res["gmail"] == 1, f"expected 1 new message synced, got {res['gmail']}"
    assert res["embedded"] >= 1, "the new message's chunk should have been embedded"

    # The new message's chunk is now indexed and searchable.
    new_chunk = store2.get_chunk("gmail-m-new-body-0")
    assert new_chunk is not None, "post-snapshot message chunk missing"
    roster_fts = store2.fts_search("roster", k=5)
    assert any(doc_id == "gmail-m-new-body-0" for doc_id, _ in roster_fts), (
        "post-snapshot message not searchable after delta-sync"
    )

    # Cursor advanced past the restored value — delta-sync resumed from the
    # restored cursor and moved forward (not a re-bootstrap to "9999").
    advanced = store2.get_cursor("gmail")
    assert advanced == "1042", f"cursor should advance to the delta page's historyId, got {advanced}"
    assert advanced != SNAPSHOT_CURSOR, "cursor must move past the snapshot point"


# --- find_latest_snapshot -----------------------------------------------------

class _FLReq:
    def __init__(self, result):
        self._r = result

    def execute(self, num_retries=0):
        return self._r


class _FLFiles:
    """Fake files() that returns folder_response on the first list (folder
    lookup) and files_response on the second (file listing). Records list
    kwargs for assertion."""

    def __init__(self, folder_response, files_response=None):
        self._folder_response = folder_response
        self._files_response = files_response
        self.list_calls = []

    def list(self, **kw):
        self.list_calls.append(kw)
        # First call is the folder lookup (q references the FOLDER_MIME).
        if len(self.list_calls) == 1:
            return _FLReq(self._folder_response)
        return _FLReq(self._files_response or {"files": []})


class _FLService:
    def __init__(self, files):
        self._files = files

    def files(self):
        return self._files


def test_find_latest_snapshot_returns_newest_by_created_time():
    folder_resp = {"files": [{"id": "folder-sam", "name": "sam"}]}
    files_resp = {
        "files": [
            {"id": "snap-old", "name": "a.enc", "createdTime": "2026-05-01T10:00:00Z"},
            {"id": "snap-new", "name": "b.enc", "createdTime": "2026-05-30T10:00:00Z"},
        ]
    }
    files = _FLFiles(folder_resp, files_resp)
    service = _FLService(files)

    result = find_latest_snapshot(service, "drive-XYZ", "sam")
    assert result == "snap-new"

    # Both list calls must set the Shared Drive params.
    for call in files.list_calls:
        assert call["supportsAllDrives"] is True
        assert call["includeItemsFromAllDrives"] is True
        assert call["corpora"] == "drive"
        assert call["driveId"] == "drive-XYZ"


def test_find_latest_snapshot_returns_none_when_folder_absent():
    folder_resp = {"files": []}  # per-user folder doesn't exist
    files = _FLFiles(folder_resp)
    service = _FLService(files)

    assert find_latest_snapshot(service, "drive-XYZ", "sam") is None
    # Only the folder lookup ran — no second listing.
    assert len(files.list_calls) == 1


def test_find_latest_snapshot_returns_none_when_folder_empty():
    folder_resp = {"files": [{"id": "folder-sam", "name": "sam"}]}
    files_resp = {"files": []}  # folder exists but holds nothing
    files = _FLFiles(folder_resp, files_resp)
    service = _FLService(files)

    assert find_latest_snapshot(service, "drive-XYZ", "sam") is None


def test_find_latest_snapshot_rejects_unsafe_user_id():
    import pytest

    files = _FLFiles({"files": []})
    service = _FLService(files)
    with pytest.raises(ValueError, match="unsafe in a Drive query"):
        find_latest_snapshot(service, "drive-XYZ", "o'brien@example.com")
    # Guard trips before any Drive call.
    assert files.list_calls == []


# --- download_snapshot --------------------------------------------------------

class _DLMediaRequest:
    """Stand-in for the get_media request object."""


class _DLFiles:
    def __init__(self):
        self.get_media_calls = []

    def get_media(self, **kw):
        self.get_media_calls.append(kw)
        return _DLMediaRequest()


class _DLService:
    def __init__(self, files):
        self._files = files

    def files(self):
        return self._files


class _FakeDownloader:
    """Writes known bytes to the file handle over two chunks, proving the
    chunked download loop runs without any real MediaIoBaseDownload import."""

    def __init__(self, fh, request, payload):
        self._fh = fh
        self._payload = payload
        self._idx = 0
        # Split payload into two chunks to exercise the loop.
        self._chunks = [payload[: len(payload) // 2], payload[len(payload) // 2:]]

    def next_chunk(self):
        if self._idx < len(self._chunks):
            self._fh.write(self._chunks[self._idx])
            self._idx += 1
        done = self._idx >= len(self._chunks)
        return (None, done)


def test_download_snapshot_writes_bytes_via_injected_factory(tmp_path):
    from pathlib import Path

    files = _DLFiles()
    service = _DLService(files)
    payload = b"encrypted-snapshot-bytes \x00\x01\xff and more"

    captured = {}

    def factory(fh, request):
        captured["request"] = request
        return _FakeDownloader(fh, request, payload)

    dest = tmp_path / "nested" / "snap.enc"
    result = download_snapshot(
        service, "file-abc", dest, downloader_factory=factory
    )

    assert isinstance(result, Path)
    assert result == dest
    assert dest.read_bytes() == payload

    # get_media called with the file id and supportsAllDrives=True.
    assert len(files.get_media_calls) == 1
    call = files.get_media_calls[0]
    assert call["fileId"] == "file-abc"
    assert call["supportsAllDrives"] is True
    # The injected factory received the get_media request object.
    assert isinstance(captured["request"], _DLMediaRequest)


# --- snapshot retention (prune) ---------------------------------------------

class _FakeDelete:
    def __init__(self, deleted, file_id, executes=None):
        self.deleted = deleted
        self.file_id = file_id
        self.executes = executes if executes is not None else []

    def execute(self, num_retries=0):
        self.executes.append(num_retries)
        self.deleted.append(self.file_id)
        return {}


class FakeFilesPrune:
    """Fake Drive files() for prune_snapshots: a folder lookup, a file list
    (newest-first sortable createdTime), and delete() recording ids."""
    FOLDER_MIME = "application/vnd.google-apps.folder"

    def __init__(self, snapshot_files):
        self._snaps = snapshot_files
        self.deleted = []
        self.list_retries = []
        self.delete_retries = []

    def list(self, **kw):
        q = kw.get("q", "")
        if self.FOLDER_MIME in q:
            return _FakeList([], {"files": [{"id": "folder-1"}]}, executes=self.list_retries)
        return _FakeList([], {"files": list(self._snaps)}, executes=self.list_retries)

    def delete(self, *, fileId, supportsAllDrives=False):
        return _FakeDelete(self.deleted, fileId, self.delete_retries)


def _snap(i, day):
    return {"id": f"snap-{i}", "name": "snapshot.enc",
            "createdTime": f"2026-06-{day:02d}T00:00:00Z", "modifiedTime": ""}


def test_prune_keeps_newest_n_deletes_rest():
    from mcpbrain.backup import prune_snapshots
    # 5 snapshots, days 1..5; keep newest 3 → delete days 1 and 2 (snap-1, snap-2)
    files = [_snap(i, i) for i in range(1, 6)]
    svc = FakeService(FakeFilesPrune(files))
    deleted = prune_snapshots(svc, "drive-X", "sam", keep=3)
    assert deleted == 2
    assert set(svc._files.deleted) == {"snap-1", "snap-2"}


def test_prune_noop_when_within_keep():
    from mcpbrain.backup import prune_snapshots
    files = [_snap(i, i) for i in range(1, 4)]  # 3 files
    svc = FakeService(FakeFilesPrune(files))
    assert prune_snapshots(svc, "drive-X", "sam", keep=7) == 0
    assert svc._files.deleted == []


def test_prune_keep_zero_is_noop():
    from mcpbrain.backup import prune_snapshots
    files = [_snap(i, i) for i in range(1, 4)]
    svc = FakeService(FakeFilesPrune(files))
    assert prune_snapshots(svc, "drive-X", "sam", keep=0) == 0
    assert svc._files.deleted == []


def test_upload_snapshot_folder_lookup_and_create_pass_num_retries(tmp_path):
    from mcpbrain.backup import upload_snapshot, _NUM_RETRIES, _MEDIA_NUM_RETRIES

    src = tmp_path / "snap.enc"
    src.write_bytes(b"ciphertext")
    files = FakeFiles(list_response={"files": []})
    service = FakeService(files)

    upload_snapshot(service, src, "drive-XYZ", "sam", media_factory=_fake_media)

    # Folder lookup list uses _NUM_RETRIES.
    assert files.list_retries == [_NUM_RETRIES]
    # Folder create uses _NUM_RETRIES; media file upload uses _MEDIA_NUM_RETRIES.
    assert files.execute_retries == [_NUM_RETRIES, _MEDIA_NUM_RETRIES]


def test_prune_snapshots_list_and_delete_pass_num_retries():
    from mcpbrain.backup import prune_snapshots, _NUM_RETRIES

    files = [_snap(i, i) for i in range(1, 6)]
    fake_files = FakeFilesPrune(files)
    svc = FakeService(fake_files)

    prune_snapshots(svc, "drive-X", "sam", keep=3)

    # Folder lookup list and file list both use _NUM_RETRIES.
    assert fake_files.list_retries == [_NUM_RETRIES, _NUM_RETRIES]
    # Delete calls also use _NUM_RETRIES.
    assert fake_files.delete_retries == [_NUM_RETRIES, _NUM_RETRIES]


# --- Task 2: the artifact carries an intact vector index -----------------------


def test_snapshot_preserves_the_vector_index_across_the_rebuild(tmp_path):
    """VACUUM may renumber rowids of tables without an INTEGER PRIMARY KEY, and
    vec_chunks_vector_chunks00 is declared `rowid PRIMARY KEY` untyped. If it
    renumbered while vec_chunks_chunks.chunk_id (INTEGER PRIMARY KEY
    AUTOINCREMENT) did not, KNN would silently return the wrong chunks.

    NULL-INSTRUMENT GUARD: gaps must exist in that table before the snapshot.
    With contiguous rowids a renumbering VACUUM is the identity map and this
    test could not go red — the first design probe passed for exactly that
    reason and proved nothing.
    """
    import struct

    from mcpbrain.store import _open_db

    dim = 8
    store = Store(tmp_path / "live.sqlite3", dim=dim)
    store.init()

    def vec(i):
        return [((i * 7919 + j * 104729) % 1000) / 1000.0 for j in range(dim)]

    def ser(v):
        return struct.pack(f"{len(v)}f", *v)

    db = _open_db(store.path, read_only=False)
    db.execute("BEGIN")
    for i in range(6000):          # > 5 vec0 chunks at the 1024 default
        db.execute("INSERT INTO chunks(rowid,doc_id,text,content_hash,metadata,embedded) "
                   "VALUES(?,?,?,?,'{}',1)", (i + 1, f"doc-{i:05d}", f"text {i}", f"h{i}"))
        db.execute("INSERT INTO vec_chunks(rowid,embedding) VALUES(?,?)", (i + 1, ser(vec(i))))
    db.execute("COMMIT")
    db.execute("BEGIN")            # free whole vector chunks -> rowid gaps
    db.execute("DELETE FROM vec_chunks WHERE rowid BETWEEN 1100 AND 4200")
    db.execute("DELETE FROM chunks WHERE rowid BETWEEN 1100 AND 4200")
    db.execute("COMMIT")

    cnt, lo, hi = db.execute(
        "SELECT count(*), min(rowid), max(rowid) FROM vec_chunks_vector_chunks00").fetchone()
    assert (hi - lo + 1) - cnt > 0, (
        "NULL INSTRUMENT: no rowid gaps, a renumbering VACUUM would be the "
        "identity map and this test could not fail")

    def knn(path, q, k=10):
        d = _open_db(path, read_only=False)
        try:
            return [(r["doc_id"], round(r["distance"], 6)) for r in d.execute(
                "SELECT c.doc_id, v.distance FROM vec_chunks v "
                "JOIN chunks c ON c.rowid = v.rowid "
                "WHERE v.embedding MATCH ? AND k = ? ORDER BY v.distance", (ser(q), k))]
        finally:
            d.close()

    queries = [vec(i * 137 + 3) for i in range(4)]
    before = [knn(store.path, q) for q in queries]
    fts_before = db.execute(
        "SELECT count(*) FROM fts_chunks WHERE fts_chunks MATCH 'text'").fetchone()[0]
    db.close()

    out = snapshot(store.path, tmp_path / "snap.sqlite3")

    assert [knn(out, q) for q in queries] == before, "KNN differs after the rebuild"
    d = _open_db(out, read_only=False)
    try:
        assert d.execute("SELECT count(*) FROM fts_chunks "
                         "WHERE fts_chunks MATCH 'text'").fetchone()[0] == fts_before
    finally:
        d.close()


def test_snapshot_rejects_an_artifact_whose_vectors_do_not_resolve(tmp_path, monkeypatch):
    """The runtime guard: snapshot() must not hand back an artifact whose
    vector index does not resolve. Simulated by corrupting the artifact between
    the copy and the check."""
    import pytest

    import mcpbrain.backup as backup_mod
    from mcpbrain.store import _open_db

    store = Store(tmp_path / "live.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("d1", "hello", "h1", {})
    with store._connect(write=True) as db:
        rid = db.execute("SELECT rowid FROM chunks WHERE doc_id='d1'").fetchone()["rowid"]
    store.write_embedding(rid, [0.1, 0.2, 0.3, 0.4])

    real_verify = backup_mod._verify_artifact

    def corrupt_then_verify(out_path):
        d = _open_db(out_path, read_only=False)
        try:
            d.execute("DELETE FROM vec_chunks")   # chunks still claim embedded=1
            d.commit()
        finally:
            d.close()
        return real_verify(out_path)

    monkeypatch.setattr(backup_mod, "_verify_artifact", corrupt_then_verify)

    out = tmp_path / "snap.sqlite3"
    with pytest.raises(RuntimeError, match="vector"):
        snapshot(store.path, out)
    assert not out.exists(), "a failed artifact must not be left behind"


def test_verify_artifact_no_ops_when_the_store_has_no_vec0_table_at_all(tmp_path):
    """A store can have embedded=1 chunks with NO vec0 table at all -- e.g. a
    pre-vec0 schema, or one where the virtual table was dropped/never created.
    That is exactly the "already-broken store" case the docstring calls out
    (bin/repair.py must still be able to snapshot it): the probe has nothing
    to say and must no-op, not raise.

    Distinct from test_snapshot_rejects_an_artifact_whose_vectors_do_not_resolve,
    where vec_chunks EXISTS but a specific row's vector is missing -- that is a
    real hazard and must keep raising. Here the table itself never existed.
    """
    from mcpbrain.backup import _verify_artifact
    from mcpbrain.store import _open_db

    store = Store(tmp_path / "live.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("d1", "hello", "h1", {})

    db = _open_db(store.path, read_only=False)
    try:
        db.execute("UPDATE chunks SET embedded=1 WHERE doc_id='d1'")
        db.execute("DROP TABLE vec_chunks")
        db.commit()
    finally:
        db.close()

    _verify_artifact(store.path)   # must not raise

    out = snapshot(store.path, tmp_path / "snap.sqlite3")
    assert out.exists()


def test_verify_artifact_no_ops_when_the_chunks_table_does_not_exist(tmp_path):
    """A store with no chunks table at all (e.g. bin/repair.py snapshotting an
    already-broken store, per the function's own docstring) has nothing to
    verify and must no-op, not raise -- this is the legitimate "table absent"
    case the sqlite_master check is meant to catch.
    """
    from mcpbrain.backup import _verify_artifact
    from mcpbrain.store import _open_db

    store = Store(tmp_path / "live.sqlite3", dim=4)
    store.init()

    db = _open_db(store.path, read_only=False)
    try:
        db.execute("DROP TABLE chunks")
        db.commit()
    finally:
        db.close()

    _verify_artifact(store.path)   # must not raise


def test_verify_artifact_raises_on_genuine_chunks_corruption(tmp_path, monkeypatch):
    """The chunks-table check must only swallow "table doesn't exist" -- a
    genuinely corrupt table (e.g. sqlite3.DatabaseError("database disk image is
    malformed")) must propagate, not be silently treated as "nothing to
    verify". Before the fix, the broad `except sqlite3.DatabaseError: return`
    around the chunks query would have swallowed this too, letting a corrupt
    rebuild silently pass verification.
    """
    import sqlite3

    import pytest

    import mcpbrain.backup as backup_mod

    store = Store(tmp_path / "live.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("d1", "hello", "h1", {})

    real_open_db = backup_mod._open_db

    def spy_open_db(*args, **kwargs):
        conn = real_open_db(*args, **kwargs)

        class _Proxy:
            def __getattr__(self, name):
                return getattr(conn, name)

            def execute(self, sql, *a, **k):
                if "FROM chunks WHERE embedded" in sql:
                    raise sqlite3.DatabaseError("database disk image is malformed")
                return conn.execute(sql, *a, **k)

        return _Proxy()

    monkeypatch.setattr(backup_mod, "_open_db", spy_open_db)

    with pytest.raises(sqlite3.DatabaseError, match="malformed"):
        backup_mod._verify_artifact(store.path)


def test_free_space_preflight_sizes_from_live_pages_not_file_size(tmp_path, monkeypatch):
    """After the enrich_payloads re-key the store file stays large while most
    of it is freelist. VACUUM INTO's output tracks LIVE pages, so an estimate
    based on stat().st_size would keep refusing a backup that now fits."""
    import mcpbrain.backup as backup_mod

    store = Store(tmp_path / "live.sqlite3", dim=4)
    store.init()
    for i in range(4000):
        store.upsert_chunk(f"d-{i}", "x" * 2000, f"h{i}", {})
    store.delete_chunks([f"d-{i}" for i in range(3600)])   # big freelist, no VACUUM

    file_bytes = Path(store.path).stat().st_size
    live_bytes = backup_mod._live_bytes(store.path)
    assert live_bytes < file_bytes * 0.7, (
        f"NULL INSTRUMENT: freelist too small to tell the two apart "
        f"(live={live_bytes} file={file_bytes})")

    class _Usage:
        free = int(live_bytes * 2.0)       # room for live data, not for the file

    monkeypatch.setattr(backup_mod.shutil, "disk_usage", lambda p: _Usage)
    backup_mod._require_free_space(tmp_path, tmp_path / "out.enc", store.path)


def test_live_bytes_does_not_create_file_for_missing_store(tmp_path):
    """_live_bytes must not call sqlite3.connect on a path that doesn't exist,
    because sqlite3.connect auto-creates an empty file immediately -- and then
    the PRAGMAs succeed against that empty file (page_count=0, freelist=0),
    returning 0, which is wrong and unsafe. It must check path existence first,
    so missing paths raise FileNotFoundError (via stat()) rather than silently
    returning 0 or creating a stray file."""
    import mcpbrain.backup as backup_mod

    missing_path = tmp_path / "nonexistent.sqlite3"
    assert not missing_path.exists(), "sanity: path should not exist yet"

    # Call _live_bytes on a missing path and expect FileNotFoundError
    try:
        result = backup_mod._live_bytes(str(missing_path))
        # If we get here without raising, the result must not be 0 (which would
        # make _require_free_space trivially pass). This would mean _open_db
        # succeeded and returned live pages, which shouldn't happen for a
        # nonexistent path.
        assert result > 0, (
            f"_live_bytes returned {result} for a missing store; must raise "
            f"FileNotFoundError or return a safe non-zero value")
    except FileNotFoundError:
        # This is the expected safe behavior: stat() raises FileNotFoundError
        # for a path that doesn't exist. This is louder and safer than
        # silently returning 0.
        pass
    finally:
        # Must not create a stray file as a side effect
        assert not missing_path.exists(), (
            f"_live_bytes created a stray file at {missing_path}; "
            "sqlite3.connect must not auto-create on a missing path")
