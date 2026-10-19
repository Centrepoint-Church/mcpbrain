"""Restore-on-first-run from the latest encrypted Drive snapshot (Task 2.5).

When the daemon starts with an EMPTY store but a configured backup, it pulls the
latest encrypted snapshot from Drive, decrypts+restores it as the live store,
then lets the normal loop delta-sync. These tests cover the daemon trigger
(maybe_restore_on_first_run) and the backup composition primitive
(download_and_restore), written against the REAL shipped signatures:
find_latest_snapshot(service, shared_drive_id, user_id).
"""

from mcpbrain.store import Store
from mcpbrain import daemon as dmod
from mcpbrain import backup as bmod
from mcpbrain import backup_restore


class FakeBC:
    """BackupConfig-like stand-in carrying the four attributes the restore path
    reads: drive_service, shared_drive_id, user_id, key, plus out_path."""

    def __init__(self):
        self.drive_service = "DRIVE"
        self.shared_drive_id = "D"
        self.user_id = "sam"
        self.key = b"k" * 32
        self.out_path = None


def _result(file_id="snap-id"):
    cand = backup_restore.Candidate("manifest", 1.0, file_id, "CF", "chain-x seq 1")
    return backup_restore.RestoreResult(cand, [], None)


def test_empty_store_with_backup_restores(monkeypatch, tmp_path):
    s = Store(str(tmp_path / "b.sqlite3"), dim=384)
    s.init()  # empty
    called = {}
    monkeypatch.setattr(dmod, "_backup_from_config", lambda home: (FakeBC(), 3600))

    def fake(home, svc, key, **kw):
        called.update(svc=svc, key=key, **kw)
        return _result()
    monkeypatch.setattr(dmod.backup_restore, "restore_latest", fake)
    assert dmod.maybe_restore_on_first_run(s, str(tmp_path)) is True
    assert called["svc"] == "DRIVE" and called["key"] == FakeBC().key
    assert str(called["dest_store"]) == str(s.path)
    assert called["records_dir"] and called["config_path"]   # the whole bundle


def test_non_empty_store_skips_restore(monkeypatch, tmp_path):
    s = Store(str(tmp_path / "b.sqlite3"), dim=384)
    s.init()
    s.upsert_chunk(doc_id="d", text="x", content_hash="h", metadata={})
    assert s.chunk_count() == 1
    monkeypatch.setattr(dmod, "_backup_from_config", lambda home: (FakeBC(), 3600))
    flag = {"restored": False}
    monkeypatch.setattr(
        dmod.backup_restore, "restore_latest",
        lambda *a, **k: flag.__setitem__("restored", True),
    )
    assert dmod.maybe_restore_on_first_run(s, str(tmp_path)) is False
    assert flag["restored"] is False


def test_no_backup_configured_skips(monkeypatch, tmp_path):
    s = Store(str(tmp_path / "b.sqlite3"), dim=384)
    s.init()  # empty
    monkeypatch.setattr(dmod, "_backup_from_config", lambda home: (None, None))
    looked = {"called": False}

    def _should_not_run(*a, **k):
        looked["called"] = True
        return _result()

    monkeypatch.setattr(dmod.backup_restore, "restore_latest", _should_not_run)
    assert dmod.maybe_restore_on_first_run(s, str(tmp_path)) is False
    assert looked["called"] is False


def test_no_restore_point_is_no_snapshot_but_all_broken_raises(monkeypatch, tmp_path):
    import pytest
    s = Store(str(tmp_path / "b.sqlite3"), dim=384)
    s.init()
    monkeypatch.setattr(dmod, "_backup_from_config", lambda home: (FakeBC(), 3600))

    def none_found(*a, **k):
        raise backup_restore.NoRestorePoint("none found")
    monkeypatch.setattr(dmod.backup_restore, "restore_latest", none_found)
    assert dmod._restore_on_first_run_classified(s, str(tmp_path)) == ("no_snapshot", None)

    cand = backup_restore.Candidate("legacy", 1.0, "L", "F", "x.enc")

    def all_broken(*a, **k):
        raise backup_restore.NoRestorePoint("all broken", [(cand, "integrity_check failed")])
    monkeypatch.setattr(dmod.backup_restore, "restore_latest", all_broken)
    with pytest.raises(backup_restore.NoRestorePoint):
        dmod._restore_on_first_run_classified(s, str(tmp_path))


