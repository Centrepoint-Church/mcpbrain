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


def test_transitive_tombstone_chain_resolves_regardless_of_list_order(tmp_path):
    """org_curate._tombstones() republishes full merge history every publish,
    so a single snapshot can carry both links of a chain formed by two
    separate curator merges (A merged into B, then later B merged into C).
    Applying them in the given order must not matter: if B->C happens to be
    processed before A->B, B is already gone by the time A->B is reached,
    and the correct behavior is still to land A's local flesh on C — not
    strand it via a demote fallback because the immediate target vanished."""
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    s = _store(tmp_path)
    _publish(fs, [_ent("a", "A"), _ent("b", "B")], [], version=1)
    org_import.import_snapshot(s, fs)
    with s._connect() as db:                         # local flesh attached to A
        db.execute("INSERT INTO entities(id,name,type,origin) VALUES('doc','Doc','document','local')")
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b,origin) "
                   "VALUES('doc','mentioned_with','a','local')")
    # Tombstones listed in the order that exposes the bug: the SECOND link of
    # the chain (b->c) appears BEFORE the first (a->b).
    _publish(fs, [_ent("c", "C")], [], version=2,
             tombstones=[Tombstone(entity_id="b", merged_into="c"),
                        Tombstone(entity_id="a", merged_into="b")])
    org_import.import_snapshot(s, fs)
    assert s.get_entity("a") is None and s.get_entity("b") is None
    c = s.get_entity("c")
    assert c is not None and c["origin"] == "org"    # C survives, not demoted
    with s._connect() as db:
        row = db.execute("SELECT entity_b FROM entity_relations WHERE entity_a='doc'").fetchone()
    assert row["entity_b"] == "c"                    # A's flesh landed on the true final target


def test_slug_drift_email_equality_merges_local_into_org(tmp_path):
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    s = _store(tmp_path)
    with s._connect() as db:                        # local variant with a private observation
        db.execute("INSERT INTO entities(id,name,type,email_addr,origin,mentions) "
                   "VALUES('joel-c','Joel C','person','joel@acme.org','local',3)")
        db.execute("INSERT INTO entity_observations(entity_id,attribute,value,source,valid_from) "
                   "VALUES('joel-c','note','private','local','2026-01-01')")
    _publish(fs, [_ent("joel-chelliah", "Joel Chelliah", email_addr="joel@acme.org")],
             [], version=1)
    org_import.import_snapshot(s, fs)
    assert s.get_entity("joel-c") is None            # local merged away
    surv = s.get_entity("joel-chelliah")
    assert surv is not None and surv["origin"] == "org"   # org node survives
    with s._connect() as db:
        obs = db.execute("SELECT entity_id FROM entity_observations WHERE attribute='note'").fetchone()
        rep = db.execute("SELECT from_entity_id,to_entity_id FROM org_repoint_log").fetchone()
    assert obs["entity_id"] == "joel-chelliah"       # private flesh re-attached
    assert (rep["from_entity_id"], rep["to_entity_id"]) == ("joel-c", "joel-chelliah")


def test_role_address_pair_never_auto_merges(tmp_path):
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    s = _store(tmp_path)
    with s._connect() as db:
        db.execute("INSERT INTO entities(id,name,type,email_addr,origin) "
                   "VALUES('office-local','Office','person','office@acme.org','local')")
    _publish(fs, [_ent("office-org", "Office Org", email_addr="office@acme.org")], [], version=1)
    org_import.import_snapshot(s, fs)
    assert s.get_entity("office-local") is not None  # NOT merged (role inbox)


def test_role_address_org_entity_never_merges_via_fuzzy_name_match(tmp_path):
    """A role-address-keyed ORG entity must never absorb a real local person's
    flesh via the name/alias/token-match path either — "role-address pairs
    never auto-merge" holds regardless of which match strategy would
    otherwise fire, not just the email-equality path."""
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    s = _store(tmp_path)
    with s._connect() as db:                        # real person, no email at all
        db.execute("INSERT INTO entities(id,name,type,origin) VALUES('joel-local','Joel Chelliah','person','local')")
    _publish(fs, [_ent("office-org", "Joel Chelliah", email_addr="office@acme.org")], [], version=1)
    org_import.import_snapshot(s, fs)
    assert s.get_entity("joel-local") is not None    # NOT merged into the role inbox


