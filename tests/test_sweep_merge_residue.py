"""bin/sweep_merge_residue.py — repoint rows left pointing at merged-away ids."""

import sqlite3

from mcpbrain.store import Store


def _raw(s):
    """A connection with foreign_keys OFF, for seeding dangling rows.

    Store._connect turns FKs ON at connect time, and `PRAGMA foreign_keys=OFF`
    inside an already-open transaction is a documented no-op — so a dangling row
    cannot be seeded through the store at all. A raw sqlite3 connection defaults
    to FKs off, which is also exactly how the real residue got written.
    """
    return sqlite3.connect(s.path if isinstance(s.path, str) else str(s.path))


def _store(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    return s


def _seed(s):
    s.upsert_entity("winner", "Winner", "person")
    s.upsert_entity("other", "Other", "person")
    with s._connect(write=True) as db:
        db.execute("INSERT INTO entity_merge_log(winner_id, loser_id, loser_name, "
                   "method, at) VALUES('winner','loser','Loser','deterministic','2026-07-07')")


def test_scan_finds_only_residue_for_losers_that_no_longer_exist(tmp_path):
    from bin import sweep_merge_residue
    s = _store(tmp_path)
    _seed(s)
    db = _raw(s)
    db.execute("INSERT INTO entity_relations(entity_a, relation, entity_b, strength) "
               "VALUES('loser','attended','other',3)")
    db.commit(); db.close()
    plan = sweep_merge_residue.scan(s)
    assert [(p["loser_id"], p["winner_id"]) for p in plan] == [("loser", "winner")]
    assert plan[0]["counts"]["entity_relations"] == 1


def test_apply_repoints_a_relation_onto_the_winner(tmp_path):
    from bin import sweep_merge_residue
    s = _store(tmp_path)
    _seed(s)
    db = _raw(s)
    db.execute("INSERT INTO entity_relations(entity_a, relation, entity_b, strength) "
               "VALUES('loser','attended','other',3)")
    db.commit(); db.close()
    sweep_merge_residue.apply(s, sweep_merge_residue.scan(s))
    with s._connect() as db:
        rows = db.execute("SELECT entity_a, entity_b FROM entity_relations").fetchall()
        assert [tuple(r) for r in rows] == [("winner", "other")]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_apply_drops_a_duplicate_instead_of_violating_the_unique_triple(tmp_path):
    """The winner may already carry the same edge — on the live store josh-kemp
    already had `attended campus-pastors` (strength 18) while the dead
    joshua-kemp carried its own copy. Repointing must not collide."""
    from bin import sweep_merge_residue
    s = _store(tmp_path)
    _seed(s)
    db = _raw(s)
    db.execute("INSERT INTO entity_relations(entity_a, relation, entity_b, strength) "
               "VALUES('winner','attended','other',18)")
    db.execute("INSERT INTO entity_relations(entity_a, relation, entity_b, strength) "
               "VALUES('loser','attended','other',3)")
    db.commit(); db.close()
    sweep_merge_residue.apply(s, sweep_merge_residue.scan(s))
    with s._connect() as db:
        rows = [tuple(r) for r in db.execute(
            "SELECT entity_a, entity_b, strength FROM entity_relations")]
        assert rows == [("winner", "other", 18)]      # winner's edge kept, dup gone
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_apply_repoints_email_entities_and_communities(tmp_path):
    """entity_communities is the one merge_entities never repoints — it relies on
    ON DELETE CASCADE, which cannot fire for a row inserted AFTER the entity was
    already gone (replace_communities did exactly that before FKs were enforced)."""
    from bin import sweep_merge_residue
    s = _store(tmp_path)
    _seed(s)
    db = _raw(s)
    db.execute("INSERT INTO email_entities(message_id, entity_id, role) "
               "VALUES('m1','loser','mentioned')")
    db.execute("INSERT INTO entity_communities(entity_id, community_id, level) "
               "VALUES('loser', 7, 0)")
    db.commit(); db.close()
    sweep_merge_residue.apply(s, sweep_merge_residue.scan(s))
    with s._connect() as db:
        assert [r[0] for r in db.execute("SELECT entity_id FROM email_entities")] == ["winner"]
        assert [r[0] for r in db.execute("SELECT entity_id FROM entity_communities")] == ["winner"]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_scan_is_empty_when_the_loser_still_exists(tmp_path):
    """A loser row that still exists is not residue — merge_entities owns that."""
    from bin import sweep_merge_residue
    s = _store(tmp_path)
    _seed(s)
    s.upsert_entity("loser", "Loser", "person")
    assert sweep_merge_residue.scan(s) == []
