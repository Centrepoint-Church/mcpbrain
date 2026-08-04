"""Tests for mcpbrain.backup — store snapshot with mandatory WAL checkpoint.

The store runs journal_mode=WAL. Committed writes can still live in the -wal
sidecar, so a bare copy of the main .sqlite3 file can MISS the latest writes.
snapshot() must run PRAGMA wal_checkpoint(TRUNCATE) to fold those frames into
the main DB file BEFORE copying. The latest-writes roundtrip test below is the
behavioural proof: a freshly-committed row must survive the snapshot.
"""

import sqlite3

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


def test_checkpoint_runs_before_copy(tmp_path, monkeypatch):
    """Behavioural-adjacent guard: assert wal_checkpoint(TRUNCATE) is executed
    and that it runs BEFORE shutil.copy2. Ordering matters — checkpoint after
    copy would not fold the WAL into the copied file."""
    import mcpbrain.backup as backup_mod

    store = Store(tmp_path / "live.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("d1", "hello", "h1", {})

    events = []

    real_open_db = backup_mod._open_db

    def spy_open_db(*args, **kwargs):
        # sqlite3.Connection is immutable, so wrap the instance's execute via a
        # thin proxy that records wal_checkpoint calls and delegates everything
        # else to the real connection.
        conn = real_open_db(*args, **kwargs)
        real_execute = conn.execute

        def recording_execute(sql, *a, **k):
            if "wal_checkpoint" in sql.lower():
                events.append("checkpoint")
            return real_execute(sql, *a, **k)

        # Bind onto a lightweight proxy so we don't mutate the immutable type.
        class _Proxy:
            def __getattr__(self, name):
                return getattr(conn, name)

            def execute(self, sql, *a, **k):
                return recording_execute(sql, *a, **k)

        return _Proxy()

    real_copy2 = backup_mod.shutil.copy2

    def spy_copy2(*args, **kwargs):
        events.append("copy")
        return real_copy2(*args, **kwargs)

    monkeypatch.setattr(backup_mod, "_open_db", spy_open_db)
    monkeypatch.setattr(backup_mod.shutil, "copy2", spy_copy2)

    snapshot(store.path, tmp_path / "snap.sqlite3")

    assert "checkpoint" in events, "wal_checkpoint(TRUNCATE) was never executed"
    assert "copy" in events, "shutil.copy2 was never called"
    assert events.index("checkpoint") < events.index("copy"), (
        "checkpoint must run BEFORE the file copy"
    )


def test_snapshot_raises_on_busy_checkpoint_no_partial_file(tmp_path, monkeypatch):
    """A busy checkpoint (busy != 0) must abort with RuntimeError and write NO
    file — a degraded snapshot must never look successful to 5.2/5.3 callers."""
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
                if "wal_checkpoint" in sql.lower():
                    # Force a busy result so the checkpoint reports it could not
                    # fully fold the WAL. (busy, log, checkpointed).
                    class _Cur:
                        def fetchone(self_inner):
                            return (1, -1, -1)

                    return _Cur()
                return conn.execute(sql, *a, **k)

        return _Proxy()

    monkeypatch.setattr(backup_mod, "_open_db", spy_open_db)

    out = tmp_path / "snap.sqlite3"
    with pytest.raises(RuntimeError, match="busy=1"):
        snapshot(store.path, out)

    assert not out.exists(), "no partial artifact may be written on a busy checkpoint"


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


def test_spliced_frames_from_two_archives_are_rejected(tmp_path):
    """A chimera built from two archives under the SAME key must be rejected.

    The daily cadence reuses one escrow key, so "two archives under one key" is
    the steady state, and the escrow key lives in an all-members-readable fleet
    folder. With only (index, is_final) inside the authenticated plaintext,
    frames 0..k of archive A followed by frames k+1..final of archive B form a
    perfectly ordered, final-flagged run — so splicing two stores together
    needed NO key at all. Each frame must also bind the archive it belongs to.
    """
    from cryptography.fernet import InvalidToken

    import pytest

    key = generate_escrow_key()
    a_src = tmp_path / "a.bin"
    b_src = tmp_path / "b.bin"
    a_src.write_bytes(b"A" * 20_000)
    b_src.write_bytes(b"B" * 20_000)
    a = encrypt_file(a_src, tmp_path / "a.enc", key, chunk_size=4096)
    b = encrypt_file(b_src, tmp_path / "b.enc", key, chunk_size=4096)

    a_frames, magic = _split_frames(a.read_bytes())
    b_frames, _ = _split_frames(b.read_bytes())
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
    """Split a framed archive into (list_of_raw_frames, header)."""
    import struct

    from mcpbrain.backup import _ARCHIVE_MAGIC

    assert blob.startswith(_ARCHIVE_MAGIC)
    pos = len(_ARCHIVE_MAGIC)
    frames = []
    while pos < len(blob):
        (n,) = struct.unpack(">I", blob[pos:pos + 4])
        frames.append(blob[pos:pos + 4 + n])
        pos += 4 + n
    return frames, _ARCHIVE_MAGIC


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
    def __init__(self, calls, canned):
        self.calls = calls
        self.canned = canned

    def execute(self):
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
        # num_retries passed to each create(...).execute(), in call order.
        self.execute_retries = []

    def list(self, **kw):
        self.list_calls.append(kw)
        return _FakeList(self.list_calls, self.list_response)

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

    def execute(self):
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
    def __init__(self, deleted, file_id):
        self.deleted = deleted
        self.file_id = file_id

    def execute(self, num_retries=0):
        self.deleted.append(self.file_id)
        return {}


class FakeFilesPrune:
    """Fake Drive files() for prune_snapshots: a folder lookup, a file list
    (newest-first sortable createdTime), and delete() recording ids."""
    FOLDER_MIME = "application/vnd.google-apps.folder"

    def __init__(self, snapshot_files):
        self._snaps = snapshot_files
        self.deleted = []

    def list(self, **kw):
        q = kw.get("q", "")
        if self.FOLDER_MIME in q:
            return _FakeList([], {"files": [{"id": "folder-1"}]})
        return _FakeList([], {"files": list(self._snaps)})

    def delete(self, *, fileId, supportsAllDrives=False):
        return _FakeDelete(self.deleted, fileId)


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
