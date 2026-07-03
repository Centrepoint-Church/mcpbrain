import json

from mcpbrain import onboarding
from mcpbrain.org_contracts import FleetPin
from tests.helpers.org_fleet import make_install


def _configure(home, *, import_on=True, cache_on=True, pinned=True):
    from mcpbrain import config
    cfg = {"owner_name": "Al", "owner_email": "al@x.org",
           "orgs": [{"name": "Acme"}],
           "org_import_enabled": import_on, "ingest_cache": cache_on}
    if pinned:
        cfg["org_config"] = {"org_pin": {"embed_model": "bge-small", "dim": 4,
                                         "chunker_version": "v1",
                                         "fleet_secret": "s3cret"}}
    config.write_config(str(home), cfg)


def _fakes(calls):
    def imp(store, fs):
        calls.append("snapshot")
        return {"status": "imported", "entity_count": 1}

    def boot(store, fs, drive_id, pin):
        calls.append(f"drive:{drive_id}")
        return {"cache_hits": 3, "drive_id": drive_id}
    return imp, boot


def test_flags_off_skips(tmp_path):
    inst = make_install(tmp_path, "al")
    _configure(inst.home, import_on=False, cache_on=False)
    res = onboarding.run_bootstrap(str(inst.home), inst.store)
    assert res["status"] == "skipped" and res["reason"] == "flags_off"


def test_done_writes_marker_and_second_run_is_skipped(tmp_path):
    inst = make_install(tmp_path, "al")
    _configure(inst.home)
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    calls = []
    imp, boot = _fakes(calls)
    res = onboarding.run_bootstrap(
        str(inst.home), inst.store, fleet_storage=fs, drives=["D1"],
        import_snapshot=imp, bootstrap_drive=boot)
    assert res["status"] == "done"
    marker = json.loads((inst.home / "baseline_bootstrap.json").read_text())
    assert marker["snapshot_done"] is True
    assert marker["done_drive_ids"] == ["D1"]
    assert marker["completed_at"]
    # Second run: marker present -> skipped, no fakes re-invoked.
    calls.clear()
    res2 = onboarding.run_bootstrap(
        str(inst.home), inst.store, fleet_storage=fs, drives=["D1"],
        import_snapshot=imp, bootstrap_drive=boot)
    assert res2["status"] == "skipped" and res2["reason"] == "already_bootstrapped"
    assert calls == []


def test_force_reruns_even_when_marked_done(tmp_path):
    inst = make_install(tmp_path, "al")
    _configure(inst.home)
    (inst.home / "baseline_bootstrap.json").write_text(
        json.dumps({"snapshot_done": True, "done_drive_ids": ["D1"],
                    "completed_at": "2026-01-01T00:00:00Z"}))
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    calls = []
    imp, boot = _fakes(calls)
    res = onboarding.run_bootstrap(
        str(inst.home), inst.store, fleet_storage=fs, drives=["D2"], force=True,
        import_snapshot=imp, bootstrap_drive=boot)
    assert res["status"] == "done"
    # resume: prior snapshot_done + D1 preserved, D2 newly imported.
    assert calls == ["drive:D2"]
    marker = json.loads((inst.home / "baseline_bootstrap.json").read_text())
    assert set(marker["done_drive_ids"]) == {"D1", "D2"}


def test_no_transport_is_degraded_and_retryable(tmp_path):
    inst = make_install(tmp_path, "al")
    _configure(inst.home)
    calls = []
    imp, boot = _fakes(calls)
    # make_fleet_storage returns None -> degraded, marker NOT finalized.
    res = onboarding.run_bootstrap(
        str(inst.home), inst.store, drive_service=object(),
        make_fleet_storage=lambda h, s: None,
        enumerate_drives=lambda s: ["D1"],
        import_snapshot=imp, bootstrap_drive=boot)
    assert res["status"] == "degraded"
    marker = json.loads((inst.home / "baseline_bootstrap.json").read_text())
    assert marker["completed_at"] == ""          # so the daemon retries next cycle
    assert onboarding.should_bootstrap(str(inst.home)) is True


def test_pin_resolved_from_config_gates_cache(tmp_path):
    inst = make_install(tmp_path, "al")
    _configure(inst.home, pinned=False)          # no fleet_secret -> not pinned
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    calls = []
    imp, boot = _fakes(calls)
    res = onboarding.run_bootstrap(
        str(inst.home), inst.store, fleet_storage=fs, drives=["D1"],
        import_snapshot=imp, bootstrap_drive=boot)
    assert res["status"] == "done"
    assert calls == ["snapshot"]                 # unpinned -> drive cache skipped
    assert res["drives"]["D1"]["reason"] == "no_pin"


def test_should_bootstrap_gate(tmp_path):
    inst = make_install(tmp_path, "al")
    # Not configured yet -> False.
    assert onboarding.should_bootstrap(str(inst.home)) is False
    _configure(inst.home)
    assert onboarding.should_bootstrap(str(inst.home)) is True
    # Completed marker -> False.
    (inst.home / "baseline_bootstrap.json").write_text(
        json.dumps({"completed_at": "2026-01-01T00:00:00Z"}))
    assert onboarding.should_bootstrap(str(inst.home)) is False


def test_end_to_end_with_fakes_zero_extraction_and_idempotent(tmp_path):
    from tests.helpers.org_fleet import make_fleet, LocalDirFleetStorage
    members, curator, _ = make_fleet(tmp_path, n_members=1)
    inst = members[0]
    _configure(inst.home)                         # configured + pinned
    fs = LocalDirFleetStorage(tmp_path / "fleet")

    extracted = {"count": 0}                        # sentinel: must stay 0

    def imp(store, fs_):
        with store._connect() as db:
            db.execute("INSERT OR IGNORE INTO entities(id,name,type,origin) "
                       "VALUES('ceo','CEO','person','org')")
        return {"status": "imported", "entity_count": 1}

    def boot(store, fs_, drive_id, pin):
        extracted["count"] += 0                     # cache import != extraction
        with store._connect() as db:
            db.execute("INSERT OR IGNORE INTO entities(id,name,type,origin) "
                       f"VALUES('doc-{drive_id}','Doc','document','local')")
        return {"cache_hits": 10, "drive_id": drive_id}

    res = onboarding.run_bootstrap(
        str(inst.home), inst.store, fleet_storage=fs, drives=["D1", "D2"],
        import_snapshot=imp, bootstrap_drive=boot)

    assert res["status"] == "done"
    assert res["cache_hits"] == 20
    assert extracted["count"] == 0                  # nothing extracted on cache hits
    with inst.store._connect() as db:
        origins = dict(db.execute("SELECT id, origin FROM entities").fetchall())
    assert origins["ceo"] == "org"                  # snapshot skeleton present
    assert origins["doc-D1"] == "local"             # cache-imported rows present

    # Re-run: marker makes it a no-op (no duplicate work).
    res2 = onboarding.run_bootstrap(
        str(inst.home), inst.store, fleet_storage=fs, drives=["D1", "D2"],
        import_snapshot=imp, bootstrap_drive=boot)
    assert res2["status"] == "skipped"
