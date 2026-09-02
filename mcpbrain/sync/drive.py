"""Google Drive delta sync via the Changes API.

Implements bootstrap (getStartPageToken) + incremental delta (changes.list).
Content fetch covers:
  - Google Docs / Slides  → export as text/plain
  - Google Sheets         → export as text/csv
  - text/plain, text/markdown, text/csv → get_media
  - application/pdf       → get_media + pymupdf extraction (OCR optional via tesseract)
  - application/vnd.openxmlformats-officedocument.wordprocessingml.document → get_media + python-docx
  - application/vnd.openxmlformats-officedocument.spreadsheetml.sheet → get_media + openpyxl
  - application/vnd.ms-excel (legacy .xls) → get_media + xlrd (A2)
  - message/rfc822 (.eml) → get_media + stdlib email (A2)
  - text/tab-separated-values, application/csv, application/rtf,
    application/json, text/html → get_media

Images, audio, video and anything else not in the routing tables below are
skipped (fetch_content returns None) — but the skip is now RECORDED rather than
silent (B7); see `_note_skip` / `flush_skip_report`.

The cursor-advance-after-durable-write guarantee is maintained by collecting
all pending (file_meta, Content, folder) triples across pages before writing
anything to the store, then advancing the cursor only after all upserts
complete.
"""

import hashlib
import json
import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from dataclasses import dataclass

from googleapiclient.errors import HttpError

from mcpbrain import config
from mcpbrain.chunking import CHUNKER_VERSION, chunk_text, content_hash, has_content
from mcpbrain.org_contracts import DRIVE_ID_META_KEY
from mcpbrain.sync import ingest_report, tabular
from mcpbrain.sync.normalise import Chunk
from mcpbrain.sync.extractors import (
    extract_tables_from_xls,
    extract_tables_from_xlsx,
    extract_text_from_docx,
    extract_text_from_eml,
    extract_text_from_pdf,
    extract_text_from_pptx,
    is_partial,
)
from mcpbrain.sync.tabular import Table

log = logging.getLogger(__name__)

_NUM_RETRIES = 5  # see mcpbrain.backup._NUM_RETRIES for the full rationale


# ---------------------------------------------------------------------------
# MIME routing tables
# ---------------------------------------------------------------------------

_EXPORT = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}

_DOWNLOAD_TEXT = {"text/plain", "text/markdown", "text/csv",
                  "application/csv", "text/tab-separated-values",
                  "application/rtf", "application/json", "text/html"}

_DOWNLOAD_BINARY = {
    "application/pdf": extract_text_from_pdf,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": extract_text_from_docx,
    # NOTE(Task 2 -> Task 4 handoff): extract_text_from_xlsx was replaced by
    # extract_tables_from_xlsx, which returns structured Table objects rather
    # than pre-rendered text (mcpbrain.sync.tabular) so chunk boundaries can be
    # decided by tabular.render_chunks instead of orphaning the header in
    # chunk 0 (B2). .xlsx/.xls are wired in below via fetch_content's
    # binary_tables dict instead — this dict stays text-shaped.
    "application/vnd.openxmlformats-officedocument.presentationml.presentation":
        extract_text_from_pptx,
    "message/rfc822": extract_text_from_eml,
}

# Per-MIME extraction metadata: (extraction_method, content_subtype, confidence).
# Stored on each chunk so the enrich pipeline and recall layer know what kind of
# content they are dealing with (table vs prose vs slides) and how reliable the
# text extraction is (PDFs may miss layout; scanned PDFs degrade further but
# tesseract is not tracked here — it stays at pdf_layout confidence for now).
_MIME_EXTRACTION_META: dict[str, tuple[str, str, float]] = {
    "application/pdf": ("pdf_layout", "prose", 0.95),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ("docx", "prose", 1.0),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ("spreadsheet", "table", 1.0),
    "application/vnd.google-apps.spreadsheet": ("spreadsheet", "table", 1.0),
    "application/vnd.google-apps.document": ("gdocs", "prose", 1.0),
    "application/vnd.google-apps.presentation": ("slides", "slides", 1.0),
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ("slides", "slides", 1.0),
    "text/csv": ("text", "table", 1.0),
    "application/csv": ("text", "table", 1.0),
    "text/tab-separated-values": ("text", "table", 1.0),
    "text/plain": ("text", "prose", 1.0),
    "text/markdown": ("text", "prose", 1.0),
    "application/rtf": ("text", "prose", 0.9),
    "application/json": ("text", "prose", 1.0),
    "text/html": ("text", "prose", 0.9),
    "message/rfc822": ("eml", "prose", 0.95),
    "application/vnd.ms-excel": ("spreadsheet", "table", 1.0),   # legacy .xls
}

_CHANGES_FIELDS = (
    "nextPageToken,newStartPageToken,"
    "changes(fileId,removed,file(id,name,mimeType,modifiedTime,owners,"
    "md5Checksum,version,size,parents))"
)


# ---------------------------------------------------------------------------
# Content fetch
# ---------------------------------------------------------------------------

@dataclass
class Content:
    """What one Drive file yielded. `tables` is set only for tabular MIME types,
    where the chunker needs structure rather than text (see sync/tabular.py).

    `partial` (I9) means the extractor died PARTWAY through the document and this
    is what it had so far — so the caller must not read the short result as a
    shrunk document and delete the chunks the failed extraction never reached.
    See extractors.PartialTables and upsert_file_chunks."""
    text: str = ""
    tables: list[Table] | None = None
    partial: bool = False


def _fetch_text(service, file_meta: dict) -> str | None:
    """Return decoded text for supported types, else None (skip).

    Google Docs/Slides/Sheets are exported (_EXPORT); the plain-text family
    (text/plain, text/markdown, text/csv, TSV, RTF, JSON, HTML — _DOWNLOAD_TEXT)
    is fetched via get_media and decoded. PDF, DOCX, PPTX and .eml
    (_DOWNLOAD_BINARY) are fetched via get_media and run through the binary
    extractors in mcpbrain.sync.extractors. Images and other binary types
    return None.

    XLSX/XLS are deliberately NOT here: they are intercepted earlier by
    fetch_content's `binary_tables` dict and extracted as structured Tables
    (sync/tabular.py) rather than pre-rendered text, so they never reach this
    function. This docstring said otherwise for a while — see _DOWNLOAD_BINARY's
    note.
    """
    mime = file_meta.get("mimeType", "")
    if mime in _EXPORT:
        # NB: files.export does NOT accept supportsAllDrives (unlike get/get_media/
        # list) — it is not in the Drive v3 discovery doc for export, so passing it
        # raises TypeError at call-build time. Shared-drive Google-native docs export
        # fine by fileId alone.
        raw = service.files().export(
            fileId=file_meta["id"], mimeType=_EXPORT[mime]
        ).execute(num_retries=_NUM_RETRIES)
    elif mime in _DOWNLOAD_TEXT:
        raw = service.files().get_media(
            fileId=file_meta["id"], supportsAllDrives=True
        ).execute(num_retries=_NUM_RETRIES)
    elif mime in _DOWNLOAD_BINARY:
        raw = service.files().get_media(
            fileId=file_meta["id"], supportsAllDrives=True
        ).execute(num_retries=_NUM_RETRIES)
        data = raw if isinstance(raw, bytes) else str(raw).encode("utf-8", "replace")
        return _DOWNLOAD_BINARY[mime](data)
    else:
        return None
    return raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw


def _note_skip(store, report: dict | None, kind: str, fid: str, mime: str, name: str) -> None:
    """Record one skipped file — either immediately (today's behaviour, when
    no `report` is given) or by tallying it into `report` for the caller to
    flush as one bounded summary row per `kind` at the end of a sync round.

    Important (review finding, post-Task-4-approval): `fetch_content` used to
    call `ingest_report.record_skip` — one `store.record_change` write — per
    skipped file. `change_log` is pruned to 500 rows and doubles as the
    user-facing change digest (dashboard.py's recent_changes); a Drive sync
    whose window contains a few hundred images would evict the entire genuine
    audit trail and fill the digest with `ingest_skip: image/png` noise, plus
    cost one write transaction per skip inside the (unbounded) fetch loop.
    Mirrors the reviewed `report=` pattern in sync/gmail.py + normalise.py's
    `_note`, adapted to key on (kind, mime) so the eventual summary can still
    say WHICH mimes were skipped, not just a bare count.
    """
    if report is None:
        ingest_report.record_skip(store, kind, fid, f"{mime} ({name})")
        return
    # Aggregating loses the per-file audit trail that the immediate-write branch
    # above keeps, and in production EVERY caller passes `report` — so without
    # this line it is no longer possible to tell from any record WHICH file was
    # skipped. Debug level: the point of the tally is that these can be
    # hundreds per round.
    log.debug("drive: %s skipped file %s (%s, %r)", kind, fid, mime, name)
    key = (kind, mime)
    report[key] = report.get(key, 0) + 1


