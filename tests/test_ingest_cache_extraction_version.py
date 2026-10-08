"""Task 8: per-MIME extraction version in the ingest-cache fingerprint.

A block-extracted MIME (PDF, DOCX, PPTX, RTF, Google Docs/Slides) suffixes
`+x<N>` onto the effective chunker version so a future extractor-output
change can invalidate only that MIME's cached artifacts, never spreadsheets
or any other untouched type — see sync/blocks.EXTRACTION_VERSIONS.
"""
import base64
import gzip
import json
import struct

from mcpbrain import ingest_cache
from mcpbrain.org_contracts import CacheArtifact, CacheChunk, FleetPin, artifact_filename
from mcpbrain.store import Store
from mcpbrain.sync.blocks import extraction_version
from tests.helpers.org_fleet import LocalDirFleetStorage

PIN = FleetPin(embed_model="bge-small", dim=4, chunker_version="v1",
               enrich_logic_floor=1, fleet_secret="s3cret")


def test_block_mime_suffixes_version():
    base = ingest_cache.effective_chunker_version(PIN)
    xv = extraction_version("application/pdf")
    assert ingest_cache.effective_chunker_version(PIN, "application/pdf") == base + f"+x{xv}"


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
         "drive_id": "D1", "mime_type": "application/pdf",
         "extraction_version": extraction_version("application/pdf")},
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


def _b64(vec):
    return base64.b64encode(struct.pack(f"<{len(vec)}f", *vec)).decode("ascii")


def _write_raw_artifact(fs, file_id, content_hash, *, chunker, mime_type="", dim=4,
                        embed_model="bge-small", published_at="2026-07-03", extra=None):
    """Write a CacheArtifact straight to fleet storage, bypassing publish_file,
    to simulate an artifact a PEER install left behind under a specific
    (possibly stale/pre-change) fingerprint — exactly what bootstrap_drive and
    the GC functions must be able to see and correctly accept/reject."""
    meta = {"source_type": "gdrive", "file_id": file_id, "chunk_index": 0}
    if mime_type:
        meta["mime_type"] = mime_type
    meta.update(extra or {})
    chunks = (CacheChunk(idx=0, text="pdf text", embedding_b64=_b64([0.1, 0.2, 0.3, 0.4]),
                        metadata=meta),)
    art = CacheArtifact(file_id=file_id, content_hash=content_hash,
                        extraction_method="pdf", chunker_version=chunker,
                        embed_model=embed_model, dim=dim, chunks=chunks,
                        enrich={}, published_by="p@x.org", published_at=published_at)
    fname = artifact_filename(file_id, content_hash, embed_model, dim, chunker)
    fs.put_bytes(f"{ingest_cache.CACHE_DIR}/{fname}",
                gzip.compress(json.dumps(art.to_dict()).encode()))


def test_bootstrap_drive_imports_a_suffixed_pdf_artifact(tmp_path):
    """Fix round 1, finding 1: bootstrap_drive's fingerprint filter used to
    hard-match only the unsuffixed base pf8, so a +x1-suffixed PDF artifact
    was skipped before it even reached _import_artifact (and wasn't counted
    in 'skipped' either). It must now be recognised as a current-pipeline
    artifact and actually imported."""
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    A = _store(tmp_path, "A.sqlite3")
    A.import_cached_chunk(
        "gdrive-F1-0", "pdf text", "ch0",
        {"source_type": "gdrive", "file_id": "F1", "chunk_index": 0,
         "drive_id": "D1", "mime_type": "application/pdf",
         "extraction_version": extraction_version("application/pdf")},
        [0.1, 0.2, 0.3, 0.4])
    assert ingest_cache.publish_file(A, fs, "D1", "F1", "vh1", PIN) is True

    B = _store(tmp_path, "B.sqlite3")
    summary = ingest_cache.bootstrap_drive(B, fs, "D1", PIN)
    assert summary["imported"] == 1 and summary["chunks"] == 1
    assert summary["cache_hits"] == 1
    assert B.get_chunk("gdrive-F1-0") is not None


