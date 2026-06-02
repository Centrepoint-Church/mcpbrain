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

from mcpbrain.chunking import chunk_text, content_hash
from mcpbrain.sync.normalise import Chunk
from mcpbrain.sync.extractors import (
    extract_text_from_pdf,
    extract_text_from_docx,
    extract_text_from_xlsx,
)


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

_CHANGES_FIELDS = (
    "nextPageToken,newStartPageToken,"
    "changes(fileId,removed,file(id,name,mimeType,modifiedTime,owners))"
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
        raw = service.files().export(
            fileId=file_meta["id"], mimeType=_EXPORT[mime]
        ).execute()
    elif mime in _DOWNLOAD_TEXT:
        raw = service.files().get_media(fileId=file_meta["id"]).execute()
    elif mime in _DOWNLOAD_BINARY:
        raw = service.files().get_media(fileId=file_meta["id"]).execute()
        data = raw if isinstance(raw, bytes) else str(raw).encode("utf-8", "replace")
        return _DOWNLOAD_BINARY[mime](data)
    else:
        return None
    return raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def normalise_drive(file_meta: dict, text: str) -> list[Chunk]:
    """Convert Drive file metadata + text content into indexable Chunks.

    doc_id format: gdrive-<file_id>-<chunk_index>.
    Empty or whitespace-only text returns [].
    """
    if not text or not text.strip():
        return []

    fid = file_meta["id"]

    owner = ""
    owners = file_meta.get("owners") or []
    if owners:
        owner = owners[0].get("displayName", "")

    base_meta = {
        "source_type": "gdrive",
        "file_id": fid,
        "file_name": file_meta.get("name", "")[:200],
        "mime_type": file_meta.get("mimeType", "")[:100],
        "modified": file_meta.get("modifiedTime", ""),
        "owner": owner[:100],
    }

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


def backfill_drive(service, store, modified_after: str, max_files: int | None = None) -> int:
    """One-shot bounded backfill via files.list with a modifiedTime filter.

    Text-native files only (reuses _fetch_text, which returns None for binaries).
    Does NOT touch the changes cursor. Returns the number of files indexed.
    """
    q = f"modifiedTime > '{modified_after}'"
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
