import sqlite3
from pathlib import Path

import pytest

from bin.optimise_store import (UnmigratedStore, _copy_all, _populate_trigram,
                                check_migrations, embedded_without_vectors,
                                main, rebuild, report_orphans)
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
    kept = _main_files(tmp_path, "brain.sqlite3.pre-rebuild-*")
    assert len(kept) == 1, kept
    assert sqlite3.connect(kept[0]).execute(
        "SELECT count(*) FROM email_entities").fetchone()[0] == 2  # orphan intact


def test_main_refuses_a_missing_store(tmp_path):
    assert main(["--src", str(tmp_path / "nope.sqlite3"),
                 "--home", str(tmp_path)]) == 2


# --- the -wal hazard: a store is THREE files, not one ----------------------

def _main_files(d, pattern):
    """Glob, excluding sidecars -- they follow the file they belong to."""
    return [p for p in d.glob(pattern) if not p.name.endswith(("-wal", "-shm"))]


def _crash_left_wal(path, sql):
    """Commit `sql` and leave a real, non-empty `-wal` on disk afterwards.

    In WAL mode the last connection to close checkpoints and DELETES the -wal,
    so a stray one only exists after a crash (or after this tool moves one
    aside). Reproduced faithfully: hold a second connection open while writing
    so the WAL is live, capture its bytes, then put them back once everything
    has closed. SQLite replays that file on the next open, silently.
    """
    from pathlib import Path as _P
    keeper = sqlite3.connect(path)
    keeper.execute("PRAGMA journal_mode=WAL")
    keeper.execute("SELECT count(*) FROM chunks").fetchone()   # hold a read
    w = sqlite3.connect(path)
    w.execute("PRAGMA journal_mode=WAL")
    w.execute(sql)
    w.commit()
    w.close()
    wal = _P(f"{path}-wal").read_bytes()
    keeper.close()
    assert wal, "no WAL content captured -- the simulation is not testing anything"
    _P(f"{path}-wal").write_bytes(wal)
    return wal


def test_a_stray_wal_really_does_rewrite_a_store(tmp_path):
    """The hazard itself, pinned. If SQLite ever stops replaying a foreign WAL
    this test tells us the sidecar handling below is belt-only."""
    p = tmp_path / "s.sqlite3"
    _seed(p)
    _crash_left_wal(p, "INSERT INTO entities(id,name,type) "
                       "VALUES('x','LATER','person')")
    db = sqlite3.connect(p)
    try:
        assert db.execute("SELECT count(*) FROM entities "
                          "WHERE name='LATER'").fetchone()[0] == 1
    finally:
        db.close()


