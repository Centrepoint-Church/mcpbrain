import sqlite3

import pytest

from bin.optimise_store import (_copy_all, _populate_trigram, main, rebuild,
                                report_orphans)
from mcpbrain.store import Store


def test_report_orphans_counts_dangling_entity_refs(tmp_path):
    p = tmp_path / "b.sqlite3"
    db = sqlite3.connect(p)
    db.execute("CREATE TABLE entities(id TEXT PRIMARY KEY, name TEXT)")
    db.execute("CREATE TABLE email_entities(message_id TEXT, entity_id TEXT)")
    db.execute("INSERT INTO entities VALUES('e1','Real')")
    db.executemany("INSERT INTO email_entities VALUES(?,?)",
                   [("m1", "e1"), ("m2", "GONE"), ("m3", "GONE")])
    db.commit(); db.close()

    r = report_orphans(p)

    assert r["email_entities.entity_id"] == 2


def test_report_orphans_ignores_unset_nullable_refs(tmp_path):
    """Task 7 added a NULLABLE self-FK; a NULL is not an orphan.

    Without the `IS NOT NULL` guard the LEFT JOIN counts every unset pointer
    as dangling — on the live store that would report all 19,778
    entity_observations rows as orphans and the rebuild would drop them.
    """
    p = tmp_path / "b.sqlite3"
    s = Store(str(p), dim=4)
    s.init()
    with s._connect(write=True) as db:
        db.execute("INSERT INTO entities(id,name,type) VALUES('e1','A','person')")
        db.executemany(
            "INSERT INTO entity_observations(id,entity_id,attribute,value,"
            "invalidated_by_observation_id) VALUES(?,?,?,?,?)",
            [(1, "e1", "role", "boss", None),
             (2, "e1", "role", "chief", 1)])

    r = report_orphans(p)

    assert r["entity_observations.invalidated_by_observation_id"] == 0


# --- the two tests from the task brief -------------------------------------
# Store(path, dim) has no dim default (a store's vector width is not
# guessable), so every construction here passes dim=4, as tests/
# test_store_constraints.py does for the same reason.

def _seed(src):
    s = Store(str(src), dim=4)
    s.init()
    with s._connect(write=True) as db:
        db.execute("INSERT INTO entities(id,name,type) VALUES('e1','Real','person')")
        db.execute("INSERT INTO email_entities(message_id,entity_id) VALUES('m1','e1')")
        db.execute("INSERT INTO chunks(doc_id,text,metadata,embedded) "
                   "VALUES('d1','hello','{\"message_id\":\"m1\"}',0)")
    # embedded=1 has to MEAN "has a vector": backup._verify_artifact refuses to
    # snapshot a store where a sampled embedded chunk's vector does not resolve,
    # so the CLI tests below cannot use a store that merely claims one.
    s.write_embedding(1, [0.1, 0.2, 0.3, 0.4])
    # foreign_keys is ON now (Task 7), so an orphan can only be planted with
    # enforcement off — which is exactly the state the live store's rows were
    # written in.
    db = sqlite3.connect(src)
    db.execute("INSERT INTO email_entities(message_id,entity_id) VALUES('m2','GONE')")
    db.commit()
    db.close()
    return s


def test_rebuild_preserves_rows_and_drops_only_orphans(tmp_path):
    src, dst = tmp_path / "src.sqlite3", tmp_path / "dst.sqlite3"
    _seed(src)

    r = rebuild(src, dst)

    assert r["copied"]["chunks"] == 1
    assert r["copied"]["email_entities"] == 1
    assert r["dropped"]["email_entities.entity_id"] == 1
    assert r["dropped_rows"]["email_entities"] == 1


def test_rebuild_sets_the_larger_page_size(tmp_path):
    src, dst = tmp_path / "s.sqlite3", tmp_path / "d.sqlite3"
    Store(str(src), dim=4).init()
    rebuild(src, dst)
    db = sqlite3.connect(dst)
    try:
        assert db.execute("PRAGMA page_size").fetchone()[0] == 8192
    finally:
        db.close()


def test_rebuild_refuses_an_existing_destination(tmp_path):
    src, dst = tmp_path / "s.sqlite3", tmp_path / "d.sqlite3"
    Store(str(src), dim=4).init()
    dst.write_bytes(b"")
    with pytest.raises(FileExistsError):
        rebuild(src, dst)


# --- what Task 7 left for this task to handle ------------------------------

