"""68,193 of 196,396 live chunks (34.7%) contain no alphanumeric character at
all — ~2,000-char strings of '| | | | |' from empty spreadsheet cells, every one
embedded, none matchable by any query. 67,210 of them share a single
content_hash, which alone accounts for 63% of the store's 106,357 redundant
copies.
"""
from mcpbrain.store import Store


def _store(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    return s


def _cite(store, doc_id):
    """A relation citing `doc_id` as its provenance.

    entity_relations.entity_a/entity_b are real foreign keys into entities (and
    foreign_keys is ON), so the cited relation needs its two endpoints to exist
    -- a citation from nowhere is not a state the store can hold.
    """
    with store._connect(write=True) as db:
        db.execute("INSERT OR IGNORE INTO entities(id,name,type) VALUES('e1','E One','person')")
        db.execute("INSERT OR IGNORE INTO entities(id,name,type) VALUES('e2','E Two','person')")
        db.execute("INSERT INTO entity_relations"
                   "(entity_a,relation,entity_b,source_doc_id,valid_from) "
                   "VALUES('e1','mentioned_with','e2',?,'2026-01-01')", (doc_id,))


def test_content_free_selection_finds_pipes_and_spares_real_content(tmp_path):
    store = _store(tmp_path)
    store.upsert_chunk("d-pipes", "|  |  |  |\n|  |  |  |", "h1", {})
    store.upsert_chunk("d-sep", "| --- | --- |", "h2", {})
    store.upsert_chunk("d-real", "| Rent | 500 |", "h3", {})
    store.upsert_chunk("d-cjk", "| 会議 |", "h4", {})

    doomed = set(store.content_free_doc_ids(limit=100))

    assert doomed == {"d-pipes", "d-sep"}
    assert store.count_content_free() == 2


def test_purge_clears_the_vector_and_fts_mirrors(tmp_path):
    """The findings register is explicit that a purge 'must clear the matching
    vector and FTS rows'. A chunk row deleted while its vec_chunks row survives
    leaves a dangling embedding the kNN arm can still return.

    Note: write_embedding takes a chunk ROWID, not a doc_id — the rowid is
    looked up via unembedded_chunks() (upsert_chunk leaves embedded=0). The
    fts_chunks mirror is a bare `fts5(text)` virtual table with no doc_id
    column, so the mirror check asserts total row count instead (this test
    only ever writes the one chunk, so that's equivalent and dodges
    "no such column: doc_id").
    """
    store = _store(tmp_path)
    store.upsert_chunk("d1", "|  |  |", "h1", {})
    rowid = store.unembedded_chunks()[0]["rowid"]
    store.write_embedding(rowid, [0.1, 0.2, 0.3, 0.4])

    assert store.purge_doc_ids(["d1"]) == 1

    assert store.get_chunk("d1") is None
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM vec_chunks").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM fts_chunks").fetchone()[0] == 0


def test_purge_refuses_a_doc_id_the_graph_cites(tmp_path):
    """store.delete_chunks deliberately does NOT touch graph rows ('invalidation
    is a separate, bitemporal step'), so deleting a cited chunk leaves dangling
    provenance. Measured on the live store: ZERO of the 68,193 content-free
    chunks are cited. This asserts that at runtime rather than trusting the
    measurement — if a future chunk shape ever gets cited, the purge must stop,
    not silently orphan the graph."""
    store = _store(tmp_path)
    store.upsert_chunk("d-cited", "|  |  |", "h1", {})
    _cite(store, "d-cited")

    import pytest
    with pytest.raises(ValueError, match="cited"):
        store.purge_doc_ids(["d-cited"])

    assert store.get_chunk("d-cited") is not None, "nothing may be deleted on refusal"


def test_purge_is_all_or_nothing(tmp_path):
    """A partial purge would leave the caller unable to say what happened."""
    store = _store(tmp_path)
    store.upsert_chunk("d-ok", "|  |", "h1", {})
    store.upsert_chunk("d-cited", "|  |", "h2", {})
    _cite(store, "d-cited")

    import pytest
    with pytest.raises(ValueError):
        store.purge_doc_ids(["d-ok", "d-cited"])

    assert store.get_chunk("d-ok") is not None