def test_rollback_does_not_replay_the_promoted_stores_wal(tmp_path):
    """C1. `mv <kept> <src>` -- the instruction this tool used to print -- is
    silent corruption.

    By rollback time the daemon has run against the PROMOTED store, so
    `<src>-wal` belongs to the new store. Moving only the main file back leaves
    it there and SQLite replays it into the old file: no error, integrity_check
    still `ok`, the old content gone and the page size flipped to the new
    store's. So `--rollback` moves the current store aside WITH its sidecars
    and restores the retained store WITH its own.
    """
    src = tmp_path / "brain.sqlite3"
    s = _seed(src)
    with s._connect(write=True) as db:
        db.execute("INSERT INTO entities(id,name,type) VALUES('old','OLD-ONLY','person')")
    old_page_size = sqlite3.connect(src).execute("PRAGMA page_size").fetchone()[0]
    assert old_page_size == 4096, "the old store must differ from the rebuild's 8192"

    assert main(["--src", str(src), "--home", str(tmp_path), "--yes"]) == 0
    assert main(["--src", str(src), "--home", str(tmp_path), "--swap", "--yes"]) == 0
    assert sqlite3.connect(src).execute("PRAGMA page_size").fetchone()[0] == 8192

    # The daemon writes to the promoted store and leaves its WAL behind.
    _crash_left_wal(src, "INSERT INTO entities(id,name,type) "
                         "VALUES('new','WRITTEN-AFTER-SWAP','person')")
    assert (tmp_path / "brain.sqlite3-wal").exists()

    assert main(["--src", str(src), "--home", str(tmp_path),
                 "--rollback", "--yes"]) == 0

    # Read the sidecar state FIRST, before anything opens the database:
    # opening it checkpoints and deletes a stray -wal, which would hide the
    # very thing under test.
    #
    # And assert the INVARIANT (no foreign sidecar survives beside the restored
    # file), not the corruption symptom. What SQLite does with a foreign WAL is
    # not deterministic -- planting one has been observed to (a) be rejected and
    # the WAL zeroed, (b) raise `database disk image is malformed`, and (c) be
    # replayed, silently replacing the content and flipping page_size while
    # integrity_check still says `ok`. Which one you get depends on the frame
    # checksums. That non-determinism is exactly why the sidecar must not be
    # there at all, and why asserting on any one outcome would be a flaky test
    # of the wrong thing.
    stray = {sc for sc in ("-wal", "-shm")
             if (tmp_path / f"brain.sqlite3{sc}").exists()}
    assert stray == set(), f"the promoted store's {stray} was left beside the " \
                           "restored file, for SQLite to replay into it"
    # Nothing was destroyed: the rolled-back store is retained WITH its
    # sidecars, which is where they went.
    aside = _main_files(tmp_path, "brain.sqlite3.rolled-back-*")
    assert len(aside) == 1, aside
    assert Path(f"{aside[0]}-wal").exists(), \
        "the promoted store's WAL was discarded rather than moved aside with it"

    db = sqlite3.connect(src)
    try:
        names = {r[0] for r in db.execute("SELECT name FROM entities")}
        # And the OLD store is what came back, as its own self.
        assert db.execute("PRAGMA page_size").fetchone()[0] == old_page_size
        assert "OLD-ONLY" in names
        assert "WRITTEN-AFTER-SWAP" not in names
        assert [r[0] for r in db.execute("PRAGMA integrity_check")] == ["ok"]
    finally:
        db.close()


def test_swap_preserves_a_committed_write_left_in_the_rebuilds_wal(tmp_path):
    """I1, the destination-side mirror of C1.

    `<store>.new-wal` was never relocated or checkpointed, so anything that
    opened the rebuild write-capable between gate 4 and the swap had its
    committed pages silently dropped by `os.replace(dst, src)`.
    """
    src = tmp_path / "brain.sqlite3"
    dst = tmp_path / "brain.sqlite3.new"
    _seed(src)
    assert main(["--src", str(src), "--home", str(tmp_path), "--yes"]) == 0

    _crash_left_wal(dst, "INSERT INTO entities(id,name,type) "
                         "VALUES('w','IN-DST-WAL','person')")
    assert (tmp_path / "brain.sqlite3.new-wal").exists()

    assert main(["--src", str(src), "--home", str(tmp_path), "--swap", "--yes"]) == 0

    db = sqlite3.connect(src)
    try:
        assert db.execute("SELECT count(*) FROM entities "
                          "WHERE name='IN-DST-WAL'").fetchone()[0] == 1, \
            "a committed write in the rebuild's WAL was dropped by the swap"
    finally:
        db.close()
    assert not (tmp_path / "brain.sqlite3.new-wal").exists()
    assert not (tmp_path / "brain.sqlite3.new-shm").exists()


