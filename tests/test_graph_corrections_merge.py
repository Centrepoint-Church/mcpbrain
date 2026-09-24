"""Merge honours distinct pairs; merge -> unmerge round-trips (Task 3)."""
import pytest

from mcpbrain import graph_view, resolve
from mcpbrain.store import Store, _merge_entities_tx, _unmerge_tx

L, W, C = "dana-okafor", "dana-okafor-2", "priya-anand"


def _store(tmp_path):
    s = Store(tmp_path / "g.sqlite3", dim=4)
    s.init()
    with s._connect(write=True) as db:
        db.execute("INSERT INTO entities(id,name,type,org,mentions,email_addr,notes) VALUES"
                   "(?, 'Dana Okafor', 'person', 'Northgate Trust', 3, 'dana@northgate.example', 'n1')", (L,))
        db.execute("INSERT INTO entities(id,name,type,org,mentions,degree) VALUES"
                   "(?, 'Dana Okafor', 'person', '', 9, 5)", (W,))
        db.execute("INSERT INTO entities(id,name,type) VALUES(?, 'Priya Anand', 'person')", (C,))
        db.execute("INSERT INTO entities(id,name,type) VALUES('northgate-trust','Northgate Trust','org')")
        # loser-only relation, a collision with a winner triple, and a self-loop-to-be
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b) VALUES(?, 'knows', ?)", (L, C))
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b,user_verdict,invalidated_at) "
                   "VALUES(?, 'works_at', 'northgate-trust', 'rejected', 'x')", (L,))
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b) VALUES(?, 'works_at', 'northgate-trust')", (W,))
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b) VALUES(?, 'mentioned_with', ?)", (L, W))
        db.execute("INSERT INTO entity_observations(entity_id,attribute,value,source,valid_from) "
                   "VALUES(?, 'role', 'Pastor', 'manual', '2026-01-01')", (L,))
        db.execute("INSERT INTO email_entities(message_id,entity_id,role) VALUES('m1', ?, 'from')", (L,))
        db.execute("INSERT INTO email_entities(message_id,entity_id,role) VALUES('m2', ?, 'to')", (L,))
        db.execute("INSERT INTO email_entities(message_id,entity_id,role) VALUES('m2', ?, 'to')", (W,))
        db.execute("INSERT INTO entity_suppressions(entity_id,reason) VALUES(?, 'junk')", (L,))
        db.execute("INSERT INTO entity_field_locks(entity_id,field) VALUES(?, 'org')", (L,))
    return s


def _state(s):
    with s._connect() as db:
        q = lambda sql: sorted(tuple(r) for r in db.execute(sql))
        return {
            "entities": q("SELECT * FROM entities ORDER BY id"),
            "relations": q("SELECT * FROM entity_relations"),
            "observations": q("SELECT * FROM entity_observations"),
            "emails": q("SELECT * FROM email_entities"),
            "suppressions": q("SELECT * FROM entity_suppressions"),
            "pairs": q("SELECT * FROM entity_distinct_pairs"),
            "locks": q("SELECT * FROM entity_field_locks"),
        }


def test_merge_then_unmerge_round_trips_exactly(tmp_path):
    s = _store(tmp_path)
    before = _state(s)
    with s._connect(write=True) as db:
        snap = _merge_entities_tx(db, L, W, method="user")
    assert s.get_entity(L) is None
    with s._connect(write=True) as db:
        _unmerge_tx(db, snap)
    assert _state(s) == before
    with s._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM entity_merge_log").fetchone()[0] == 0


def test_merge_carries_rejection_onto_the_surviving_triple(tmp_path):
    s = _store(tmp_path)
    s.merge_entities(L, W)
    with s._connect() as db:
        row = db.execute("SELECT user_verdict, invalidated_at FROM entity_relations "
                         "WHERE entity_a=? AND relation='works_at'", (W,)).fetchone()
    assert row["user_verdict"] == "rejected" and row["invalidated_at"] is not None


