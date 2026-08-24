"""FK enforcement, STRICT tables, trigram index and the new partial indexes.

The brief's tests construct `Store(str(p))`; the real signature is
`Store(path, dim)` (dim has no default — a store's vector width is not
guessable), so every test here passes dim=4 like the rest of the suite does.
"""
import sqlite3

import pytest

from mcpbrain.store import Store, strict_supported


def test_foreign_keys_are_enforced(tmp_path):
    p = tmp_path / "b.sqlite3"
    s = Store(str(p), dim=4)
    s.init()
    with pytest.raises(sqlite3.IntegrityError):
        with s._connect(write=True) as db:
            db.execute("INSERT INTO email_entities(message_id, entity_id) "
                       "VALUES('m1','NO_SUCH_ENTITY')")


def test_strict_tables_reject_wrong_types(tmp_path):
    if not strict_supported():
        pytest.skip("SQLite < 3.37")
    p = tmp_path / "b.sqlite3"
    s = Store(str(p), dim=4)
    s.init()
    with pytest.raises(sqlite3.IntegrityError):
        with s._connect(write=True) as db:
            db.execute("INSERT INTO chunks(doc_id, text, embedded) "
                       "VALUES('d1','t','not-an-integer')")


def test_email_mentions_like_is_index_backed(tmp_path):
    """CLAUDE.md records this LIKE as unindexable; a trigram index fixes that."""
    p = tmp_path / "b.sqlite3"
    s = Store(str(p), dim=4)
    s.init()
    with s._connect() as db:
        plan = db.execute(
            "EXPLAIN QUERY PLAN SELECT rowid FROM fts_chunks_trigram "
            "WHERE fts_chunks_trigram MATCH 'byford'").fetchall()
    assert plan


# --- supporting coverage for the same change -------------------------------

def test_strict_supported_matches_runtime_version():
    parts = tuple(int(x) for x in sqlite3.sqlite_version.split(".")[:2])
    assert strict_supported() == (parts >= (3, 37))


def test_no_strict_table_declares_a_non_strict_type(tmp_path):
    """STRICT permits only INT/INTEGER/REAL/TEXT/BLOB/ANY.

    entity_observations.created_at was DATETIME, which SQLite accepts happily
    on a loose table and rejects outright on a STRICT one, so this pins the
    whole schema rather than that one column.
    """
    if not strict_supported():
        pytest.skip("SQLite < 3.37")
    p = tmp_path / "b.sqlite3"
    Store(str(p), dim=4).init()
    db = sqlite3.connect(p)
    try:
        rows = db.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' "
            "AND sql LIKE '%STRICT%'").fetchall()
        assert rows, "no STRICT tables were created"
        allowed = {"INT", "INTEGER", "REAL", "TEXT", "BLOB", "ANY"}
        for name, _sql in rows:
            for col in db.execute(f'PRAGMA table_info("{name}")').fetchall():
                assert (col[2] or "").upper() in allowed, (name, col[1], col[2])
    finally:
        db.close()


def test_invalidated_by_relation_id_is_a_real_foreign_key(tmp_path):
    """entity_observations.invalidated_by_observation_id already declared
    REFERENCES entity_observations(id) ON DELETE SET NULL from the start, but
    its sibling entity_relations.invalidated_by_relation_id (added by an older
    ALTER TABLE, before foreign_keys=ON existed) never got the matching
    REFERENCES clause -- so foreign_key_check could never see it, and 416
    dangling pointers accumulated silently on the live store (merge_entities/
    decay_relations delete relations without repointing back-pointers to
    them). This closes that asymmetry for new installs and rebuilt stores.
    """
    p = tmp_path / "b.sqlite3"
    s = Store(str(p), dim=4)
    s.init()
    with pytest.raises(sqlite3.IntegrityError):
        with s._connect(write=True) as db:
            db.execute(
                "INSERT INTO entities(id,name,type) VALUES('e1','A','person')")
            db.execute(
                "INSERT INTO entities(id,name,type) VALUES('e2','B','person')")
            db.execute(
                "INSERT INTO entity_relations(entity_a,relation,entity_b,"
                "invalidated_by_relation_id) VALUES('e1','knows','e2',999999)")


def test_deleting_the_invalidating_relation_nulls_the_back_pointer(tmp_path):
    """ON DELETE SET NULL, not the default NO ACTION or a CASCADE: deleting the
    relation that superseded another must not delete the superseded row too,
    and must not abort the delete either -- it just clears the now-dangling
    pointer, same policy as the entity_observations sibling column."""
    p = tmp_path / "b.sqlite3"
    s = Store(str(p), dim=4)
    s.init()
    with s._connect(write=True) as db:
        db.execute("INSERT INTO entities(id,name,type) VALUES('e1','A','person')")
        db.execute("INSERT INTO entities(id,name,type) VALUES('e2','B','person')")
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b) "
                   "VALUES('e1','knows','e2')")
        newer_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b,"
                   "invalidated_by_relation_id) VALUES('e1','knew','e2',?)",
                   (newer_id,))
        old_id = db.execute("SELECT id FROM entity_relations WHERE relation='knew'"
                            ).fetchone()[0]
        db.execute("DELETE FROM entity_relations WHERE id=?", (newer_id,))
    with s._connect() as db:
        row = db.execute(
            "SELECT invalidated_by_relation_id, id FROM entity_relations "
            "WHERE id=?", (old_id,)).fetchone()
        assert row is not None, "the superseded row itself must survive"
        assert row["invalidated_by_relation_id"] is None


