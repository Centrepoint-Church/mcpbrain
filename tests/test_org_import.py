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
