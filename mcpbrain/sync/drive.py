"""Google Drive delta sync via the Changes API.

Implements bootstrap (getStartPageToken) + incremental delta (changes.list).
Content fetch covers:
  - Google Docs / Slides  → export as text/plain
  - Google Sheets         → export as text/csv
  - text/plain, text/markdown, text/csv → get_media
  - application/pdf       → get_media + pymupdf extraction (OCR optional via tesseract)
  - application/vnd.openxmlformats-officedocument.wordprocessingml.document → get_media + python-docx
  - application/vnd.openxmlformats-officedocument.spreadsheetml.sheet → get_media + openpyxl

Images and other binary types are still skipped (return None).

The cursor-advance-after-durable-write guarantee is maintained by collecting
all pending (file_meta, text) pairs across pages before writing anything to
the store, then advancing the cursor only after all upserts complete.
"""

import hashlib
import logging
import uuid

from mcpbrain.chunking import chunk_text, content_hash
from mcpbrain.org_contracts import DRIVE_ID_META_KEY
from mcpbrain.sync.normalise import Chunk
from mcpbrain.sync.extractors import (
    extract_text_from_pdf,
    extract_text_from_docx,
    extract_text_from_xlsx,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# MIME routing tables
# ---------------------------------------------------------------------------

_EXPORT = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}

_DOWNLOAD_TEXT = {"text/plain", "text/markdown", "text/csv"}

_DOWNLOAD_BINARY = {
    "application/pdf": extract_text_from_pdf,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": extract_text_from_docx,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": extract_text_from_xlsx,
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
}

_CHANGES_FIELDS = (
    "nextPageToken,newStartPageToken,"
    "changes(fileId,removed,file(id,name,mimeType,modifiedTime,owners,"
    "md5Checksum,version,size))"
)


# ---------------------------------------------------------------------------
# Content fetch
# ---------------------------------------------------------------------------

def _fetch_text(service, file_meta: dict) -> str | None:
    """Return decoded text for supported types, else None (skip).

    Google Docs/Slides/Sheets are exported; text/plain, text/markdown,
    text/csv are fetched via get_media and decoded. PDF, DOCX, and XLSX are
    fetched via get_media and extracted by the binary extractors in
    mcpbrain.sync.extractors. Images and other binary types return None.
    """
    mime = file_meta.get("mimeType", "")
    if mime in _EXPORT:
        # NB: files.export does NOT accept supportsAllDrives (unlike get/get_media/
        # list) — it is not in the Drive v3 discovery doc for export, so passing it
        # raises TypeError at call-build time. Shared-drive Google-native docs export
        # fine by fileId alone.
        raw = service.files().export(
            fileId=file_meta["id"], mimeType=_EXPORT[mime]
        ).execute()
    elif mime in _DOWNLOAD_TEXT:
        raw = service.files().get_media(
            fileId=file_meta["id"], supportsAllDrives=True
        ).execute()
    elif mime in _DOWNLOAD_BINARY:
        raw = service.files().get_media(
            fileId=file_meta["id"], supportsAllDrives=True
        ).execute()
        data = raw if isinstance(raw, bytes) else str(raw).encode("utf-8", "replace")
        return _DOWNLOAD_BINARY[mime](data)
    else:
        return None
    return raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def normalise_drive(file_meta: dict, text: str, drive_id: str | None = None) -> list[Chunk]:
    """Convert Drive file metadata + text content into indexable Chunks.

    doc_id format: gdrive-<file_id>-<chunk_index>.
    When drive_id is given (a true Shared Drive file), it is stamped into each
    chunk's metadata under DRIVE_ID_META_KEY so revocation can target it; My Drive
    / shared-with-me files pass drive_id=None and the key stays absent.
    """
    if not text or not text.strip():
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

    out = []
    for i, chunk in enumerate(chunk_text(text)):
        meta = {**base_meta, "chunk_index": i}
        out.append(Chunk(
            doc_id=f"gdrive-{fid}-{i}",
            text=chunk,
            content_hash=content_hash(chunk),
            metadata=meta,
        ))
    return out


def list_shared_drives(service) -> list[dict]:
    """Every Shared Drive the user can see (paginated drives.list). Returns dicts
    with at least id + name. My Drive is NOT included — it has no shared cache."""
    out: list[dict] = []
    page_token = None
    while True:
        resp = service.drives().list(
            pageSize=100, fields="nextPageToken,drives(id,name)",
            pageToken=page_token,
        ).execute()
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


