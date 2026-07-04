from mcpbrain import onboarding
from mcpbrain.org_contracts import FleetPin
from mcpbrain.store import Store


def _store(tmp_path):
    s = Store(tmp_path / "brain.sqlite3", dim=4)
    s.init()
    return s


def _pin(pinned=True):
    return FleetPin(embed_model="bge-small", dim=4, chunker_version="v1",
                    fleet_secret="s3cret" if pinned else "")


def _fakes(calls, *, snap_status="imported", drive_hits=2, raise_on=()):
    """Return (import_snapshot, bootstrap_drive) fakes that record call order
    and mutate the store, so a test can prove real work happened + ordering."""
    def import_snapshot(store, fleet_storage):
        calls.append("snapshot")
        with store._connect() as db:
            db.execute("INSERT OR IGNORE INTO entities(id,name,type,origin) "
                       "VALUES('ceo','CEO','person','org')")
        return {"status": snap_status, "entity_count": 1}

    def bootstrap_drive(store, fleet_storage, drive_id, pin):
        calls.append(f"drive:{drive_id}")
        if drive_id in raise_on:
            raise RuntimeError(f"boom on {drive_id}")
        with store._connect() as db:
            db.execute("INSERT OR IGNORE INTO entities(id,name,type,origin) "
                       f"VALUES('doc-{drive_id}','Doc','document','local')")
        return {"cache_hits": drive_hits, "drive_id": drive_id}

    return import_snapshot, bootstrap_drive


def test_snapshot_imported_before_any_drive(tmp_path):
    store = _store(tmp_path)
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    calls = []
    imp, boot = _fakes(calls)
    res = onboarding.bootstrap_baseline(
        store, fs, ["D1", "D2"], _pin(),
        import_snapshot=imp, bootstrap_drive=boot)
    # Ordering contract: snapshot first, then drives in order.
    assert calls == ["snapshot", "drive:D1", "drive:D2"]
    assert res["snapshot"]["status"] == "imported"
    assert res["snapshot_done"] is True
    assert res["drives"]["D1"]["status"] == "ok"
    assert res["cache_hits"] == 4
    assert res["done_drive_ids"] == {"D1", "D2"}


def test_no_fleet_storage_skips_everything(tmp_path):
    store = _store(tmp_path)
    calls = []
    imp, boot = _fakes(calls)
    res = onboarding.bootstrap_baseline(
        store, None, ["D1"], _pin(),
        import_snapshot=imp, bootstrap_drive=boot)
    assert calls == []                                   # nothing called
    assert res["snapshot"]["status"] == "skipped"
    assert res["snapshot"]["reason"] == "no_fleet_storage"
    assert res["drives"] == {}
    assert res["done_drive_ids"] == set()


def test_unpinned_skips_drive_cache_but_imports_snapshot(tmp_path):
    store = _store(tmp_path)
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    calls = []
    imp, boot = _fakes(calls)
    res = onboarding.bootstrap_baseline(
        store, fs, ["D1"], _pin(pinned=False),
        import_snapshot=imp, bootstrap_drive=boot)
    assert calls == ["snapshot"]                         # drive cache skipped
    assert res["snapshot_done"] is True
    assert res["drives"]["D1"]["status"] == "skipped"
    assert res["drives"]["D1"]["reason"] == "no_pin"


def test_no_snapshot_is_benign_and_drives_still_run(tmp_path):
    store = _store(tmp_path)
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    calls = []
    imp, boot = _fakes(calls, snap_status="no_snapshot")
    res = onboarding.bootstrap_baseline(
        store, fs, ["D1"], _pin(),
        import_snapshot=imp, bootstrap_drive=boot)
    assert res["snapshot"]["status"] == "no_snapshot"
    assert res["snapshot_done"] is False                 # nothing to import
    assert res["drives"]["D1"]["status"] == "ok"


def test_one_bad_drive_does_not_block_the_rest(tmp_path):
    store = _store(tmp_path)
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    calls = []
    imp, boot = _fakes(calls, raise_on=("D1",))
    res = onboarding.bootstrap_baseline(
        store, fs, ["D1", "D2"], _pin(),
        import_snapshot=imp, bootstrap_drive=boot)
    assert res["drives"]["D1"]["status"] == "error"
    assert res["drives"]["D2"]["status"] == "ok"
    assert res["done_drive_ids"] == {"D2"}               # errored drive not marked done
    assert any("D1" in e for e in res["errors"])


def test_snapshot_error_is_caught_and_drives_still_run(tmp_path):
    store = _store(tmp_path)
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    calls = []

    def imp(store, fs_):
        calls.append("snapshot")
        raise RuntimeError("corrupt manifest")

    _, boot = _fakes(calls)
    res = onboarding.bootstrap_baseline(
        store, fs, ["D1"], _pin(), import_snapshot=imp, bootstrap_drive=boot)
    assert res["snapshot"]["status"] == "error"
    assert res["snapshot_done"] is False
    assert res["drives"]["D1"]["status"] == "ok"         # degraded, not aborted


