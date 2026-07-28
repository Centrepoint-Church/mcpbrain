"""patch_chunk_metadata + note_chunks: the expiry/index plumbing for memory notes."""
import json

from mcpbrain.store import Store


def _store(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    return s


def _add_note(s, doc_id, title="T", otype="memory", expired=None):
    meta = {"source": "note", "title": title, "observation_type": otype,
            "tags": "", "org": "", "captured_at": "2026-06-04T12:00:00Z"}
    if expired is not None:
        meta["expired"] = expired
    s.upsert_chunk(doc_id=doc_id, text=f"{title}\n\nbody", content_hash=doc_id,
                   metadata=meta)


def test_patch_merges_without_touching_embedded(tmp_path):
    s = _store(tmp_path)
    _add_note(s, "note-1")
    with s._connect() as db:
        db.execute("UPDATE chunks SET embedded=1 WHERE doc_id='note-1'")
    assert s.patch_chunk_metadata("note-1", expired=True) is True
    chunk = s.get_chunk("note-1")
    meta = chunk["metadata"] if isinstance(chunk["metadata"], dict) \
        else json.loads(chunk["metadata"])
    assert meta["expired"] is True
    assert meta["title"] == "T"          # existing keys kept
    with s._connect() as db:
        row = db.execute("SELECT embedded FROM chunks WHERE doc_id='note-1'").fetchone()
    assert row["embedded"] == 1          # patch did not re-queue embedding


def test_patch_unknown_doc_returns_false(tmp_path):
    assert _store(tmp_path).patch_chunk_metadata("nope", expired=True) is False


def test_note_chunks_filters_type_and_expiry(tmp_path):
    s = _store(tmp_path)
    _add_note(s, "note-mem", otype="memory")
    _add_note(s, "note-ref", otype="reference")
    _add_note(s, "note-old", otype="memory", expired=True)
    ids = {c["doc_id"] for c in s.note_chunks(observation_type="memory")}
    assert ids == {"note-mem"}
    all_ids = {c["doc_id"] for c in s.note_chunks()}
    assert all_ids == {"note-mem", "note-ref"}   # expired excluded by default
    with_expired = {c["doc_id"] for c in s.note_chunks(include_expired=True)}
    assert "note-old" in with_expired


def test_note_chunks_limit_counts_live_not_expired(tmp_path):
    """LIMIT must apply after the expired filter, newest first — a store full of
    expired notes must not truncate live ones."""
    s = _store(tmp_path)
    # Insert alternating live/expired in rowid order: live-0, exp-1, live-2, ...
    for i in range(6):
        _add_note(s, f"note-{i}", otype="memory", expired=(i % 2 == 1))
    got = s.note_chunks(limit=3)
    assert [c["doc_id"] for c in got] == ["note-4", "note-2", "note-0"]


def test_every_drive_chunk_records_how_many_chunks_its_document_has():
    """C1: 154,601 chunks carry chunk_index and ZERO carry a total. Given 'chunk
    7', nothing can tell whether the document has 8 chunks or 17,281 — so there
    is no integrity check for partial ingestion, and no consumer can detect the
    B5 orphaning."""
    from mcpbrain.sync.drive import normalise_drive

    fmeta = {"id": "f1", "name": "Doc.txt", "mimeType": "text/plain"}
    text = "\n\n".join(f"Paragraph {i} " + "word " * 300 for i in range(4))

    chunks = normalise_drive(fmeta, text)

    assert len(chunks) > 1
    for c in chunks:
        assert c.metadata["chunk_total"] == len(chunks)
