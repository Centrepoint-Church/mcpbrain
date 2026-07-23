from mcpbrain.store import Store


def _store(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4); s.init(); return s


def test_fts_indexes_contextual_prefix_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr("mcpbrain.config.contextual_retrieval_enabled", lambda home: True)
    s = _store(tmp_path)
    # a gdrive chunk whose body never mentions the title
    s.upsert_chunk("gdrive-F1-0", "attendance rota rows",
                   "h0", {"source_type": "gdrive", "file_name": "Citywide Youth Term Plan"})
    s.write_embedding(_rowid(s, "gdrive-F1-0"), [0.0, 0.0, 0.0, 0.0])
    # keyword search for a title-only term now hits via the FTS prefix
    hits = [d for d, _ in s.fts_search("Citywide Youth", 5)]
    assert "gdrive-F1-0" in hits


def _rowid(store, doc_id):
    with store._connect() as db:
        return db.execute("SELECT rowid FROM chunks WHERE doc_id=?", (doc_id,)).fetchone()["rowid"]


def test_reindex_fts_batch_refreshes_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr("mcpbrain.config.contextual_retrieval_enabled", lambda home: True)
    s = _store(tmp_path)
    s.upsert_chunk("gdrive-F2-0", "rota rows", "h", {"source_type": "gdrive",
                   "file_name": "Master Rosters"})
    rid = _rowid(s, "gdrive-F2-0")
    # simulate a legacy raw-text FTS row (pre-Phase-C)
    with s._connect() as db:
        db.execute("DELETE FROM fts_chunks WHERE rowid=?", (rid,))
        db.execute("INSERT INTO fts_chunks(rowid, text) VALUES(?,?)", (rid, "rota rows"))
        db.execute("UPDATE chunks SET embedded=1 WHERE rowid=?", (rid,))
    assert "gdrive-F2-0" not in [d for d, _ in s.fts_search("Master Rosters", 5)]
    n = s.reindex_fts_batch(cap=100)
    assert n >= 1
    assert "gdrive-F2-0" in [d for d, _ in s.fts_search("Master Rosters", 5)]


# --- Task 3: write-time version stamp + flag consistency (fix #7) ---------

def test_write_embedding_stamps_version_by_flag(tmp_path, monkeypatch):
    """A contextual write (flag ON) stamps FTS_CONTEXT_VERSION; a raw write
    (flag OFF) stamps 0 so it self-corrects when the flag later flips ON."""
    s = _store(tmp_path)
    s.upsert_chunk("d1", "body one", "h1", {})
    s.upsert_chunk("d2", "body two", "h2", {})
    rid1, rid2 = _rowid(s, "d1"), _rowid(s, "d2")

    monkeypatch.setattr("mcpbrain.config.contextual_retrieval_enabled", lambda home: True)
    s.write_embedding(rid1, [0.0, 0.0, 0.0, 0.0])
    monkeypatch.setattr("mcpbrain.config.contextual_retrieval_enabled", lambda home: False)
    s.write_embedding(rid2, [0.0, 0.0, 0.0, 0.0])

    with s._connect() as db:
        v1 = db.execute(
            "SELECT fts_context_version FROM chunks WHERE rowid=?", (rid1,)).fetchone()[0]
        v2 = db.execute(
            "SELECT fts_context_version FROM chunks WHERE rowid=?", (rid2,)).fetchone()[0]
    assert v1 == s.FTS_CONTEXT_VERSION
    assert v2 == 0


def test_reindex_fts_batch_noop_when_all_current(tmp_path, monkeypatch):
    """Idempotent: a second pass over an already-current store touches nothing."""
    monkeypatch.setattr("mcpbrain.config.contextual_retrieval_enabled", lambda home: True)
    s = _store(tmp_path)
    s.upsert_chunk("d1", "body", "h1", {})
    s.write_embedding(_rowid(s, "d1"), [0.0, 0.0, 0.0, 0.0])
    assert s.reindex_fts_batch(cap=100) == 0


def test_reindex_fts_batch_migrates_v0_row_after_flag_flips_on(tmp_path, monkeypatch):
    """A raw write under an OFF flag stamps v0; when the flag flips ON later,
    reindex_fts_batch picks it up and rebuilds it as contextual."""
    monkeypatch.setattr("mcpbrain.config.contextual_retrieval_enabled", lambda home: False)
    s = _store(tmp_path)
    s.upsert_chunk("gdrive-F3-0", "rota rows", "h",
                   {"source_type": "gdrive", "file_name": "Term Plan Two"})
    rid = _rowid(s, "gdrive-F3-0")
    s.write_embedding(rid, [0.0, 0.0, 0.0, 0.0])

    with s._connect() as db:
        v = db.execute(
            "SELECT fts_context_version FROM chunks WHERE rowid=?", (rid,)).fetchone()[0]
    assert v == 0
    assert "gdrive-F3-0" not in [d for d, _ in s.fts_search("Term Plan Two", 5)]

    monkeypatch.setattr("mcpbrain.config.contextual_retrieval_enabled", lambda home: True)
    n = s.reindex_fts_batch(cap=100)
    assert n == 1
    assert "gdrive-F3-0" in [d for d, _ in s.fts_search("Term Plan Two", 5)]
    with s._connect() as db:
        v2 = db.execute(
            "SELECT fts_context_version FROM chunks WHERE rowid=?", (rid,)).fetchone()[0]
    assert v2 == s.FTS_CONTEXT_VERSION


class _FakeEmbedder:
    def embed_passages(self, texts):
        return [[0.0, 0.0, 0.0, 0.0] for _ in texts]


def test_embed_doc_respects_contextual_flag_off(tmp_path, monkeypatch):
    """embed_doc must NOT prepend the contextual prefix (and must stamp v0)
    when contextual_retrieval is OFF."""
    monkeypatch.setattr("mcpbrain.config.contextual_retrieval_enabled", lambda home: False)
    s = _store(tmp_path)
    s.upsert_chunk("gdrive-F4-0", "rota rows", "h",
                   {"source_type": "gdrive", "file_name": "Special Roster Title"})
    assert s.embed_doc("gdrive-F4-0", _FakeEmbedder()) is True
    assert "gdrive-F4-0" not in [d for d, _ in s.fts_search("Special Roster Title", 5)]
    rid = _rowid(s, "gdrive-F4-0")
    with s._connect() as db:
        v = db.execute(
            "SELECT fts_context_version FROM chunks WHERE rowid=?", (rid,)).fetchone()[0]
    assert v == 0


def test_embed_doc_respects_contextual_flag_on(tmp_path, monkeypatch):
    """embed_doc DOES prepend the contextual prefix when the flag is ON,
    matching index_pending's batch path."""
    monkeypatch.setattr("mcpbrain.config.contextual_retrieval_enabled", lambda home: True)
    s = _store(tmp_path)
    s.upsert_chunk("gdrive-F5-0", "rota rows", "h",
                   {"source_type": "gdrive", "file_name": "Special Roster Title Two"})
    assert s.embed_doc("gdrive-F5-0", _FakeEmbedder()) is True
    assert "gdrive-F5-0" in [d for d, _ in s.fts_search("Special Roster Title Two", 5)]