def _cache_first_extract_one(
    service, store, fleet_storage, drive_id, fmeta, pin,
    *, contextual_retrieval: bool = False,
) -> tuple[bool, tuple[str, str] | None]:
    """Cache-first extraction of ONE Shared-Drive file, shared by the delta-sync
    (sync_shared_drive) and backfill (backfill_shared_drive) loops.

    Sequence: compute the content-version hash; try the ingest cache; on a miss
    fetch the text, RE-CHECK the cache immediately before the expensive path
    (herd-race shrink, spec §A2 — another daemon may have just published while
    we were fetching), then normalise + upsert.

    Returns (processed, miss):
      - processed is True when the file counted as processed — either a cache
        hit or a successful local extraction that yielded at least one chunk;
        False when skipped (unsupported/empty text, or no chunks produced).
      - miss is (file_id, content_hash) when we extracted locally and the
        caller must publish the artifact after embedding; None otherwise
        (cache hit or skip — nothing new to publish).

    Exceptions propagate; callers that need per-file isolation wrap the call.
    """
    from mcpbrain import ingest_cache

    fid = fmeta["id"]
    content_h = _file_content_hash(fmeta)
    if ingest_cache.try_import(store, fleet_storage, drive_id, fid, content_h, pin,
                               contextual_retrieval=contextual_retrieval):
        return True, None
    text = _fetch_text(service, fmeta)
    if not text:
        return False, None
    # Re-check right before extraction: another daemon may have just published.
    if ingest_cache.try_import(store, fleet_storage, drive_id, fid, content_h, pin,
                               contextual_retrieval=contextual_retrieval):
        return True, None
    chunks = normalise_drive(fmeta, text, drive_id=drive_id)
    if not chunks:
        return False, None
    for c in chunks:
        store.upsert_chunk(c.doc_id, c.text, c.content_hash, c.metadata)
    return True, (fid, content_h)


# ---------------------------------------------------------------------------
# Sync entry point
# ---------------------------------------------------------------------------