def test_fan_in_two_org_entities_matching_same_local_id_is_ambiguous(tmp_path):
    """Two org entities in the SAME snapshot independently matching one local
    id (an upstream data-quality hiccup, e.g. two org entities sharing an
    email the curator's own dedup should have caught but hasn't yet) is
    ambiguity from this consumer's point of view too — neither should win by
    accident of processing order. The local candidate stays untouched (left
    for the local fuzzy-review queue) and no repoint is logged for either."""
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    s = _store(tmp_path)
    with s._connect() as db:
        db.execute("INSERT INTO entities(id,name,type,email_addr,origin) "
                   "VALUES('joel-local','Joel','person','joel@acme.org','local')")
    _publish(fs, [_ent("joel-a", "Joel A", email_addr="joel@acme.org"),
                  _ent("joel-b", "Joel B", email_addr="joel@acme.org")], [], version=1)
    org_import.import_snapshot(s, fs)
    assert s.get_entity("joel-local") is not None    # neither org entity claimed it
    with s._connect() as db:
        logs = db.execute("SELECT from_entity_id, to_entity_id FROM org_repoint_log").fetchall()
    assert logs == []                                # no repoint for either side of the fan-in


def test_multiple_candidates_left_ambiguous_not_arbitrarily_merged(tmp_path):
    """Two LOCAL rows both plausibly matching one incoming org entity (fan-in
    the other direction) must be left alone for the fuzzy-review queue, not
    resolved by picking one arbitrarily."""
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    s = _store(tmp_path)
    with s._connect() as db:                         # two distinct local people, same email
        db.execute("INSERT INTO entities(id,name,type,email_addr,origin) "
                   "VALUES('j1','Joel One','person','shared@acme.org','local')")
        db.execute("INSERT INTO entities(id,name,type,email_addr,origin) "
                   "VALUES('j2','Joel Two','person','shared@acme.org','local')")
    _publish(fs, [_ent("joel-org", "Joel Org", email_addr="shared@acme.org")], [], version=1)
    org_import.import_snapshot(s, fs)
    assert s.get_entity("j1") is not None and s.get_entity("j2") is not None   # neither merged


def test_restore_does_not_move_observation_added_after_the_merge(tmp_path):
    """A curator SPLIT restore must not pull back onto the resurrected node an
    observation the merge target accrued NATIVELY, after the original merge
    happened — only observations recorded at-or-before the repoint belong to
    the resurrected node's migrated flesh."""
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    s = _store(tmp_path)
    _publish(fs, [_ent("joel-old", "Joel Old"), _ent("joel-generic", "Joel Generic")], [], version=1)
    org_import.import_snapshot(s, fs)
    with s._connect() as db:                         # pre-merge local flesh on joel-old
        db.execute("INSERT INTO entity_observations(entity_id,attribute,value,source,valid_from) "
                   "VALUES('joel-old','note','pre-merge','local','2020-01-01')")
    _publish(fs, [_ent("joel-generic", "Joel Generic")], [], version=2,
             tombstones=[Tombstone(entity_id="joel-old", merged_into="joel-generic")])
    org_import.import_snapshot(s, fs)                # joel-old merged away; pre-merge note now on joel-generic
    with s._connect() as db:
        db.execute("INSERT INTO entity_observations(entity_id,attribute,value,source,valid_from) "
                   "VALUES('joel-generic','note','native-post-merge','local','2099-01-01')")
    _publish(fs, [_ent("joel-old", "Joel Old"), _ent("joel-generic", "Joel Generic")], [], version=3)
    org_import.import_snapshot(s, fs)                # curator splits joel-old back out
    with s._connect() as db:
        old_notes = {r["value"] for r in db.execute(
            "SELECT value FROM entity_observations WHERE entity_id='joel-old'").fetchall()}
        generic_notes = {r["value"] for r in db.execute(
            "SELECT value FROM entity_observations WHERE entity_id='joel-generic'").fetchall()}
    assert old_notes == {"pre-merge"}                 # migrated flesh restored
    assert generic_notes == {"native-post-merge"}     # native-to-target flesh stays put