def test_startup_restore_restores_records_and_config(monkeypatch, tmp_path):
    """The start-up empty-store restore used to place the STORE only; now it
    goes through restore_latest and restores the records repo + config.json
    from the bundle too (spec §5). Real restore_latest, real bundle; only Drive
    discovery and download are faked."""
    import shutil
    import sqlite3
    import time
    from pathlib import Path

    from mcpbrain import config

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("MCPBRAIN_HOME", str(home))
    # The snapshot source: a store with content, a records repo, a config.
    src = tmp_path / "src"
    src.mkdir()
    snap_store = Store(str(src / "brain.sqlite3"), dim=384)
    snap_store.init()
    snap_store.upsert_chunk(doc_id="d1", text="x", content_hash="h1", metadata={})
    sqlite3.connect(str(snap_store.path)).execute("PRAGMA wal_checkpoint(TRUNCATE)").close()
    (src / "records").mkdir()
    (src / "records" / "voice.md").write_text("restored voice")
    (src / "config.json").write_text('{"owner_email": "sam@example.org", "marker": "restored"}')
    key = bmod.generate_escrow_key()
    art = tmp_path / "snap.enc"
    bmod.make_encrypted_snapshot(snap_store.path, art, key, records_dir=src / "records",
                                 config_path=src / "config.json")

    config.write_config(str(home), {"owner_email": "sam@example.org"})
    bc = FakeBC()
    bc.key = key
    monkeypatch.setattr(dmod, "_backup_from_config", lambda h: (bc, 3600))
    cand = backup_restore.Candidate("legacy", time.time() - 60, "L1", "LF", "snap.enc")
    monkeypatch.setattr(backup_restore, "_discover", lambda h, svc: ([], [cand]))
    monkeypatch.setattr(backup_restore, "_download",
                        lambda svc, fid, dest, **kw: (Path(dest).parent.mkdir(
                            parents=True, exist_ok=True), shutil.copyfile(art, dest)))

    live = Store(str(home / "brain.sqlite3"), dim=384)
    live.init()
    assert dmod._restore_on_first_run_classified(live, str(home)) == ("restored", None)
    assert live.chunk_count() == 1
    assert (Path(config.records_dir(str(home))) / "voice.md").read_text() == "restored voice"
    assert config.read_config(str(home)).get("marker") == "restored"


def test_records_failure_after_swap_is_a_partial_restore(monkeypatch, tmp_path):
    """R24: the store was swapped in, then placing records/config failed. The
    store has content now, so the start-up path reports it (partial) instead of
    raising into a failed-and-retry the empty-store rule would then block."""
    from pathlib import Path
    s = Store(str(tmp_path / "b.sqlite3"), dim=384)
    s.init()
    monkeypatch.setattr(dmod, "_backup_from_config", lambda home: (FakeBC(), 3600))
    monkeypatch.setattr(dmod.backup, "swap_in_store", lambda stage, dest, gate=None: None)

    def swap_then_fail(home, svc, key, **kw):
        stage = Path(str(kw["dest_store"]) + ".stage")
        kw["swap"](stage, kw["dest_store"])
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(dmod.backup_restore, "restore_latest", swap_then_fail)
    status, exc = dmod._restore_on_first_run_classified(s, str(tmp_path))
    assert status == "partial"
    assert "No space left" in str(exc)


