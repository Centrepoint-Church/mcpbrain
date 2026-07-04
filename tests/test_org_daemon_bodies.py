from mcpbrain import daemon as d


def _daemon():
    # A bare Daemon-like object is heavy to build; assert the bodies gate on the
    # module-level seams by patching them. Use the real class with a minimal shim.
    class Shim(d.Daemon):
        def __init__(self):
            self._org_contrib_upload_interval_s = 1.0
            self._last_org_contrib_upload = None
            self._org_import_interval_s = 1.0
            self._last_org_import = None
            self._org_curate_interval_s = 1.0
            self._last_org_curate = None
            self._clock = lambda: 1000.0

        def ensure_services(self):          # real daemon resolves services here
            return {"drive_service": None}
    return Shim()


def test_contrib_upload_skips_when_unpinned(tmp_path, monkeypatch):
    from mcpbrain import config
    monkeypatch.setattr(config, "app_dir", lambda: tmp_path)
    dm = _daemon()
    res = dm._run_org_contrib_upload()
    assert res == {"skipped": "unpinned"} or res == {"skipped": "disabled"}
    assert dm._last_org_contrib_upload == 1000.0   # advanced despite skip


def test_curate_skips_when_not_curator(tmp_path, monkeypatch):
    from mcpbrain import config
    monkeypatch.setattr(config, "app_dir", lambda: tmp_path)   # role defaults to 'member'
    dm = _daemon()
    assert dm._run_org_curate() == {"skipped": "not_curator"}


def test_import_noops_without_fleet_storage(tmp_path, monkeypatch):
    # Simulate the pre-A state: subsystem A's fleet_storage module either isn't
    # importable, or its factory returns None (no Drive service). Inject a fake
    # that returns None so the assertion holds regardless of whether A has landed.
    import sys
    import types
    from mcpbrain import config
    monkeypatch.setattr(config, "app_dir", lambda: tmp_path)
    fake = types.ModuleType("mcpbrain.fleet_storage")
    fake.fleet_folder_storage = lambda home, drive_service=None: None
    monkeypatch.setitem(sys.modules, "mcpbrain.fleet_storage", fake)
    dm = _daemon()
    assert dm._run_org_import() == {"skipped": "no_fleet_storage"}
    assert dm._last_org_import == 1000.0           # advanced despite skip


def test_daemon_bodies_round_trip_contrib_curate_import(tmp_path, monkeypatch):
    """A full, successful end-to-end cadence run through all three daemon
    wrappers — not the underlying org_contrib/org_curate/org_import modules
    directly (already covered by test_org_phase_b_gate.py) — proving the
    daemon-level gating/wiring (config lookups, the guarded fleet_storage
    import, _last_* advancement) doesn't silently break the real happy path.
    Every prior test in this file only exercises a skip/gate branch; none
    confirms the wrappers actually produce real results when nothing is
    disabled or missing."""
    import sys
    import types
    from mcpbrain import config, graph_write
    from tests.helpers.org_fleet import make_fleet

    members, curator, fs = make_fleet(tmp_path, n_members=2)
    alice, bob = members
    for inst in (alice, bob, curator):
        config.write_config(str(inst.home), {"org_config": {"org_pin": {
            "fleet_secret": "s3cret",
            "relation_allowlist": ["works_at", "member_of", "mentioned_with"]}}})

    fake = types.ModuleType("mcpbrain.fleet_storage")
    fake.fleet_folder_storage = lambda home, drive_service=None: fs
    monkeypatch.setitem(sys.modules, "mcpbrain.fleet_storage", fake)

    # (1) alice's local graph learns joel works_at acme; her daemon body
    # collects the delta and uploads it — the real happy path, not a skip.
    # Entity id must match what graph_write.upsert_entity would itself derive
    # (slugify("Joel Chelliah") == "joel-chelliah") — the curator's own
    # _materialise re-derives ids via upsert_entity rather than trusting a
    # claim's arbitrary local id, so a mismatched id here would silently
    # publish under a different id than this test expects (the same lesson
    # test_org_phase_b_gate.py's own fixture already learned).
    with alice.store._connect() as db:
        db.execute("INSERT INTO entities(id,name,type,email_addr,origin) "
                   "VALUES('joel-chelliah','Joel Chelliah','person','joel@acme.org','local')")
        db.execute("INSERT INTO entities(id,name,type,origin) VALUES('acme','Acme','org','local')")
        db.execute("INSERT INTO chunks(doc_id,text,content_hash,metadata,enrich_state) "
                   "VALUES('msg-1','t','h','{\"source_type\":\"gmail\"}','')")
    graph_write.upsert_relation(alice.store, "joel-chelliah", "works_at", "acme", valid_from="2026-01-01",
                                source_doc_id="msg-1")
    monkeypatch.setattr(config, "app_dir", lambda: alice.home)
    dm_alice = _daemon()
    dm_alice._store = alice.store
    res = dm_alice._run_org_contrib_upload()
    assert "skipped" not in res
    assert res.get("uploaded") == 3   # joel entity, acme entity, works_at relation

    # (2) curator's daemon body ingests the upload and publishes a snapshot.
    monkeypatch.setattr(config, "app_dir", lambda: curator.home)
    dm_curator = _daemon()
    dm_curator._store = curator.store
    res = dm_curator._run_org_curate()
    assert "skipped" not in res
    assert res.get("published") is True and res.get("version") == 1

    # (3) bob's daemon body imports the published snapshot.
    monkeypatch.setattr(config, "app_dir", lambda: bob.home)
    dm_bob = _daemon()
    dm_bob._store = bob.store
    res = dm_bob._run_org_import()
    assert "skipped" not in res
    assert res.get("status") == "imported"
    joel = bob.store.get_entity("joel-chelliah")
    assert joel is not None and joel["origin"] == "org"
    with bob.store._connect() as db:
        rel = db.execute("SELECT origin FROM entity_relations WHERE entity_a='joel-chelliah' "
                         "AND relation='works_at'").fetchone()
    assert rel is not None and rel["origin"] == "org"
    # Every wrapper advanced its own cadence timer despite doing real work,
    # same contract as the skip paths.
    assert dm_alice._last_org_contrib_upload == 1000.0
    assert dm_curator._last_org_curate == 1000.0
    assert dm_bob._last_org_import == 1000.0
