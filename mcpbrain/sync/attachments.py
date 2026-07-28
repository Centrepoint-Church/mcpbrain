"""Email attachment ingestion.

`_find_part_text` returns only text/plain and text/html body parts, and before
this module there was no attachment-handling code in the repo at all: a PDF
emailed to the user was invisible to the brain, while the byte-identical file in
Drive was extracted normally.

Design notes
------------
Attachments keep `source_type: "gmail"` and carry their parent's `message_id`
and `thread_id`, so they join their thread for enrichment, thread expansion and
`doc_ids_for_messages` rather than becoming orphan documents. They are
distinguished by `content_type: "email_attachment"`.

They also inherit the parent's `date`, without which they would be date-blind
and `importance.recency_decay` would return its neutral 0.5 fallback for every
one — the same defect C2 documents for the enriched layer.

Fetching lives here rather than in normalise.py because that module is declared
pure data transformation ("No Google API calls here"), and the boundary is worth
keeping: `normalise_attachment` takes bytes and is testable with no service.
"""

import base64
import logging

from mcpbrain import config
from mcpbrain.chunking import chunk_text, content_hash, has_content
from mcpbrain.sync import ingest_report, tabular
from mcpbrain.sync.extractors import (
    extract_tables_from_xlsx,
    extract_text_from_docx,
    extract_text_from_pdf,
    extract_text_from_pptx,
)
from mcpbrain.sync.normalise import Chunk, get_header

log = logging.getLogger(__name__)

# Gmail's own attachment ceiling is 25 MB; anything claiming more is not
# something we will get whole anyway.
_MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024

# One message with 40 attachments must not spend a whole cycle's budget.
_MAX_ATTACHMENTS_PER_MESSAGE = 10

_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

_EXTRACTORS = {
    "application/pdf": extract_text_from_pdf,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        extract_text_from_docx,
    "application/vnd.openxmlformats-officedocument.presentationml.presentation":
        extract_text_from_pptx,
    "text/plain": lambda b: b.decode("utf-8", errors="replace"),
    "text/markdown": lambda b: b.decode("utf-8", errors="replace"),
}

# Tabular attachments yield Tables, not text, so they get the row-group chunker
# rather than chunk_text — an emailed budget must not be character-split any
# more than a Drive one.
_TABLE_EXTRACTORS = {_XLSX: extract_tables_from_xlsx}

_CSV_MIMES = ("text/csv", "application/csv", "text/tab-separated-values")

_EXTRACTION_METHOD = {
    "application/pdf": "pdf_layout",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    _XLSX: "spreadsheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "slides",
}

# Never worth fetching: no text to extract, and images in particular are almost
# always signature logos rather than content.
_SKIP_PREFIXES = ("image/", "audio/", "video/")


def _supported(mime: str) -> bool:
    return mime in _EXTRACTORS or mime in _TABLE_EXTRACTORS or mime in _CSV_MIMES


def iter_attachment_parts(payload: dict) -> list[dict]:
    """Every attachment part in a message payload, at any nesting depth.

    A part with an empty `filename` is the message BODY (already handled by
    `_find_part_text`); treating it as an attachment would double-ingest the
    email text.
    """
    found: list[dict] = []

    def walk(part: dict) -> None:
        if len(found) >= _MAX_ATTACHMENTS_PER_MESSAGE:
            return
        filename = (part.get("filename") or "").strip()
        body = part.get("body") or {}
        attachment_id = body.get("attachmentId")
        mime = part.get("mimeType", "")
        if filename and attachment_id and not mime.startswith(_SKIP_PREFIXES):
            size = int(body.get("size") or 0)
            if size <= _MAX_ATTACHMENT_BYTES:
                # `index` is assigned HERE, not by the caller: it is part of the
                # doc_id (gmail-<mid>-att-<index>-<i>), so it must be stable for
                # a given message and present for every consumer, including a
                # direct normalise_attachment call.
                found.append({"filename": filename, "mime": mime,
                              "attachment_id": attachment_id, "size": size,
                              "index": len(found)})
            else:
                log.info("attachment %r skipped: %d bytes", filename, size)
        for child in part.get("parts", []) or []:
            walk(child)

    walk(payload)
    return found[:_MAX_ATTACHMENTS_PER_MESSAGE]


