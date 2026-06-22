"""Tests for Q5 Drive-appropriate extraction.

Covers:
- reassemble_thread grouping multi-chunk Drive docs by file_id (not doc_id)
- normalise_drive tagging chunks with content_subtype / extraction_method / confidence
- Two distinct Drive docs staying separate in reassembly
"""

from mcpbrain import thread_enrich
from mcpbrain.sync.drive import normalise_drive


# ---------------------------------------------------------------------------
# reassemble_thread — Drive doc assembly
# ---------------------------------------------------------------------------

def _drive_chunk(doc_id, file_id, text, chunk_index=0, *, file_name="doc.txt",
                 mime="text/plain", modified="2026-01-01", owner="Alice"):
    return {
        "doc_id": doc_id,
        "text": text,
        "metadata": {
            "source_type": "gdrive",
            "file_id": file_id,
            "file_name": file_name,
            "mime_type": mime,
            "modified": modified,
            "owner": owner,
            "chunk_index": chunk_index,
        },
    }


def _email_chunk(doc_id, message_id, thread_id, text, chunk_index=0):
    return {
        "doc_id": doc_id,
        "text": text,
        "metadata": {
            "source_type": "gmail",
            "message_id": message_id,
            "thread_id": thread_id,
            "sender": "bob@example.com",
            "date": "2026-01-01",
            "chunk_index": chunk_index,
        },
    }


def test_multichunk_drive_doc_assembles_into_one_message():
    """All chunks of the same Drive file join into a single assembled message."""
    chunks = [
        _drive_chunk("gdrive-abc-0", "abc", "Part 1 of the doc.", chunk_index=0),
        _drive_chunk("gdrive-abc-1", "abc", "Part 2 continues here.", chunk_index=1),
        _drive_chunk("gdrive-abc-2", "abc", "Part 3 wraps up.", chunk_index=2),
    ]
    messages = thread_enrich.reassemble_thread(chunks)
    assert len(messages) == 1, f"expected 1 assembled message, got {len(messages)}"
    body = messages[0]["text"]
    assert "Part 1 of the doc." in body
    assert "Part 2 continues here." in body
    assert "Part 3 wraps up." in body
    assert messages[0]["message_id"] == "abc"


def test_multichunk_drive_doc_chunks_in_order():
    """Chunks are joined in chunk_index order regardless of insertion order."""
    chunks = [
        _drive_chunk("gdrive-abc-2", "abc", "Third.", chunk_index=2),
        _drive_chunk("gdrive-abc-0", "abc", "First.", chunk_index=0),
        _drive_chunk("gdrive-abc-1", "abc", "Second.", chunk_index=1),
    ]
    messages = thread_enrich.reassemble_thread(chunks)
    assert len(messages) == 1
    body = messages[0]["text"]
    assert body.index("First.") < body.index("Second.") < body.index("Third.")


def test_two_distinct_drive_docs_stay_separate():
    """Chunks from different Drive files produce separate messages."""
    chunks = [
        _drive_chunk("gdrive-abc-0", "abc", "Doc A content.", file_name="a.txt"),
        _drive_chunk("gdrive-xyz-0", "xyz", "Doc B content.", file_name="b.txt"),
    ]
    messages = thread_enrich.reassemble_thread(chunks)
    assert len(messages) == 2
    ids = {m["message_id"] for m in messages}
    assert ids == {"abc", "xyz"}


def test_drive_doc_subject_comes_from_file_name():
    """The assembled message uses file_name as its subject field."""
    chunks = [
        _drive_chunk("gdrive-abc-0", "abc", "Content.", file_name="Board Minutes.docx"),
    ]
    messages = thread_enrich.reassemble_thread(chunks)
    assert messages[0]["subject"] == "Board Minutes.docx"


def test_drive_doc_sender_comes_from_owner():
    """Drive chunks carry 'owner' (not 'sender'); the assembled message exposes it."""
    chunks = [
        _drive_chunk("gdrive-abc-0", "abc", "Content.", owner="Jane Smith"),
    ]
    messages = thread_enrich.reassemble_thread(chunks)
    assert messages[0]["sender"] == "Jane Smith"


def test_email_chunks_still_group_by_message_id():
    """Email chunks continue to group by message_id after the Drive fix."""
    chunks = [
        _email_chunk("gmail-m1-0", "m1", "thread-A", "Email body part 1.", chunk_index=0),
        _email_chunk("gmail-m1-1", "m1", "thread-A", "Email body part 2.", chunk_index=1),
    ]
    messages = thread_enrich.reassemble_thread(chunks)
    assert len(messages) == 1
    assert messages[0]["message_id"] == "m1"
    assert "Email body part 1." in messages[0]["text"]


def test_drive_doc_date_comes_from_modified():
    """The assembled Drive message uses the file's modifiedTime as its date."""
    chunks = [
        _drive_chunk("gdrive-abc-0", "abc", "Content.", modified="2025-11-15T10:00:00Z"),
    ]
    messages = thread_enrich.reassemble_thread(chunks)
    assert "2025-11-15" in messages[0]["date"]


