"""note_chunks: multi-chunk notes (Task 7) reassemble into one row per note."""
from mcpbrain.store import Store


def _store(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    return s


def test_note_chunks_reassembles_a_multi_chunk_note(tmp_path):
    s = _store(tmp_path)
    base = "note-abc"
    for i, piece in enumerate(["first", "second", "third"]):
        s.upsert_chunk(f"{base}-{i}", piece, "h",
                       {"source": "note", "observation_type": "memory",
                        "title": "T", "note_id": base,
                        "chunk_index": i, "chunk_total": 3})
    rows = s.note_chunks(observation_type="memory")
    assert len(rows) == 1
    assert rows[0]["doc_id"] == base
    assert rows[0]["text"] == "first\n\nsecond\n\nthird"


def test_note_chunks_legacy_single_chunk_note_unchanged(tmp_path):
    s = _store(tmp_path)
    s.upsert_chunk("note-xyz", "body", "h",
                   {"source": "note", "observation_type": "memory", "title": "T"})
    rows = s.note_chunks(observation_type="memory")
    assert len(rows) == 1
    assert rows[0]["doc_id"] == "note-xyz"
    assert rows[0]["text"] == "body"
