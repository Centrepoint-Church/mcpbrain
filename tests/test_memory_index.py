"""memory_index.regenerate: the mechanical memory.md index (MEMORY.md pattern)."""
from mcpbrain.memory_index import regenerate
from mcpbrain.store import Store


def _store(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    return s


def _note(s, doc_id, title, otype="memory", expired=False):
    meta = {"source": "note", "title": title, "observation_type": otype,
            "captured_at": "2026-06-04T12:00:00Z"}
    if expired:
        meta["expired"] = True
    s.upsert_chunk(doc_id=doc_id, text=f"{title}\n\nFirst line of body.\nMore.",
                   content_hash=doc_id, metadata=meta)


def test_writes_one_line_per_live_memory(tmp_path):
    s = _store(tmp_path)
    _note(s, "note-a", "Prefers tables")
    _note(s, "note-b", "Old habit", expired=True)
    _note(s, "note-c", "A reference", otype="reference")
    regenerate(s, str(tmp_path))
    text = (tmp_path / "context" / "memory.md").read_text()
    assert "Prefers tables" in text and "note-a" in text
    assert "Old habit" not in text          # expired excluded
    assert "A reference" not in text        # memory-typed only
    assert text.startswith("# Memory Index")


def test_empty_store_writes_empty_index(tmp_path):
    s = _store(tmp_path)
    regenerate(s, str(tmp_path))
    text = (tmp_path / "context" / "memory.md").read_text()
    assert "# Memory Index" in text


def test_regenerate_is_atomic_overwrite(tmp_path):
    s = _store(tmp_path)
    _note(s, "note-a", "First")
    regenerate(s, str(tmp_path))
    s.patch_chunk_metadata("note-a", expired=True)
    regenerate(s, str(tmp_path))
    assert "First" not in (tmp_path / "context" / "memory.md").read_text()