def flush_skip_report(store, report: dict, *, source: str = "drive") -> None:
    """Turn one sync round's `fetch_content` skip tally into a small, bounded
    number of change_log rows — one per `kind` (today: at most
    'unsupported_mime' and 'extraction_empty'), with a per-mime breakdown in
    the detail string, instead of one row per skipped file. `report` is
    typically empty (nothing skipped) — then this is a no-op.

    `source` is the round's own identifier ("drive", or "drive:<driveId>" for a
    Shared Drive) and becomes the row's `ref_id`, mirroring sync/gmail.py's
    reviewed pattern of passing `source` there. An empty ref_id made the
    aggregated rows untraceable to the drive that produced them.
    """
    by_kind: dict[str, list[tuple[str, int]]] = {}
    for (kind, mime), count in report.items():
        by_kind.setdefault(kind, []).append((mime, count))
    for kind, mimes in sorted(by_kind.items()):
        detail = ", ".join(f"{mime}: {count}" for mime, count in sorted(mimes))
        ingest_report.record_skip(store, f"drive_{kind}", source, detail)


def fetch_content(service, file_meta: dict, *, store=None,
                  report: dict | None = None) -> Content | None:
    """Fetch one Drive file, and leave a durable trace when it yields nothing.

    Three outcomes, and they must stay distinguishable (B7 exists because they
    were not):
      - a Content with text or tables — ingest it;
      - a Content that is empty — a SUPPORTED type that extracted to nothing,
        i.e. a corrupt or image-only file worth investigating;
      - None — a type we never claimed to handle.

    A Content may also be `partial` (I9): the extractor died partway and this is
    what it had. Callers must pass that through to `upsert_file_chunks` so the
    orphan sweep is skipped.

    Types deliberately still unsupported, and now RECORDED rather than silently
    skipped: legacy .doc/.ppt, Apple .pages/.numbers/.keynote, .zip, and every
    image/audio/video format. Each needs a new dependency of doubtful value or a
    different pipeline; what changes here is that a sync no longer reports
    success while discarding them. (Legacy .xls and .eml WERE on that list and
    are now supported — extract_tables_from_xls / extract_text_from_eml, A2.)

    `report`, when passed (by a sync-round caller — see `flush_skip_report`),
    accumulates skips as {(kind, mime): count} instead of writing one
    change_log row per file immediately. Direct callers (tests, one-off
    lookups) that omit it keep today's immediate-write behaviour unchanged.
    """
    mime = file_meta.get("mimeType", "")
    fid = file_meta.get("id", "")
    name = file_meta.get("name", "")
    budget = config.sheet_char_budget(str(config.app_dir()))

    binary_tables = {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
            extract_tables_from_xlsx,
        "application/vnd.ms-excel": extract_tables_from_xls,   # legacy .xls
    }
    if mime in binary_tables:
        raw = service.files().get_media(
            fileId=fid, supportsAllDrives=True).execute(num_retries=_NUM_RETRIES)
        data = raw if isinstance(raw, bytes) else str(raw).encode("utf-8", "replace")
        tables = binary_tables[mime](data, char_budget=budget)
        if not tables:
            _note_skip(store, report, "extraction_empty", fid, mime, name)
        return Content(tables=tables, partial=is_partial(tables))

    text = _fetch_text(service, file_meta)
    if text is None:
        _note_skip(store, report, "unsupported_mime", fid, mime, name)
        return None
    if not text.strip():
        _note_skip(store, report, "extraction_empty", fid, mime, name)
        return Content(partial=is_partial(text))
    if tabular.is_tabular(mime):
        # Google Sheets export as text/csv, and CSV/TSV download verbatim — both
        # converge on the same Table shape as XLSX so there is ONE renderer. The
        # delimiter comes from the MIME (I4): TSV parsed with a comma collapsed
        # every row into one 300-char-truncated cell.
        return Content(tables=tabular.tables_from_csv(
            text, sheet=name or "Sheet1", char_budget=budget,
            delimiter=tabular.delimiter_for_mime(mime)))
    return Content(text=text, partial=is_partial(text))


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def normalise_drive(file_meta: dict, text: str, drive_id: str | None = None, *,
                    tables: list[Table] | None = None, folder: str = "") -> list[Chunk]:
    """Convert Drive file metadata + text content (or structured `tables`) into
    indexable Chunks.

    doc_id format: gdrive-<file_id>-<chunk_index>.
    When drive_id is given (a true Shared Drive file), it is stamped into each
    chunk's metadata under DRIVE_ID_META_KEY so revocation can target it; My Drive
    / shared-with-me files pass drive_id=None and the key stays absent.

    `tables` (from fetch_content) routes through tabular.render_chunks instead
    of chunk_text — a spreadsheet is not prose, and character-splitting it
    orphans the header in chunk 0 (B2). `folder` (from folder_path) is stamped
    as metadata['folder_path'] for embed.contextual_prefix (C5).
    """
    if not tables and (not text or not text.strip()):
        return []

    fid = file_meta["id"]

    owner = ""
    owners = file_meta.get("owners") or []
    if owners:
        owner = owners[0].get("displayName", "")

    mime = file_meta.get("mimeType", "")
    extraction_method, content_subtype, confidence = _MIME_EXTRACTION_META.get(
        mime, ("text", "prose", 1.0)
    )

    base_meta = {
        "source_type": "gdrive",
        "chunker_version": CHUNKER_VERSION,
        "file_id": fid,
        "file_name": file_meta.get("name", "")[:200],
        "mime_type": mime[:100],
        "modified": file_meta.get("modifiedTime", ""),
        "owner": owner[:100],
        "extraction_method": extraction_method,
        "content_subtype": content_subtype,
        "confidence": confidence,
    }
    if drive_id:
        base_meta[DRIVE_ID_META_KEY] = drive_id
    if folder:
        base_meta["folder_path"] = folder[:300]

    if tables:
        rendered = tabular.render_chunks(tables, file_name=base_meta["file_name"],
                                         max_chars=tabular.CHUNK_CHARS)
    else:
        rendered = [(t, {}) for t in chunk_text(text)]

    kept = [(t, extra) for t, extra in rendered if has_content(t)]
    out = []
    for i, (chunk, extra) in enumerate(kept):
        meta = {**base_meta, **extra, "chunk_index": i, "chunk_total": len(kept)}
        out.append(Chunk(doc_id=f"gdrive-{fid}-{i}", text=chunk,
                         content_hash=content_hash(chunk), metadata=meta))
    return out


def folder_path(service, file_meta: dict, cache: dict) -> str:
    """The file's folder chain, e.g. 'Finance/Budgets'.

    C5: embed.contextual_prefix (default ON) reads metadata['folder_path'] and
    normalise_drive never wrote it. `cache` maps folder_id -> (name, parents) and
    is owned by the CALLER for a whole sync round, so a Drive with 5,000 files in
    40 folders costs 40 lookups, not 5,000. Any failure degrades to '' —
    provenance must never break a sync. Depth-capped at 8 and cycle-guarded,
    because Drive shortcuts can produce a loop.
    """
    names: list[str] = []
    parents = file_meta.get("parents") or []
    seen: set[str] = set()
    while parents and len(names) < 8:
        fid = parents[0]
        if fid in seen:
            break
        seen.add(fid)
        if fid not in cache:
            try:
                info = service.files().get(
                    fileId=fid, fields="id,name,parents",
                    supportsAllDrives=True).execute(num_retries=_NUM_RETRIES)
                cache[fid] = (info.get("name", ""), info.get("parents") or [])
            except Exception as exc:  # noqa: BLE001 — provenance is best-effort
                log.debug("folder_path: lookup failed for %s: %s", fid, exc)
                cache[fid] = ("", [])
        name, parents = cache[fid]
        if name:
            names.append(name)
    return "/".join(reversed(names))


