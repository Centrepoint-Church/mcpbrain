from mcpbrain.store import Store


def _store(tmp_path):
    s = Store(tmp_path / "a.sqlite3", dim=4)
    s.init()
    return s


def test_doc_ids_for_drive_and_file(tmp_path):
    s = _store(tmp_path)
    s.import_cached_chunk("gdrive-F1-0", "a", "c", {"file_id": "F1", "drive_id": "D1"}, [0.0]*4)
    s.import_cached_chunk("gdrive-F1-1", "b", "c", {"file_id": "F1", "drive_id": "D1"}, [0.0]*4)
    s.import_cached_chunk("gdrive-F2-0", "c", "c", {"file_id": "F2", "drive_id": "D2"}, [0.0]*4)
    assert set(s.doc_ids_for_drive("D1")) == {"gdrive-F1-0", "gdrive-F1-1"}
    assert set(s.doc_ids_for_file("F1")) == {"gdrive-F1-0", "gdrive-F1-1"}
    assert s.doc_ids_for_drive("D2") == ["gdrive-F2-0"]


def test_doc_ids_for_file_escapes_like_wildcards(tmp_path):
    """Google Drive file ids legitimately contain '_', a SQL LIKE single-char
    wildcard. Without escaping, doc_ids_for_file('F_1') would also match a
    sibling file like 'FA1' (any char substituting for '_') — an over-match
    that feeds the purge/delete path. It must scope to the exact file only."""
    s = _store(tmp_path)
    s.import_cached_chunk("gdrive-F_1-0", "a", "c", {"file_id": "F_1"}, [0.0] * 4)
    s.import_cached_chunk("gdrive-FA1-0", "b", "c", {"file_id": "FA1"}, [0.0] * 4)
    assert set(s.doc_ids_for_file("F_1")) == {"gdrive-F_1-0"}
    assert set(s.doc_ids_for_file("FA1")) == {"gdrive-FA1-0"}


def test_chunks_for_file_escapes_like_wildcards(tmp_path):
    """Same over-match risk as doc_ids_for_file, but for chunks_for_file —
    this feeds the publish path (collect_chunks -> publish)."""
    s = _store(tmp_path)
    s.import_cached_chunk("gdrive-F_1-0", "a", "c", {"file_id": "F_1", "chunk_index": 0}, [0.0] * 4)
    s.import_cached_chunk("gdrive-FA1-0", "b", "c", {"file_id": "FA1", "chunk_index": 0}, [0.0] * 4)
    rows = s.chunks_for_file("F_1")
    assert [r["doc_id"] for r in rows] == ["gdrive-F_1-0"]


def test_delete_chunks_removes_row_and_mirrors(tmp_path):
    s = _store(tmp_path)
    s.import_cached_chunk("gdrive-F1-0", "a", "c", {"file_id": "F1", "drive_id": "D1"}, [0.1]*4)
    n = s.delete_chunks(["gdrive-F1-0"])
    assert n == 1
    assert s.get_chunk("gdrive-F1-0") is None
    assert s.embedding_for_doc("gdrive-F1-0") is None
    assert s.delete_chunks([]) == 0


def test_invalidate_local_relations_scopes_to_local_and_docs(tmp_path):
    s = _store(tmp_path)
    with s._connect() as db:
        db.execute("INSERT INTO entities(id,name,type,origin) VALUES('a','A','person','local')")
        db.execute("INSERT INTO entities(id,name,type,origin) VALUES('b','B','org','local')")
        # local relation sourced from a purged doc -> invalidate
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b,source_doc_id,origin) "
                   "VALUES('a','works_at','b','gdrive-F1-0','local')")
        # local relation from a still-live doc -> survive
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b,source_doc_id,origin) "
                   "VALUES('a','member_of','b','gdrive-LIVE-0','local')")
        # org relation from a purged doc -> untouched (layer 1 is safe-by-construction)
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b,source_doc_id,origin) "
                   "VALUES('a','mentioned_with','b','gdrive-F1-0','org')")
    n = s.invalidate_local_relations_for_docs(["gdrive-F1-0"])
    assert n == 1
    with s._connect() as db:
        rows = {(r["relation"], r["origin"]): r["invalidated_at"]
                for r in db.execute("SELECT relation,origin,invalidated_at FROM entity_relations")}
    assert rows[("works_at", "local")] is not None       # invalidated
    assert rows[("member_of", "local")] is None           # live source survives
    assert rows[("mentioned_with", "org")] is None         # org untouched
    # idempotent: a second call invalidates nothing new
    assert s.invalidate_local_relations_for_docs(["gdrive-F1-0"]) == 0


def test_purge_drive_deletes_chunks_and_invalidates_local(tmp_path):
    from mcpbrain import ingest_cache
    s = _store(tmp_path)
    s.import_cached_chunk("gdrive-F1-0", "a", "c", {"file_id": "F1", "drive_id": "D1"}, [0.0]*4)
    s.import_cached_chunk("gdrive-F1-1", "b", "c", {"file_id": "F1", "drive_id": "D1"}, [0.0]*4)
    with s._connect() as db:
        db.execute("INSERT INTO entities(id,name,type,origin) VALUES('a','A','person','local')")
        db.execute("INSERT INTO entities(id,name,type,origin) VALUES('b','B','org','local')")
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b,source_doc_id,origin) "
                   "VALUES('a','works_at','b','gdrive-F1-0','local')")
    out = ingest_cache.purge_drive(s, "D1")
    assert out["chunks_deleted"] == 2 and out["relations_invalidated"] == 1
    assert s.doc_ids_for_drive("D1") == []
    with s._connect() as db:
        r = db.execute("SELECT invalidated_at FROM entity_relations WHERE entity_a='a'").fetchone()
    assert r["invalidated_at"] is not None


