import importlib

from tests.test_fleet_storage_drive import FakeDrive
from mcpbrain.fleet_storage import DriveFleetStorage

relocate = importlib.import_module("bin.relocate_ingest_cache")


def _seed_in_drive_cache(drive, drive_id, n):
    """Create <drive_id>/.mcpbrain-cache/ with n artifact files (in-drive layout)."""
    fs = DriveFleetStorage(drive, drive_id, root_is_drive=True)
    for i in range(n):
        fs.put_bytes(f".mcpbrain-cache/FID{i}.h.pf.mbc.gz", b"x")


def _patch_drives(monkeypatch, drives):
    monkeypatch.setattr(relocate, "list_shared_drives", lambda svc: drives)


def test_scan_finds_only_drives_with_cache(monkeypatch):
    drive = FakeDrive()
    _seed_in_drive_cache(drive, "D1", 3)
    # D2 has no cache folder
    _patch_drives(monkeypatch, [{"id": "D1", "name": "Ops"}, {"id": "D2", "name": "HR"}])
    entries = relocate.scan(drive)
    assert len(entries) == 1
    assert entries[0]["drive_id"] == "D1"
    assert entries[0]["drive_name"] == "Ops"
    assert entries[0]["count"] == 3


def test_scan_isolates_count_failure(monkeypatch):
    drive = FakeDrive()
    _seed_in_drive_cache(drive, "D1", 1)
    _seed_in_drive_cache(drive, "D2", 2)
    _patch_drives(monkeypatch, [{"id": "D1", "name": "Ops"}, {"id": "D2", "name": "HR"}])
    real_count = relocate._count_children
    calls = {"n": 0}

    def flaky_count(service, folder_id):
        calls["n"] += 1
        if calls["n"] == 1:                 # first drive's count blows up
            raise RuntimeError("transient 5xx")
        return real_count(service, folder_id)

    monkeypatch.setattr(relocate, "_count_children", flaky_count)
    entries = relocate.scan(drive)
    # D1 aborted in isolation; D2 still reported
    assert [e["drive_id"] for e in entries] == ["D2"]
    assert entries[0]["count"] == 2


def test_delete_legacy_removes_the_folder(monkeypatch):
    drive = FakeDrive()
    _seed_in_drive_cache(drive, "D1", 2)
    _patch_drives(monkeypatch, [{"id": "D1", "name": "Ops"}])
    entries = relocate.scan(drive)
    deleted = relocate.delete_legacy(drive, entries)
    assert deleted == 1
    # folder is gone -> a re-scan finds nothing
    assert relocate.scan(drive) == []


def test_scan_and_delete_handle_duplicate_folders(monkeypatch):
    # Drive allows two same-named folders under one parent (a resolved
    # _ensure_folder race). The cleanup must find and delete BOTH in one pass,
    # and sum their children — the Centrepoint Care case that survived the
    # first-match-only version.
    drive = FakeDrive()

    def _mk(k):
        fid = drive.create(body={"name": ".mcpbrain-cache",
                                 "mimeType": relocate._FOLDER_MIME,
                                 "parents": ["D1"]}).execute()["id"]
        for i in range(k):
            drive.create(body={"name": f"F{i}.mbc.gz", "parents": [fid]}).execute()
        return fid

    _mk(2)
    _mk(3)
    _patch_drives(monkeypatch, [{"id": "D1", "name": "Ops"}])

    entries = relocate.scan(drive)
    assert len(entries) == 1
    assert len(entries[0]["folder_ids"]) == 2          # both duplicates found
    assert entries[0]["count"] == 5                    # 2 + 3 summed across both

    deleted = relocate.delete_legacy(drive, entries)
    assert deleted == 2                                 # both folders removed
    assert relocate.scan(drive) == []                  # drive fully clean in one pass


def test_dry_run_does_not_delete(monkeypatch, capsys):
    drive = FakeDrive()
    _seed_in_drive_cache(drive, "D1", 1)
    _patch_drives(monkeypatch, [{"id": "D1", "name": "Ops"}])
    monkeypatch.setattr(relocate, "_drive_service", lambda home: drive)
    monkeypatch.setattr(relocate.config, "app_dir", lambda: ".")
    rc = relocate.main([])                 # no --delete-legacy
    assert rc == 0
    assert relocate.scan(drive)            # still there
    assert "Dry-run" in capsys.readouterr().out


def test_home_is_threaded_into_token_path(monkeypatch, tmp_path):
    from pathlib import Path
    import mcpbrain.auth as auth
    captured = {}

    def _fake_bgs(*a, token_file=None, **k):
        captured["token_file"] = token_file
        return {"drive_service": FakeDrive()}

    monkeypatch.setattr(auth, "build_google_services", _fake_bgs)
    monkeypatch.setattr(relocate, "list_shared_drives", lambda svc: [])
    rc = relocate.main(["--home", str(tmp_path)])
    assert rc == 0
    assert captured["token_file"] == Path(str(tmp_path)) / "google_token.json"
