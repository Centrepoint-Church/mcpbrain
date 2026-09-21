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
    # `external_event_id` is the GOOGLE calendar event id and is what chunk
    # metadata's `event_id` carries; `event_id` here is anarlog's OWN foreign
    # key into its `events` table (a UUID) and is read only so drift over it is
    # still caught while read_session keeps selecting it. `external_provider`
    # is read too — it says WHOSE id external_event_id is (google | granola |
    # …), and stamping a granola import's id as a Google event id would be a
    # lie every downstream consumer of `event_id` believes.
    "sessions": {"id", "title", "updated_at", "deleted_at", "started_at",
                 "created_at", "event_id", "external_event_id", "series_id",
                 "external_provider"},
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
_LIST_TYPES = {"bulletList", "orderedList"}
# A node of one of these types is its own line/block. _inline_text must never
# flatten one into a sibling's text: doing so fuses the last word of one block
# to the first word of the next ("...sign-offPolicy: no sponsors"), which is
# exactly what the nested bulletLists anarlog's AI notes are built from used to
# produce. Measured on the live database before this fix: 126 block texts
# concatenated with no separator across the two meetings' four hot documents,
# 82 of them fusing two real words into one token. After: 0 of each.
_INLINE_STOP = _BLOCK_TYPES | _LIST_TYPES

# Two spaces per nesting level. anarlog's notes nest a full level deep
# (`listItem -> [paragraph, bulletList]` is the dominant shape in the live
# data), and that nesting is semantic — a sub-point belongs to the point above
# it. Indenting keeps that relationship legible to both the embedding model and
# the extraction prompt; flattening every item to a bare `- ` line would render
# the same words while losing which point each sub-point hangs off.
_NEST_INDENT = "  "


def _inline_text(node: dict) -> str:
    """Concatenate the text of a node's INLINE descendants.

    Marks (strong/em/link) are dropped, not rendered: the consumer is an
    embedding model and an extraction prompt, neither of which benefits from
    emphasis, and keeping them would put markdown noise into the vector.

    Nested BLOCK and LIST children are skipped, not flattened — see
    _INLINE_STOP. Callers that need those render them as their own lines
    (_render_list / walk); nothing may glue two blocks into one word.
    """
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return node.get("text") or ""
    content = node.get("content")
    if not isinstance(content, list):
        return ""
    return "".join(_inline_text(c) for c in content
                   if isinstance(c, dict) and c.get("type") not in _INLINE_STOP)


def _has_block_children(node: dict) -> bool:
    """True when this node contains nested block/list children of its own."""
    content = node.get("content")
    return isinstance(content, list) and any(
        isinstance(c, dict) and c.get("type") in _INLINE_STOP for c in content)


def _render_item(item: dict, depth: int, marker: str,
                 lines: list[str]) -> None:
    """Render one listItem, recursing into nested lists as their own lines."""
    indent = _NEST_INDENT * depth
    first = True
    for child in item.get("content") or []:
        if not isinstance(child, dict):
            continue
        if child.get("type") in _LIST_TYPES:
            _render_list(child, depth + 1, lines)
            continue
        text = _inline_text(child).strip()
        if not text:
            continue
        if first:
            lines.append(f"{indent}{marker}{text}")
            first = False
        else:
            # a listItem's second and later paragraphs are continuation lines,
            # aligned under the marker rather than given a marker of their own
            lines.append(f"{indent}{' ' * len(marker)}{text}")


def _render_list(node: dict, depth: int, lines: list[str]) -> None:
    """Render a bulletList/orderedList into `lines`, one line per item."""
    ordered = node.get("type") == "orderedList"
    n = 0
    for item in node.get("content") or []:
        if not isinstance(item, dict) or item.get("type") != "listItem":
            continue
        n += 1
        _render_item(item, depth, f"{n}. " if ordered else "- ", lines)


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
        if ntype in _LIST_TYPES:
            lines: list[str] = []
            _render_list(node, 0, lines)
            if lines:
                blocks.append("\n".join(lines))
            return
        if ntype == "listItem":          # orphan item outside a list
            lines = []
            _render_item(node, 0, "- ", lines)
            if lines:
                blocks.append("\n".join(lines))
            return
        if ntype == "heading":
            text = _inline_text(node).strip()
            if text:
                level = int((node.get("attrs") or {}).get("level") or 1)
                blocks.append(f"{'#' * level} {text}")
            return
        # Anything else: a node holding nested blocks (blockquote, or an
        # unknown container anarlog adds later) is RECURSED into so each block
        # becomes its own line; a leaf block is emitted as one block. The old
        # code flattened every descendant of a _BLOCK_TYPES node with no
        # separator, which is what glued words together.
        if _has_block_children(node):
            for child in node.get("content") or []:
                if isinstance(child, dict):
                    walk(child)
            return
        text = _inline_text(node).strip()
        if text:
            blocks.append(text)

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


