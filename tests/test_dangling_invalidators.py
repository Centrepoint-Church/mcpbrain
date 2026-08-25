"""Bi-temporal back-pointers must never outlive the row they point at.

`entity_relations.invalidated_by_relation_id` and its `entity_observations`
sibling record WHICH row superseded this one. Both now declare
`REFERENCES … ON DELETE SET NULL`, so a parent delete nulls its children and a
pointer to a nonexistent row is rejected outright — but the constraint only
does that while `foreign_keys=ON` is actually set on the connection, and it
reaches an existing store only via a rebuild. These tests pin the guarantee and
cover the one-time sweep for stores that already carry residue.
"""
import sqlite3

import pytest

from mcpbrain.store import Store


def _store(tmp_path):
    s = Store(str(tmp_path / "b.sqlite3"), 384)
    s.init()
    return s


def _two_entities(db):
    db.execute("INSERT INTO entities(id,name,type) VALUES('e1','A','person')")
    db.execute("INSERT INTO entities(id,name,type) VALUES('e2','B','org')")


def _seed_residue(store, relation="r_bad", target=999999):
    """Create a dangling pointer the way pre-FK code did: on a connection that
    never enabled foreign_keys.

    NOT via `PRAGMA foreign_keys=OFF` inside store._connect() -- that pragma is
    a NO-OP inside an open transaction, and _connect(write=True) has already
    begun one, so the insert is still rejected.
    """
    db = sqlite3.connect(store.path)
    try:
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b,"
                   "invalidated_by_relation_id) VALUES('e1',?,'e2',?)",
                   (relation, target))
        db.commit()
    finally:
        db.close()


def test_pointer_to_a_nonexistent_relation_is_rejected(tmp_path):
    s = _store(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        with s._connect(write=True) as db:
            _two_entities(db)
            db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b,"
                       "invalidated_by_relation_id) VALUES('e1','works_at','e2',999999)")


def test_deleting_the_invalidator_nulls_its_children(tmp_path):
    s = _store(tmp_path)
    with s._connect(write=True) as db:
        _two_entities(db)
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b) "
                   "VALUES('e1','works_at','e2')")
        parent = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b,"
                   "invalidated_by_relation_id) VALUES('e1','works_at_old','e2',?)",
                   (parent,))
        child = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.execute("DELETE FROM entity_relations WHERE id=?", (parent,))
    with s._connect() as db:
        assert db.execute("SELECT invalidated_by_relation_id FROM entity_relations "
                          "WHERE id=?", (child,)).fetchone()[0] is None


def test_count_dangling_invalidators_reports_residue(tmp_path):
    """Residue predates FK enforcement, so it can only be created with FKs off."""
    s = _store(tmp_path)
    with s._connect(write=True) as db:
        _two_entities(db)
    _seed_residue(s, "works_at")

    assert s.count_dangling_invalidators() == 1


def test_nullify_dangling_invalidators_clears_only_the_dangling(tmp_path):
    s = _store(tmp_path)
    with s._connect(write=True) as db:
        _two_entities(db)
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b) "
                   "VALUES('e1','works_at','e2')")
        good_parent = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b,"
                   "invalidated_by_relation_id) VALUES('e1','r_good','e2',?)",
                   (good_parent,))
    _seed_residue(s, "r_bad")

    n = s.nullify_dangling_invalidators()

    assert n == {"entity_relations.invalidated_by_relation_id": 1}
    with s._connect() as db:
        assert db.execute("SELECT invalidated_by_relation_id FROM entity_relations "
                          "WHERE relation='r_bad'").fetchone()[0] is None
        # the valid pointer is untouched
        assert db.execute("SELECT invalidated_by_relation_id FROM entity_relations "
                          "WHERE relation='r_good'").fetchone()[0] == good_parent
    assert s.count_dangling_invalidators() == 0


def test_nullify_is_idempotent(tmp_path):
    s = _store(tmp_path)
    with s._connect(write=True) as db:
        _two_entities(db)
    _seed_residue(s, "r")
    assert s.nullify_dangling_invalidators()
    assert s.nullify_dangling_invalidators() == {}


def _home_store(tmp_path):
    """A store at the exact path doctor probes: <home>/brain.sqlite3."""
    s = Store(str(tmp_path / "brain.sqlite3"), 384)
    s.init()
    return s


def test_doctor_reports_dangling_invalidators(tmp_path):
    """Recurrence should surface on its own, not require someone to go looking."""
    from mcpbrain import doctor
    s = _home_store(tmp_path)
    with s._connect(write=True) as db:
        _two_entities(db)
    _seed_residue(s, "works_at")

    _, msg = doctor.run_doctor(tmp_path, conns={}, repairs={}, offline=True)

    line = [ln for ln in msg.splitlines() if "dangling invalidator" in ln.lower()]
    assert line, msg
    assert "⚠️" in line[0] and "1" in line[0]


def test_doctor_is_quiet_when_there_is_no_residue(tmp_path):
    from mcpbrain import doctor
    s = _home_store(tmp_path)
    with s._connect(write=True) as db:
        _two_entities(db)

    _, msg = doctor.run_doctor(tmp_path, conns={}, repairs={}, offline=True)

    line = [ln for ln in msg.splitlines() if "dangling invalidator" in ln.lower()]
    assert line and "✅" in line[0]


def test_cli_reports_without_writing_unless_yes(tmp_path, capsys):
    """Report-only by default: a non-zero count on a rebuilt store is a bug
    signal that should be read before it is swept away."""
    import sys
    sys.path.insert(0, "bin")
    from optimise_store import main as optimise_main

    s = _home_store(tmp_path)
    with s._connect(write=True) as db:
        _two_entities(db)
    _seed_residue(s, "works_at")

    rc = optimise_main(["--src", str(tmp_path / "brain.sqlite3"), "--nullify-dangling"])
    assert rc == 0
    assert "report only" in capsys.readouterr().out.lower()
    assert s.count_dangling_invalidators() == 1, "report-only must not write"

    rc = optimise_main(["--src", str(tmp_path / "brain.sqlite3"),
                        "--nullify-dangling", "--yes"])
    assert rc == 0
    assert s.count_dangling_invalidators() == 0