def normalise_attachment(raw_message: dict, part: dict, data: bytes) -> list[Chunk]:
    """One attachment's bytes -> Chunks. Pure: no API calls.

    doc_id: gmail-<message_id>-att-<attachment_index>-<chunk_index>.
    """
    mime = part["mime"]
    budget = config.sheet_char_budget(str(config.app_dir()))
    tables = None
    text = ""
    try:
        if mime in _TABLE_EXTRACTORS:
            tables = _TABLE_EXTRACTORS[mime](data, char_budget=budget)
        elif mime in _CSV_MIMES:
            tables = tabular.tables_from_csv(
                data.decode("utf-8", errors="replace"),
                sheet=part["filename"], char_budget=budget)
        elif mime in _EXTRACTORS:
            text = _EXTRACTORS[mime](data)
        else:
            return []
    except Exception as exc:  # noqa: BLE001 — one bad attachment must not kill a sync
        log.warning("attachment %r extraction failed: %s", part["filename"], exc)
        return []
    if not tables and not (text or "").strip():
        return []

    msg_id = raw_message["id"]
    headers = (raw_message.get("payload") or {}).get("headers", [])
    base = {
        "source_type": "gmail",
        "content_type": "email_attachment",
        "message_id": msg_id,
        "thread_id": raw_message.get("threadId", ""),
        "subject": get_header(headers, "subject")[:200],
        "sender": get_header(headers, "from")[:200],
        "date": get_header(headers, "date")[:80],
        "attachment_name": part["filename"][:200],
        "attachment_mime": mime[:100],
        "extraction_method": _EXTRACTION_METHOD.get(mime, "text"),
    }

    if tables:
        rendered = tabular.render_chunks(tables, file_name=part["filename"],
                                         max_chars=tabular.CHUNK_CHARS)
    else:
        rendered = [(t, {}) for t in chunk_text(text)]
    kept = [(t, extra) for t, extra in rendered if has_content(t)]

    return [
        Chunk(doc_id=f"gmail-{msg_id}-att-{part['index']}-{i}", text=t,
              content_hash=content_hash(t),
              metadata={**base, **extra, "chunk_index": i, "chunk_total": len(kept)})
        for i, (t, extra) in enumerate(kept)
    ]


def fetch_and_normalise(service, raw_message: dict, *, store=None) -> list[Chunk]:
    """Fetch every attachment of one message and normalise it.

    Best-effort per attachment: a 404 or a failed extraction skips that one and
    is recorded, rather than aborting the message or the sync.
    """
    payload = raw_message.get("payload") or {}
    msg_id = raw_message["id"]
    out: list[Chunk] = []
    for part in iter_attachment_parts(payload):
        if not _supported(part["mime"]):
            ingest_report.record_skip(store, "attachment_unsupported", msg_id,
                                      f"{part['mime']} ({part['filename']})")
            continue
        try:
            resp = service.users().messages().attachments().get(
                userId="me", messageId=msg_id, id=part["attachment_id"]).execute()
            data = base64.urlsafe_b64decode(resp.get("data") or "")
        except Exception as exc:  # noqa: BLE001 — one attachment must not kill a sync
            log.warning("attachment fetch failed for %s/%s: %s",
                        msg_id, part["filename"], exc)
            ingest_report.record_skip(store, "attachment_fetch_failed", msg_id,
                                      part["filename"])
            continue
        chunks = normalise_attachment(raw_message, part, data)
        if not chunks:
            ingest_report.record_skip(store, "attachment_empty", msg_id,
                                      f"{part['mime']} ({part['filename']})")
        out.extend(chunks)
    return out
