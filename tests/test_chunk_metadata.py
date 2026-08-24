"""patch_chunk_metadata + note_chunks: the expiry/index plumbing for memory notes."""
import base64
import json

from mcpbrain.store import Store


def _b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode()


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


def test_every_write_path_stamps_the_chunker_version():
    """The store cannot currently answer 'which of my chunks predate the current
    chunker' — chunker_version lives only in the org pin, where it keys the
    ingest-cache fingerprint. That question is the whole repair selector, so it
    has to be answerable from a chunk."""
    from mcpbrain.chunking import CHUNKER_VERSION
    from mcpbrain.sync.calendar import normalise_calendar
    from mcpbrain.sync.drive import normalise_drive
    from mcpbrain.sync.normalise import normalise_gmail

    drive = normalise_drive({"id": "f1", "name": "D.txt", "mimeType": "text/plain"},
                            "Some prose worth keeping.")
    gmail = normalise_gmail({"id": "m1", "threadId": "t1", "labelIds": ["INBOX"],
                             "payload": {"mimeType": "text/plain",
                                         "headers": [{"name": "Subject", "value": "s"}],
                                         "body": {"data": _b64("Body text here.")}}})
    cal = normalise_calendar({"id": "e1", "summary": "Standup",
                              "start": {"dateTime": "2026-06-02T09:00:00Z"}})

    for label, chunks in (("drive", drive), ("gmail", gmail), ("calendar", cal)):
        assert chunks, f"{label} fixture produced no chunks"
        for c in chunks:
            assert c.metadata["chunker_version"] == CHUNKER_VERSION, (
                f"{label} chunk is not stamped with the chunker version"
            )


def test_an_attachment_chunk_is_also_stamped():
    from mcpbrain.chunking import CHUNKER_VERSION
    from mcpbrain.sync import attachments

    raw = {"id": "m1", "threadId": "t1", "labelIds": [],
           "payload": {"headers": [{"name": "Subject", "value": "Invoice"}],
                       "parts": [{"filename": "n.txt", "mimeType": "text/plain",
                                  "body": {"attachmentId": "a1", "size": 20}}]}}
    part = attachments.iter_attachment_parts(raw["payload"])[0]

    chunks = attachments.normalise_attachment(raw, part, b"Attachment prose body.")

    assert chunks
    assert chunks[0].metadata["chunker_version"] == CHUNKER_VERSION


def test_the_chunker_version_is_ahead_of_the_pre_spec_2_chunker():
    """Spec 2 changed chunking materially — the chunk_text empty/oversize fix,
    row-group tabular chunks, the has_content guard — and left this unbumped. An
    unbumped version means a fleet member still importing a pre-spec-2 cache
    artifact gets old-shape chunks with no way to know."""
    from mcpbrain.chunking import CHUNKER_VERSION

    assert CHUNKER_VERSION >= 2


def test_stale_chunker_ids_selects_only_out_of_date_drive_files(tmp_path):
    """The level-triggered selector. No queue, no cursor: re-running walks
    forward because each repaired file stops matching. Same shape as
    reflow_outdated_chunks, which is the established pattern here."""
    from mcpbrain.store import Store

    store = Store(tmp_path / "b.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("gdrive-old-0", "legacy text", "h1",
                       {"source_type": "gdrive", "file_id": "old"})          # no version
    store.upsert_chunk("gdrive-mid-0", "half text", "h2",
                       {"source_type": "gdrive", "file_id": "mid",
                        "chunker_version": 1})
    store.upsert_chunk("gdrive-new-0", "fresh text", "h3",
                       {"source_type": "gdrive", "file_id": "new",
                        "chunker_version": 2})

    got = [d["id"] for d in store.stale_chunker_ids(2, limit=10)]

    assert sorted(got) == ["mid", "old"]


def test_stale_chunker_ids_respects_its_limit_across_source_types(tmp_path):
    """Gmail is no longer excluded (that assumption didn't hold -- see
    stale_chunker_ids' docstring) -- this now tests that `limit` is respected
    as a TOTAL across source types, not that Gmail is filtered out."""
    from mcpbrain.store import Store

    store = Store(tmp_path / "b.sqlite3", dim=4)
    store.init()
    for i in range(5):
        store.upsert_chunk(f"gdrive-f{i}-0", f"text {i}", f"h{i}",
                           {"source_type": "gdrive", "file_id": f"f{i}"})
    store.upsert_chunk("gmail-m1-body-0", "mail text", "hm",
                       {"source_type": "gmail", "thread_id": "t1"})

    got = store.stale_chunker_ids(2, limit=3)

    assert len(got) == 3
    # The limit is hit within the gdrive batch (5 candidates, limit 3) before
    # gmail's single candidate is even considered -- sequential by source type.
    assert all(d["source_type"] == "gdrive" for d in got)