def upsert_file_chunks(store, chunks: list[Chunk], *, file_id: str,
                       partial: bool = False) -> int:
    """Upsert one Drive file's chunks and delete the ones it no longer has.

    B5: doc_ids are positional (gdrive-<fid>-<i>) and every write path only ever
    upserted. When a document shrank from m chunks to n, indices n..m-1 survived
    — deleted paragraphs stayed searchable indefinitely and were re-fed to
    expansion as current content, with nothing able to detect it (no chunk
    recorded its document's total until C1).

    `partial=True` (I9, from Content.partial) means the extraction that produced
    `chunks` died PARTWAY through the document: the chunks it never reached are
    missing because of the failure, not because the document shrank. The orphan
    sweep is skipped entirely in that case — whatever WAS extracted is still
    written/updated, but nothing is deleted. Without this, a transient failure on
    sheet 3 of 5 permanently deleted sheets 3-5's previously-good chunks, and
    nothing re-triggers extraction for a file whose metadata never changes again.

    Returns the number of orphans deleted (0 when partial). `store.delete_chunks`
    also clears the matching vec_chunks and fts_chunks rows, so the stale text
    does not survive in either retrieval arm.

    A chunk whose text is byte-identical to what is already stored still has its
    METADATA refreshed. `store.upsert_chunk` short-circuits on an unchanged
    content_hash and writes nothing at all — text, embedding AND metadata — so
    without this a re-ingest of a file that re-chunks identically (most legacy
    prose: spec 2 changed empty/oversize emission and tabular routing, not prose
    boundaries) would never acquire `chunker_version`. Since
    `store.stale_chunker_file_ids` selects on exactly that field and orders by
    MIN(rowid), those files would stay in the stale set forever and
    `bin/repair.py reingest-stale --limit N` would re-fetch the same oldest N
    files on every run, burning Drive quota with zero progress while reporting
    success. `patch_chunk_metadata` MERGES the fresh metadata without touching
    content_hash or `embedded`, so nothing is spuriously re-queued for embedding
    and post-write flags (e.g. `expired`) on an unchanged chunk survive.
    """
    for c in chunks:
        if not store.upsert_chunk(c.doc_id, c.text, c.content_hash, c.metadata):
            store.patch_chunk_metadata(c.doc_id, **c.metadata)
    if partial:
        log.warning("drive: %s extracted only partially; skipping the orphan "
                    "sweep so previously-good chunks are not deleted", file_id)
        return 0
    written = {c.doc_id for c in chunks}
    orphans = [d for d in store.doc_ids_for_file(file_id) if d not in written]
    if orphans:
        log.info("drive: %s shrank; deleting %d orphaned chunk(s)",
                 file_id, len(orphans))
        store.delete_chunks(orphans)
    return len(orphans)