def test_unmerge_refuses_when_winner_was_merged_again(tmp_path):
    s = _store(tmp_path)
    with s._connect(write=True) as db:
        snap = _merge_entities_tx(db, L, W, method="user")
    s.merge_entities(W, C)  # the winner itself is folded away
    with s._connect(write=True) as db, pytest.raises(ValueError, match="no longer exists"):
        _unmerge_tx(db, snap)


def test_merge_repoints_distinct_pairs(tmp_path):
    s = _store(tmp_path)
    with s._connect(write=True) as db:
        db.execute("INSERT INTO entity_distinct_pairs(a,b) VALUES(?,?)", tuple(sorted((L, C))))
    s.merge_entities(L, W)
    assert s.is_distinct_pair(W, C)


def _mark_distinct(s, a, b):
    with s._connect(write=True) as db:
        db.execute("INSERT INTO entity_distinct_pairs(a,b) VALUES(?,?)", tuple(sorted((a, b))))


def test_deterministic_merge_skips_distinct_pair(tmp_path):
    s = _store(tmp_path)  # L and W share a canonical key ("Dana Okafor")
    _mark_distinct(s, L, W)
    assert resolve._deterministic_merges(s) == 0
    assert s.get_entity(L) and s.get_entity(W)


def test_candidate_pairs_skip_distinct_pair(tmp_path):
    ents = [{"id": "a", "name": "Dana Okafor", "type": "person"},
            {"id": "b", "name": "Dana J Okafor", "type": "person"}]
    assert resolve._candidate_pairs(ents, distinct={("a", "b")}) == []
    assert len(resolve._candidate_pairs(ents)) == 1


def test_email_equality_merge_skips_distinct_pair(tmp_path, monkeypatch):
    from mcpbrain import config
    s = _store(tmp_path)
    with s._connect(write=True) as db:
        db.execute("UPDATE entities SET email_addr='dana@northgate.example' WHERE id=?", (W,))
    _mark_distinct(s, L, W)
    monkeypatch.setattr(config, "write_time_dedup_enabled", lambda home: True)
    assert resolve._email_equality_merges(s, home=tmp_path) == 0


def test_duplicate_verdict_applier_guards_distinct_pair(tmp_path):
    from mcpbrain.review_apply import apply_duplicate_verdicts
    s = _store(tmp_path)
    _mark_distinct(s, L, W)
    out = apply_duplicate_verdicts(s, [{"pair_id": "|".join(sorted((L, W))), "same": True}], cap=5)
    assert out["merged"] == 0 and out["guarded"] == 1


def test_graph_ui_refuses_distinct_pair(tmp_path):
    s = _store(tmp_path)
    _mark_distinct(s, L, W)
    out = graph_view.merge_entities(s, L, W)
    assert out == {"ok": False, "error": "marked_distinct",
                   "message": "These were marked as different entities. Undo that correction first."}


def test_round_trip_restores_invalidation_pointers_into_deleted_relations(tmp_path):
    """Deviation from the brief, proven here: a relation whose
    invalidated_by_relation_id points at a loser relation the merge DELETES
    (collision or self-loop) has that pointer SET NULL by the FK. The brief's
    snapshot captured only the loser's own relations, so unmerge left the
    pointer NULL and the round trip was not exact."""
    s = _store(tmp_path)
    with s._connect(write=True) as db:
        dead = db.execute("SELECT id FROM entity_relations WHERE entity_a=? "
                          "AND relation='works_at'", (L,)).fetchone()[0]
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b,invalidated_at,"
                   "invalidated_by_relation_id) VALUES(?, 'works_at', 'northgate-trust', 'y', ?)",
                   (C, dead))
    before = _state(s)
    with s._connect(write=True) as db:
        snap = _merge_entities_tx(db, L, W, method="user")
    with s._connect(write=True) as db:
        _unmerge_tx(db, snap)
    assert _state(s) == before
