import gzip
import hashlib
import json

from mcpbrain import org_import
from mcpbrain.org_contracts import SnapshotManifest, Tombstone
from mcpbrain.store import Store


def _store(tmp_path, name="consumer"):
    s = Store(tmp_path / f"{name}.sqlite3", dim=4)
    s.init()
    return s


def _publish(fs, entities, relations, *, version, tombstones=()):
    lines = ([json.dumps({"kind": "entity", **e}, sort_keys=True) for e in entities] +
             [json.dumps({"kind": "relation", **r}, sort_keys=True) for r in relations])
    gz = gzip.compress(("\n".join(lines) + "\n").encode())
    man = SnapshotManifest(version=version, created_at="t", entity_count=len(entities),
                           relation_count=len(relations), tombstone_count=len(tombstones),
                           snapshot_sha256=hashlib.sha256(gz).hexdigest())
    fs.put_bytes("org-graph/snapshot.jsonl.gz", gz)
    fs.put_bytes("org-graph/tombstones.jsonl",
                 ("\n".join(json.dumps(t.to_dict(), sort_keys=True) for t in tombstones) + "\n").encode()
                 if tombstones else b"")
    fs.put_bytes("org-graph/manifest.json", json.dumps(man.to_dict(), sort_keys=True).encode())


def _ent(id, name, type="person", **kw):
    return {"id": id, "name": name, "type": type, "org": kw.get("org", ""),
            "email_addr": kw.get("email_addr", ""), "aliases": kw.get("aliases", "")}


def test_import_writes_org_rows(tmp_path):
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    s = _store(tmp_path)
    _publish(fs, [_ent("joel", "Joel"), _ent("acme", "Acme", "org")],
             [{"entity_a": "joel", "relation": "works_at", "entity_b": "acme",
               "valid_from": "2026-01-01", "valid_to": "", "confidence": 1.0}], version=1)
    res = org_import.import_snapshot(s, fs)
    assert res["status"] == "imported" and res["version"] == 1
    with s._connect() as db:
        assert db.execute("SELECT COUNT(*) c FROM entities WHERE origin='org'").fetchone()["c"] == 2
        assert db.execute("SELECT origin FROM entity_relations WHERE entity_a='joel'").fetchone()["origin"] == "org"


def test_not_newer_is_skipped(tmp_path):
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    s = _store(tmp_path)
    _publish(fs, [_ent("acme", "Acme", "org")], [], version=1)
    org_import.import_snapshot(s, fs)
    again = org_import.import_snapshot(s, fs)
    assert again["status"] == "unchanged" and again["version"] == 1


def test_sha_mismatch_aborts_and_leaves_layer_intact(tmp_path):
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    s = _store(tmp_path)
    _publish(fs, [_ent("acme", "Acme", "org")], [], version=1)
    org_import.import_snapshot(s, fs)
    # publish v2 but corrupt the gzip after the manifest is written
    _publish(fs, [_ent("acme", "Acme", "org"), _ent("beta", "Beta", "org")], [], version=2)
    fs.put_bytes("org-graph/snapshot.jsonl.gz", b"corrupt-not-matching-sha")
    res = org_import.import_snapshot(s, fs)
    assert res["status"] == "error" and res["reason"] == "sha_mismatch"
    with s._connect() as db:                       # v1 layer survives
        assert db.execute("SELECT COUNT(*) c FROM entities WHERE origin='org'").fetchone()["c"] == 1


def test_wholesale_replace_removes_absent_org_rows_but_keeps_local(tmp_path):
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    s = _store(tmp_path)
    with s._connect() as db:
        db.execute("INSERT INTO entities(id,name,type,origin) VALUES('mine','Mine','person','local')")
    _publish(fs, [_ent("acme", "Acme", "org"), _ent("beta", "Beta", "org")], [], version=1)
    org_import.import_snapshot(s, fs)
    _publish(fs, [_ent("acme", "Acme", "org")], [], version=2)   # beta gone
    org_import.import_snapshot(s, fs)
    assert s.get_entity("beta") is None            # absent org row removed
    assert s.get_entity("acme") is not None        # still present
    assert s.get_entity("mine") is not None        # local untouched


def test_removal_demotes_when_local_data_attached(tmp_path):
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    s = _store(tmp_path)
    _publish(fs, [_ent("beta", "Beta", "org")], [], version=1)
    org_import.import_snapshot(s, fs)
    with s._connect() as db:                        # attach a LOCAL relation to the org node
        db.execute("INSERT INTO entities(id,name,type,origin) VALUES('me','Me','person','local')")
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b,origin) "
                   "VALUES('me','mentioned_with','beta','local')")
    _publish(fs, [_ent("acme", "Acme", "org")], [], version=2)   # beta absent
    org_import.import_snapshot(s, fs)
    beta = s.get_entity("beta")
    assert beta is not None and beta["origin"] == "local"   # demoted, not deleted