def test_rebuild_carries_tables_the_new_schema_no_longer_defines(tmp_path):
    """The live store has six such tables (areas, projects, bandit_arms,
    doc_context, suppressed_entities, enrich_payloads_legacy).

    INSERTing into them on the destination raises `no such table`, so they must
    be handled explicitly. They are recreated from the source's own DDL and
    copied: bandit_arms is live learned state and areas/projects are real user
    rows from a removed feature. Auditable via `carried`, never silent.
    """
    src, dst = tmp_path / "s.sqlite3", tmp_path / "d.sqlite3"
    Store(str(src), dim=4).init()
    db = sqlite3.connect(src)
    db.execute("CREATE TABLE bandit_arms(arm_value REAL PRIMARY KEY, alpha REAL)")
    db.execute("CREATE INDEX idx_ba_alpha ON bandit_arms(alpha)")
    db.execute("INSERT INTO bandit_arms VALUES(0.5, 2.0)")
    db.commit()
    db.close()

    r = rebuild(src, dst)

    assert r["carried"]["bandit_arms"] == 1
    assert "bandit_arms" not in r["copied"]
    out = sqlite3.connect(dst)
    try:
        assert out.execute("SELECT alpha FROM bandit_arms").fetchone()[0] == 2.0
        assert out.execute("SELECT count(*) FROM sqlite_master WHERE "
                           "type='index' AND name='idx_ba_alpha'").fetchone()[0] == 1
    finally:
        out.close()


def test_rebuild_reports_columns_the_new_schema_drops(tmp_path):
    """entity_relations.normalised_strength/.since exist on the live store with
    80,577 / 61,512 non-null values and no reader or writer left in the code.

    Dropping dead columns is part of what the rebuild is for; doing it without
    a count in the report is the silent truncation the plan forbids.
    """
    src, dst = tmp_path / "s.sqlite3", tmp_path / "d.sqlite3"
    Store(str(src), dim=4).init()
    db = sqlite3.connect(src)
    db.execute("ALTER TABLE entities ADD COLUMN legacy_score REAL")
    db.execute("INSERT INTO entities(id,name,type,legacy_score) "
               "VALUES('e1','A','person',0.5)")
    db.execute("INSERT INTO entities(id,name,type) VALUES('e2','B','person')")
    db.commit()
    db.close()

    r = rebuild(src, dst)

    assert r["dropped_columns"]["entities.legacy_score"] == 1
    assert r["copied"]["entities"] == 2


def test_rebuild_preserves_every_embedding(tmp_path):
    """vec_chunks is the ONE thing in this store that cannot be re-derived.

    Skipping the whole vec_chunks prefix (as the shadow-table rule alone would)
    leaves the live store's 170,695 chunks flagged embedded=1 with no vector —
    semantic recall silently empty, recoverable only by re-embedding the corpus.
    """
    src, dst = tmp_path / "s.sqlite3", tmp_path / "d.sqlite3"
    s = Store(str(src), dim=4)
    s.init()
    with s._connect(write=True) as db:
        db.execute("INSERT INTO chunks(doc_id,text,metadata,embedded) "
                   "VALUES('d1','hello','{}',0)")
    s.write_embedding(1, [0.1, 0.2, 0.3, 0.4])

    r = rebuild(src, dst)

    assert r["vectors"] == 1
    assert r["requeued_embeddings"] == 0
    out = Store(str(dst), dim=4)
    with out._connect() as db:
        row = db.execute("SELECT rowid, embedding FROM vec_chunks").fetchone()
        assert row["rowid"] == 1
        src_db = Store(str(src), dim=4)
        with src_db._connect() as sdb:
            assert row["embedding"] == sdb.execute(
                "SELECT embedding FROM vec_chunks").fetchone()["embedding"]
        assert db.execute("SELECT embedded FROM chunks").fetchone()[0] == 1


def test_rebuild_requeues_a_chunk_whose_vector_is_missing(tmp_path):
    """embedded=1 must MEAN "has a vector"; a flag that lies is worse than
    re-embedding one chunk."""
    src, dst = tmp_path / "s.sqlite3", tmp_path / "d.sqlite3"
    s = Store(str(src), dim=4)
    s.init()
    with s._connect(write=True) as db:
        db.execute("INSERT INTO chunks(doc_id,text,metadata,embedded) "
                   "VALUES('d1','hello','{}',1)")   # claims a vector, has none

    r = rebuild(src, dst)

    assert r["requeued_embeddings"] == 1
    out = sqlite3.connect(dst)
    try:
        assert out.execute("SELECT embedded FROM chunks").fetchone()[0] == 0
    finally:
        out.close()