def test_purge_drive_logs_at_warning_not_info(tmp_path, caplog):
    """Purging a drive's entire cached content because access was revoked is
    consequential/destructive — it must be visible at warning, not buried at
    info level."""
    import logging
    from mcpbrain import ingest_cache
    s = _store(tmp_path)
    s.import_cached_chunk("gdrive-F1-0", "a", "c", {"file_id": "F1", "drive_id": "D1"}, [0.0] * 4)
    with caplog.at_level(logging.INFO, logger="mcpbrain.ingest_cache"):
        ingest_cache.purge_drive(s, "D1")
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "purge_drive should log at WARNING"
    assert "purged drive D1" in warnings[0].message


def test_note_drive_presence_logs_warning_when_crossing_absence_threshold(tmp_path, caplog):
    """The 'why did this happen' operator signal must be logged right at the
    point a drive crosses the absence threshold, naming the drive id and the
    threshold — not just inferred from purge_drive's own log line."""
    import logging
    from mcpbrain import ingest_cache
    s = _store(tmp_path)
    s.import_cached_chunk("gdrive-F1-0", "a", "c", {"file_id": "F1", "drive_id": "D1"}, [0.0] * 4)
    ingest_cache.note_drive_presence(s, ["D1"], threshold=3)
    ingest_cache.note_drive_presence(s, [], threshold=3)
    ingest_cache.note_drive_presence(s, [], threshold=3)
    with caplog.at_level(logging.INFO, logger="mcpbrain.ingest_cache"):
        ingest_cache.note_drive_presence(s, [], threshold=3)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("D1" in r.message and "3" in r.message for r in warnings)


def test_purge_drive_empty_drive_returns_zeros(tmp_path):
    from mcpbrain import ingest_cache
    s = _store(tmp_path)
    # purge_drive on a drive with no chunks should return zero counts without raising
    out = ingest_cache.purge_drive(s, "NONEXISTENT_DRIVE")
    assert out == {"drive_id": "NONEXISTENT_DRIVE", "docs": 0,
                   "chunks_deleted": 0, "relations_invalidated": 0}


def test_note_drive_presence_purges_after_threshold(tmp_path):
    from mcpbrain import ingest_cache
    s = _store(tmp_path)
    s.import_cached_chunk("gdrive-F1-0", "a", "c", {"file_id": "F1", "drive_id": "D1"}, [0.0]*4)
    # cycle 1: D1 present -> known, counter 0
    assert ingest_cache.note_drive_presence(s, ["D1"], threshold=3)["purged"] == []
    # cycles 2-4: D1 absent -> counts 1, 2, then purge on the 3rd
    assert ingest_cache.note_drive_presence(s, [], threshold=3)["purged"] == []
    assert ingest_cache.note_drive_presence(s, [], threshold=3)["purged"] == []
    out = ingest_cache.note_drive_presence(s, [], threshold=3)
    assert out["purged"] == ["D1"]
    assert s.doc_ids_for_drive("D1") == []          # purge ran
    # forgotten: a further absent cycle does nothing
    assert ingest_cache.note_drive_presence(s, [], threshold=3)["purged"] == []


def test_note_drive_presence_purges_and_deletes_orphaned_absence_meta_key(tmp_path):
    """When a drive is purged, its absent:<id> meta key must be DELETED, not
    reset to '0' — otherwise it accumulates forever as an orphan row."""
    from mcpbrain import ingest_cache
    s = _store(tmp_path)
    s.import_cached_chunk("gdrive-F1-0", "a", "c", {"file_id": "F1", "drive_id": "D1"}, [0.0] * 4)
    ingest_cache.note_drive_presence(s, ["D1"], threshold=3)
    ingest_cache.note_drive_presence(s, [], threshold=3)
    ingest_cache.note_drive_presence(s, [], threshold=3)
    out = ingest_cache.note_drive_presence(s, [], threshold=3)
    assert out["purged"] == ["D1"]
    # the absence meta row must be gone entirely, not lingering at "0"
    assert s.get_meta(ingest_cache._absence_key("D1")) is None


def test_note_drive_presence_transient_glitch_recovers(tmp_path):
    from mcpbrain import ingest_cache
    s = _store(tmp_path)
    s.import_cached_chunk("gdrive-F1-0", "a", "c", {"file_id": "F1", "drive_id": "D1"}, [0.0]*4)
    ingest_cache.note_drive_presence(s, ["D1"], threshold=3)
    ingest_cache.note_drive_presence(s, [], threshold=3)          # 1 absent
    ingest_cache.note_drive_presence(s, ["D1"], threshold=3)      # reappears -> reset
    ingest_cache.note_drive_presence(s, [], threshold=3)          # 1 absent again
    out = ingest_cache.note_drive_presence(s, [], threshold=3)    # 2 absent — NOT purged
    assert out["purged"] == []
    assert s.doc_ids_for_drive("D1") == ["gdrive-F1-0"]
