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


def test_patch_note_metadata_stamps_every_sibling(tmp_path):
    s = _store(tmp_path)
    base = "note-abc"
    for i in range(3):
        s.upsert_chunk(f"{base}-{i}", f"p{i}", "h",
                       {"source": "note", "observation_type": "memory",
                        "note_id": base, "chunk_index": i, "chunk_total": 3})
    assert s.patch_note_metadata(base, distilled_at="2026-08-27", distilled_verdict="keep") is True
    rows = s.note_chunks(observation_type="memory", include_expired=True)
    assert rows[0]["metadata"]["distilled_verdict"] == "keep"
    # EVERY sibling, not just the first: note_chunks() reports the group's
    # metadata from one piece, so checking only that hid an `any(generator)`
    # short-circuit that patched exactly one row.
    for i in range(3):
        assert s.get_chunk(f"{base}-{i}")["metadata"]["distilled_verdict"] == "keep"


def test_patch_note_metadata_falls_back_to_the_bare_doc_id(tmp_path):
    s = _store(tmp_path)
    s.upsert_chunk("note-xyz", "body", "h",
                   {"source": "note", "observation_type": "memory"})
    assert s.patch_note_metadata("note-xyz", expired=True) is True


def test_read_doc_reassembles_a_chunked_note(tmp_path):
    """brain_read's plain get_chunk(doc_id) returns None for a chunked note's
    base id -- only its `-0`/`-1`/... siblings have rows. note_chunks() (and
    memory_index, which renders exactly this base id) still hand out that
    base id as the note's identity, so the documented "read it with
    brain_read" path silently failed for every multi-chunk note. read_doc is
    the single place both brain_read dispatch sites call."""
    s = _store(tmp_path)
    base = "note-abc"
    for i, piece in enumerate(["first", "second", "third"]):
        s.upsert_chunk(f"{base}-{i}", piece, "h",
                       {"source": "note", "observation_type": "memory",
                        "title": "T", "note_id": base,
                        "chunk_index": i, "chunk_total": 3})
    assert s.get_chunk(base) is None          # the failure mode
    doc = s.read_doc(base)
    assert doc["doc_id"] == base
    assert doc["text"] == "first\n\nsecond\n\nthird"
    assert doc["metadata"]["title"] == "T"


def test_read_doc_falls_through_to_get_chunk_for_ordinary_docs(tmp_path):
    """The common case (email/drive chunks, a legacy or single-piece note)
    must behave exactly like get_chunk, unchanged."""
    s = _store(tmp_path)
    s.upsert_chunk("gmail-m1-0", "hello", "h", {"source_type": "gmail"})
    assert s.read_doc("gmail-m1-0") == s.get_chunk("gmail-m1-0")
    assert s.read_doc("missing-entirely") is None


def test_read_doc_missing_note_returns_none(tmp_path):
    s = _store(tmp_path)
    assert s.read_doc("note-never-captured") is None


def test_note_chunks_reassembles_a_lossless_split_note_with_empty_join(tmp_path):
    """Lossless pieces carry their own separators, so they rejoin with "" — not
    CHUNK_JOIN, which would inject a blank line that was never in the note."""
    s = _store(tmp_path)
    base = "note-lossless"
    for i, piece in enumerate(["first\n\n", "second\n", "third"]):
        s.upsert_chunk(f"{base}-{i}", piece, "h",
                       {"source": "note", "observation_type": "memory",
                        "title": "T", "note_id": base, "split": "lossless",
                        "chunk_index": i, "chunk_total": 3})
    rows = s.note_chunks(observation_type="memory")
    assert len(rows) == 1
    assert rows[0]["text"] == "first\n\nsecond\nthird"


def test_note_chunks_legacy_chunk_text_pieces_still_join_with_blank_lines(tmp_path):
    """The 250 notes already re-chunked under the old convention must keep
    reassembling the way they were written."""
    s = _store(tmp_path)
    base = "note-legacy"
    for i, piece in enumerate(["first", "second"]):
        s.upsert_chunk(f"{base}-{i}", piece, "h",
                       {"source": "note", "observation_type": "memory",
                        "title": "T", "note_id": base,
                        "chunk_index": i, "chunk_total": 2})
    rows = s.note_chunks(observation_type="memory")
    assert rows[0]["text"] == "first\n\nsecond"