def test_bootstrap_drive_rejects_a_pre_change_unsuffixed_pdf_artifact(tmp_path):
    """Fix round 1, finding 1: an unsuffixed pre-Task-8 PDF artifact still
    sitting in the fleet must NOT be imported at onboarding — it is exactly
    the old-extractor-output chunks the +x<N> suffix exists to exclude. Its
    pf8 matches the BASE fingerprint (so it must still be considered a
    candidate — other-pipeline artifacts coexist untouched), but deriving its
    mime from the artifact's own chunk metadata and re-checking against
    effective_chunker_version(pin, mime) must reject it."""
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    unsuffixed = ingest_cache.effective_chunker_version(PIN)
    _write_raw_artifact(fs, "F2", "vh2", chunker=unsuffixed, mime_type="application/pdf")

    B = _store(tmp_path, "B.sqlite3")
    summary = ingest_cache.bootstrap_drive(B, fs, "D1", PIN)
    assert summary["imported"] == 0
    assert summary["skipped"] == 1          # a real candidate, correctly rejected
    assert B.get_chunk("gdrive-F2-0") is None


def test_gc_superseded_collects_a_stale_pdf_version(tmp_path):
    """Fix round 1, finding 2: gc_superseded compared only against the
    unsuffixed base pf8, so a superseded PDF (or DOCX/PPTX/RTF/Google-Doc)
    artifact was never collected — an unbounded leak, one artifact per edit
    for every live block-MIME file. It must now collect the stale version
    while never touching the current one."""
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    A1 = _store(tmp_path, "A1.sqlite3")
    A1.import_cached_chunk(
        "gdrive-F1-0", "v1 text", "ch0",
        {"source_type": "gdrive", "file_id": "F1", "chunk_index": 0,
         "drive_id": "D1", "mime_type": "application/pdf",
         "extraction_version": extraction_version("application/pdf")},
        [0.1, 0.2, 0.3, 0.4])
    assert ingest_cache.publish_file(A1, fs, "D1", "F1", "vh1", PIN, skip_gc=True) is True

    A2 = _store(tmp_path, "A2.sqlite3")
    A2.import_cached_chunk(
        "gdrive-F1-0", "v2 text", "ch0b",
        {"source_type": "gdrive", "file_id": "F1", "chunk_index": 0,
         "drive_id": "D1", "mime_type": "application/pdf",
         "extraction_version": extraction_version("application/pdf")},
        [0.5, 0.6, 0.7, 0.8])
    assert ingest_cache.publish_file(A2, fs, "D1", "F1", "vh2", PIN, skip_gc=True) is True

    removed = ingest_cache.gc_superseded(fs, "D1", "F1", "vh2", PIN)
    assert removed == 1

    B = _store(tmp_path, "B.sqlite3")
    assert ingest_cache.try_import(B, fs, "D1", "F1", "vh1", PIN, mime="application/pdf") is False
    # The current artifact must never be collected.
    assert ingest_cache.try_import(B, fs, "D1", "F1", "vh2", PIN, mime="application/pdf") is True


def test_gc_superseded_batch_collects_a_stale_pdf_version(tmp_path):
    """Same defect and fix as test_gc_superseded_collects_a_stale_pdf_version,
    through the batch entry point onboarding/publish loops actually use."""
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    A1 = _store(tmp_path, "A1.sqlite3")
    A1.import_cached_chunk(
        "gdrive-F1-0", "v1 text", "ch0",
        {"source_type": "gdrive", "file_id": "F1", "chunk_index": 0,
         "drive_id": "D1", "mime_type": "application/pdf",
         "extraction_version": extraction_version("application/pdf")},
        [0.1, 0.2, 0.3, 0.4])
    assert ingest_cache.publish_file(A1, fs, "D1", "F1", "vh1", PIN, skip_gc=True) is True

    A2 = _store(tmp_path, "A2.sqlite3")
    A2.import_cached_chunk(
        "gdrive-F1-0", "v2 text", "ch0b",
        {"source_type": "gdrive", "file_id": "F1", "chunk_index": 0,
         "drive_id": "D1", "mime_type": "application/pdf",
         "extraction_version": extraction_version("application/pdf")},
        [0.5, 0.6, 0.7, 0.8])
    assert ingest_cache.publish_file(A2, fs, "D1", "F1", "vh2", PIN, skip_gc=True) is True

    removed = ingest_cache.gc_superseded_batch(fs, "D1", {"F1": "vh2"}, PIN)
    assert removed == 1

    B = _store(tmp_path, "B.sqlite3")
    assert ingest_cache.try_import(B, fs, "D1", "F1", "vh1", PIN, mime="application/pdf") is False
    assert ingest_cache.try_import(B, fs, "D1", "F1", "vh2", PIN, mime="application/pdf") is True