def test_restore_precision_survives_same_day_post_merge_observation(tmp_path):
    """The observation-restore boundary is an exact set of migrated
    entity_observations.id, not a date/timestamp comparison — so a native
    observation added to the merge target on the SAME calendar day as the
    repoint (org_repoint_log.at is a full timestamp; entity_observations
    lacks one) must still be correctly excluded from restore. A date-only
    valid_from<=at comparison would have wrongly included it, since a
    same-day date string is lexicographically a prefix of (and therefore
    "less than") that day's full timestamp."""
    import datetime as _dt
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    s = _store(tmp_path)
    today = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    _publish(fs, [_ent("joel-old", "Joel Old"), _ent("joel-generic", "Joel Generic")], [], version=1)
    org_import.import_snapshot(s, fs)
    with s._connect() as db:                         # pre-merge flesh, dated today too
        db.execute("INSERT INTO entity_observations(entity_id,attribute,value,source,valid_from) "
                   "VALUES('joel-old','note','pre-merge','local',?)", (today,))
    _publish(fs, [_ent("joel-generic", "Joel Generic")], [], version=2,
             tombstones=[Tombstone(entity_id="joel-old", merged_into="joel-generic")])
    org_import.import_snapshot(s, fs)                # repoint happens "now" — org_repoint_log.at is today
    with s._connect() as db:                         # native observation added the SAME day, after the merge
        db.execute("INSERT INTO entity_observations(entity_id,attribute,value,source,valid_from) "
                   "VALUES('joel-generic','note','native-same-day','local',?)", (today,))
    _publish(fs, [_ent("joel-old", "Joel Old"), _ent("joel-generic", "Joel Generic")], [], version=3)
    org_import.import_snapshot(s, fs)                # curator splits joel-old back out
    with s._connect() as db:
        old_notes = {r["value"] for r in db.execute(
            "SELECT value FROM entity_observations WHERE entity_id='joel-old'").fetchall()}
        generic_notes = {r["value"] for r in db.execute(
            "SELECT value FROM entity_observations WHERE entity_id='joel-generic'").fetchall()}
    assert old_notes == {"pre-merge"}
    assert generic_notes == {"native-same-day"}      # not pulled onto joel-old despite matching dates


def test_ambiguous_name_only_pair_left_for_fuzzy_queue(tmp_path):
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    s = _store(tmp_path)
    with s._connect() as db:                         # same-ish name, no shared email/alias
        db.execute("INSERT INTO entities(id,name,type,origin) VALUES('jsmith','J Smith','person','local')")
    _publish(fs, [_ent("john-smith", "John Smith")], [], version=1)
    org_import.import_snapshot(s, fs)
    assert s.get_entity("jsmith") is not None         # not auto-merged
    assert s.get_entity("john-smith") is not None


def test_restore_from_repoint_log_reattaches_after_curator_split(tmp_path):
    """A curator SPLIT: an id merged away by an earlier tombstone (repoint
    logged from_entity_id='joel-old' -> to_entity_id='joel-generic') later
    reappears as its own entity in a newer snapshot. The local flesh that had
    been re-pointed onto the merge target must move back onto the resurrected
    node."""
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    s = _store(tmp_path)
    _publish(fs, [_ent("joel-old", "Joel Old"), _ent("joel-generic", "Joel Generic")], [], version=1)
    org_import.import_snapshot(s, fs)
    with s._connect() as db:                         # local flesh attached to joel-old
        db.execute("INSERT INTO entities(id,name,type,origin) VALUES('doc','Doc','document','local')")
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b,origin) "
                   "VALUES('doc','mentioned_with','joel-old','local')")
    _publish(fs, [_ent("joel-generic", "Joel Generic")], [], version=2,
             tombstones=[Tombstone(entity_id="joel-old", merged_into="joel-generic")])
    org_import.import_snapshot(s, fs)
    assert s.get_entity("joel-old") is None          # merged away by the tombstone
    with s._connect() as db:
        row = db.execute("SELECT entity_b FROM entity_relations WHERE entity_a='doc'").fetchone()
    assert row["entity_b"] == "joel-generic"
    # v3: curator splits joel-old back out as its own entity
    _publish(fs, [_ent("joel-old", "Joel Old"), _ent("joel-generic", "Joel Generic")], [], version=3)
    org_import.import_snapshot(s, fs)
    resurrected = s.get_entity("joel-old")
    assert resurrected is not None and resurrected["origin"] == "org"
    with s._connect() as db:
        row = db.execute("SELECT entity_b FROM entity_relations WHERE entity_a='doc'").fetchone()
    assert row["entity_b"] == "joel-old"             # local ref moved back onto the resurrected node