def _google_event_id(row) -> str:
    """The GOOGLE calendar event id for a session row, or "".

    Chunk metadata's `event_id` means "the Google calendar event id" — it is
    what `cal-<event_id>`, `meeting_packs` and store's event_id arm are keyed
    on. anarlog keeps that value in `external_event_id`, and says whose id it
    is in `external_provider`: a session imported from Granola carries a
    GRANOLA uuid there (both live imported meetings do), which matches no
    calendar row anywhere and would make every consumer of that field believe
    a Google linkage that does not exist.

    An EMPTY provider still carries the id through rather than dropping it:
    "unlabelled" must not become a silent loss of a real linkage, and
    `external_provider` rides along in the metadata so a reader can always see
    on what basis the id was accepted.
    """
    eid = (row["external_event_id"] or "").strip()
    if not eid:
        return ""
    provider = (row["external_provider"] or "").strip().lower()
    return eid if (not provider or provider.startswith("google")) else ""


def read_session(db, session_id: str) -> dict | None:
    """Assemble one session's row, its documents and its transcript."""
    row = db.execute(
        "SELECT id, title, started_at, created_at, event_id, external_event_id, "
        "external_provider, series_id FROM sessions "
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
        # `event_id` here is the GOOGLE calendar event id — the key behind
        # mcpbrain's `cal-<event_id>` and meeting_packs — which anarlog stores
        # in `external_event_id`. `sessions.event_id` is anarlog's OWN foreign
        # key into its `events` table (verified on the live DB: UUIDs that join
        # `events.id`) and means nothing to any consumer here; it is carried as
        # `anarlog_event_id` so the distinction stays visible rather than
        # looking like an omission.
        "event_id": _google_event_id(row),
        "anarlog_event_id": row["event_id"] or "",
        "external_provider": row["external_provider"] or "",
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
        # The GOOGLE calendar event id (anarlog's external_event_id), which is
        # what every consumer of this key expects — see _google_event_id.
        "event_id": session.get("event_id") or "",
        # Whose id that is. Stamped so a "" event_id on a session that plainly
        # HAS an external event is explainable from the chunk alone.
        "external_provider": session.get("external_provider") or "",
        # PRESENTLY ALWAYS "": `sessions.series_id` is empty on every live row
        # and anarlog keeps recurrence in `events.recurrence_series_id`, which
        # this source does not read. Nothing in mcpbrain reads `series_id` off
        # chunk metadata either, so this is a placeholder carried for the
        # spec's §4 shape — do not mistake it for wired-up recurrence linkage.
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
    """Delete every chunk resolvable by this session id, if any exist.

    Invalidates any local relations sourced from those doc_ids FIRST, same
    pattern as drive.py's remove-event handlers and ingest_cache.purge_drive
    -- otherwise a relation extracted before this session was deleted/edited
    keeps pointing at a source_doc_id whose chunk row no longer exists, which
    org_contrib.collect_from_drain can no longer distinguish from "fine"
    provenance by querying alone (see _chunk_provenance in org_contrib.py).
    """
    doc_ids = store.doc_ids_for_messages([f"anarlog-{sid}"])
    if doc_ids:
        store.invalidate_local_relations_for_docs(
            doc_ids, reason="anarlog_session_removed")
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
            stale_ids = list(stale)
            store.invalidate_local_relations_for_docs(
                stale_ids, reason="anarlog_note_shrank")
            store.delete_chunks(stale_ids)
        for ch in chunks:
            store.upsert_chunk(ch.doc_id, ch.text, ch.content_hash, ch.metadata)
