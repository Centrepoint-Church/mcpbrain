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


class FakeBC:
    """BackupConfig-like stand-in carrying the four attributes the restore path
    reads: drive_service, shared_drive_id, user_id, key, plus out_path."""

    def __init__(self):
        self.drive_service = "DRIVE"
        self.shared_drive_id = "D"
        self.user_id = "sam"
        self.key = b"k" * 32
        self.out_path = None


def test_empty_store_with_backup_restores(monkeypatch, tmp_path):
    s = Store(str(tmp_path / "b.sqlite3"), dim=384)
    s.init()  # empty
    called = {}
    monkeypatch.setattr(dmod, "_backup_from_config", lambda home: (FakeBC(), 3600))
    monkeypatch.setattr(dmod.backup, "find_latest_snapshot", lambda svc, sd, uid: "snap-id")
    monkeypatch.setattr(
        dmod.backup, "download_and_restore",
        lambda bc, store, snap: called.setdefault("restored", snap),
    )
    assert dmod.maybe_restore_on_first_run(s, str(tmp_path)) is True
    assert called.get("restored") == "snap-id"


def test_non_empty_store_skips_restore(monkeypatch, tmp_path):
    s = Store(str(tmp_path / "b.sqlite3"), dim=384)
    s.init()
    s.upsert_chunk(doc_id="d", text="x", content_hash="h", metadata={})
    assert s.chunk_count() == 1
    monkeypatch.setattr(dmod, "_backup_from_config", lambda home: (FakeBC(), 3600))
    monkeypatch.setattr(dmod.backup, "find_latest_snapshot", lambda *a: "snap-id")
    flag = {"restored": False}
    monkeypatch.setattr(
        dmod.backup, "download_and_restore",
        lambda *a: flag.__setitem__("restored", True),
    )
    assert dmod.maybe_restore_on_first_run(s, str(tmp_path)) is False
    assert flag["restored"] is False


def test_no_backup_configured_skips(monkeypatch, tmp_path):
    s = Store(str(tmp_path / "b.sqlite3"), dim=384)
    s.init()  # empty
    monkeypatch.setattr(dmod, "_backup_from_config", lambda home: (None, None))
    looked = {"called": False}

    def _should_not_run(*a):
        looked["called"] = True
        return "snap-id"

    monkeypatch.setattr(dmod.backup, "find_latest_snapshot", _should_not_run)
    assert dmod.maybe_restore_on_first_run(s, str(tmp_path)) is False
    assert looked["called"] is False


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
        dmod, "maybe_restore_on_first_run",
        lambda store, home: order.append("restore"),
    )
    monkeypatch.setattr(
        Daemon, "migrate_embed_backend",
        lambda self, backend=dmod.EMBED_BACKEND: order.append("migrate") or 0,
    )

    d = Daemon(s, _FakeEmbedder(), services={}, interval_s=0.01)
    d.stop()  # pre-stop so run() does startup work then exits the loop at once
    d.run()

    assert order == ["restore", "migrate"], (
        "restore must run before migrate so the backend check sees restored data"
    )


def test_download_and_restore_composes_primitives(monkeypatch, tmp_path):
    bc = FakeBC()
    bc.out_path = tmp_path / "snapshot.enc"
    store = Store(str(tmp_path / "live.sqlite3"), dim=384)
    calls = {}

    def fake_download(service, file_id, dest_path, *, downloader_factory=None):
        calls["download"] = (service, file_id, dest_path)
        return dest_path

    def fake_restore(encrypted_path, dest_store_path, key):
        calls["restore"] = (encrypted_path, dest_store_path, key)
        return dest_store_path

    monkeypatch.setattr(bmod, "download_snapshot", fake_download)
    monkeypatch.setattr(bmod, "restore", fake_restore)

    result = bmod.download_and_restore(bc, store, "file-123")

    from pathlib import Path

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

    # restore decrypts FROM that same temp file INTO the live store.
    restore_src, restore_dest, restore_key = calls["restore"]
    assert Path(restore_src) == download_dest
    assert restore_dest == store.path
    assert restore_key == bc.key

    assert result == store.path

    # Temp file is cleaned up (the mocked download never created it, so the
    # finally unlink hits OSError and is swallowed — either way nothing remains).
    leftovers = list(store_parent.glob(".restore-*.enc"))
    assert leftovers == []
