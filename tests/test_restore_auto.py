"""Post-OAuth auto-restore: after Google sign-in, detect an existing backup in
the org escrow folder (key fetched from Drive automatically) and offer to
restore — no manual key or folder-ID entry.
"""
from pathlib import Path

import pytest

from mcpbrain import config, org_defaults, restore


def _cfg(tmp_path, **extra):
    config.write_config(str(tmp_path), {"owner_email": "j@x.com", **extra})


def test_store_has_content_false_for_missing_and_empty_store(tmp_path):
    # Missing file → empty.
    assert restore.store_has_content(str(tmp_path / "nope.sqlite3")) is False
    # Daemon-initialized but unsynced store (schema only, no chunks) → empty.
    # This is the regression: the file exists and is non-empty (SQLite header +
    # tables) yet has no real content, so auto-restore MUST be eligible.
    from mcpbrain.store import Store
    p = tmp_path / "b.sqlite3"
    s = Store(str(p), dim=384)
    s.init()
    assert p.stat().st_size > 0                          # file is non-empty…
    assert restore.store_has_content(str(p)) is False    # …but has no content


def test_store_has_content_true_when_chunks_present(tmp_path):
    from mcpbrain.store import Store
    p = tmp_path / "b.sqlite3"
    s = Store(str(p), dim=384)
    s.init()
    s.upsert_chunk(doc_id="d", text="x", content_hash="h", metadata={})
    assert restore.store_has_content(str(p)) is True


def test_detect_restorable_true_when_key_and_snapshot_present(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    _cfg(tmp_path, fleet={"escrow_folder_id": "ESC"})
    monkeypatch.setattr(restore, "_download_escrow_key", lambda svc, folder, email: b"KEYBYTES")
    monkeypatch.setattr("mcpbrain.backup.find_latest_in_subfolder",
                        lambda svc, folder, user: "SNAP1")
    out = restore.detect_restorable(str(tmp_path), drive_service=object())
    assert out["available"] is True
    assert out["snapshot_id"] == "SNAP1"
    assert out["escrow_folder_id"] == "ESC"
    assert out["user_email"] == "j@x.com"


def test_detect_falls_back_to_org_default_escrow_folder(tmp_path, monkeypatch):
    # Fresh machine: only owner_email set (no fleet block yet). Must use the
    # baked-in org escrow folder so detection works straight after OAuth.
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    _cfg(tmp_path)
    seen = {}
    monkeypatch.setattr(restore, "_download_escrow_key",
                        lambda svc, folder, email: seen.setdefault("folder", folder) or b"K")
    monkeypatch.setattr("mcpbrain.backup.find_latest_in_subfolder",
                        lambda svc, folder, user: "SNAP")
    out = restore.detect_restorable(str(tmp_path), drive_service=object())
    assert seen["folder"] == org_defaults.ESCROW_FOLDER_ID
    assert out["available"] is True


def test_detect_false_when_no_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    _cfg(tmp_path, fleet={"escrow_folder_id": "ESC"})
    monkeypatch.setattr(restore, "_download_escrow_key", lambda svc, folder, email: b"K")
    monkeypatch.setattr("mcpbrain.backup.find_latest_in_subfolder", lambda svc, folder, user: None)
    assert restore.detect_restorable(str(tmp_path), drive_service=object())["available"] is False


def test_detect_false_when_no_key(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    _cfg(tmp_path, fleet={"escrow_folder_id": "ESC"})
    monkeypatch.setattr(restore, "_download_escrow_key", lambda svc, folder, email: None)
    monkeypatch.setattr("mcpbrain.backup.find_latest_in_subfolder", lambda svc, folder, user: "SNAP")
    assert restore.detect_restorable(str(tmp_path), drive_service=object())["available"] is False


def test_detect_false_when_no_user_email(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    config.write_config(str(tmp_path), {})  # no owner_email yet
    assert restore.detect_restorable(str(tmp_path), drive_service=object())["available"] is False


def test_run_restore_auto_fetches_key_and_restores_bundle(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    _cfg(tmp_path, fleet={"escrow_folder_id": "ESC"})
    monkeypatch.setattr(restore, "_download_escrow_key", lambda svc, folder, email: b"KEYBYTES")
    monkeypatch.setattr("mcpbrain.backup.find_latest_in_subfolder", lambda svc, folder, user: "SNAP1")
    monkeypatch.setattr("mcpbrain.backup.download_snapshot",
                        lambda svc, fid, dest: Path(dest).write_bytes(b"enc") or Path(dest))
    captured = {}
    monkeypatch.setattr("mcpbrain.backup.restore",
                        lambda enc, dest, key, **kw: captured.update(key=key, dest=str(dest), **kw) or Path(dest))

    restored = restore.run_restore_auto(str(tmp_path), drive_service=object())

    assert captured["key"] == b"KEYBYTES"          # key came from Drive, not config
    assert "records_dir" in captured and "config_path" in captured  # full bundle restored
    assert restored.endswith("brain.sqlite3")


def test_run_restore_auto_raises_when_nothing_to_restore(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    _cfg(tmp_path, fleet={"escrow_folder_id": "ESC"})
    monkeypatch.setattr(restore, "_download_escrow_key", lambda svc, folder, email: None)
    monkeypatch.setattr("mcpbrain.backup.find_latest_in_subfolder", lambda svc, folder, user: None)
    with pytest.raises(RuntimeError, match="[Nn]o restorable backup"):
        restore.run_restore_auto(str(tmp_path), drive_service=object())
