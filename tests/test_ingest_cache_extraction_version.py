"""Task 8: per-MIME extraction version in the ingest-cache fingerprint.

A block-extracted MIME (PDF, DOCX, PPTX, RTF, Google Docs/Slides) suffixes
`+x<N>` onto the effective chunker version so a future extractor-output
change can invalidate only that MIME's cached artifacts, never spreadsheets
or any other untouched type — see sync/blocks.EXTRACTION_VERSIONS.
"""
from mcpbrain import ingest_cache
from mcpbrain.org_contracts import FleetPin
from mcpbrain.store import Store
from tests.helpers.org_fleet import LocalDirFleetStorage

PIN = FleetPin(embed_model="bge-small", dim=4, chunker_version="v1",
               enrich_logic_floor=1, fleet_secret="s3cret")


def test_block_mime_suffixes_version():
    base = ingest_cache.effective_chunker_version(PIN)
    assert ingest_cache.effective_chunker_version(PIN, "application/pdf") == base + "+x1"


def test_non_block_mime_unchanged():
    base = ingest_cache.effective_chunker_version(PIN)
    xlsx = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert ingest_cache.effective_chunker_version(PIN, xlsx) == base
    assert ingest_cache.effective_chunker_version(PIN, "") == base


def _store(tmp_path, name="a.sqlite3"):
    s = Store(tmp_path / name, dim=4)
    s.init()
    return s


def test_pdf_artifact_round_trips_under_the_suffixed_fingerprint(tmp_path):
    """publish_file derives mime from the stored chunk's mime_type metadata, so
    a PDF file publishes under the +x1-suffixed fingerprint, and a peer install
    passing mime="application/pdf" to try_import finds it there."""
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    A = _store(tmp_path, "A.sqlite3")
    doc_id = "gdrive-F1-0"
    A.import_cached_chunk(
        doc_id, "some pdf text", "ch0",
        {"source_type": "gdrive", "file_id": "F1", "chunk_index": 0,
         "drive_id": "D1", "mime_type": "application/pdf"},
        [0.1, 0.2, 0.3, 0.4])

    assert ingest_cache.publish_file(A, fs, "D1", "F1", "vh1", PIN) is True

    B = _store(tmp_path, "B.sqlite3")
    assert ingest_cache.try_import(B, fs, "D1", "F1", "vh1", PIN,
                                    mime="application/pdf") is True


def test_unsuffixed_artifact_is_not_served_to_a_pdf_mime_request(tmp_path):
    """An artifact published under the PRE-CHANGE (unsuffixed) fingerprint —
    e.g. a file whose stored chunks carry no mime_type — sits at a different
    cache path than a mime="application/pdf" request looks for, so it is a
    clean miss (never silently served as if it were the same pipeline)."""
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    A = _store(tmp_path, "A.sqlite3")
    doc_id = "gdrive-F2-0"
    A.import_cached_chunk(
        doc_id, "some other text", "ch0",
        {"source_type": "gdrive", "file_id": "F2", "chunk_index": 0, "drive_id": "D1"},
        [0.1, 0.2, 0.3, 0.4])

    # No mime_type in metadata -> publish_file derives mime="" -> unsuffixed
    # fingerprint (today's behaviour, unchanged).
    assert ingest_cache.publish_file(A, fs, "D1", "F2", "vh1", PIN) is True

    B = _store(tmp_path, "B.sqlite3")
    assert ingest_cache.try_import(B, fs, "D1", "F2", "vh1", PIN,
                                    mime="application/pdf") is False
    # The unsuffixed request still finds it — proves the miss above is the
    # fingerprint split, not a broken publish.
    assert ingest_cache.try_import(B, fs, "D1", "F2", "vh1", PIN) is True
