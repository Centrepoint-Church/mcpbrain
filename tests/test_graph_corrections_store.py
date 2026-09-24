"""Schema + read helpers for graph corrections (Task 1)."""
import json

from mcpbrain.store import Store


def _store(tmp_path):
    s = Store(tmp_path / "g.sqlite3", dim=4)
    s.init()
    return s


def _cols(s, table):
    with s._connect() as db:
        return {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}


def test_init_creates_correction_tables_and_verdict_column(tmp_path):
    s = _store(tmp_path)
    assert {"id", "op", "basis", "status", "payload", "snapshot", "reason",
            "confirmed_via", "dedup_key", "error", "created_at", "applied_at",
            "reverted_at", "change_log_id"} <= _cols(s, "graph_corrections")
    assert {"a", "b"} <= _cols(s, "entity_distinct_pairs")
    assert {"entity_id", "field"} <= _cols(s, "entity_field_locks")
    assert "user_verdict" in _cols(s, "entity_relations")


def test_init_is_idempotent(tmp_path):
    s = _store(tmp_path)
    s.init()  # second run must not raise
    assert "user_verdict" in _cols(s, "entity_relations")


def _seed(s):
    with s._connect(write=True) as db:
        for eid, name in (("dana-okafor", "Dana Okafor"), ("marcus-reyes", "Marcus Reyes")):
            db.execute("INSERT INTO entities(id,name,type) VALUES(?,?,'person')", (eid, name))


def test_get_and_pending_corrections_decode_json(tmp_path):
    s = _store(tmp_path)
    with s._connect(write=True) as db:
        db.execute(
            "INSERT INTO graph_corrections(op,basis,status,payload,snapshot,dedup_key) "
            "VALUES('hide','inferred','pending',?,'{}','hide:[\"x\"]')",
            (json.dumps({"entity_id": "x"}),))
    rows = s.pending_corrections()
    assert len(rows) == 1 and rows[0]["payload"] == {"entity_id": "x"}
    assert s.get_correction(rows[0]["id"])["snapshot"] == {}
    assert s.get_correction(999) is None


def test_distinct_pairs_and_locks(tmp_path):
    s = _store(tmp_path)
    _seed(s)
    with s._connect(write=True) as db:
        db.execute("INSERT INTO entity_distinct_pairs(a,b) VALUES('dana-okafor','marcus-reyes')")
        db.execute("INSERT INTO entity_field_locks(entity_id,field) VALUES('dana-okafor','org')")
    assert s.is_distinct_pair("marcus-reyes", "dana-okafor")  # order-insensitive
    assert s.distinct_pair_set() == {("dana-okafor", "marcus-reyes")}
    assert s.locked_fields("dana-okafor") == {"org"}
    assert s.locked_fields("marcus-reyes") == set()


def test_pairs_and_locks_cascade_with_entity(tmp_path):
    s = _store(tmp_path)
    _seed(s)
    with s._connect(write=True) as db:
        db.execute("INSERT INTO entity_distinct_pairs(a,b) VALUES('dana-okafor','marcus-reyes')")
        db.execute("INSERT INTO entity_field_locks(entity_id,field) VALUES('dana-okafor','org')")
        db.execute("DELETE FROM entities WHERE id='dana-okafor'")  # admin-delete-ok
    assert s.distinct_pair_set() == set()
    assert s.locked_fields("dana-okafor") == set()