def test_swap_moves_the_old_stores_sidecars_with_it(tmp_path):
    """The forward half: nothing of the OLD store may be left beside the
    promoted rebuild, and --rollback needs those sidecars to exist."""
    src = tmp_path / "brain.sqlite3"
    _seed(src)
    assert main(["--src", str(src), "--home", str(tmp_path), "--yes"]) == 0
    _crash_left_wal(src, "INSERT INTO entities(id,name,type) "
                         "VALUES('o','IN-OLD-WAL','person')")

    assert main(["--src", str(src), "--home", str(tmp_path), "--swap", "--yes"]) == 0

    kept = _main_files(tmp_path, "brain.sqlite3.pre-rebuild-*")
    assert len(kept) == 1, kept
    assert Path(f"{kept[0]}-wal").exists(), "the old store's WAL was abandoned"
    # And the rollback that follows brings that content back with the file.
    assert main(["--src", str(src), "--home", str(tmp_path),
                 "--rollback", "--yes"]) == 0
    db = sqlite3.connect(src)
    try:
        assert db.execute("SELECT count(*) FROM entities "
                          "WHERE name='IN-OLD-WAL'").fetchone()[0] == 1
    finally:
        db.close()


def test_rollback_requires_yes_and_reports_nothing_to_restore(tmp_path):
    src = tmp_path / "brain.sqlite3"
    _seed(src)
    assert main(["--src", str(src), "--home", str(tmp_path), "--rollback"]) == 2
    assert main(["--src", str(src), "--home", str(tmp_path),
                 "--rollback", "--yes"]) == 2   # nothing retained yet
    assert main(["--src", str(src), "--home", str(tmp_path),
                 "--swap", "--rollback", "--yes"]) == 2   # opposite operations


# --- what --from is allowed to promote over the live store (final-review I3) --

def _swapped_with_a_retained_generation(tmp_path, name="brain.sqlite3"):
    """Rebuild + swap, so <src>.pre-rebuild-* exists beside the live store.

    This is the real starting state for --rollback, and the state in which the
    sidecar hazard exists: several retained paths now share a common prefix.
    """
    src = tmp_path / name
    _seed(src)
    assert main(["--src", str(src), "--home", str(tmp_path), "--yes"]) == 0
    assert main(["--src", str(src), "--home", str(tmp_path),
                 "--swap", "--yes"]) == 0
    kept = _main_files(tmp_path, f"{name}.pre-rebuild-*")
    assert len(kept) == 1, kept
    return src, kept[0]


def _store_fingerprint(p: Path) -> tuple:
    """Enough of a store's identity to prove it was NOT replaced."""
    db = sqlite3.connect(p)
    try:
        return (db.execute("PRAGMA page_size").fetchone()[0],
                db.execute("PRAGMA page_count").fetchone()[0],
                db.execute("SELECT count(*) FROM chunks").fetchone()[0],
                {r[0] for r in db.execute("SELECT name FROM entities")})
    finally:
        db.close()


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_rollback_refuses_a_sidecar_passed_via_from(tmp_path, suffix, capsys):
    """A 0-byte -wal/-shm opens as a VALID, EMPTY database.

    So integrity_check answers `ok`, every downstream gate passes, and the tool
    would report "restored store: integrity_check=['ok']" having installed an
    empty brain over the real one. The retained generations all share the store
    name as a prefix, so this is one tab-completion away for the operator
    running the highest-stakes command in the runbook. It must refuse in CODE,
    like check_migrations and embedded_without_vectors do -- not in prose.
    """
    src, kept = _swapped_with_a_retained_generation(tmp_path)
    sidecar = Path(f"{kept}{suffix}")
    sidecar.write_bytes(b"")            # exactly the hazard: 0 bytes
    assert sidecar.exists()
    before = _store_fingerprint(src)

    rc = main(["--src", str(src), "--home", str(tmp_path),
               "--rollback", "--yes", "--from", str(sidecar)])

    assert rc != 0, "a sidecar was accepted as a store"
    out = capsys.readouterr().out
    assert "REFUSING to roll back" in out, out
    assert "sidecar" in out, out
    # The live store is untouched, and nothing was moved aside.
    assert _store_fingerprint(src) == before
    assert _main_files(tmp_path, "brain.sqlite3.rolled-back-*") == []
    assert kept.exists(), "the retained generation was disturbed"


