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