def test_run_restores_before_migrate(monkeypatch, tmp_path):
    """run() must restore the snapshot BEFORE migrating the embed backend.

    Migrate writes the embed-backend marker into the store; restore overwrites
    the whole store with the snapshot. If migrate ran first, restore would
    clobber the marker with the snapshot's (older) one and the next migrate
    check would force a full re-embed of the restored corpus. Asserting the
    order pins the fix.
    """
    from mcpbrain.daemon import Daemon

    class _FakeEmbedder:
        dim = 384

    s = Store(str(tmp_path / "b.sqlite3"), dim=384)
    s.init()

    order = []
    monkeypatch.setattr(dmod, "config",
                        type("C", (), {"app_dir": staticmethod(lambda: tmp_path)}))
    monkeypatch.setattr(
        dmod, "_restore_on_first_run_classified",
        lambda store, home, **kw: order.append("restore") or ("not_empty", None),
    )
    monkeypatch.setattr(
        Daemon, "migrate_embed_backend",
        lambda self, backend=dmod.EMBED_BACKEND: order.append("migrate") or 0,
    )

    # Inject a tmp-path single-writer lock so run() never contends for the real
    # ~/.../mcpbrain/daemon.lock. The lock guard resolves its default path via the
    # module-level `app_dir` import, which the dmod.config monkeypatch above does
    # NOT intercept — without this injection the test fails with AlreadyRunningError
    # whenever a real daemon is running on the same machine.
    d = Daemon(s, _FakeEmbedder(), services={}, interval_s=0.01,
               lock=dmod.SingleWriterLock(tmp_path / "daemon.lock"))
    d.stop()  # pre-stop so run() does startup work then exits the loop at once
    d.run()

    assert order == ["restore", "migrate"], (
        "restore must run before migrate so the backend check sees restored data"
    )


def test_download_and_restore_composes_primitives(monkeypatch, tmp_path):
    """Download to a temp .enc, decrypt into a STAGING file (store untouched),
    then -- inside the store's restoring gate, after a busy-checked WAL
    checkpoint -- replace the live store with the staging file."""
    from pathlib import Path

    bc = FakeBC()
    bc.out_path = tmp_path / "snapshot.enc"
    store = Store(str(tmp_path / "live.sqlite3"), dim=384)
    calls = {}

    def fake_download(service, file_id, dest_path, *, downloader_factory=None):
        calls["download"] = (service, file_id, dest_path)
        return dest_path

    def fake_restore(encrypted_path, dest_store_path, key):
        calls["restore"] = (encrypted_path, dest_store_path, key)
        Path(dest_store_path).write_bytes(b"restored-store")
        return dest_store_path

    gates = []
    real_gate = store.restoring_gate

    def gate():
        gates.append(1)
        return real_gate()
    monkeypatch.setattr(store, "restoring_gate", gate)
    monkeypatch.setattr(bmod, "download_snapshot", fake_download)
    monkeypatch.setattr(bmod, "restore", fake_restore)

    result = bmod.download_and_restore(bc, store, "file-123")

    store_parent = Path(store.path).parent
    download_service, download_file_id, download_dest = calls["download"]
    download_dest = Path(download_dest)

    # Downloads to a dedicated temp file, NOT the backup-upload artifact path.
    assert download_service == "DRIVE"
    assert download_file_id == "file-123"
    assert download_dest != Path(bc.out_path)
    assert download_dest.parent == store_parent
    assert download_dest.suffix == ".enc"
    assert download_dest.name.startswith(".restore-")

    # restore decrypts FROM that temp file into a staging file, not the store.
    restore_src, restore_dest, restore_key = calls["restore"]
    assert Path(restore_src) == download_dest
    assert Path(restore_dest) != Path(store.path)
    assert Path(restore_dest).parent == store_parent
    assert restore_key == bc.key

    # The staging file was swapped in, under the gate.
    assert gates == [1]
    assert result == Path(store.path)
    assert Path(store.path).read_bytes() == b"restored-store"
    assert not list(store_parent.glob(".restore-*"))

