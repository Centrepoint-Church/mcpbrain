import json
from mcpbrain.sync.anarlog import normalise_session


def _session(**over):
    base = {
        "id": "sess-1",
        "title": "ACC Staff Meeting",
        "started_at": "2026-09-17T02:00:00Z",
        "event_id": "evt-9",
        "series_id": "ser-3",
        "documents": {
            "summary": json.dumps({"type": "doc", "content": [
                {"type": "paragraph",
                 "content": [{"type": "text", "text": "We agreed on X."}]}]}),
            "note": json.dumps({"type": "doc", "content": [
                {"type": "paragraph",
                 "content": [{"type": "text", "text": "My rough note."}]}]}),
        },
        "transcript": "Someone said a thing.",
    }
    base.update(over)
    return base


def test_produces_all_three_subtypes():
    chunks = normalise_session(_session())
    subtypes = {c.metadata["content_subtype"] for c in chunks}
    assert subtypes == {"summary", "note", "transcript"}


def test_doc_ids_follow_the_namespace():
    chunks = normalise_session(_session())
    for c in chunks:
        assert c.doc_id.startswith("anarlog-sess-1-")
        kind = c.metadata["content_subtype"]
        assert c.doc_id.startswith(f"anarlog-sess-1-{kind}-")


def test_metadata_carries_linkage_fields():
    c = normalise_session(_session())[0]
    assert c.metadata["source_type"] == "anarlog"
    assert c.metadata["session_id"] == "sess-1"
    assert c.metadata["event_id"] == "evt-9"
    assert c.metadata["series_id"] == "ser-3"
    assert c.metadata["meeting_title"] == "ACC Staff Meeting"
    assert c.metadata["started_at"] == "2026-09-17T02:00:00Z"


def test_missing_transcript_yields_no_transcript_chunk():
    chunks = normalise_session(_session(transcript=""))
    assert all(c.metadata["content_subtype"] != "transcript" for c in chunks)


def test_empty_session_yields_no_chunks():
    assert normalise_session(_session(documents={}, transcript="")) == []


def test_content_hash_is_stable_across_calls():
    a = normalise_session(_session())
    b = normalise_session(_session())
    assert [c.content_hash for c in a] == [c.content_hash for c in b]


def test_multi_piece_lineage_tracks_total_and_resets_per_kind():
    # Create a transcript long enough to force multiple pieces
    long_text = " ".join(["word"] * 500)  # ~2500 chars, will split into multiple pieces
    chunks = normalise_session(_session(transcript=long_text))

    # Filter to transcript chunks only
    transcript_chunks = [c for c in chunks if c.metadata["content_subtype"] == "transcript"]
    assert len(transcript_chunks) >= 2, "Expected at least 2 transcript pieces"

    # Check doc_id suffixes increment
    for i, c in enumerate(transcript_chunks):
        assert c.doc_id == f"anarlog-sess-1-transcript-{i}"

    # Check chunk_index increments
    for i, c in enumerate(transcript_chunks):
        assert c.metadata["chunk_index"] == i

    # Check chunk_total equals piece count
    for c in transcript_chunks:
        assert c.metadata["chunk_total"] == len(transcript_chunks)

    # Check that summary/note chunks have their own chunk_total (should be 1 each)
    summary_chunks = [c for c in chunks if c.metadata["content_subtype"] == "summary"]
    note_chunks = [c for c in chunks if c.metadata["content_subtype"] == "note"]

    for c in summary_chunks:
        assert c.metadata["chunk_total"] == len(summary_chunks)
    for c in note_chunks:
        assert c.metadata["chunk_total"] == len(note_chunks)


def test_metadata_records_the_external_provider():
    """M3: external_provider is stamped so a "" event_id on a session that
    plainly HAS an external event is explainable from the chunk alone."""
    c = normalise_session(_session(external_provider="granola"))[0]
    assert c.metadata["external_provider"] == "granola"


def test_series_id_is_carried_but_is_not_wired_recurrence():
    """`sessions.series_id` is '' on every live row — anarlog keeps recurrence
    in `events.recurrence_series_id`, which this source does not read, and
    nothing in mcpbrain reads series_id off chunk metadata. Pinned so a future
    reader does not mistake the key for a working linkage."""
    c = normalise_session(_session(series_id=""))[0]
    assert c.metadata["series_id"] == ""
