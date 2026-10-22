"""Schema for ingestion integrity: source classes, per-source support, undo log."""

import sqlite3

from mcpbrain.store import Store, _meta_extract


def _store(tmp_path):
    s = Store(tmp_path / "t.db", dim=4)
    s.init()
    return s


def _cols(path, table):
    c = sqlite3.connect(path)
    try:
        return {r[1]: r for r in c.execute(f"PRAGMA table_info({table})")}
    finally:
        c.close()


def test_fresh_init_has_columns_tables_and_index(tmp_path):
    _store(tmp_path)
    p = tmp_path / "t.db"
    assert "dup_of" in _cols(p, "chunks")
    assert _cols(p, "email_entities")["derived"][3] == 1  # NOT NULL
    for t in ("email_entities", "entity_relations", "entity_observations"):
        assert _cols(p, t)["derived"][4] == "0"
    assert "legacy_strength" in _cols(p, "entity_relations")
    assert {"prior_thread_id", "prior_owner"} <= set(_cols(p, "actions"))
    assert set(_cols(p, "relation_sources")) == {
        "relation_id", "source_key", "source_class", "first_seen"}
    assert set(_cols(p, "observation_sources")) == {
        "observation_id", "source_key", "source_class", "first_seen"}
    assert set(_cols(p, "cleanup_log")) == {
        "id", "step", "table_name", "row_key", "column_name", "old_value", "at"}
    c = sqlite3.connect(p)
    names = {r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    c.close()
    assert "idx_chunks_noteid" in names


def test_reinit_is_noop_and_noteid_index_usable(tmp_path):
    _store(tmp_path)
    s = _store(tmp_path)  # second init on initialised store
    p = tmp_path / "t.db"
    c = sqlite3.connect(p)
    plan = " ".join(r[3] for r in c.execute(
        "EXPLAIN QUERY PLAN SELECT doc_id FROM chunks WHERE "
        f"{_meta_extract('$.note_id')} = ?", ("x",)))
    c.close()
    assert "idx_chunks_noteid" in plan
    assert s is not None


def test_relation_sources_cascade_on_relation_delete(tmp_path):
    s = _store(tmp_path)
    with s._connect(write=True) as db:
        for e in ("a", "b"):
            db.execute("INSERT INTO entities(id, name, type) VALUES(?,?,'person')",
                       (e, e))
        db.execute("INSERT INTO entity_relations(id, entity_a, entity_b, relation) "
                   "VALUES(1,'a','b','knows')")
        db.execute("INSERT INTO relation_sources VALUES(1,'k','primary_mail','t')")
        db.execute("DELETE FROM entity_relations WHERE id=1")
        n = db.execute("SELECT COUNT(*) FROM relation_sources").fetchone()[0]
    assert n == 0
