"""anarlog (fastrepl/anarlog) meeting source: notes, summaries and transcripts.

READ-ONLY. anarlog is the sole writer of its own database and its docs warn
that editing it corrupts notes; this repo also has a store-corruption incident
(2026-09-10) caused by two concurrent writers to a single-writer SQLite store.
Every connection here opens with `mode=ro` so we can never be that writer.

The schema is anarlog's PRIVATE schema with no stability promise. That risk is
handled explicitly rather than hoped away: schema_status() compares
_sqlx_migrations against a pinned version and, on any drift, validates the
columns actually used. A drifted-but-valid schema proceeds; a missing column
stops the source loudly instead of ingesting partial content.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import nullcontext

from mcpbrain.chunking import CHUNKER_VERSION, chunk_text, content_hash
from mcpbrain.sync.normalise import Chunk

log = logging.getLogger(__name__)

_PINNED_SCHEMA_VERSION = "20260909160300"

# Exactly the columns this module reads. Kept explicit so drift detection
# checks what we actually depend on, not the whole schema.
_REQUIRED_COLUMNS: dict[str, set[str]] = {
    "sessions": {"id", "title", "updated_at", "deleted_at", "started_at",
                 "created_at", "event_id", "series_id", "external_provider"},
    # deleted_at on the child tables matters: read_session filters on it, so a
    # schema that dropped it would raise mid-ingest rather than being caught
    # here. Every column this module names in SQL must appear in this map.
    "session_documents": {"session_id", "kind", "body", "body_format",
                          "deleted_at", "updated_at"},
    "transcripts": {"session_id", "words_json", "deleted_at", "updated_at"},
}


def connect_ro(db_path: str) -> sqlite3.Connection:
    """Open anarlog's database READ-ONLY. Never open it any other way."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def schema_status(db) -> tuple[bool, str, list[str]]:
    """Return (ok, version, missing_columns).

    ok is True when every column in _REQUIRED_COLUMNS exists, whatever the
    migration version says. The version is returned so the caller can log a
    drift once; it is deliberately NOT a gate on its own, because anarlog ships
    migrations constantly and most do not touch what we read.
    """
    try:
        row = db.execute("SELECT MAX(version) AS v FROM _sqlx_migrations").fetchone()
        version = str(row["v"]) if row and row["v"] is not None else ""
    except sqlite3.Error:
        version = ""
    missing: list[str] = []
    for table, cols in _REQUIRED_COLUMNS.items():
        try:
            present = {r["name"] for r in db.execute(
                f"PRAGMA table_info({table})").fetchall()}
        except sqlite3.Error:
            present = set()
        if not present:
            missing.append(f"{table}.*")
            continue
        missing.extend(f"{table}.{c}" for c in sorted(cols - present))
    return (not missing), version, missing


def changed_sessions(db, cursor: str, limit: int) -> list[dict]:
    """Sessions whose updated_at is at or after `cursor`, oldest first.

    `>=`, not `>`: an exclusive boundary would skip a row sharing the cursor's
    exact timestamp. The re-read costs nothing because upsert_chunk checkpoints
    on id+hash, which is the same convention org_contrib documents.

    Soft-deletes are included and flagged: anarlog bumps updated_at when it
    sets deleted_at (verified on all 15 deleted sessions in the live store), so
    ONE watermark covers both edits and deletions.
    """
    rows = db.execute(
        "SELECT id, updated_at, deleted_at FROM sessions "
        "WHERE updated_at >= ? ORDER BY updated_at LIMIT ?",
        (cursor or "", limit)).fetchall()
    return [{"id": r["id"], "updated_at": r["updated_at"],
             "deleted": r["deleted_at"] is not None} for r in rows]


_BLOCK_TYPES = {"paragraph", "heading", "listItem", "blockquote",
                "codeBlock"}


def _inline_text(node: dict) -> str:
    """Concatenate the text of a node's inline descendants.

    Marks (strong/em/link) are dropped, not rendered: the consumer is an
    embedding model and an extraction prompt, neither of which benefits from
    emphasis, and keeping them would put markdown noise into the vector.
    """
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return node.get("text") or ""
    content = node.get("content")
    if not isinstance(content, list):
        return ""
    return "".join(_inline_text(c) for c in content if isinstance(c, dict))