def test_resume_skips_already_done_drives_and_snapshot(tmp_path):
    store = _store(tmp_path)
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    calls = []
    imp, boot = _fakes(calls)
    res = onboarding.bootstrap_baseline(
        store, fs, ["D1", "D2"], _pin(),
        import_snapshot=imp, bootstrap_drive=boot,
        done_drive_ids={"D1"}, snapshot_done=True)
    assert calls == ["drive:D2"]                          # snapshot + D1 skipped
    assert res["snapshot"]["status"] == "skipped"
    assert res["drives"]["D1"]["status"] == "skipped"
    assert res["drives"]["D2"]["status"] == "ok"
    assert res["done_drive_ids"] == {"D1", "D2"}


def test_unavailable_status_not_marked_done(tmp_path):
    """Prove that a benign 'unavailable' status (Phase A not built yet) does NOT
    mark the drive as done, so it will retry once the subsystem lands."""
    store = _store(tmp_path)
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    calls = []

    def imp(store, fs_):
        calls.append("snapshot")
        return {"status": "imported", "entity_count": 1}

    def boot(store, fs_, drive_id, pin):
        calls.append(f"drive:{drive_id}")
        # Simulate Phase A not built yet (e.g., ingest_cache not imported).
        return {"status": "unavailable", "cache_hits": 0,
                "reason": "ingest_cache not built (Phase A)"}

    res = onboarding.bootstrap_baseline(
        store, fs, ["D1"], _pin(),
        import_snapshot=imp, bootstrap_drive=boot)
    assert calls == ["snapshot", "drive:D1"]
    assert res["drives"]["D1"]["status"] == "unavailable"  # status is preserved
    assert res["done_drive_ids"] == set()                 # NOT added to done
    assert res["cache_hits"] == 0                         # cache_hits still recorded


def test_default_bindings_delegate_to_real_modules(monkeypatch):
    # Post-convergence, the default bindings resolve to the real A/B functions.
    from mcpbrain import org_import, ingest_cache
    monkeypatch.setattr(org_import, "import_snapshot",
                        lambda store, fs: {"status": "imported", "via": "real"})
    monkeypatch.setattr(ingest_cache, "bootstrap_drive",
                        lambda store, fs, drive_id, pin: {"status": "ok", "cache_hits": 3})
    assert onboarding._default_import_snapshot(object(), object())["via"] == "real"
    d = onboarding._default_bootstrap_drive(object(), object(), "D1", _pin())
    assert d["status"] == "ok" and d["cache_hits"] == 3


def test_default_make_fleet_storage_none_without_service(tmp_path):
    assert onboarding._default_make_fleet_storage(str(tmp_path), None) is None


def test_default_enumerate_drives_empty_without_service():
    assert onboarding._default_enumerate_drives(None) == []


def test_default_make_fleet_storage_delegates_to_A(tmp_path, monkeypatch):
    from mcpbrain import fleet_storage
    sentinel = object()
    monkeypatch.setattr(fleet_storage, "fleet_folder_storage",
                        lambda home, drive_service=None: sentinel)
    assert onboarding._default_make_fleet_storage(str(tmp_path), object()) is sentinel


def test_default_enumerate_drives_maps_ids(monkeypatch):
    from mcpbrain import fleet_storage
    monkeypatch.setattr(fleet_storage, "list_shared_drives",
                        lambda svc: [{"id": "D1", "name": "Ops"}, {"id": "D2", "name": "HR"}])
    assert onboarding._default_enumerate_drives(object()) == ["D1", "D2"]


def test_default_make_drive_storage_builds_per_drive(monkeypatch):
    from mcpbrain import fleet_storage
    seen = []
    monkeypatch.setattr(fleet_storage, "drive_cache_storage",
                        lambda svc, drive_id: ("cache", drive_id))
    factory = onboarding._default_make_drive_storage(object())
    assert factory("D7") == ("cache", "D7")
    # No drive service -> a factory that yields None (degrade, don't crash).
    assert onboarding._default_make_drive_storage(None)("D7") is None


def test_bootstrap_uses_per_drive_storage_not_fleet_folder(tmp_path):
    # bootstrap_drive must receive the per-drive cache storage, never the
    # fleet-folder storage handed to import_snapshot.
    store = _store(tmp_path)
    folder_fs, drive_fs = object(), object()
    got = {}

    def imp(store, fs_):
        got["snapshot_fs"] = fs_
        return {"status": "imported"}

    def boot(store, fs_, drive_id, pin):
        got["drive_fs"] = fs_
        return {"status": "ok", "cache_hits": 1}

    onboarding.bootstrap_baseline(
        store, folder_fs, ["D1"], _pin(),
        import_snapshot=imp, bootstrap_drive=boot,
        make_drive_storage=lambda drive_id: drive_fs)
    assert got["snapshot_fs"] is folder_fs
    assert got["drive_fs"] is drive_fs


def test_fleet_folder_id_falls_back_to_org_default(tmp_path):
    from mcpbrain import org_defaults
    assert onboarding._fleet_folder_id(str(tmp_path)) == org_defaults.FLEET_FOLDER_ID
