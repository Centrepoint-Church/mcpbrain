from mcpbrain.store import Store
from mcpbrain import org_contracts  # contracts import cleanly


def test_init_is_idempotent_on_populated_store(tmp_path):
    path = tmp_path / "brain.sqlite3"
    s = Store(path, dim=4); s.init()
    with s._connect() as db:
        db.execute("INSERT INTO entities(id,name,type,origin) "
                   "VALUES('joel','Joel','person','local')")
        db.execute("INSERT INTO entities(id,name,type,origin) "
                   "VALUES('ceo','CEO','person','org')")
    # Re-init (simulates a daemon restart / re-open): must not error, must not
    # drop or mutate existing rows or their origin tags.
    s2 = Store(path, dim=4); s2.init()
    with s2._connect() as db:
        rows = dict(db.execute("SELECT id, origin FROM entities").fetchall())
    assert rows == {"joel": "local", "ceo": "org"}


def test_contracts_module_surface():
    # The frozen surface A/B/C import must all be present.
    for name in ("CacheArtifact", "ContributionRecord", "SnapshotManifest",
                 "Tombstone", "FleetPin", "FleetStorage",
                 "pipeline_fingerprint", "source_ref", "artifact_filename",
                 "DRIVE_ID_META_KEY", "DEFAULT_RELATION_ALLOWLIST"):
        assert hasattr(org_contracts, name), name
