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

log = logging.getLogger(__name__)

_PINNED_SCHEMA_VERSION = "20260909160300"

# Exactly the columns this module reads. Kept explicit so drift detection
# checks what we actually depend on, not the whole schema.
_REQUIRED_COLUMNS: dict[str, set[str]] = {
    "sessions": {"id", "title", "updated_at", "deleted_at", "started_at",
                 "event_id", "series_id", "external_provider"},
    # deleted_at on the child tables matters: read_session filters on it, so a
    # schema that dropped it would raise mid-ingest rather than being caught
    # here. Every column this module names in SQL must appear in this map.
    "session_documents": {"session_id", "kind", "body", "body_format",
                          "deleted_at"},
    "transcripts": {"session_id", "words_json", "deleted_at"},
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
    if node.get("type") == "text":
        return node.get("text") or ""
    return "".join(_inline_text(c) for c in (node.get("content") or []))


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
        ntype = node.get("type")
        if ntype in ("bulletList", "orderedList"):
            items: list[str] = []
            for item in node.get("content") or []:
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
        for child in node.get("content") or []:
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