def sync_drive(service, store, source: str = "drive") -> int:
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

    Returns the number of files processed (files that yielded at least one
    chunk).
    """
    cursor = store.get_cursor(source)

    # Bootstrap: no prior cursor
    if cursor is None:
        tok = service.changes().getStartPageToken().execute()["startPageToken"]
        store.set_cursor(source, str(tok))
        return 0

    # Delta: page through changes.list
    page_token = cursor
    new_start = None
    # Collect (file_meta, text) across all pages before writing to the store.
    # This keeps the advance-after-durable-write guarantee simple: the cursor
    # is set only after every upsert completes.
    pending: list[tuple[dict, str]] = []

    while True:
        resp = service.changes().list(
            pageToken=page_token,
            spaces="drive",
            includeRemoved=True,
            fields=_CHANGES_FIELDS,
        ).execute()

        for ch in resp.get("changes", []):
            if ch.get("removed"):
                continue
            fmeta = ch.get("file") or {}
            if not fmeta.get("id"):
                continue
            text = _fetch_text(service, fmeta)
            if text:
                pending.append((fmeta, text))

        new_start = resp.get("newStartPageToken", new_start)
        nxt = resp.get("nextPageToken")
        if not nxt:
            break
        page_token = nxt

    # Upsert all collected files, then advance cursor
    processed = 0
    for fmeta, text in pending:
        chunks = normalise_drive(fmeta, text)
        for c in chunks:
            store.upsert_chunk(c.doc_id, c.text, c.content_hash, c.metadata)
        if chunks:
            processed += 1

    if new_start:
        store.set_cursor(source, str(new_start))

    return processed


def sync_shared_drive(service, store, drive_id, *, fleet_storage, pin,
                      contextual_retrieval: bool = False) -> dict:
    """Incremental sync for ONE Shared Drive via the Changes API, cache-first.

    Cursor key is 'drive:<driveId>' in sync_cursors. First run stores
    getStartPageToken(driveId=...) and returns. Delta runs page through
    changes.list(driveId=..., corpora='drive', includeItemsFromAllDrives=True).
    For each non-removed file: try the ingest cache first; on a miss, fetch the
    text, RE-CHECK the cache immediately before the expensive path (herd-race
    shrink, spec §A2), then extract + upsert and record the miss so the caller can
    publish after embedding. Removed files are purged locally and their artifacts
    deleted. The cursor advances only after every write completes.

    Returns {'processed', 'miss': [(file_id, content_hash)], 'live_file_ids': set}.
    """
    from mcpbrain import ingest_cache

    source = f"drive:{drive_id}"
    cursor = store.get_cursor(source)
    if cursor is None:
        tok = service.changes().getStartPageToken(
            driveId=drive_id, supportsAllDrives=True).execute()["startPageToken"]
        store.set_cursor(source, str(tok))
        return {"processed": 0, "miss": [], "live_file_ids": set()}

    page_token = cursor
    new_start = None
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
        resp = service.changes().list(
            pageToken=page_token,
            driveId=drive_id,
            corpora="drive",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
            includeRemoved=True,
            fields=_CHANGES_FIELDS,
        ).execute()
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

    live_ids = {fid for fid, ev in events.items() if not ev["removed"]}

    processed = 0
    miss: list[tuple[str, str]] = []
    for fid, ev in events.items():
        if ev["removed"]:
            continue
        try:
            did_process, file_miss = _cache_first_extract_one(
                service, store, fleet_storage, drive_id, ev["fmeta"], pin,
                contextual_retrieval=contextual_retrieval)
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
            log.warning("drive: extraction failed for file %s in drive %s: %s",
                        fid, drive_id, exc)
            continue

    for fid, ev in events.items():
        if not ev["removed"]:
            continue
        doc_ids = store.doc_ids_for_file(fid)
        if doc_ids:
            store.invalidate_local_relations_for_docs(doc_ids)
            store.delete_chunks(doc_ids)
        try:
            ingest_cache.remove_file_artifacts(fleet_storage, fid)
        except Exception as exc:  # noqa: BLE001 — artifact GC is best-effort
            log.info("drive: artifact GC skipped for removed file %s: %s", fid, exc)

    if new_start:
        store.set_cursor(source, str(new_start))
    return {"processed": processed, "miss": miss, "live_file_ids": live_ids}


def sync_shared_drives(service, store, *, pin, storage_factory,
                       absence_threshold: int = 3,
                       contextual_retrieval: bool = False) -> dict:
    """Enumerate all Shared Drives, sync each cache-first, and run the
    consecutive-absence revocation counter.

    `storage_factory(drive_id) -> FleetStorage` builds a drive-scoped transport
    (prod: DriveFleetStorage; tests: LocalDirFleetStorage). Per-drive failures are
    isolated so one broken drive never aborts the others. Returns
    {drive_id: {'processed','miss','storage'}} plus {'_revoked': [ids]}. The
    caller publishes each drive's misses after embedding (see run_sync_cycle).

    Deliberately does NOT sweep the ingest cache off each cycle's delta — see
    the note inline below.
    """
    from mcpbrain import ingest_cache

    out: dict = {}
    present: list[str] = []
    for d in list_shared_drives(service):
        drive_id = d.get("id")
        if not drive_id:
            continue
        present.append(drive_id)
        drive_name = d.get("name") or "<unnamed>"
        fs = storage_factory(drive_id)
        try:
            res = sync_shared_drive(service, store, drive_id, fleet_storage=fs, pin=pin,
                                    contextual_retrieval=contextual_retrieval)
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
    revoked = ingest_cache.note_drive_presence(
        store, present, threshold=absence_threshold)["purged"]
    out["_revoked"] = revoked
    return out


def backfill_drive(service, store, modified_after: str,
                   modified_before: str | None = None,
                   max_files: int | None = None) -> int:
    """One-shot bounded backfill via files.list with a modifiedTime filter.

    Text-native files only (reuses _fetch_text, which returns None for binaries).
    Does NOT touch the changes cursor. Returns the number of files indexed.

    `modified_before` optionally caps the upper bound (RFC 3339 timestamp) so
    callers can walk a historical window without re-fetching newer files.
    Omit it for the original "everything since X" semantics.
    """
    q = f"modifiedTime > '{modified_after}'"
    if modified_before:
        q += f" and modifiedTime < '{modified_before}'"
    fields = "nextPageToken, files(id,name,mimeType,modifiedTime,owners)"
    page_token, processed = None, 0
    while True:
        params = {"q": q, "fields": fields, "pageSize": 100, "spaces": "drive"}
        if page_token:
            params["pageToken"] = page_token
        resp = service.files().list(**params).execute()
        for f in resp.get("files", []):
            if max_files is not None and processed >= max_files:
                return processed
            text = _fetch_text(service, f)
            if text:
                for ch in normalise_drive(f, text):
                    store.upsert_chunk(ch.doc_id, ch.text, ch.content_hash, ch.metadata)
                processed += 1
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return processed


def backfill_shared_drive(service, store, drive_id, modified_after, *,
                          fleet_storage, pin, modified_before=None,
                          max_files=None, contextual_retrieval: bool = False) -> dict:
    """One-shot bounded backfill for ONE Shared Drive (files.list, driveId-scoped),
    cache-first. Mirrors backfill_drive but adds Shared-Drive query flags, cache
    import/publish parity, and drive_id stamping. Does NOT touch the delta cursor.
    Returns {'processed', 'miss': [(file_id, content_hash)]}."""
    q = f"modifiedTime > '{modified_after}'"
    if modified_before:
        q += f" and modifiedTime < '{modified_before}'"
    fields = ("nextPageToken, files(id,name,mimeType,modifiedTime,owners,"
              "md5Checksum,version,size)")
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
        resp = service.files().list(**params).execute()
        for f in resp.get("files", []):
            if max_files is not None and processed >= max_files:
                return {"processed": processed, "miss": miss}
            did_process, file_miss = _cache_first_extract_one(
                service, store, fleet_storage, drive_id, f, pin,
                contextual_retrieval=contextual_retrieval)
            if did_process:
                processed += 1
            if file_miss:
                miss.append(file_miss)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return {"processed": processed, "miss": miss}
