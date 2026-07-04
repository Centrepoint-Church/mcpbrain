"""Phase B exit gate: member contributes -> curator publishes -> a second
consumer imports the same claims as origin='org' rows. Proves the three
Phase-B subsystems (org_contrib, org_curate, org_import) compose end-to-end
on the shared test harness (tests/helpers/org_fleet.py)."""
from mcpbrain import org_contrib, org_curate, org_import
from mcpbrain.org_contracts import FleetPin


def _pin():
    return FleetPin(fleet_secret="s3cret",
                    relation_allowlist=("works_at", "member_of", "mentioned_with"))


def test_member_curator_consumer_round_trip(tmp_path):
    from tests.helpers.org_fleet import make_fleet
    members, curator, fs = make_fleet(tmp_path, n_members=2)
    alice, bob = members
    # pin every install's config so contribution is enabled
    from mcpbrain import config
    for inst in (alice, bob, curator):
        config.write_config(str(inst.home), {"org_config": {"org_pin": {
            "fleet_secret": "s3cret",
            "relation_allowlist": ["works_at", "member_of", "mentioned_with"]}}})

    # (1) alice's local graph learns joel works_at acme; contribute + upload.
    # Entity ids are deterministic name-slugs (graph_write.slugify), matching
    # what a real local extraction via graph_write.upsert_entity would have
    # produced for "Joel Chelliah" -- using anything else here would make this
    # a test-harness artifact, not a realistic round trip.
    a = alice.store
    with a._connect() as db:
        db.execute("INSERT INTO entities(id,name,type,email_addr,origin) "
                   "VALUES('joel-chelliah','Joel Chelliah','person','joel@acme.org','local')")
        db.execute("INSERT INTO entities(id,name,type,origin) VALUES('acme','Acme','org','local')")
    from mcpbrain import graph_write
    graph_write.upsert_relation(a, "joel-chelliah", "works_at", "acme", valid_from="2026-01-01",
                                source_doc_id="msg-1")
    with a._connect() as db:                          # give the provenance chunk a source_type
        db.execute("INSERT INTO chunks(doc_id,text,content_hash,metadata,enrich_state) "
                   "VALUES('msg-1','t','h','{\"source_type\":\"gmail\"}','')")
    delta, _wm = org_contrib._delta_since_watermark(a)
    assert org_contrib.collect_from_drain(a, delta, _pin(), "alice@x.org") == 3
    org_contrib.upload_pending(a, fs, "alice@x.org")

    # (2) curator ingests + publishes
    summary = org_curate.run(curator.store, fs, str(curator.home))
    assert summary["published"] is True and summary["version"] == 1

    # (3) bob imports the snapshot as origin='org'
    res = org_import.import_snapshot(bob.store, fs)
    assert res["status"] == "imported"
    joel = bob.store.get_entity("joel-chelliah")
    assert joel is not None and joel["origin"] == "org"
    with bob.store._connect() as db:
        rel = db.execute("SELECT origin FROM entity_relations WHERE entity_a='joel-chelliah' "
                         "AND relation='works_at'").fetchone()
    assert rel is not None and rel["origin"] == "org"