def test_rederived_fts_covers_every_chunk_with_the_prefix_off(monkeypatch):
    """reindex_fts_batch's selection cannot drive this loop.

    It picks rows by `fts_context_version < FTS_CONTEXT_VERSION` and re-stamps
    version 0 for a row written WITHOUT the contextual prefix — correct for it,
    but with the flag OFF the same first `cap` rows come back forever and every
    later chunk never gets an FTS row. A rowid cursor covers each row once,
    under either flag state.
    """
    import mcpbrain.config as cfg
    monkeypatch.setattr(cfg, "contextual_retrieval_enabled", lambda *a, **k: False)
    import tempfile
    from pathlib import Path as _P
    with tempfile.TemporaryDirectory() as td:
        src, dst = _P(td) / "s.sqlite3", _P(td) / "d.sqlite3"
        s = Store(str(src), dim=4)
        s.init()
        with s._connect(write=True) as db:
            for i in range(25):
                db.execute("INSERT INTO chunks(doc_id,text,metadata,embedded) "
                           "VALUES(?,?,'{}',0)", (f"d{i}", f"body {i}"))
        for i in range(1, 26):
            s.write_embedding(i, [0.1, 0.2, 0.3, 0.4])

        r = rebuild(src, dst)   # must terminate, and cover all 25

        assert r["fts_rows"] == 25
        out = sqlite3.connect(dst)
        try:
            assert out.execute("SELECT count(*) FROM fts_chunks").fetchone()[0] == 25
        finally:
            out.close()


def test_rebuild_leaves_the_trigram_index_empty_by_default(tmp_path):
    """fts_chunks_trigram has neither a writer nor a reader (email_mentions
    still scans chunks.text). Filling it would be correct only until the next
    write, then drift while LOOKING ready — so the default reports it empty.
    """
    src, dst = tmp_path / "s.sqlite3", tmp_path / "d.sqlite3"
    s = Store(str(src), dim=4)
    s.init()
    with s._connect(write=True) as db:
        db.execute("INSERT INTO chunks(doc_id,text,metadata,embedded) "
                   "VALUES('d1','byford road','{}',1)")

    r = rebuild(src, dst)

    assert r["trigram_rows"] == 0
    out = sqlite3.connect(dst)
    try:
        assert out.execute(
            "SELECT count(*) FROM fts_chunks_trigram").fetchone()[0] == 0
    finally:
        out.close()


def test_populate_trigram_indexes_raw_text_when_asked(tmp_path):
    """The opt-in path, for whoever lands the writer + reader. Indexes RAW
    chunks.text — email_mentions matches the body a user wrote, so a
    synthesised contextual prefix would produce phantom hits."""
    src, dst = tmp_path / "s.sqlite3", tmp_path / "d.sqlite3"
    s = Store(str(src), dim=4)
    s.init()
    with s._connect(write=True) as db:
        db.execute("INSERT INTO chunks(doc_id,text,metadata,embedded) "
                   "VALUES('d1','byford road','{}',1)")

    r = rebuild(src, dst, populate_trigram=True)

    assert r["trigram_rows"] == 1
    out = Store(str(dst), dim=4)
    with out._connect() as db:
        hit = db.execute("SELECT rowid FROM fts_chunks_trigram "
                         "WHERE fts_chunks_trigram MATCH 'byford'").fetchone()
        assert hit["rowid"] == 1


def test_populate_trigram_is_not_run_by_the_default_rebuild(tmp_path):
    """Guard on the DEFAULT, not just on the count: a future edit that wires
    _populate_trigram into rebuild() unconditionally must fail here."""
    src, dst = tmp_path / "s.sqlite3", tmp_path / "d.sqlite3"
    Store(str(src), dim=4).init()
    calls = []
    import bin.optimise_store as mod
    real = mod._populate_trigram
    mod._populate_trigram = lambda *a, **k: (calls.append(1), real(*a, **k))[1]
    try:
        rebuild(src, dst)
    finally:
        mod._populate_trigram = real
    assert calls == []


def test_rebuild_nullifies_a_dangling_self_reference(tmp_path):
    """The column declares ON DELETE SET NULL, so the rebuild repairs a
    dangling pointer the same way — it does NOT drop the observation, which
    would lose good data and cascade. Left unrepaired, foreign_key_check on
    the result would fail and the rebuild could not be swapped in.
    """
    src, dst = tmp_path / "s.sqlite3", tmp_path / "d.sqlite3"
    s = Store(str(src), dim=4)
    s.init()
    with s._connect(write=True) as db:
        db.execute("INSERT INTO entities(id,name,type) VALUES('e1','A','person')")
        db.execute("INSERT INTO entity_observations(id,entity_id,attribute,value) "
                   "VALUES(1,'e1','role','boss')")
    db = sqlite3.connect(src)   # foreign_keys OFF: plant the dangling pointer
    db.execute("INSERT INTO entity_observations(id,entity_id,attribute,value,"
               "invalidated_by_observation_id) VALUES(2,'e1','role','chief',999)")
    db.commit()
    db.close()

    r = rebuild(src, dst)

    assert r["nullified"]["entity_observations.invalidated_by_observation_id"] == 1
    assert r["copied"]["entity_observations"] == 2   # kept, not dropped
    out = Store(str(dst), dim=4)
    with out._connect() as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT invalidated_by_observation_id FROM "
                          "entity_observations WHERE id=2").fetchone()[0] is None