def test_rollback_refuses_an_empty_file_passed_via_from(tmp_path, capsys):
    """The same hazard without a sidecar name: a 0-byte file is a valid,
    zero-table SQLite database that integrity_check calls `ok`."""
    src, kept = _swapped_with_a_retained_generation(tmp_path)
    empty = tmp_path / "brain.sqlite3.pre-rebuild-0000000000"
    empty.write_bytes(b"")
    before = _store_fingerprint(src)

    rc = main(["--src", str(src), "--home", str(tmp_path),
               "--rollback", "--yes", "--from", str(empty)])

    assert rc != 0
    out = capsys.readouterr().out
    assert "REFUSING to roll back" in out, out
    assert "page(s)" in out, out
    assert _store_fingerprint(src) == before
    assert _main_files(tmp_path, "brain.sqlite3.rolled-back-*") == []


def test_rollback_refuses_a_sqlite_file_that_is_not_a_store(tmp_path, capsys):
    """Big enough to pass the page floor, but no `chunks` table -- e.g. some
    other SQLite file that happens to sit beside the store."""
    src, kept = _swapped_with_a_retained_generation(tmp_path)
    other = tmp_path / "not-a-store.sqlite3"
    db = sqlite3.connect(other)
    db.execute("CREATE TABLE junk(a TEXT)")
    db.executemany("INSERT INTO junk VALUES(?)", [("x" * 500,)] * 500)
    db.commit(); db.close()
    before = _store_fingerprint(src)

    rc = main(["--src", str(src), "--home", str(tmp_path),
               "--rollback", "--yes", "--from", str(other)])

    assert rc != 0
    out = capsys.readouterr().out
    assert "REFUSING to roll back" in out, out
    assert "chunks" in out, out
    assert _store_fingerprint(src) == before


def test_rollback_still_accepts_the_real_retained_store(tmp_path):
    """The guard must not break the operation it guards: the same --from path
    that the tool itself prints has to keep working."""
    src, kept = _swapped_with_a_retained_generation(tmp_path)
    assert sqlite3.connect(src).execute("PRAGMA page_size").fetchone()[0] == 8192

    rc = main(["--src", str(src), "--home", str(tmp_path),
               "--rollback", "--yes", "--from", str(kept)])

    assert rc == 0
    # The pre-rebuild store really came back (4096, not the rebuild's 8192).
    assert sqlite3.connect(src).execute("PRAGMA page_size").fetchone()[0] == 4096


# --- pre-migration sources (I2) --------------------------------------------

def _unmigrate_actions(src):
    """Put the store back into its pre-Task-1.7 shape, which is a valid input
    to init() and therefore a real store someone could try to rebuild."""
    db = sqlite3.connect(src)
    db.execute("ALTER TABLE graph_actions_legacy RENAME TO graph_actions")
    db.execute("DELETE FROM meta WHERE k='actions_migrated'")
    db.commit()
    db.close()


def test_rebuild_refuses_a_pre_migration_graph_actions_source(tmp_path):
    """Rebuilding it passes all seven gates and yields a store that cannot be
    opened (`already another table or index with this name:
    graph_actions_legacy`), so it is refused instead."""
    src, dst = tmp_path / "s.sqlite3", tmp_path / "d.sqlite3"
    _seed(src)
    _unmigrate_actions(src)
    assert check_migrations(src)
    with pytest.raises(UnmigratedStore, match="graph_actions"):
        rebuild(src, dst)
    assert not dst.exists()


def test_rebuild_refuses_a_doc_id_keyed_enrich_payloads_source(tmp_path):
    """The destination's table is file_id-keyed, so every copied row hits
    `NOT NULL constraint failed: enrich_payloads.file_id` mid-rebuild."""
    src, dst = tmp_path / "s.sqlite3", tmp_path / "d.sqlite3"
    _seed(src)
    db = sqlite3.connect(src)
    db.execute("DROP TABLE enrich_payloads")
    db.execute("CREATE TABLE enrich_payloads(doc_id TEXT PRIMARY KEY, "
               "payload TEXT NOT NULL, logic_version INTEGER DEFAULT 0, at TEXT)")
    db.execute("INSERT INTO enrich_payloads(doc_id,payload) VALUES('d1','{}')")
    db.commit()
    db.close()
    assert check_migrations(src)
    with pytest.raises(UnmigratedStore, match="enrich_payloads"):
        rebuild(src, dst)