def prosemirror_to_markdown(body: str) -> str:
    """Render an anarlog prosemirror_json document as plain markdown.

    Headings are preserved because the summary's structure is what makes it
    readable when recall surfaces it. Returns "" for empty or malformed input
    rather than raising — a single unparseable note must not fail the item.
    """
    if not body:
        return ""
    try:
        doc = json.loads(body)
    except (ValueError, TypeError):
        return ""
    if not isinstance(doc, dict):
        return ""

    blocks: list[str] = []

    def walk(node: dict) -> None:
        if not isinstance(node, dict):
            return
        ntype = node.get("type")
        if ntype in ("bulletList", "orderedList"):
            content = node.get("content")
            if isinstance(content, list):
                items: list[str] = []
                for item in content:
                    if isinstance(item, dict):
                        text = _inline_text(item).strip()
                        if text:
                            items.append(f"- {text}")
                if items:
                    blocks.append("\n".join(items))
            return
        if ntype == "heading":
            text = _inline_text(node).strip()
            if text:
                level = int((node.get("attrs") or {}).get("level") or 1)
                blocks.append(f"{'#' * level} {text}")
            return
        if ntype in _BLOCK_TYPES:
            text = _inline_text(node).strip()
            if text:
                blocks.append(text)
            return
        content = node.get("content")
        if isinstance(content, list):
            for child in content:
                if isinstance(child, dict):
                    walk(child)

    for child in doc.get("content") or []:
        walk(child)
    return "\n\n".join(blocks)


def transcript_to_text(words_json: str) -> str:
    """Join a transcript's `text` fields in order.

    Handles both shapes without branching: imported transcripts carry coarse
    blocks (ids like `meeting-import:<hash>:word:0`) and live recordings carry
    true word-level entries with timings. Both are ordered arrays of objects
    with a `text` field, so an ordered join is correct for each.
    """
    if not words_json:
        return ""
    try:
        words = json.loads(words_json)
    except (ValueError, TypeError):
        return ""
    if not isinstance(words, list):
        return ""
    parts = [str(w.get("text") or "").strip()
             for w in words if isinstance(w, dict)]
    return " ".join(p for p in parts if p)


_DOC_KINDS = ("summary", "note")


def read_session(db, session_id: str) -> dict | None:
    """Assemble one session's row, its documents and its transcript."""
    row = db.execute(
        "SELECT id, title, started_at, created_at, event_id, series_id FROM sessions "
        "WHERE id = ? AND deleted_at IS NULL", (session_id,)).fetchone()
    if row is None:
        return None
    docs = {}
    for d in db.execute(
            "SELECT kind, body FROM session_documents "
            "WHERE session_id = ? AND deleted_at IS NULL "
            "ORDER BY updated_at", (session_id,)).fetchall():
        if d["kind"] in _DOC_KINDS and d["body"]:
            docs[d["kind"]] = d["body"]
    tr = db.execute(
        "SELECT words_json FROM transcripts WHERE session_id = ? "
        "AND deleted_at IS NULL ORDER BY updated_at LIMIT 1", (session_id,)).fetchone()
    return {
        "id": row["id"],
        "title": row["title"] or "",
        "started_at": row["started_at"] or row["created_at"] or "",
        "event_id": row["event_id"] or "",
        "series_id": row["series_id"] or "",
        "documents": docs,
        "transcript": transcript_to_text(tr["words_json"]) if tr else "",
    }


def normalise_session(session: dict) -> list[Chunk]:
    """One session -> chunks, one lineage per content subtype.

    `content_subtype` is load-bearing: prepare.should_enrich() cold-marks
    'transcript' chunks, so tagging them here is the whole of the
    hot-summary/cold-transcript policy. The handler does no cold-marking.
    """
    sid = session["id"]
    base = {
        "source_type": "anarlog",
        "session_id": sid,
        "meeting_title": session.get("title") or "",
        "started_at": session.get("started_at") or "",
        "event_id": session.get("event_id") or "",
        "series_id": session.get("series_id") or "",
        "chunker_version": CHUNKER_VERSION,
    }
    out: list[Chunk] = []
    bodies = [(k, prosemirror_to_markdown(session["documents"][k]))
              for k in _DOC_KINDS if session.get("documents", {}).get(k)]
    bodies.append(("transcript", session.get("transcript") or ""))
    for kind, text in bodies:
        if not text.strip():
            continue
        pieces = chunk_text(text)
        if not pieces:
            continue
        for i, piece in enumerate(pieces):
            out.append(Chunk(
                doc_id=f"anarlog-{sid}-{kind}-{i}",
                text=piece,
                content_hash=content_hash(piece),
                metadata={**base, "content_subtype": kind,
                          "chunk_index": i, "chunk_total": len(pieces)},
            ))
    return out


_DISCOVER_LIMIT = 200
_SOURCE = "anarlog"

# Logged once per process when anarlog's schema version moves but every column
# we read is still present. Module-level so a 5-minute cadence does not emit
# the same line all day.
_drift_logged = False