def test_rebuild_result_passes_foreign_key_check(tmp_path):
    src, dst = tmp_path / "s.sqlite3", tmp_path / "d.sqlite3"
    _seed(src)
    rebuild(src, dst)
    out = Store(str(dst), dim=4)
    with out._connect() as db:
        assert [r[0] for r in db.execute("PRAGMA integrity_check")] == ["ok"]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_copy_all_skips_only_derived_and_internal_tables(tmp_path):
    src, dst = tmp_path / "s.sqlite3", tmp_path / "d.sqlite3"
    _seed(src)
    Store(str(dst), dim=4).init()
    out = _copy_all(src, dst)
    assert all(t.startswith(("fts_chunks", "vec_chunks", "sqlite_"))
               for t in out["skipped"]), out["skipped"]
    assert "chunks" in out["copied"]


def test_populate_trigram_terminates_on_an_empty_store(tmp_path):
    p = tmp_path / "s.sqlite3"
    Store(str(p), dim=4).init()
    assert _populate_trigram(p, 4) == 0


# --- CLI safety gates ------------------------------------------------------

def _cli_store(tmp_path):
    src = tmp_path / "brain.sqlite3"
    _seed(src)
    return src


def test_main_refuses_while_another_holds_the_lock_and_takes_no_snapshot(tmp_path):
    """GATE 1 comes before GATE 2: a refused run must not have written
    anything at all, snapshot included."""
    from mcpbrain.daemon import SingleWriterLock
    src = _cli_store(tmp_path)
    held = SingleWriterLock(tmp_path / "brain.sqlite3.rebuild.lock")
    held.acquire()
    try:
        rc = main(["--src", str(src), "--home", str(tmp_path), "--yes"])
    finally:
        held.release()
    assert rc == 2
    assert not list(tmp_path.glob("*.enc"))
    assert not (tmp_path / "brain.sqlite3.new").exists()


def test_main_snapshots_and_reports_but_does_not_rebuild_without_yes(tmp_path):
    """GATE 2 (verified snapshot) precedes GATE 3 (the --yes consent gate),
    so a report-only run still leaves a proven rollback behind."""
    src = _cli_store(tmp_path)
    rc = main(["--src", str(src), "--home", str(tmp_path)])
    assert rc == 0
    assert len(list(tmp_path.glob("*.enc"))) == 1
    assert (tmp_path / "brain.sqlite3.rebuild-key").exists()
    assert not (tmp_path / "brain.sqlite3.new").exists()


def test_main_rebuilds_to_dot_new_and_never_swaps(tmp_path):
    """GATES 4-7: the rebuild lands beside the store, is verified, and the
    live file is left exactly where it was."""
    src = _cli_store(tmp_path)
    before = src.read_bytes()
    rc = main(["--src", str(src), "--home", str(tmp_path), "--yes"])
    assert rc == 0
    assert (tmp_path / "brain.sqlite3.new").exists()
    assert src.read_bytes() == before


def test_main_swap_requires_yes_and_retains_the_old_file(tmp_path):
    src = _cli_store(tmp_path)
    assert main(["--src", str(src), "--home", str(tmp_path), "--yes"]) == 0
    assert main(["--src", str(src), "--home", str(tmp_path), "--swap"]) == 2
    assert (tmp_path / "brain.sqlite3.new").exists()   # not swapped

    assert main(["--src", str(src), "--home", str(tmp_path),
                 "--swap", "--yes"]) == 0
    assert not (tmp_path / "brain.sqlite3.new").exists()
    # The -wal/-shm sidecars follow the file they belong to, so glob the DB only.
    kept = [p for p in tmp_path.glob("brain.sqlite3.pre-rebuild-*")
            if not p.name.endswith(("-wal", "-shm"))]
    assert len(kept) == 1, kept
    assert sqlite3.connect(kept[0]).execute(
        "SELECT count(*) FROM email_entities").fetchone()[0] == 2  # orphan intact


def test_main_refuses_a_missing_store(tmp_path):
    assert main(["--src", str(tmp_path / "nope.sqlite3"),
                 "--home", str(tmp_path)]) == 2