def list_shared_drives(service) -> list[dict]:
    """Every Shared Drive the user can see (paginated drives.list). Returns dicts
    with at least id + name. My Drive is NOT included — it has no shared cache."""
    out: list[dict] = []
    page_token = None
    while True:
        resp = service.drives().list(
            pageSize=100, fields="nextPageToken,drives(id,name)",
            pageToken=page_token,
        ).execute(num_retries=_NUM_RETRIES)
        out.extend(resp.get("drives", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return out


def _file_content_hash(file_meta: dict) -> str:
    """A cross-user-stable file-VERSION id, computable from Changes metadata alone
    (so the cache read path can key on it before extraction). Binary files carry a
    Drive md5Checksum; Google-native files (Docs/Sheets/Slides) do not, so we hash
    the monotonic `version` + modifiedTime, which is identical across installs.

    If BOTH md5Checksum and version/modifiedTime are missing/empty, there is no
    usable version signal at all — hashing the empty pair would produce a
    constant ("|") that never changes, meaning the file's cache entry would
    never invalidate even after the file's content changes (permanent silent
    staleness). Given this function's signature (no cache/store access, no
    way to signal "uncacheable" to callers without changing every call site),
    the safest choice is to force a perpetual cache miss instead: fold in a
    fresh random nonce so the returned hash can never match any previously
    (or subsequently) cached hash for this file, including one from a prior
    call with the exact same degenerate metadata. Callers keep working
    unchanged — they just always treat this file as changed and re-extract
    it, which is wasteful but never silently stale.
    """
    md5 = file_meta.get("md5Checksum")
    if md5:
        return md5
    version = file_meta.get("version") or ""
    modified = file_meta.get("modifiedTime") or ""
    if not version and not modified:
        fid = file_meta.get("id", "<unknown>")
        log.info(
            "drive: file %s has no md5Checksum, version, or modifiedTime — "
            "content hash cannot be computed; forcing a permanent cache miss "
            "for this file instead of a degenerate constant hash", fid,
        )
        raw = f"{fid}|uncacheable|{uuid.uuid4().hex}"
        return hashlib.sha256(raw.encode()).hexdigest()
    raw = f"{version}|{modified}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _file_resume_key(fmeta: dict) -> str | None:
    """Resume-set key for one file: file id + a version signal, so an item
    EDITED mid-round (while its id is already in the resume set from an
    earlier budget-truncated call) is recognized as new work rather than
    silently skipped for the rest of the round (Critical bug found in
    adversarial review round 4: keying on bare id meant an edit between two
    calls in the same open round was permanently lost once the round closed
    and the cursor advanced past it — reproduced directly: an edited file's
    stored text stayed at its pre-edit content after the round closed).

    Deliberately does NOT reuse `_file_content_hash` here: that function's
    "no usable version signal at all" fallback folds in a fresh random nonce
    on every call (correct for ITS purpose — never serve a stale cache hit
    for a versionless file) but would be wrong here — a resume key that
    changes every call for the same unedited file would never re-match
    `resumed_ids`, blocking that file's round from ever closing at all. This
    instead degrades to plain id-only keying for that narrow edge case (no
    md5Checksum, no version, no modifiedTime) — matching the pre-fix
    behaviour for exactly that subset of files (an edit to a genuinely
    versionless file mid-round can still be missed) — because real Drive
    files overwhelmingly carry at least one of these fields.
    """
    fid = fmeta.get("id")
    if not fid:
        return None
    md5 = fmeta.get("md5Checksum")
    if md5:
        return f"{fid}|{md5}"
    version = fmeta.get("version") or ""
    modified = fmeta.get("modifiedTime") or ""
    if version or modified:
        return f"{fid}|{version}|{modified}"
    return fid


def _cache_first_extract_one(
    service, store, fleet_storage, drive_id, fmeta, pin,
    *, contextual_retrieval: bool = False, bulk_section=None,
    folder_cache: dict | None = None, report: dict | None = None,
) -> tuple[bool, tuple[str, str] | None]:
    """Cache-first extraction of ONE Shared-Drive file, shared by the delta-sync
    (sync_shared_drive) and backfill (backfill_shared_drive) loops.

    Sequence: compute the content-version hash; try the ingest cache; on a miss
    fetch the content, RE-CHECK the cache immediately before the expensive path
    (herd-race shrink, spec §A2 — another daemon may have just published while
    we were fetching), then normalise + upsert.

    `folder_cache` (C5) is a dict the CALLER owns for a whole sync round (see
    `folder_path`'s docstring) — a fresh `{}` is used when a caller doesn't
    pass one, but callers that want cross-file caching within a round (both
    real callers do) must pass their own. `report`, similarly caller-owned,
    accumulates `fetch_content`'s skip tally for the round instead of writing
    one change_log row per file — see `fetch_content`/`flush_skip_report`.

    `bulk_section` (Task 2 duty-cycle fix: a zero-arg context-manager factory,
    default `contextlib.nullcontext`) brackets ONLY the final local-extraction
    upsert loop below — NOT the two `ingest_cache.try_import` calls, the
    `fetch_content` call, or the `folder_path` lookup. Those are genuinely
    unbounded network I/O (a Drive-API cache-artifact download, a Drive export
    + PDF/DOCX extraction, and a folder-metadata lookup) that must never be
    held under `_bulk_lock` — an earlier version of this function put the
    caller's WHOLE call (including both of those) inside one bulk section,
    directly contradicting the same hoist-network-I/O-out-of-the-section
    principle already applied one function away in `backfill_drive`. The
    cache-HIT path's own write (inside `try_import` -> `_import_artifact`) is
    its own separate, already-atomic SQLite transaction that runs outside this
    advisory lock — a deliberate, small trade-off (cache hits are the common,
    fast case) rather than further restructuring `ingest_cache.try_import`
    itself.

    Returns (processed, miss):
      - processed is True when the file counted as processed — either a cache
        hit or a successful local extraction that yielded at least one chunk;
        False when skipped (unsupported/empty content, or no chunks produced).
      - miss is (file_id, content_hash) when we extracted locally and the
        caller must publish the artifact after embedding; None otherwise
        (cache hit or skip — nothing new to publish).

    A PARTIAL extraction (I9: the extractor died partway, so `content.partial`)
    is indexed locally but deliberately returns miss=None: the miss list is what
    gets published to the shared-drive ingest cache, and publishing a truncated
    document would propagate the truncation to every other install in the fleet
    keyed on a content hash that says it is complete — self-healing only when the
    file next changes. Locally it self-heals on the next sync of the same version;
    the fleet artifact would not. Same "don't do X for a partial" policy as the
    orphan-delete skip in upsert_file_chunks.

    Exceptions propagate; callers that need per-file isolation wrap the call.
    """
    from mcpbrain import ingest_cache

    if bulk_section is None:
        bulk_section = nullcontext
    if folder_cache is None:
        folder_cache = {}

    fid = fmeta["id"]
    content_h = _file_content_hash(fmeta)
    if ingest_cache.try_import(store, fleet_storage, drive_id, fid, content_h, pin,
                               contextual_retrieval=contextual_retrieval):
        return True, None
    content = fetch_content(service, fmeta, store=store, report=report)
    if content is None or (not content.text and not content.tables):
        return False, None
    # Re-check right before extraction: another daemon may have just published.
    if ingest_cache.try_import(store, fleet_storage, drive_id, fid, content_h, pin,
                               contextual_retrieval=contextual_retrieval):
        return True, None
    chunks = normalise_drive(fmeta, content.text, drive_id=drive_id,
                             tables=content.tables,
                             folder=folder_path(service, fmeta, folder_cache))
    if not chunks:
        return False, None
    with bulk_section():
        upsert_file_chunks(store, chunks, file_id=fid, partial=content.partial)
    if content.partial:
        log.warning("drive: %s extracted only partially; NOT publishing it to "
                    "the ingest cache (a truncated artifact would propagate "
                    "fleet-wide under a complete-looking content hash)", fid)
        return True, None
    return True, (fid, content_h)


# ---------------------------------------------------------------------------
# Sync entry point
# ---------------------------------------------------------------------------

def sync_drive(service, store, source: str = "drive", *, budget=None,
               bulk_section=None) -> int:
    """Incremental Drive sync via the Changes API.

    First run (no cursor): calls changes.getStartPageToken, stores the token
    as the cursor, and returns 0. No files are fetched; the next run will
    pick up all changes since that point.

    Subsequent runs: pages through changes.list since the stored cursor.
    For each non-removed change with a text-native MIME type, text is fetched
    and buffered. After all pages are consumed, every pending file is
    normalised and upserted. The cursor advances to newStartPageToken only
    after all upserts are durable.

    Any exception during fetch or upsert propagates before the cursor is
    written, leaving the cursor unchanged (safe to retry).

    Bounded and INCREMENTALLY checkpoint-safe (Task 2 duty-cycle fix):
    `budget` (a `Budget`, or None for unbounded) is checked once per
    changes.list page (that loop only fetches+buffers text, it does not
    write the store) and once per not-yet-resumed pending file in the upsert
    loop. On expiry the REAL cursor is NOT advanced — `newStartPageToken` is
    only emitted by Google on the FINAL page anyway (mirroring Gmail's
    historyId, an intermediate advance would silently skip unvisited
    changes).

    Merely "don't advance the cursor" is not enough once one delta exceeds a
    single budget's worth of files: every subsequent call would re-list the
    same cursor, collect the same pending list, and upsert the same prefix
    every time — the cursor would never advance and files past the prefix
    would never be durably ingested (this exact livelock was reproduced and
    fixed for Gmail; see sync_gmail's docstring). So a second piece of
    state — `f"{source}:resume_ids"` (a JSON list of `_file_resume_key`
    id+version composites already durably upserted for the CURRENT,
    not-yet-committed delta round) — is checked in the upsert loop: a key
    already in it is skipped (genuine forward progress, not just an
    idempotent re-upsert), and the set is grown and persisted after each
    call. Keying on id+version rather than bare id matters: a file EDITED
    mid-round (while its id is already resumed from an earlier
    budget-truncated call) produces a DIFFERENT key and is therefore
    reprocessed, not silently skipped for the rest of the round (Critical bug
    found in adversarial review — reproduced directly: an edited file's
    stored text stayed at its pre-edit content after the round closed and the
    cursor advanced past it). Only once every pending file this round is
    accounted for does the real cursor advance and the resume set clear.
    (The pagination loop's own `fetch_content`/`folder_path` calls ARE gated
    on the resume set — see the `rkey in resumed_ids` check below — so a
    resumed round does not re-fetch already-checkpointed files; the upsert
    loop repeats the SAME check independently because `pending` is rebuilt
    fresh each call.) `bulk_section` (a zero-arg context-manager factory,
    default `contextlib.nullcontext`) brackets each file's writes so
    `_bulk_lock` is released between files.

    Returns the number of files processed (files that yielded at least one
    chunk) THIS call. May be a partial count when the budget expired
    mid-run; already-resumed files from a prior call are not re-counted.
    """
    if bulk_section is None:
        bulk_section = nullcontext
    cursor = store.get_cursor(source)

    # Bootstrap: no prior cursor
    if cursor is None:
        tok = service.changes().getStartPageToken().execute(
            num_retries=_NUM_RETRIES)["startPageToken"]
        store.set_cursor(source, str(tok))
        return 0

    resume_key = f"{source}:resume_ids"
    page_key = f"{source}:page_token"
    try:
        resumed_ids: set = set(json.loads(store.get_cursor(resume_key) or "[]"))
    except (ValueError, TypeError):
        resumed_ids = set()

    # Delta: page through changes.list, RESUMING where a budget-truncated round
    # left off. Restarting at `cursor` every round is a livelock whenever the
    # feed is longer than one budget: `newStartPageToken` comes back only on the
    # final page, so a round that never reaches it can never advance `cursor`,
    # and the next round re-walks the identical prefix. Live (2026-07-29 ->
    # 2026-09-02, author's store): five weeks of re-walking ~5,000 changes every
    # ~80s, ~25% of a core, and not one Drive change ingested in that window.
    # The durable watermark stays `cursor`; this is only a within-feed offset,
    # so a lost/cleared page_token costs a re-walk, never a missed change.
    page_token = store.get_cursor(page_key) or cursor
    new_start = None
    interrupted = False
    # Collect (file_meta, content, folder) across all pages before writing to
    # the store. This keeps the advance-after-durable-write guarantee simple:
    # the cursor is set only after every upsert completes.
    pending: list[tuple[dict, Content, str]] = []
    # C5: owned by this whole sync call (not per file) — folder_path's own
    # cache contract, so a Drive with many files in few folders costs one
    # lookup per folder, not per file.
    folder_cache: dict = {}
    # Review finding (post-Task-4-approval): fetch_content used to record one
    # change_log row per skipped file. A window with a few hundred images
    # would flood the 500-row-pruned, user-facing digest. Tallied here for
    # this whole round and flushed as one bounded summary row per kind — see
    # flush_skip_report.
    skip_report: dict = {}

    while True:
        if budget is not None and budget.expired():
            interrupted = True
            break
        resp = service.changes().list(
            pageToken=page_token,
            spaces="drive",
            includeRemoved=True,
            fields=_CHANGES_FIELDS,
        ).execute(num_retries=_NUM_RETRIES)

        for ch in resp.get("changes", []):
            if ch.get("removed"):
                continue
            fmeta = ch.get("file") or {}
            if not fmeta.get("id"):
                continue
            # Skip files this round already durably wrote BEFORE paying for the
            # download. _file_resume_key is computable from the change metadata
            # alone, so a truncated round no longer re-exports work it has
            # already checkpointed -- pure waste, and charged against the very
            # budget that is running out.
            rkey = _file_resume_key(fmeta)
            if rkey and rkey in resumed_ids:
                continue
            content = fetch_content(service, fmeta, store=store, report=skip_report)
            if content is None or (not content.text and not content.tables):
                continue
            folder = folder_path(service, fmeta, folder_cache)
            pending.append((fmeta, content, folder))

        new_start = resp.get("newStartPageToken", new_start)
        nxt = resp.get("nextPageToken")
        if not nxt:
            break
        page_token = nxt

    # Upsert all collected files not already resumed, then advance cursor.
    # Keyed on id+version (_file_resume_key), NOT bare id: a file edited
    # mid-round (after its id was already resumed from an earlier
    # budget-truncated call) must be recognized as new work, not skipped.
    # The resume set is persisted PER FILE (not once after the whole loop) so
    # an exception partway through (a poison file, a process death, a
    # STALL_S watchdog restart) never discards the checkpoint for files
    # already durably upserted earlier in this same call.
    processed = 0
    for fmeta, content, folder in pending:
        rkey = _file_resume_key(fmeta)
        if rkey and rkey in resumed_ids:
            continue
        # Minimum forward progress: honour the budget only once this call has
        # written something. The fetch phase above is unbounded (one network
        # export per changed file), so the budget is routinely already spent by
        # the time we get here -- checking it before the first item yields zero
        # writes, leaves resumed_ids unchanged, and re-does the identical work
        # next cycle. Guaranteeing one item per call is what makes the round
        # monotonic and the livelock impossible.
        if processed and budget is not None and budget.expired():
            interrupted = True
            break
        with bulk_section():
            chunks = normalise_drive(fmeta, content.text, tables=content.tables,
                                     folder=folder)
            upsert_file_chunks(store, chunks, file_id=fmeta["id"],
                               partial=content.partial)
            if chunks:
                processed += 1
        if rkey:
            resumed_ids.add(rkey)
            store.set_cursor(resume_key, json.dumps(sorted(resumed_ids)))

    pending_keys = {_file_resume_key(fmeta) for fmeta, _, _ in pending
                   if _file_resume_key(fmeta)}
    # Everything this round PAGED PAST is now durably handled: skipped files
    # write nothing, and collected ones are in resumed_ids. Only then may the
    # paging offset move -- advancing it past a file the write loop did not
    # reach would step over that file for good.
    all_written = pending_keys <= resumed_ids
    if new_start and not interrupted and all_written:
        store.set_cursor(source, str(new_start))
        store.set_cursor(resume_key, "[]")
        store.set_cursor(page_key, "")      # round complete; start clean
    elif all_written:
        store.set_cursor(page_key, str(page_token))

    flush_skip_report(store, skip_report, source=source)
    return processed


def sync_shared_drive(service, store, drive_id, *, fleet_storage, pin,
                      contextual_retrieval: bool = False, budget=None,
                      bulk_section=None) -> dict:
    """Incremental sync for ONE Shared Drive via the Changes API, cache-first.

    Cursor key is 'drive:<driveId>' in sync_cursors. First run stores
    getStartPageToken(driveId=...) and returns. Delta runs page through
    changes.list(driveId=..., includeItemsFromAllDrives=True). NOTE:
    `corpora` is a files.list-only kwarg — changes.list rejects it (TypeError),
    so it must never be added here (see drive.py:560 for the legitimate
    files.list use).
    For each non-removed file: try the ingest cache first; on a miss, fetch the
    text, RE-CHECK the cache immediately before the expensive path (herd-race
    shrink, spec §A2), then extract + upsert and record the miss so the caller can
    publish after embedding. Removed files are purged locally and their artifacts
    deleted. The cursor advances only after every write completes.

    Bounded and INCREMENTALLY checkpoint-safe (Task 2 duty-cycle fix):
    `budget` (a `Budget`, or None for unbounded).

    The changes.list PAGINATION loop below collapses possibly-recurring
    per-fileId events into ONE final-state view (see the comment inline) — it
    genuinely needs every page to be correct, unlike Gmail/Calendar/My-Drive's
    independent read/write loops. So if `budget` expires mid-pagination, the
    per-file processing loops are SKIPPED ENTIRELY this call (not run against a
    knowingly-incomplete collapse) and the cursor is left untouched — safe,
    just deferred to the next attempt, which re-lists the same window.

    If pagination completes but the (expensive: network fetch + extraction)
    per-file loop or the removed-file cleanup loop is interrupted instead, the
    collapsed `events` view IS complete/correct. Merely leaving the cursor
    untouched is not enough once ONE collapsed delta is bigger than one
    budget's worth of files, though — every subsequent call would re-collapse
    the same window and re-attempt the same prefix of files every time,
    livelocking exactly like the reproduced-and-fixed Gmail case (see
    sync_gmail's docstring). So two separate pieces of resume state — one for
    the add/change loop (`f"{source}:resume_ids"`) and one for the removal
    loop (`f"{source}:resume_removed_ids"`) — are checked and grown the same
    way as the other three sources. The cursor only advances, and both
    resume sets clear, once pagination completed AND every live file's
    resume key is in the add-resume set AND every removed file id is in the
    removal-resume set.

    The add-resume set is keyed on `_file_resume_key` (id+version), NOT bare
    id: a file EDITED mid-round (while its id is already resumed from an
    earlier budget-truncated call) produces a DIFFERENT key and is therefore
    reprocessed, not silently skipped for the rest of the round (Critical bug
    found in adversarial review — reproduced directly on this function too).
    The removal-resume set stays keyed on bare id — a removal has no
    "version" to re-check; a file can only be removed once per round (the
    add/remove collapse already converges removal-then-restore to the "add"
    bucket with its OWN, current-version key).

    `bulk_section` (a zero-arg context-manager factory, default
    `contextlib.nullcontext`) brackets only the actual chunk-mutating write in
    each loop — `_cache_first_extract_one`'s own upsert (NOT its network
    fetch/cache-download calls — see that function's docstring) and the
    removal loop's `delete_chunks`/`invalidate_local_relations_for_docs`.

    Returns {'processed', 'miss': [(file_id, content_hash)], 'live_file_ids': set}.
    """
    from mcpbrain import ingest_cache

    if bulk_section is None:
        bulk_section = nullcontext
    # C5: owned by this whole sync call, not per file — see folder_path's
    # docstring.
    folder_cache: dict = {}
    # Aggregated skip tally for this round — see flush_skip_report.
    skip_report: dict = {}
    source = f"drive:{drive_id}"
    cursor = store.get_cursor(source)
    if cursor is None:
        tok = service.changes().getStartPageToken(
            driveId=drive_id, supportsAllDrives=True).execute(
            num_retries=_NUM_RETRIES)["startPageToken"]
        store.set_cursor(source, str(tok))
        return {"processed": 0, "miss": [], "live_file_ids": set()}

    resume_key = f"{source}:resume_ids"
    resume_removed_key = f"{source}:resume_removed_ids"
    page_key = f"{source}:page_token"
    try:
        resumed_ids: set = set(json.loads(store.get_cursor(resume_key) or "[]"))
    except (ValueError, TypeError):
        resumed_ids = set()
    try:
        resumed_removed_ids: set = set(json.loads(store.get_cursor(resume_removed_key) or "[]"))
    except (ValueError, TypeError):
        resumed_removed_ids = set()

    # Resume paging where a budget-truncated round stopped -- see sync_drive's
    # page_key comment for the livelock this closes (every shared-drive cursor
    # on the author's store was stuck at 2026-07-29 alongside `drive`).
    page_token = store.get_cursor(page_key) or cursor
    new_start = None
    pagination_interrupted = False
    # Collapse the whole delta into ONE ordered, deduplicated view keyed by
    # fileId. A fileId can legitimately recur across pages (or within one page):
    # edited then re-edited, changed then removed, or removed then restored.
    # Drive emits changes in chronological order, so the LAST event for a file
    # is its true state at the cursor endpoint. We keep only that last event,
    # moving it to the end (pop + reinsert) so the processing order also
    # reflects the latest event. Consequences:
    #   * the same fileId appearing twice is fetched/extracted/published ONCE;
    #   * change-then-removal collapses to a removal (file purged, not
    #     re-extracted); removal-then-change collapses to a change (file
    #     extracted, not purged) — either way we converge on the file's actual
    #     final state rather than replaying every intermediate event.
    # Each value is {"removed": bool, "fmeta": dict | None}.
    events: dict[str, dict] = {}
    while True:
        if budget is not None and budget.expired():
            pagination_interrupted = True
            break
        resp = service.changes().list(
            pageToken=page_token,
            driveId=drive_id,
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
            includeRemoved=True,
            fields=_CHANGES_FIELDS,
        ).execute(num_retries=_NUM_RETRIES)
        for ch in resp.get("changes", []):
            if ch.get("removed"):
                fid = ch.get("fileId")
                if not fid:
                    continue
                events.pop(fid, None)
                events[fid] = {"removed": True, "fmeta": None}
                continue
            fmeta = ch.get("file") or {}
            fid = fmeta.get("id")
            if not fid:
                continue
            events.pop(fid, None)
            events[fid] = {"removed": False, "fmeta": fmeta}
        new_start = resp.get("newStartPageToken", new_start)
        nxt = resp.get("nextPageToken")
        if not nxt:
            break
        page_token = nxt

    processed = 0
    miss: list[tuple[str, str]] = []
    live_ids: set = set()
    interrupted = pagination_interrupted

    if not pagination_interrupted:
        live_ids = {fid for fid, ev in events.items() if not ev["removed"]}

        # Both loops below persist their resume set PER FILE (not once after
        # the whole loop) so an exception (an unhandled store error, a
        # process death, a STALL_S watchdog restart) never discards the
        # checkpoint for files already durably handled earlier in this call.
        for fid, ev in events.items():
            if ev["removed"]:
                continue
            # Keyed on id+version (_file_resume_key), NOT bare id: a file
            # edited mid-round (while its id is already resumed from an
            # earlier budget-truncated call) must be recognized as new work.
            rkey = _file_resume_key(ev["fmeta"])
            if rkey and rkey in resumed_ids:
                continue
            # Minimum forward progress: honour the budget only once this call
            # has written something. Checking before the first item means a
            # budget already spent upstream yields zero writes, leaves the
            # resume set unchanged, and re-does identical work next cycle --
            # the livelock reproduced in sync_drive. One item per call keeps
            # the round monotonic.
            if processed and budget is not None and budget.expired():
                interrupted = True
                break
            try:
                did_process, file_miss = _cache_first_extract_one(
                    service, store, fleet_storage, drive_id, ev["fmeta"], pin,
                    contextual_retrieval=contextual_retrieval, bulk_section=bulk_section,
                    folder_cache=folder_cache, report=skip_report)
                if did_process:
                    processed += 1
                if file_miss:
                    miss.append(file_miss)
            except Exception as exc:  # noqa: BLE001 — isolate one file's failure
                # Without this, one poison file (corrupt doc, transient export
                # error, decode failure) would propagate up to sync_shared_drives'
                # per-drive handler, which skips the WHOLE DRIVE for the cycle
                # WITHOUT advancing the cursor — so the same poison file would be
                # re-fetched and re-fail forever, permanently blocking the drive.
                log.warning(
                    "drive: extraction failed for file %s in drive %s; this "
                    "version is DROPPED and will not be retried until the file "
                    "changes again: %s", fid, drive_id, exc)
                # Still marked done: a poison file must not permanently block
                # the cursor from ever advancing past this round (matches the
                # pre-checkpoint behaviour of "skip it, keep moving").
                #
                # KNOWN GAP: this is genuinely lossy for a TRANSIENT failure (a
                # TLS reset, a 5xx export). An earlier version of this comment
                # claimed the file "will simply be re-attempted-and-fail again
                # next DELTA round" — that is wrong: once the round closes the
                # cursor advances, and the delta from the new cursor no longer
                # contains this change, so the file's current version is never
                # re-fetched. Distinguishing transient from permanent failures
                # (a bounded per-file attempt counter, like chunks.enrich_attempts)
                # is the real fix and is deliberately not attempted here.
                if rkey:
                    resumed_ids.add(rkey)
                    store.set_cursor(resume_key, json.dumps(sorted(resumed_ids)))
                continue
            if rkey:
                resumed_ids.add(rkey)
                store.set_cursor(resume_key, json.dumps(sorted(resumed_ids)))

        for fid, ev in events.items():
            if not ev["removed"]:
                continue
            if fid in resumed_removed_ids:
                continue
            if budget is not None and budget.expired():
                interrupted = True
                break
            with bulk_section():
                doc_ids = store.doc_ids_for_file(fid)
                if doc_ids:
                    store.invalidate_local_relations_for_docs(doc_ids)
                    store.delete_chunks(doc_ids)
            try:
                ingest_cache.remove_file_artifacts(fleet_storage, fid)
            except Exception as exc:  # noqa: BLE001 — artifact GC is best-effort
                log.info("drive: artifact GC skipped for removed file %s: %s", fid, exc)
            resumed_removed_ids.add(fid)
            store.set_cursor(resume_removed_key, json.dumps(sorted(resumed_removed_ids)))

    # live_keys (id+version composites, matching what resumed_ids actually
    # holds — both loops above persist incrementally per file, not once
    # after the whole loop) gates the cursor advance; live_ids (bare fids) is
    # kept separate for the live_file_ids return value other callers consume.
    live_keys = {_file_resume_key(ev["fmeta"]) for fid, ev in events.items()
                if not ev["removed"] and _file_resume_key(ev["fmeta"])}
    removed_ids = {fid for fid, ev in events.items() if ev["removed"]}
    # As in sync_drive: the paging offset may only move once everything this
    # round paged past is durably handled -- here that means both the live
    # files and the removals.
    all_handled = live_keys <= resumed_ids and removed_ids <= resumed_removed_ids
    if new_start and not interrupted and all_handled:
        store.set_cursor(source, str(new_start))
        store.set_cursor(resume_key, "[]")
        store.set_cursor(resume_removed_key, "[]")
        store.set_cursor(page_key, "")      # round complete; start clean
    elif all_handled:
        store.set_cursor(page_key, str(page_token))
    flush_skip_report(store, skip_report, source=source)
    return {"processed": processed, "miss": miss, "live_file_ids": live_ids}


def sync_shared_drives(service, store, *, pin, storage_factory,
                       absence_threshold: int = 3,
                       contextual_retrieval: bool = False,
                       budget=None, bulk_section=None) -> dict:
    """Enumerate all Shared Drives, sync each cache-first, and run the
    consecutive-absence revocation counter.

    `storage_factory(drive_id) -> FleetStorage` builds a drive-scoped transport
    (prod: DriveFleetStorage; tests: LocalDirFleetStorage). Per-drive failures are
    isolated so one broken drive never aborts the others. Returns
    {drive_id: {'processed','miss','storage'}} plus {'_revoked': [ids]}. The
    caller publishes each drive's misses after embedding (see run_sync_cycle).

    Deliberately does NOT sweep the ingest cache off each cycle's delta — see
    the note inline below.

    `budget`/`bulk_section` are threaded straight into each drive's
    `sync_shared_drive` call (see its docstring for the checkpoint contract);
    a fleet with many pinned drives also checks `budget` here, between drives,
    so a huge drive count can't run unbounded even if each individual drive's
    own sync stays within budget.

    `present` (fed to `note_drive_presence`'s absence/purge counter) is built
    from the FULL `list_shared_drives()` enumeration up front, not from which
    drives the per-drive sync loop actually reached before a budget break.
    `list_shared_drives` already fully paginates (drive.py's own
    `drives().list` loop) so the enumeration itself is never partial — but a
    budget break in the per-drive loop below IS common on a large fleet, and
    a still-authorized drive that simply wasn't reached in time is not
    "absent": it must not start accumulating toward
    `note_drive_presence`'s consecutive-absence purge threshold. Building
    `present` from enumeration (a cheap, already-authoritative signal) rather
    than from "visited before budget ran out" fixes exactly that.

    `note_drive_presence` can call `purge_drive` (deletes chunks, invalidates
    relations) — the same `chunks` table the four gated maintenance passes
    mutate — so that call is bracketed in `bulk_section` too (lock-coverage
    regression found in adversarial review: this ran with no lock at all in
    an earlier revision of this task).
    """
    from mcpbrain import ingest_cache

    if bulk_section is None:
        bulk_section = nullcontext
    drives = list_shared_drives(service)
    present = [d.get("id") for d in drives if d.get("id")]

    out: dict = {}
    for d in drives:
        drive_id = d.get("id")
        if not drive_id:
            continue
        drive_name = d.get("name") or "<unnamed>"
        fs = storage_factory(drive_id)
        try:
            res = sync_shared_drive(service, store, drive_id, fleet_storage=fs, pin=pin,
                                    contextual_retrieval=contextual_retrieval,
                                    budget=budget, bulk_section=bulk_section)
        except Exception as exc:  # noqa: BLE001 — isolate one drive's failure
            log.warning("shared-drive sync failed for %s (%s) (skipped): %s",
                        drive_name, drive_id, exc)
            continue
        out[drive_id] = {"processed": res["processed"], "miss": res["miss"], "storage": fs}
        # NOTE: deliberately no sweep_drive() call here. A per-cycle delta
        # (changes.list since the last cursor) only ever contains files that
        # changed since last time — never a complete listing of the drive's
        # files — so it can never be used as the "live" set for a correct
        # sweep. Explicit removal (changes.list's removed events, handled in
        # sync_shared_drive via remove_file_artifacts) and version-churn GC
        # (gc_superseded) already cover cleanup correctly. A genuine full-
        # drive sweep would need a complete, explicitly-full-enumeration-
        # driven pass — out of scope for this per-cycle delta loop.
        if budget is not None and budget.expired():
            break
    with bulk_section():
        revoked = ingest_cache.note_drive_presence(
            store, present, threshold=absence_threshold)["purged"]
    out["_revoked"] = revoked
    return out


def backfill_drive(service, store, modified_after: str,
                   modified_before: str | None = None,
                   max_files: int | None = None, bulk_section=None) -> int:
    """One-shot bounded backfill via files.list with a modifiedTime filter.

    Uses `fetch_content`, which now covers structured tables (.xlsx/.xls) and
    every other type wired into `_DOWNLOAD_TEXT`/`_DOWNLOAD_BINARY`; a type
    fetch_content doesn't handle is recorded as one bounded summary row per
    kind (see `flush_skip_report`) rather than silently skipped or written
    one row per file. Does NOT touch the changes cursor, so a call over an
    unchanged window re-tallies (and re-flushes) the same skips — matching
    backfill_gmail's equivalent per-call flush; still bounded, since it's one
    row per kind per call, not per file. Returns the number of files indexed.

    `modified_before` optionally caps the upper bound (RFC 3339 timestamp) so
    callers can walk a historical window without re-fetching newer files.
    Omit it for the original "everything since X" semantics.

    Already item-bounded by `max_files` (the progressive-backfill step caps
    this at `_BACKFILL_MAX_PER_SOURCE`, default 200) and touches no delta
    cursor, so no budget/checkpoint logic is needed — `bulk_section` (default
    `contextlib.nullcontext`) still brackets each file's writes.

    `folder_cache` (C5) is created once for this whole backfill call, not per
    file — see `folder_path`'s docstring.
    """
    if bulk_section is None:
        bulk_section = nullcontext
    q = f"modifiedTime > '{modified_after}'"
    if modified_before:
        q += f" and modifiedTime < '{modified_before}'"
    fields = "nextPageToken, files(id,name,mimeType,modifiedTime,owners,parents)"
    folder_cache: dict = {}
    skip_report: dict = {}
    page_token, processed = None, 0
    while True:
        params = {"q": q, "fields": fields, "pageSize": 100, "spaces": "drive"}
        if page_token:
            params["pageToken"] = page_token
        resp = service.files().list(**params).execute(num_retries=_NUM_RETRIES)
        for f in resp.get("files", []):
            if max_files is not None and processed >= max_files:
                flush_skip_report(store, skip_report)
                return processed
            content = fetch_content(service, f, store=store, report=skip_report)
            if content is None or (not content.text and not content.tables):
                continue
            chunks = normalise_drive(f, content.text, tables=content.tables,
                                     folder=folder_path(service, f, folder_cache))
            if chunks:
                with bulk_section():
                    upsert_file_chunks(store, chunks, file_id=f["id"],
                                       partial=content.partial)
                    processed += 1
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    flush_skip_report(store, skip_report)
    return processed


def backfill_shared_drive(service, store, drive_id, modified_after, *,
                          fleet_storage, pin, modified_before=None,
                          max_files=None, contextual_retrieval: bool = False,
                          bulk_section=None) -> dict:
    """One-shot bounded backfill for ONE Shared Drive (files.list, driveId-scoped),
    cache-first. Mirrors backfill_drive but adds Shared-Drive query flags, cache
    import/publish parity, and drive_id stamping. Does NOT touch the delta cursor.
    Returns {'processed', 'miss': [(file_id, content_hash)]}.

    Already item-bounded by `max_files` (default `_BACKFILL_MAX_PER_SOURCE`,
    200) and touches no cursor, so no budget/checkpoint logic is needed —
    `bulk_section` (default `contextlib.nullcontext`) still brackets each
    file's extraction+upsert.
    """
    if bulk_section is None:
        bulk_section = nullcontext
    # C5: owned by this whole backfill call, not per file — see folder_path's
    # docstring.
    folder_cache: dict = {}
    # Aggregated skip tally for this round — see flush_skip_report.
    skip_report: dict = {}
    q = f"modifiedTime > '{modified_after}'"
    if modified_before:
        q += f" and modifiedTime < '{modified_before}'"
    fields = ("nextPageToken, files(id,name,mimeType,modifiedTime,owners,"
              "md5Checksum,version,size,parents)")
    page_token, processed = None, 0
    miss: list[tuple[str, str]] = []
    while True:
        params = {
            "q": q, "fields": fields, "pageSize": 100,
            "driveId": drive_id, "corpora": "drive",
            "includeItemsFromAllDrives": True, "supportsAllDrives": True,
        }
        if page_token:
            params["pageToken"] = page_token
        resp = service.files().list(**params).execute(num_retries=_NUM_RETRIES)
        for f in resp.get("files", []):
            if max_files is not None and processed >= max_files:
                flush_skip_report(store, skip_report, source=f"drive:{drive_id}")
                return {"processed": processed, "miss": miss}
            did_process, file_miss = _cache_first_extract_one(
                service, store, fleet_storage, drive_id, f, pin,
                contextual_retrieval=contextual_retrieval, bulk_section=bulk_section,
                folder_cache=folder_cache, report=skip_report)
            if did_process:
                processed += 1
            if file_miss:
                miss.append(file_miss)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    flush_skip_report(store, skip_report, source=f"drive:{drive_id}")
    return {"processed": processed, "miss": miss}


def _reingest_one(service, store, fid, fields, folder_cache, report):
    """One file's fetch+normalise, run either inline (max_workers=1) or on a
    worker thread (max_workers>1). Returns (fid, outcome, payload):

    outcome is "missing" (payload None), "empty" (payload None), "failed"
    (payload None), or "ok" (payload = (chunks, partial)). Never touches the
    store — the caller writes (upsert_file_chunks) on its own thread, so DB
    access stays single-threaded regardless of how many workers fetch
    concurrently.

    "empty" vs "failed" is the difference between PERMANENT and RETRYABLE, and
    conflating them caused a live non-convergence loop: 465 `extraction_empty`
    change_log rows across 10 files in 41 minutes (~46 re-fetches each), every
    one still selected by stale_chunker_file_ids afterwards. A file that
    deterministically yields nothing (verified on the real store: genuinely
    EMPTY spreadsheets, which the new extractor correctly declines) must stop
    being selected; a 429/503/timeout must stay retryable.

    Isolation is per FILE: a 404 (deleted since it was chunked) counts as
    `missing` and its chunks are LEFT ALONE — removal is the delta sync's job,
    not the repair's — and any other failure counts as `failed`. One unreadable
    file in 9,351 must not end the run.
    """
    try:
        # Nested so a 404 here can be reported as `missing` (see above) while
        # any OTHER HttpError (403 permission-denied, 429 rate-limited, a
        # transient 500/503 — all realistic across a 9,351-file batch)
        # re-raises straight into the outer `except Exception` below, which is
        # what actually gives it per-file isolation. Review finding: this used
        # to `raise` out of a SIBLING try/except pair, which escaped the whole
        # function on the first non-404 HttpError and aborted the run instead
        # of moving on to the next file.
        try:
            fmeta = service.files().get(
                fileId=fid, fields=fields,
                supportsAllDrives=True).execute(num_retries=_NUM_RETRIES)
        except HttpError as exc:
            resp = getattr(exc, "resp", None)
            if resp is not None and resp.status == 404:
                log.info("reingest: %s no longer exists in Drive; leaving "
                         "its chunks for the delta sync's removal path", fid)
                return fid, "missing", None
            raise
        content = fetch_content(service, fmeta, store=store, report=report)
        if content is None or (not content.text and not content.tables):
            # Deterministic: the fetch succeeded and there is nothing in it.
            log.info("reingest: %s yielded no content", fid)
            return fid, "empty", None
        chunks = normalise_drive(
            fmeta, content.text, drive_id=fmeta.get("driveId"),
            tables=content.tables,
            folder=folder_path(service, fmeta, folder_cache))
        if not chunks:
            # Also deterministic: content was fetched but nothing in it survived
            # the has_content guard (an all-empty grid, a punctuation-only doc).
            return fid, "empty", None
        return fid, "ok", (chunks, content.partial)
    except Exception as exc:  # noqa: BLE001 — one file must not end the run
        log.warning("reingest: %s failed: %s", fid, exc)
        return fid, "failed", None


def reingest_files(service, store, file_ids, *, bulk_section=None,
                   report: dict | None = None, max_workers: int = 1,
                   service_factory=None) -> dict:
    """Re-fetch and re-chunk specific Drive files by id.

    The mechanism the repair needs and the sync layer lacked: `sync_drive` only
    sees files the Changes API reports as MODIFIED, and `backfill_drive` filters
    on modifiedTime — so a file whose bytes are unchanged but whose CHUNKING is
    out of date (455 spreadsheets clipped at row 200, 9,351 files extracted by
    the pre-per-type extractor) could never be revisited by either.

    Per file: files().get for fresh metadata -> fetch_content -> normalise_drive
    -> upsert_file_chunks, which replaces the file's chunks and deletes the ones
    it no longer has (B5). Touches NO cursor, so it cannot disturb delta sync and
    is safe to interrupt.

    Deliberately bypasses the ingest cache. A cache hit would hand back the
    artifact for this content hash, which is what we are trying to replace — and
    after the chunker_version bump the fingerprint no longer matches anyway (via
    `ingest_cache.effective_chunker_version`, which floors the fleet-distributed
    pin at the local CHUNKER_VERSION; the pin itself lags), so there is nothing
    to hit. Extraction is local and republishing happens through the normal sync
    path.

    Isolation is per FILE: a 404 (deleted since it was chunked) counts as
    `missing` and its chunks are LEFT ALONE — removal is the delta sync's job,
    not the repair's — and any other failure counts as `failed` and moves on.
    One unreadable file in 9,351 must not end the run.

    `max_workers` parallelizes the network-bound half (files().get + fetch_content)
    across a thread pool; `upsert_file_chunks` (the DB write) always runs on the
    calling thread, one file at a time, as results complete — no new write-
    concurrency risk, since SQLite serializes writes anyway. Requires
    `service_factory` (a zero-arg callable building a FRESH service) because
    googleapiclient's Resource wraps a stateful httplib2.Http that is not safe
    to share across threads; each worker thread builds and caches its own via a
    thread-local on first use, so a worker's earlier files reuse its own service
    rather than rebuilding one per call. Falls back to the single-threaded path
    (the `service` argument used directly, exactly as before) whenever
    `max_workers <= 1` or `service_factory` is None — the default, and what
    every existing caller/test still gets unchanged.

    Returns {"files": n_reingested, "missing": n, "failed": n, "orphans": n}.
    """
    if bulk_section is None:
        bulk_section = nullcontext
    # driveId is requested, and passed to normalise_drive below, because
    # upsert_chunk REPLACES a chunk's metadata wholesale rather than merging:
    # re-ingesting a shared-drive file without it would strip the chunk's
    # drive_id stamp, and `ingest_cache.purge_drive` finds content to delete on
    # access revocation via `store.doc_ids_for_drive` — i.e. by that exact
    # stamp. Re-ingested content would otherwise become invisible to revocation
    # and survive it indefinitely. My Drive / shared-with-me files have no
    # driveId; `.get` yields None and the key stays absent, as normalise_drive
    # already expects.
    fields = "id,name,mimeType,modifiedTime,owners,parents,driveId"
    folder_cache: dict = {}
    summary = {"files": 0, "missing": 0, "empty": 0, "failed": 0, "orphans": 0}

    def _apply(fid, outcome, payload):
        if outcome == "missing":
            # Gone from Drive. The chunks are KEPT — removal is the delta sync's
            # job, and a 404 can be a permission change or a move rather than a
            # deletion — but the file must stop being SELECTED, or the repair
            # asks Drive about the same dead ids on every run. Measured on the
            # first real run: {'files': 8, 'missing': 14, 'empty': 10} left
            # exactly those 14 still selected, i.e. the same non-convergence
            # loop as the `empty` case reached through the other branch.
            #
            # Stamping touches only the repair's selector, not the data: if the
            # file reappears, the Changes API re-ingests it on its own terms; if
            # it is genuinely deleted, the delta sync's removal path deletes the
            # chunks.
            with bulk_section():
                for doc_id in store.doc_ids_for_file(fid):
                    store.patch_chunk_metadata(
                        doc_id, chunker_version=CHUNKER_VERSION,
                        reextract_missing=True)
            summary["missing"] += 1
        elif outcome == "empty":
            # The file was read successfully and holds nothing extractable. Stamp
            # the CURRENT chunker version onto the chunks it already has, so the
            # level-triggered selector stops returning it — otherwise every
            # `reingest-stale` run re-fetches it forever (measured live: ~46
            # times each across 10 files in 41 minutes, burning Drive quota and
            # evicting the 500-row change_log with skip noise).
            #
            # The existing chunks are KEPT: whatever the old extractor produced
            # is the best available content for a file the current one cannot
            # read. `reextract_empty` records why the stamp is there, so this is
            # auditable rather than indistinguishable from a successful pass.
            with bulk_section():
                for doc_id in store.doc_ids_for_file(fid):
                    store.patch_chunk_metadata(
                        doc_id, chunker_version=CHUNKER_VERSION,
                        reextract_empty=True)
            summary["empty"] += 1
        elif outcome == "failed":
            summary["failed"] += 1
        else:
            chunks, partial = payload
            with bulk_section():
                summary["orphans"] += upsert_file_chunks(
                    store, chunks, file_id=fid, partial=partial)
            summary["files"] += 1

    if max_workers <= 1 or service_factory is None:
        for fid in file_ids:
            _apply(*_reingest_one(service, store, fid, fields, folder_cache, report))
        return summary

    _local = threading.local()

    def _worker_service():
        if not hasattr(_local, "service"):
            _local.service = service_factory()
        return _local.service

    def _fetch(fid):
        return _reingest_one(_worker_service(), store, fid, fields, folder_cache, report)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_fetch, fid) for fid in file_ids]
        for future in as_completed(futures):
            _apply(*future.result())
    return summary