def test_main_refuses_a_pre_migration_source_even_with_yes(tmp_path):
    """Not a consent matter: --yes does not unlock an unrebuildable store."""
    src = tmp_path / "brain.sqlite3"
    _seed(src)
    _unmigrate_actions(src)
    assert main(["--src", str(src), "--home", str(tmp_path), "--yes"]) == 2
    assert not (tmp_path / "brain.sqlite3.new").exists()


def test_check_migrations_passes_a_normal_store(tmp_path):
    """The already-migrated case -- Josh's live store, and every fresh one --
    must stay unaffected."""
    src = tmp_path / "s.sqlite3"
    _seed(src)
    assert check_migrations(src) == []


# --- the sibling self-FK nothing could detect (I3) -------------------------

def test_rebuild_nullifies_a_relation_pointer_to_a_dropped_orphan(tmp_path):
    """entity_relations.invalidated_by_relation_id is added by ALTER TABLE with
    no REFERENCES clause, so `PRAGMA foreign_key_check` is structurally blind
    to it -- gate 5 can never catch a dangling one.

    This is the live scenario exactly: the rebuild drops 8 orphan
    entity_relations rows, and a surviving row pointing at one of those 8 is
    left dangling. It is not an orphan in the SOURCE (the target row is right
    there), which is why only a post-copy pass on the destination can see it.
    """
    src, dst = tmp_path / "s.sqlite3", tmp_path / "d.sqlite3"
    s = Store(str(src), dim=4)
    s.init()
    with s._connect(write=True) as db:
        db.execute("INSERT INTO entities(id,name,type) VALUES('e1','A','person')")
        db.execute("INSERT INTO entities(id,name,type) VALUES('e2','B','person')")
    db = sqlite3.connect(src)   # foreign_keys OFF: plant the orphan relation
    # id=1 is an ORPHAN (entity_a missing) and will be dropped by _KEEP.
    db.execute("INSERT INTO entity_relations(id,entity_a,relation,entity_b) "
               "VALUES(1,'GONE','knows','e2')")
    # id=2 survives, and points at the row that is about to disappear.
    db.execute("INSERT INTO entity_relations(id,entity_a,relation,entity_b,"
               "invalidated_at,invalidated_by_relation_id) "
               "VALUES(2,'e1','knows','e2','2026-01-01',1)")
    db.commit()
    db.close()

    # Not visible as an orphan in the source -- row 1 exists there.
    assert report_orphans(src)["entity_relations.invalidated_by_relation_id"] == 0

    r = rebuild(src, dst)

    assert r["dropped_rows"]["entity_relations"] == 1
    assert r["nullified"]["entity_relations.invalidated_by_relation_id"] == 1
    out = Store(str(dst), dim=4)
    with out._connect() as db:
        row = db.execute("SELECT invalidated_at, invalidated_by_relation_id "
                         "FROM entity_relations WHERE id=2").fetchone()
        assert row["invalidated_at"] == "2026-01-01"   # still invalidated
        assert row["invalidated_by_relation_id"] is None   # invalidator unknown


def test_report_orphans_covers_the_relation_self_ref(tmp_path):
    src = tmp_path / "s.sqlite3"
    s = Store(str(src), dim=4)
    s.init()
    with s._connect(write=True) as db:
        db.execute("INSERT INTO entities(id,name,type) VALUES('e1','A','person')")
    db = sqlite3.connect(src)
    db.execute("INSERT INTO entity_relations(id,entity_a,relation,entity_b,"
               "invalidated_by_relation_id) VALUES(1,'e1','knows','e1',999)")
    db.execute("INSERT INTO entity_relations(id,entity_a,relation,entity_b) "
               "VALUES(2,'e1','likes','e1')")   # NULL pointer, not an orphan
    db.commit()
    db.close()

    assert report_orphans(src)["entity_relations.invalidated_by_relation_id"] == 1