def test_entity_delete_cascades_to_children(tmp_path):
    """merge_entities repoints relations/observations/email links before it
    deletes the loser, but nothing repoints entity_communities — CASCADE is
    what keeps that delete from failing (and from leaving a dangling row)."""
    p = tmp_path / "b.sqlite3"
    s = Store(str(p), dim=4)
    s.init()
    with s._connect(write=True) as db:
        db.execute("INSERT INTO entities(id,name,type) VALUES('e1','A','person')")
        db.execute("INSERT INTO email_entities(message_id,entity_id) VALUES('m1','e1')")
        db.execute("INSERT INTO entity_communities(entity_id,community_id,level) "
                   "VALUES('e1',1,0)")
        db.execute("INSERT INTO entity_observations(entity_id,attribute,value) "
                   "VALUES('e1','role','boss')")
        db.execute("DELETE FROM entities WHERE id='e1'")
    with s._connect() as db:
        for table in ("email_entities", "entity_communities", "entity_observations"):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, table


def _seed_chunks(s, n=200):
    """Enough rows (and few enough matches) that the planner's own stats prefer
    a partial index — same reasoning as test_metadata_jsonb's index test."""
    with s._connect(write=True) as db:
        for i in range(n):
            db.execute("INSERT INTO chunks(doc_id, text, metadata, embedded, enriched, "
                       "fts_context_version) VALUES(?,'t','{}',1,1,?)",
                       (f"d{i}", Store.FTS_CONTEXT_VERSION))
        db.execute("INSERT INTO chunks(doc_id, text, metadata, embedded, enriched, "
                   "fts_context_version) VALUES('u1','t','{}',0,0,0)")
    with s._connect() as db:
        db.execute("ANALYZE")


def _plan(s, sql, params=()):
    with s._connect() as db:
        return " | ".join(
            str(cell) for row in db.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
            for cell in tuple(row))


def test_unenriched_scan_uses_the_partial_index(tmp_path):
    """The enrichment backlog scan was a full SCAN of chunks.

    Asserts the plan shape, not just the index NAME: the index must also carry
    the ORDER BY rowid DESC, which it only does while rowid is its ONLY key
    column. A (enrich_state, rowid) key still names the index in the plan but
    adds USE TEMP B-TREE FOR ORDER BY — so a name-only assertion would pass
    with the worse shape.
    """
    s = Store(str(tmp_path / "b.sqlite3"), dim=4)
    s.init()
    _seed_chunks(s)
    # The real query, verbatim from unenriched_chunks().
    plan = _plan(s, "SELECT rowid,doc_id,text,metadata FROM chunks "
                    "WHERE enriched=0 AND COALESCE(enrich_state,'') != 'cold' "
                    "AND doc_id NOT LIKE 'enriched-%' ORDER BY rowid DESC LIMIT ?", (50,))
    assert "idx_chunks_unenriched" in plan, plan
    assert "TEMP B-TREE" not in plan.upper(), plan


def test_unembedded_scan_uses_the_partial_index(tmp_path):
    s = Store(str(tmp_path / "b.sqlite3"), dim=4)
    s.init()
    _seed_chunks(s)
    plan = _plan(s, "SELECT rowid,doc_id,text,metadata FROM chunks WHERE embedded=0 LIMIT ?",
                 (50,))
    assert "idx_chunks_unembedded" in plan, plan


def test_fts_reindex_selection_is_index_backed(tmp_path):
    """reindex_fts_batch's predicate must match idx_chunks_fts_stale's key.

    A COALESCE(fts_context_version,0) wrapper cannot be served by a
    plain-column index: it planned as a full SCAN while still paying the
    index's write cost on one of the hottest tables in the store. Same
    index/query-drift class as the 0.7.105 json_extract bug _meta_extract()
    exists to prevent.
    """
    s = Store(str(tmp_path / "b.sqlite3"), dim=4)
    s.init()
    _seed_chunks(s)
    plan = _plan(s, "SELECT rowid, text, metadata FROM chunks "
                    "WHERE embedded=1 AND fts_context_version < ? LIMIT ?",
                 (Store.FTS_CONTEXT_VERSION + 1, 5000))
    assert "idx_chunks_fts_stale" in plan, plan
    assert "SEARCH" in plan, plan
    # And that IS the predicate the store runs (the docstring explains the
    # COALESCE it must not use, so match the SQL fragment, not the whole source).
    import inspect
    assert "WHERE embedded=1 AND fts_context_version < ? LIMIT ?" in \
        inspect.getsource(Store.reindex_fts_batch), \
        "reindex_fts_batch's predicate must stay seekable by idx_chunks_fts_stale"