def discover_anarlog(store, *, db_path, budget=None, bulk_section=None) -> int:
    """Enqueue anarlog sessions changed at or after the cursor. Returns count.

    Raises RuntimeError when anarlog's schema no longer carries a column we
    read: partial ingestion of a changed schema is worse than stopping, because
    it writes wrong content silently and the watermark then skips past it.
    """
    global _drift_logged
    cursor = store.get_cursor(_SOURCE) or ""
    with connect_ro(db_path) as db:
        ok, version, missing = schema_status(db)
        if not ok:
            raise RuntimeError(
                f"anarlog schema at version {version!r} is missing columns "
                f"this source reads: {', '.join(missing)} — refusing to ingest "
                f"partial content (pinned {_PINNED_SCHEMA_VERSION})")
        if version != _PINNED_SCHEMA_VERSION and not _drift_logged:
            log.info("anarlog schema version %s differs from pinned %s; all "
                     "required columns present, continuing", version,
                     _PINNED_SCHEMA_VERSION)
            _drift_logged = True
        rows = changed_sessions(db, cursor, _DISCOVER_LIMIT)

    if not rows:
        return 0
    # A full page that shares ONE exact updated_at is fatal to this watermark:
    # the cursor would advance to that timestamp, the next cycle would
    # re-query `>= T LIMIT _DISCOVER_LIMIT`, get the SAME rows back, and any
    # row beyond them sharing T would never be discovered -- silently and
    # forever (the same failure class as the five-week Drive paging livelock:
    # waiting longer never fixes it). The fix would be a composite
    # (updated_at, id) cursor, but that is not implemented here: anarlog's
    # updated_at is millisecond-precision and every value observed on the
    # live DB is distinct (largest tie group is 1 row), so this is
    # implausible in practice. What matters is that a stall must be LOUD, not
    # silent -- this module's stated principle -- so we raise rather than
    # let it degrade unnoticed.
    if (len(rows) == _DISCOVER_LIMIT
            and rows[0]["updated_at"] == rows[-1]["updated_at"]):
        raise RuntimeError(
            f"anarlog discovery got a full page of {len(rows)} sessions all "
            f"sharing updated_at={rows[0]['updated_at']!r} -- the watermark "
            f"cannot advance past this timestamp by time alone, so the next "
            f"cycle would re-read this same page forever and never reach any "
            f"row beyond it. Remedy: switch to a composite (updated_at, id) "
            f"cursor.")
    # enqueue_and_advance upserts the queue rows AND advances the cursor in ONE
    # transaction — "the cursor can never be ahead of what is recorded". Do not
    # split this into enqueue + set_cursor, and do not wrap it in bulk_section:
    # it is already a single transaction.
    #
    # `version` is the session's updated_at: a differing version resets that
    # row's attempts/backoff, which is exactly right here — an edited meeting
    # is new work, not a continuation of a failing item.
    items = [{"ref_id": r["id"],
              "event": "remove" if r["deleted"] else "upsert",
              "modified_at": r["updated_at"],
              "version": r["updated_at"]}
             for r in rows]
    store.enqueue_and_advance(items, source=_SOURCE,
                              cursor=rows[-1]["updated_at"])
    return len(items)


def _delete_session_chunks(store, sid: str) -> None:
    """Delete every chunk resolvable by this session id, if any exist."""
    doc_ids = store.doc_ids_for_messages([f"anarlog-{sid}"])
    if doc_ids:
        store.delete_chunks(doc_ids)


def handle_anarlog_item(store, item, *, db_path, bulk_section=None) -> None:
    """Work one queued session. Raises on failure so the loop backs it off."""
    bulk_section = bulk_section or nullcontext
    sid = item["ref_id"]

    if item["event"] == "remove":
        with bulk_section():
            _delete_session_chunks(store, sid)
        return

    with connect_ro(db_path) as db:
        session = read_session(db, sid)
    if session is None:
        # Deleted between discovery and handling: treat as a removal rather
        # than leaving orphaned chunks behind.
        with bulk_section():
            _delete_session_chunks(store, sid)
        return

    chunks = normalise_session(session)
    with bulk_section():
        # Drop chunks that no longer exist (a note that shrank from 3 chunks to
        # 1 would otherwise leave two stale rows resolvable by session_id).
        live = {c.doc_id for c in chunks}
        stale = set(store.doc_ids_for_messages([f"anarlog-{sid}"])) - live
        if stale:
            store.delete_chunks(list(stale))
        for ch in chunks:
            store.upsert_chunk(ch.doc_id, ch.text, ch.content_hash, ch.metadata)