def test_tombstone_never_touches_colliding_local_entity(tmp_path):
    """A tombstoned id can currently belong to an unrelated origin='local' row
    (entity ids are deterministic name-slugs, so local/org collisions are the
    common case). The tombstone step must leave such a row, and its local
    relations, completely untouched rather than repointing-away-from and
    deleting it."""
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    s = _store(tmp_path)
    with s._connect() as db:                        # a genuine LOCAL entity at id "dup"
        db.execute("INSERT INTO entities(id,name,type,origin) VALUES('dup','My Own Dup','person','local')")
        db.execute("INSERT INTO entities(id,name,type,origin) VALUES('me','Me','person','local')")
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b,origin) "
                   "VALUES('me','mentioned_with','dup','local')")
    _publish(fs, [_ent("joel-chelliah", "Joel Chelliah")], [], version=1,
             tombstones=[Tombstone(entity_id="dup", merged_into="joel-chelliah")])
    org_import.import_snapshot(s, fs)
    dup = s.get_entity("dup")
    assert dup is not None and dup["origin"] == "local"      # never touched
    assert dup["name"] == "My Own Dup"                       # not overwritten
    with s._connect() as db:
        row = db.execute("SELECT entity_b FROM entity_relations WHERE entity_a='me'").fetchone()
    assert row["entity_b"] == "dup"                          # not repointed away


def test_upsert_never_clobbers_local_entity_at_colliding_id(tmp_path):
    """An org-snapshot entity whose id collides with a pre-existing
    origin='local' row must never overwrite that row's fields or flip its
    origin — the local row stays exactly as it was before import."""
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    s = _store(tmp_path)
    with s._connect() as db:
        db.execute("INSERT INTO entities(id,name,type,org,origin) "
                   "VALUES('acme','My Local Acme','org','MyOrgField','local')")
    _publish(fs, [_ent("acme", "Acme Corp", org="SnapshotOrgField")], [], version=1)
    org_import.import_snapshot(s, fs)
    acme = s.get_entity("acme")
    assert acme["origin"] == "local"
    assert acme["name"] == "My Local Acme" and acme["org"] == "MyOrgField"


def test_upsert_never_clobbers_local_relation_at_colliding_triple(tmp_path):
    """An org-snapshot relation whose (entity_a, relation, entity_b) triple
    collides with a pre-existing origin='local' relation must never overwrite
    that row's fields or flip its origin."""
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    s = _store(tmp_path)
    with s._connect() as db:
        db.execute("INSERT INTO entities(id,name,type,origin) VALUES('joel','Joel','person','local')")
        db.execute("INSERT INTO entities(id,name,type,origin) VALUES('acme','Acme','org','local')")
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b,valid_from,origin) "
                   "VALUES('joel','works_at','acme','2020-01-01','local')")
    _publish(fs, [_ent("joel", "Joel"), _ent("acme", "Acme", "org")],
             [{"entity_a": "joel", "relation": "works_at", "entity_b": "acme",
               "valid_from": "2026-01-01", "valid_to": "", "confidence": 1.0}], version=1)
    org_import.import_snapshot(s, fs)
    with s._connect() as db:
        row = db.execute("SELECT origin, valid_from FROM entity_relations "
                         "WHERE entity_a='joel' AND relation='works_at' AND entity_b='acme'").fetchone()
    assert row["origin"] == "local" and row["valid_from"] == "2020-01-01"


def test_wholesale_replace_removes_absent_org_relation(tmp_path):
    """An org relation whose triple no longer appears in a newer snapshot must
    be removed, even when both its endpoint entities remain present."""
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    s = _store(tmp_path)
    _publish(fs, [_ent("joel", "Joel"), _ent("acme", "Acme", "org"), _ent("beta", "Beta", "org")],
             [{"entity_a": "joel", "relation": "works_at", "entity_b": "acme",
               "valid_from": "2026-01-01", "valid_to": "", "confidence": 1.0}], version=1)
    org_import.import_snapshot(s, fs)
    with s._connect() as db:
        assert db.execute("SELECT COUNT(*) c FROM entity_relations WHERE origin='org'").fetchone()["c"] == 1
    # v2: joel now works_at beta instead — the joel->acme triple must be gone,
    # not left behind as a stale relation alongside the new one.
    _publish(fs, [_ent("joel", "Joel"), _ent("acme", "Acme", "org"), _ent("beta", "Beta", "org")],
             [{"entity_a": "joel", "relation": "works_at", "entity_b": "beta",
               "valid_from": "2026-02-01", "valid_to": "", "confidence": 1.0}], version=2)
    org_import.import_snapshot(s, fs)
    with s._connect() as db:
        rows = {(r["entity_a"], r["relation"], r["entity_b"])
                for r in db.execute("SELECT entity_a, relation, entity_b FROM entity_relations "
                                    "WHERE origin='org'").fetchall()}
    assert rows == {("joel", "works_at", "beta")}


def test_tombstone_repoints_local_references(tmp_path):
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    s = _store(tmp_path)
    _publish(fs, [_ent("dup", "Dup"), _ent("joel-chelliah", "Joel Chelliah")], [], version=1)
    org_import.import_snapshot(s, fs)
    with s._connect() as db:
        db.execute("INSERT INTO entities(id,name,type,origin) VALUES('doc','Doc','document','local')")
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b,origin) "
                   "VALUES('doc','mentioned_with','dup','local')")
    _publish(fs, [_ent("joel-chelliah", "Joel Chelliah")], [], version=2,
             tombstones=[Tombstone(entity_id="dup", merged_into="joel-chelliah")])
    org_import.import_snapshot(s, fs)
    assert s.get_entity("dup") is None
    with s._connect() as db:
        row = db.execute("SELECT entity_b FROM entity_relations WHERE entity_a='doc'").fetchone()
    assert row["entity_b"] == "joel-chelliah"       # local ref re-pointed to the survivor