# ---------------------------------------------------------------------------
# normalise_drive — per-type metadata tagging
# ---------------------------------------------------------------------------

def _file_meta(file_id, name, mime, modified="2026-01-01"):
    return {"id": file_id, "name": name, "mimeType": mime,
            "modifiedTime": modified, "owners": []}


def test_normalise_drive_prose_doc_tags_prose():
    chunks = normalise_drive(_file_meta("a", "notes.txt", "text/plain"),
                             "This is a prose document with some content here.")
    assert chunks, "expected at least one chunk"
    assert chunks[0].metadata["content_subtype"] == "prose"
    assert chunks[0].metadata["extraction_method"] == "text"
    assert chunks[0].metadata["confidence"] == 1.0


def test_normalise_drive_gdoc_tags_prose():
    chunks = normalise_drive(
        _file_meta("a", "doc", "application/vnd.google-apps.document"),
        "A Google Doc with meaningful prose content inside it.")
    assert chunks
    assert chunks[0].metadata["content_subtype"] == "prose"
    assert chunks[0].metadata["extraction_method"] == "gdocs"


def test_normalise_drive_spreadsheet_tags_table():
    chunks = normalise_drive(
        _file_meta("a", "data.xlsx",
                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        "col1 | col2\nval1 | val2\nval3 | val4\n" * 10)
    assert chunks
    assert chunks[0].metadata["content_subtype"] == "table"
    assert chunks[0].metadata["extraction_method"] == "spreadsheet"
    assert chunks[0].metadata["confidence"] == 1.0


def test_normalise_drive_google_sheets_tags_table():
    chunks = normalise_drive(
        _file_meta("a", "sheet", "application/vnd.google-apps.spreadsheet"),
        "col1,col2\nval1,val2\n" * 10)
    assert chunks
    assert chunks[0].metadata["content_subtype"] == "table"


def test_normalise_drive_pdf_tags_prose_with_lower_confidence():
    chunks = normalise_drive(
        _file_meta("a", "report.pdf", "application/pdf"),
        "A PDF document with text content and prose inside it.")
    assert chunks
    assert chunks[0].metadata["content_subtype"] == "prose"
    assert chunks[0].metadata["extraction_method"] == "pdf_layout"
    assert chunks[0].metadata["confidence"] == 0.95


def test_normalise_drive_slides_tags_slides():
    chunks = normalise_drive(
        _file_meta("a", "deck", "application/vnd.google-apps.presentation"),
        "Slide 1 title. Slide 2 content. More slides here with information.")
    assert chunks
    assert chunks[0].metadata["content_subtype"] == "slides"
    assert chunks[0].metadata["extraction_method"] == "slides"


def test_normalise_drive_csv_tags_table():
    chunks = normalise_drive(
        _file_meta("a", "data.csv", "text/csv"),
        "name,value\nAlice,1\nBob,2\n" * 5)
    assert chunks
    assert chunks[0].metadata["content_subtype"] == "table"


def test_normalise_drive_unknown_mime_defaults_to_prose():
    chunks = normalise_drive(
        _file_meta("a", "file.bin", "application/octet-stream"),
        "Some binary content that was extracted as text for testing purposes here.")
    assert chunks
    assert chunks[0].metadata["content_subtype"] == "prose"
    assert chunks[0].metadata["extraction_method"] == "text"


def test_normalise_drive_empty_returns_no_chunks():
    chunks = normalise_drive(_file_meta("a", "empty.txt", "text/plain"), "")
    assert chunks == []


def test_normalise_drive_all_chunks_carry_metadata():
    """All chunks of a multi-chunk document carry the same extraction metadata."""
    # ~4000 words should exceed the default chunk threshold
    long_text = ("The quick brown fox jumps over the lazy dog. " * 100 + "\n\n") * 3
    chunks = normalise_drive(_file_meta("a", "long.txt", "text/plain"), long_text)
    # Either multi-chunk or single: every chunk must carry the metadata
    assert chunks, "expected at least one chunk"
    for chunk in chunks:
        assert chunk.metadata["content_subtype"] == "prose"
        assert chunk.metadata["extraction_method"] == "text"
        assert "confidence" in chunk.metadata


def test_xlsx_extracts_as_markdown_table():
    """Q5: spreadsheets render as a markdown table (header + separator), not flat lines."""
    import io, openpyxl
    from mcpbrain.sync.extractors import extract_text_from_xlsx
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Budget"
    ws.append(["Item", "Cost"]); ws.append(["Camp", "500"]); ws.append(["Bus", "200"])
    buf = io.BytesIO(); wb.save(buf)
    out = extract_text_from_xlsx(buf.getvalue())
    assert "### Sheet: Budget" in out
    assert "| Item | Cost |" in out
    assert "| --- | --- |" in out
    assert "| Camp | 500 |" in out