# --- graceful failure instead of a traceback (I4) ---------------------------

def test_main_refuses_a_store_whose_embedded_chunks_have_no_vector(tmp_path, capsys):
    """backup._verify_artifact raises on such a store, so gate 2 cannot take a
    snapshot of it -- which used to surface as a bare traceback in the middle
    of a deliberately careful attended operation."""
    src = tmp_path / "brain.sqlite3"
    s = Store(str(src), dim=4)
    s.init()
    with s._connect(write=True) as db:
        db.execute("INSERT INTO chunks(doc_id,text,metadata,embedded) "
                   "VALUES('d1','hello','{}',1)")   # claims a vector, has none
    assert embedded_without_vectors(src) == 1

    rc = main(["--src", str(src), "--home", str(tmp_path), "--yes"])

    assert rc == 2
    out = capsys.readouterr().out
    assert "REFUSING" in out and "embedded=1" in out
    assert "bin/repair.py embed-pending" in out
    assert "Traceback" not in out
    assert not list(tmp_path.glob("*.enc"))
    assert not (tmp_path / "brain.sqlite3.new").exists()


def test_main_reports_a_snapshot_failure_without_a_traceback(tmp_path, capsys):
    import mcpbrain.backup as backup_mod
    src = tmp_path / "brain.sqlite3"
    _seed(src)
    real = backup_mod.make_encrypted_snapshot
    backup_mod.make_encrypted_snapshot = lambda *a, **k: (_ for _ in ()).throw(
        OSError("[Errno 28] No space left on device"))
    try:
        rc = main(["--src", str(src), "--home", str(tmp_path), "--yes"])
    finally:
        backup_mod.make_encrypted_snapshot = real
    assert rc == 1
    out = capsys.readouterr().out
    assert "REFUSING" in out and "No space left on device" in out
    assert "Traceback" not in out
    assert not (tmp_path / "brain.sqlite3.new").exists()


def test_main_reports_a_rebuild_failure_without_a_traceback(tmp_path, capsys):
    import bin.optimise_store as mod
    src = tmp_path / "brain.sqlite3"
    _seed(src)
    real = mod.rebuild
    mod.rebuild = lambda *a, **k: (_ for _ in ()).throw(
        sqlite3.IntegrityError("NOT NULL constraint failed: x.y"))
    try:
        rc = main(["--src", str(src), "--home", str(tmp_path), "--yes"])
    finally:
        mod.rebuild = real
    assert rc == 1
    out = capsys.readouterr().out
    assert "REBUILD FAILED" in out and "NOT NULL constraint failed" in out
    assert "UNTOUCHED" in out
    assert "Traceback" not in out


# --- Task 10: prove the rollback path actually restores --------------------

def test_snapshot_taken_before_rebuild_restores_the_original(tmp_path):
    """Tests the raw `backup` primitive that BOTH main()'s GATE 2 snapshot and
    `--rollback --yes` depend on. Untested rollback is not rollback."""
    from mcpbrain import backup
    src = tmp_path / "src.sqlite3"
    s = Store(str(src), dim=4); s.init()
    with s._connect(write=True) as db:
        db.execute("INSERT INTO chunks(doc_id,text,embedded) VALUES('d1','keep me',0)")

    key = backup.generate_escrow_key()
    enc = tmp_path / "snap.enc"
    backup.make_encrypted_snapshot(src, enc, key)

    src.unlink()                      # simulate a failed rebuild + lost original
    restored = tmp_path / "restored.sqlite3"
    backup.restore(enc, restored, key)

    import sqlite3
    db = sqlite3.connect(restored)
    assert db.execute("SELECT text FROM chunks WHERE doc_id='d1'").fetchone()[0] == "keep me"
    db.close()
