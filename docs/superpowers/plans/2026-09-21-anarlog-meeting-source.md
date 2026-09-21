# anarlog Meeting Source Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ingest anarlog meetings (note, summary, transcript) into the brain as a normal incremental sync source, linked to calendar events, with transcripts searchable but never enriched.

**Architecture:** A new `mcpbrain/sync/anarlog.py` providing `discover_anarlog`/`handle_anarlog_item`, registered in `run_sync_cycle`'s `handlers` dict exactly like Drive/Gmail/Calendar. It reads anarlog's own SQLite database **read-only** (`file:<path>?mode=ro`), using `sessions.updated_at` as a `sync_cursors` watermark. Transcripts are tagged `content_subtype="transcript"` and cold-marked by the existing salience gate rather than by bespoke code.

**Tech Stack:** Python 3, stdlib `sqlite3` (read-only URI), existing `mcpbrain.sync.normalise.Chunk`, `mcpbrain.chunking.chunk_text`, `pytest`.

**Spec:** `docs/superpowers/specs/2026-09-21-anarlog-meeting-source-design.md`

## Global Constraints

- **Never write to anarlog's database.** Every connection uses `sqlite3.connect(f"file:{path}?mode=ro", uri=True)`. We are never a second writer — the 2026-09-10 store-corruption incident in this repo was caused by two concurrent writers to a single-writer SQLite store.
- **Default anarlog DB path:** `~/Library/Application Support/anarlog/app.db`. Overridable via config key `anarlog.db_path`. When the file is absent the source is disabled — no error, no log spam.
- **Pinned anarlog schema version:** `20260909160300` (from `_sqlx_migrations`).
- **doc_id namespace:** `anarlog-<session_id>-<kind>-<i>` where kind ∈ `summary|note|transcript`.
- **Source string in `sync_queue` / `sync_cursors`:** `anarlog`.
- **`source_type` in chunk metadata:** `anarlog`.
- **Tests build a real temporary SQLite file in anarlog's shape. Never mock the database.** This repo has shipped inert code three times behind fakes that never exercised the real constructor.
- **Do not bump version files.** This plan is source-only; releasing is a separate, explicit step.
- **Never use `_meta_extract` paths inline.** All metadata SQL is built through `store._meta_extract()` so index DDL and query text cannot drift.
- Run only the tests this plan touches. Josh runs the full suite himself.

---

### Task 1: anarlog DB reader — schema guard and session discovery queries

**Files:**
- Create: `mcpbrain/sync/anarlog.py`
- Test: `tests/test_anarlog_reader.py`

**Interfaces:**
- Consumes: nothing (first task)
- Produces:
  - `_PINNED_SCHEMA_VERSION: str = "20260909160300"`
  - `_REQUIRED_COLUMNS: dict[str, set[str]]`
  - `connect_ro(db_path: str) -> sqlite3.Connection`
  - `schema_status(db) -> tuple[bool, str, list[str]]` → `(ok, version, missing)` where `missing` is a list of `"table.column"` strings
  - `changed_sessions(db, cursor: str, limit: int) -> list[dict]` → each `{"id", "updated_at", "deleted"}`

- [ ] **Step 1: Write the failing test**

Create `tests/test_anarlog_reader.py`:

```python
import sqlite3
import pytest
from mcpbrain.sync import anarlog


def _make_db(path, *, version="20260909160300", drop_cols=()):
    """Build a real SQLite file shaped like anarlog's app.db."""
    db = sqlite3.connect(str(path))
    db.execute("CREATE TABLE _sqlx_migrations(version TEXT, description TEXT)")
    db.execute("INSERT INTO _sqlx_migrations VALUES(?, 'x')", (version,))
    sess_cols = ["id TEXT PRIMARY KEY", "title TEXT", "updated_at TEXT",
                 "deleted_at TEXT", "started_at TEXT", "event_id TEXT",
                 "series_id TEXT", "external_provider TEXT"]
    sess_cols = [c for c in sess_cols if c.split()[0] not in drop_cols]
    db.execute(f"CREATE TABLE sessions({','.join(sess_cols)})")
    db.execute("CREATE TABLE session_documents(session_id TEXT, kind TEXT, "
               "body TEXT, body_format TEXT, deleted_at TEXT)")
    db.execute("CREATE TABLE transcripts(session_id TEXT, words_json TEXT, "
               "deleted_at TEXT)")
    db.commit()
    return db


def test_schema_status_ok_on_pinned_version(tmp_path):
    p = tmp_path / "app.db"
    _make_db(p).close()
    with anarlog.connect_ro(str(p)) as db:
        ok, version, missing = anarlog.schema_status(db)
    assert ok is True
    assert version == "20260909160300"
    assert missing == []


def test_schema_status_ok_on_new_version_with_valid_columns(tmp_path):
    p = tmp_path / "app.db"
    _make_db(p, version="20270101000000").close()
    with anarlog.connect_ro(str(p)) as db:
        ok, version, missing = anarlog.schema_status(db)
    assert ok is True
    assert version == "20270101000000"


def test_schema_status_reports_missing_column(tmp_path):
    p = tmp_path / "app.db"
    _make_db(p, version="20270101000000", drop_cols=("series_id",)).close()
    with anarlog.connect_ro(str(p)) as db:
        ok, _version, missing = anarlog.schema_status(db)
    assert ok is False
    assert "sessions.series_id" in missing


def test_connect_ro_refuses_writes(tmp_path):
    p = tmp_path / "app.db"
    _make_db(p).close()
    with anarlog.connect_ro(str(p)) as db:
        with pytest.raises(sqlite3.OperationalError):
            db.execute("INSERT INTO sessions(id) VALUES('x')")


def test_changed_sessions_uses_inclusive_boundary(tmp_path):
    p = tmp_path / "app.db"
    db = _make_db(p)
    db.executemany(
        "INSERT INTO sessions(id,title,updated_at,deleted_at) VALUES(?,?,?,NULL)",
        [("a", "A", "2026-09-17T01:00:00Z"), ("b", "B", "2026-09-17T02:00:00Z")])
    db.commit(); db.close()
    with anarlog.connect_ro(str(p)) as conn:
        rows = anarlog.changed_sessions(conn, "2026-09-17T02:00:00Z", 10)
    # >= not >: the boundary row is re-read, never skipped.
    assert [r["id"] for r in rows] == ["b"]


def test_changed_sessions_flags_deleted(tmp_path):
    p = tmp_path / "app.db"
    db = _make_db(p)
    db.execute("INSERT INTO sessions(id,title,updated_at,deleted_at) "
               "VALUES('a','A','2026-09-17T01:00:00Z','2026-09-17T01:00:00Z')")
    db.commit(); db.close()
    with anarlog.connect_ro(str(p)) as conn:
        rows = anarlog.changed_sessions(conn, "", 10)
    assert rows[0]["deleted"] is True
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_anarlog_reader.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcpbrain.sync.anarlog'`

- [ ] **Step 3: Write the minimal implementation**

Create `mcpbrain/sync/anarlog.py`:

```python
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
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/test_anarlog_reader.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/sync/anarlog.py tests/test_anarlog_reader.py
git commit -m "feat(anarlog): read-only reader with schema-drift guard

Reads anarlog's private schema read-only. schema_status() validates the
columns we actually depend on rather than trusting the migration version,
so an anarlog upgrade either keeps working or fails visibly.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01LvzZKvhmShfY3FHbUF2Gx8"
```

---

### Task 2: Text extraction — prosemirror bodies and transcript assembly

**Files:**
- Modify: `mcpbrain/sync/anarlog.py`
- Test: `tests/test_anarlog_extract.py`

**Interfaces:**
- Consumes: `mcpbrain.sync.anarlog` from Task 1
- Produces:
  - `prosemirror_to_markdown(body: str) -> str`
  - `transcript_to_text(words_json: str) -> str`

- [ ] **Step 1: Write the failing test**

Create `tests/test_anarlog_extract.py`:

```python
import json
from mcpbrain.sync.anarlog import prosemirror_to_markdown, transcript_to_text


def test_headings_become_markdown():
    body = json.dumps({"type": "doc", "content": [
        {"type": "heading", "attrs": {"level": 1},
         "content": [{"type": "text", "text": "ACC Staff Meeting"}]},
        {"type": "heading", "attrs": {"level": 2},
         "content": [{"type": "text", "text": "Summary"}]},
        {"type": "paragraph", "content": [{"type": "text", "text": "We met."}]},
    ]})
    assert prosemirror_to_markdown(body) == (
        "# ACC Staff Meeting\n\n## Summary\n\nWe met.")


def test_bullet_list_becomes_dashes():
    body = json.dumps({"type": "doc", "content": [
        {"type": "bulletList", "content": [
            {"type": "listItem", "content": [
                {"type": "paragraph", "content": [
                    {"type": "text", "text": "First"}]}]},
            {"type": "listItem", "content": [
                {"type": "paragraph", "content": [
                    {"type": "text", "text": "Second"}]}]},
        ]},
    ]})
    assert prosemirror_to_markdown(body) == "- First\n- Second"


def test_nested_marks_keep_text():
    body = json.dumps({"type": "doc", "content": [
        {"type": "paragraph", "content": [
            {"type": "text", "text": "bold", "marks": [{"type": "strong"}]},
            {"type": "text", "text": " and plain"},
        ]},
    ]})
    assert prosemirror_to_markdown(body) == "bold and plain"


def test_empty_and_malformed_bodies_return_empty_string():
    assert prosemirror_to_markdown("") == ""
    assert prosemirror_to_markdown("not json") == ""
    assert prosemirror_to_markdown(json.dumps({"type": "doc"})) == ""


def test_transcript_joins_text_in_order():
    words = json.dumps([
        {"id": "w:0", "text": "Hello there."},
        {"id": "w:1", "text": "Second part."},
    ])
    assert transcript_to_text(words) == "Hello there. Second part."


def test_transcript_handles_word_level_entries():
    words = json.dumps([
        {"id": "w:0", "text": "Hello", "start_ms": 0},
        {"id": "w:1", "text": "there", "start_ms": 100},
    ])
    assert transcript_to_text(words) == "Hello there"


def test_transcript_malformed_returns_empty_string():
    assert transcript_to_text("") == ""
    assert transcript_to_text("not json") == ""
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_anarlog_extract.py -v`
Expected: FAIL — `ImportError: cannot import name 'prosemirror_to_markdown'`

- [ ] **Step 3: Write the minimal implementation**

Append to `mcpbrain/sync/anarlog.py`:

```python
import json

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
            for item in node.get("content") or []:
                text = _inline_text(item).strip()
                if text:
                    blocks.append(f"- {text}")
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
```

Move the `import json` to the module's existing import block rather than leaving it mid-file.

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/test_anarlog_extract.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/sync/anarlog.py tests/test_anarlog_extract.py
git commit -m "feat(anarlog): prosemirror and transcript text extraction

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01LvzZKvhmShfY3FHbUF2Gx8"
```

---

### Task 3: Store — resolve `anarlog-<session_id>` in `doc_ids_for_messages`

**Files:**
- Modify: `mcpbrain/store.py` (`_CAL_PREFIX` area ~line 251; index loop ~lines 520-536; `doc_ids_for_messages` ~lines 3954-3994; `_doc_ids_query` ~lines 3996-4030)
- Test: `tests/test_anarlog_doc_ids.py`

**Interfaces:**
- Consumes: nothing from earlier tasks
- Produces: `store.doc_ids_for_messages(["anarlog-<session_id>"])` resolves every chunk whose `metadata.session_id` matches, across all three subtypes. Module constant `_ANARLOG_PREFIX = "anarlog-"`.

**Why this is a UNION arm and not a `LIKE`:** `_doc_ids_query` is a UNION of single-path SELECTs precisely because SQLite will not use expression indexes across an `OR` — the `OR` form plans as a full `SCAN chunks` (~1.4s on the live store). A `doc_id LIKE 'anarlog-<id>-%'` would also defeat the index, which is the documented reason `chunks_for_file` was rewritten off `LIKE` in 0.7.105.

- [ ] **Step 1: Write the failing test**

Create `tests/test_anarlog_doc_ids.py`:

```python
from mcpbrain.store import Store


def _store(tmp_path):
    return Store(str(tmp_path / "brain.sqlite3"), dim=8)


def test_resolves_all_subtypes_of_one_session(tmp_path):
    s = _store(tmp_path)
    meta = {"source_type": "anarlog", "session_id": "sess-1"}
    s.upsert_chunk("anarlog-sess-1-summary-0", "sum", "h1",
                   {**meta, "content_subtype": "summary"})
    s.upsert_chunk("anarlog-sess-1-note-0", "note", "h2",
                   {**meta, "content_subtype": "note"})
    s.upsert_chunk("anarlog-sess-1-transcript-0", "tx", "h3",
                   {**meta, "content_subtype": "transcript"})
    s.upsert_chunk("anarlog-sess-2-summary-0", "other", "h4",
                   {"source_type": "anarlog", "session_id": "sess-2"})

    got = set(s.doc_ids_for_messages(["anarlog-sess-1"]))
    assert got == {"anarlog-sess-1-summary-0", "anarlog-sess-1-note-0",
                   "anarlog-sess-1-transcript-0"}


def test_unprefixed_id_does_not_match_a_session(tmp_path):
    s = _store(tmp_path)
    s.upsert_chunk("anarlog-sess-1-summary-0", "sum", "h1",
                   {"source_type": "anarlog", "session_id": "sess-1"})
    # A bare session id must not resolve: the anarlog identity is always
    # prefixed, exactly like the calendar arm.
    assert s.doc_ids_for_messages(["sess-1"]) == []


def test_session_arm_uses_the_index_on_an_existing_store(tmp_path):
    """Re-init an ALREADY-initialised store, then check the plan.

    Mirrors test_metadata_jsonb's re-init test: CREATE INDEX IF NOT EXISTS keys
    on the index NAME, so a fresh-store test would pass even if the DDL and the
    query expression had drifted apart.
    """
    path = str(tmp_path / "brain.sqlite3")
    Store(path, dim=8)          # first init creates the index
    s = Store(path, dim=8)      # re-init on an existing store
    sql = s._doc_ids_query(1)
    with s._connect() as db:
        plan = "\n".join(
            str(r[3]) for r in db.execute(f"EXPLAIN QUERY PLAN {sql}",
                                          ["x", "x", "x", "x", "x"]).fetchall())
    assert "idx_chunks_sessionid" in plan
    assert "SCAN chunks" not in plan
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_anarlog_doc_ids.py -v`
Expected: FAIL — the first test returns `[]` (no session arm), the third fails on the missing `idx_chunks_sessionid`.

- [ ] **Step 3: Write the minimal implementation**

**3a.** Beside `_CAL_PREFIX = "cal-"` (~line 251) add:

```python
_ANARLOG_PREFIX = "anarlog-"
```

**3b.** In `init()`'s expression-index loop, add a fifth entry after `idx_chunks_threadid`:

```python
                # anarlog meeting sessions: doc_ids_for_messages resolves an
                # `anarlog-<session_id>` key to every chunk of that meeting
                # (summary, note, transcript), so the arm needs the same
                # expression index as file_id/event_id.
                ("idx_chunks_sessionid", "$.session_id"),
```

**3c.** In `doc_ids_for_messages`, build the stripped session ids beside `event_ids` and extend the bind order:

```python
        session_ids = [m[len(_ANARLOG_PREFIX):] if m.startswith(_ANARLOG_PREFIX)
                       else None for m in ids]
        with self._connect() as db:
            rows = db.execute(self._doc_ids_query(len(ids)),
                              ids + ids + event_ids + session_ids + ids).fetchall()
```

**3d.** In `_doc_ids_query`, insert the session arm **after** the event arm and **before** the doc_id fallback arm (the fallback must stay last — the bind order above depends on it):

```python
            f"UNION "
            f"SELECT doc_id, rowid FROM chunks "
            f"WHERE {_meta_extract('$.session_id')} IN ({ph}) "
```

**3e.** Extend the `doc_ids_for_messages` docstring with the fifth case:

```
        anarlog meetings are the fifth: a meeting's chunks are
        anarlog-<session_id>-<kind>-<i> across three subtypes (summary, note,
        transcript) and never the bare `anarlog-<session_id>`, so the arm is
        bound with _ANARLOG_PREFIX STRIPPED and matches the raw session_id the
        chunks carry. One key therefore resolves the whole meeting, which is
        what deletion needs.
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/test_anarlog_doc_ids.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Run the existing store tests to check nothing regressed**

Run: `pytest tests/test_metadata_jsonb.py tests/test_chunk_metadata.py -q`
Expected: PASS — the bind order change touches every caller of `_doc_ids_query`.

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/store.py tests/test_anarlog_doc_ids.py
git commit -m "feat(store): resolve anarlog-<session_id> in doc_ids_for_messages

Fifth UNION arm plus idx_chunks_sessionid, so one key resolves every chunk
of a meeting across summary/note/transcript. A UNION arm rather than a LIKE
because LIKE defeats the expression index (the 0.7.105 chunks_for_file
lesson).

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01LvzZKvhmShfY3FHbUF2Gx8"
```

---

### Task 4: Salience gate — transcripts are never enriched

**Files:**
- Modify: `mcpbrain/prepare.py:455-456`
- Test: `tests/test_anarlog_salience.py`

**Interfaces:**
- Consumes: nothing
- Produces: `prepare.should_enrich()` returns `False` for any chunk whose `metadata.content_subtype` is `"transcript"`, for every source.

- [ ] **Step 1: Write the failing test**

Create `tests/test_anarlog_salience.py`:

```python
from mcpbrain.prepare import should_enrich


def _chunk(subtype, source="anarlog"):
    return {"metadata": {"source_type": source, "content_subtype": subtype}}


def test_transcript_is_not_enriched():
    assert should_enrich(_chunk("transcript")) is False


def test_summary_and_note_are_enriched():
    assert should_enrich(_chunk("summary")) is True
    assert should_enrich(_chunk("note")) is True


def test_transcript_gate_is_source_agnostic():
    # Any future transcript source is honoured without touching the gate.
    assert should_enrich(_chunk("transcript", source="someothertool")) is False


def test_table_gate_still_works():
    assert should_enrich(_chunk("table", source="drive")) is False
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_anarlog_salience.py -v`
Expected: FAIL — `test_transcript_is_not_enriched` returns `True` (the gate fails open on unrecognised sources).

- [ ] **Step 3: Write the minimal implementation**

In `mcpbrain/prepare.py`, replace the existing check at line 455:

```python
    if str(meta.get("content_subtype") or "").lower() == "table":
        return False
```

with:

```python
    # A transcript joins 'table' here for the same reason the comment above
    # gives: verbatim speech is not prose worth entity extraction, whoever
    # produced it. Source-agnostic on purpose — any future transcript source is
    # honoured without editing this gate again. Transcripts stay embedded and
    # searchable (embedded=1); only graph extraction skips them.
    if str(meta.get("content_subtype") or "").lower() in ("table", "transcript"):
        return False
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/test_anarlog_salience.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Check the existing salience tests still pass**

Run: `pytest tests/test_salience.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/prepare.py tests/test_anarlog_salience.py
git commit -m "feat(salience): cold-mark transcript chunks, source-agnostically

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01LvzZKvhmShfY3FHbUF2Gx8"
```

---

### Task 5: Normalise a session into chunks

**Files:**
- Modify: `mcpbrain/sync/anarlog.py`
- Test: `tests/test_anarlog_normalise.py`

**Interfaces:**
- Consumes: `prosemirror_to_markdown`, `transcript_to_text`, `connect_ro` (Tasks 1-2)
- Produces:
  - `read_session(db, session_id: str) -> dict | None` → `{"id", "title", "started_at", "event_id", "series_id", "documents": {kind: body}, "transcript": str}`
  - `normalise_session(session: dict) -> list[Chunk]`

- [ ] **Step 1: Write the failing test**

Create `tests/test_anarlog_normalise.py`:

```python
import json
from mcpbrain.sync.anarlog import normalise_session


def _session(**over):
    base = {
        "id": "sess-1",
        "title": "ACC Staff Meeting",
        "started_at": "2026-09-17T02:00:00Z",
        "event_id": "evt-9",
        "series_id": "ser-3",
        "documents": {
            "summary": json.dumps({"type": "doc", "content": [
                {"type": "paragraph",
                 "content": [{"type": "text", "text": "We agreed on X."}]}]}),
            "note": json.dumps({"type": "doc", "content": [
                {"type": "paragraph",
                 "content": [{"type": "text", "text": "My rough note."}]}]}),
        },
        "transcript": "Someone said a thing.",
    }
    base.update(over)
    return base


def test_produces_all_three_subtypes():
    chunks = normalise_session(_session())
    subtypes = {c.metadata["content_subtype"] for c in chunks}
    assert subtypes == {"summary", "note", "transcript"}


def test_doc_ids_follow_the_namespace():
    chunks = normalise_session(_session())
    for c in chunks:
        assert c.doc_id.startswith("anarlog-sess-1-")
        kind = c.metadata["content_subtype"]
        assert c.doc_id.startswith(f"anarlog-sess-1-{kind}-")


def test_metadata_carries_linkage_fields():
    c = normalise_session(_session())[0]
    assert c.metadata["source_type"] == "anarlog"
    assert c.metadata["session_id"] == "sess-1"
    assert c.metadata["event_id"] == "evt-9"
    assert c.metadata["series_id"] == "ser-3"
    assert c.metadata["meeting_title"] == "ACC Staff Meeting"
    assert c.metadata["started_at"] == "2026-09-17T02:00:00Z"


def test_missing_transcript_yields_no_transcript_chunk():
    chunks = normalise_session(_session(transcript=""))
    assert all(c.metadata["content_subtype"] != "transcript" for c in chunks)


def test_empty_session_yields_no_chunks():
    assert normalise_session(_session(documents={}, transcript="")) == []


def test_content_hash_is_stable_across_calls():
    a = normalise_session(_session())
    b = normalise_session(_session())
    assert [c.content_hash for c in a] == [c.content_hash for c in b]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_anarlog_normalise.py -v`
Expected: FAIL — `ImportError: cannot import name 'normalise_session'`

- [ ] **Step 3: Write the minimal implementation**

Append to `mcpbrain/sync/anarlog.py`:

```python
from mcpbrain.chunking import chunk_text, content_hash
from mcpbrain.sync.normalise import Chunk

_DOC_KINDS = ("summary", "note")


def read_session(db, session_id: str) -> dict | None:
    """Assemble one session's row, its documents and its transcript."""
    row = db.execute(
        "SELECT id, title, started_at, event_id, series_id FROM sessions "
        "WHERE id = ? AND deleted_at IS NULL", (session_id,)).fetchone()
    if row is None:
        return None
    docs = {}
    for d in db.execute(
            "SELECT kind, body FROM session_documents "
            "WHERE session_id = ? AND deleted_at IS NULL", (session_id,)).fetchall():
        if d["kind"] in _DOC_KINDS and d["body"]:
            docs[d["kind"]] = d["body"]
    tr = db.execute(
        "SELECT words_json FROM transcripts WHERE session_id = ? "
        "AND deleted_at IS NULL LIMIT 1", (session_id,)).fetchone()
    return {
        "id": row["id"],
        "title": row["title"] or "",
        "started_at": row["started_at"] or "",
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
    }
    out: list[Chunk] = []
    bodies = [(k, prosemirror_to_markdown(session["documents"][k]))
              for k in _DOC_KINDS if session.get("documents", {}).get(k)]
    bodies.append(("transcript", session.get("transcript") or ""))
    for kind, text in bodies:
        if not text.strip():
            continue
        for i, piece in enumerate(chunk_text(text)):
            out.append(Chunk(
                doc_id=f"anarlog-{sid}-{kind}-{i}",
                text=piece,
                content_hash=content_hash(piece),
                metadata={**base, "content_subtype": kind,
                          "chunk_index": i},
            ))
    return out
```

If `content_hash` does not live in `mcpbrain.chunking`, locate it with
`grep -rn "def content_hash" mcpbrain/` and import from there — `sync/calendar.py`
already imports it, so match that import exactly.

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/test_anarlog_normalise.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/sync/anarlog.py tests/test_anarlog_normalise.py
git commit -m "feat(anarlog): normalise a session into summary/note/transcript chunks

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01LvzZKvhmShfY3FHbUF2Gx8"
```

---

### Task 6: `discover_anarlog` and `handle_anarlog_item`

**Files:**
- Modify: `mcpbrain/sync/anarlog.py`
- Test: `tests/test_anarlog_sync.py`

**Interfaces:**
- Consumes: everything from Tasks 1-5, plus `store.enqueue`/`store.get_cursor`/`store.set_cursor`
- Produces:
  - `discover_anarlog(store, *, db_path, budget=None, bulk_section=None) -> int`
  - `handle_anarlog_item(store, item, *, db_path, bulk_section=None) -> None`

**Before writing:** read `mcpbrain/sync/calendar.py:356-460` (`discover_calendar` and `handle_calendar_item`) in full and match its enqueue call, cursor discipline and `bulk_section` usage exactly. Confirm the enqueue helper's real name with `grep -n "def enqueue" mcpbrain/sync/queue.py mcpbrain/store.py`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_anarlog_sync.py`:

```python
import json
import sqlite3
import pytest
from mcpbrain.store import Store
from mcpbrain.sync import anarlog


def _anarlog_db(path, sessions, *, version="20260909160300"):
    db = sqlite3.connect(str(path))
    db.execute("CREATE TABLE _sqlx_migrations(version TEXT, description TEXT)")
    db.execute("INSERT INTO _sqlx_migrations VALUES(?, 'x')", (version,))
    db.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY, title TEXT, "
               "updated_at TEXT, deleted_at TEXT, started_at TEXT, "
               "event_id TEXT, series_id TEXT, external_provider TEXT)")
    db.execute("CREATE TABLE session_documents(session_id TEXT, kind TEXT, "
               "body TEXT, body_format TEXT, deleted_at TEXT)")
    db.execute("CREATE TABLE transcripts(session_id TEXT, words_json TEXT, "
               "deleted_at TEXT)")
    for s in sessions:
        db.execute("INSERT INTO sessions(id,title,updated_at,deleted_at,"
                   "started_at,event_id,series_id) VALUES(?,?,?,?,?,?,?)",
                   (s["id"], s.get("title", ""), s["updated_at"],
                    s.get("deleted_at"), s.get("started_at", ""),
                    s.get("event_id", ""), s.get("series_id", "")))
        if s.get("summary"):
            db.execute("INSERT INTO session_documents VALUES(?,?,?,?,NULL)",
                       (s["id"], "summary", json.dumps({"type": "doc", "content": [
                           {"type": "paragraph", "content": [
                               {"type": "text", "text": s["summary"]}]}]}),
                        "prosemirror_json"))
        if s.get("transcript"):
            db.execute("INSERT INTO transcripts VALUES(?,?,NULL)",
                       (s["id"], json.dumps([{"id": "w:0",
                                              "text": s["transcript"]}])))
    db.commit(); db.close()


def _store(tmp_path):
    return Store(str(tmp_path / "brain.sqlite3"), dim=8)


def test_discover_enqueues_and_advances_cursor(tmp_path):
    p = tmp_path / "app.db"
    _anarlog_db(p, [{"id": "a", "updated_at": "2026-09-17T01:00:00Z",
                     "summary": "hello"}])
    s = _store(tmp_path)
    n = anarlog.discover_anarlog(s, db_path=str(p))
    assert n == 1
    assert s.get_cursor("anarlog") == "2026-09-17T01:00:00Z"


def test_discover_stops_when_a_required_column_is_missing(tmp_path):
    p = tmp_path / "app.db"
    db = sqlite3.connect(str(p))
    db.execute("CREATE TABLE _sqlx_migrations(version TEXT, description TEXT)")
    db.execute("INSERT INTO _sqlx_migrations VALUES('20270101000000','x')")
    db.execute("CREATE TABLE sessions(id TEXT, updated_at TEXT)")  # missing cols
    db.execute("CREATE TABLE session_documents(session_id TEXT)")
    db.execute("CREATE TABLE transcripts(session_id TEXT)")
    db.commit(); db.close()
    s = _store(tmp_path)
    with pytest.raises(RuntimeError) as exc:
        anarlog.discover_anarlog(s, db_path=str(p))
    assert "sessions." in str(exc.value)


def test_handle_writes_hot_summary_and_cold_transcript(tmp_path):
    p = tmp_path / "app.db"
    _anarlog_db(p, [{"id": "a", "title": "Staff",
                     "updated_at": "2026-09-17T01:00:00Z",
                     "summary": "We agreed.", "transcript": "Spoken words."}])
    s = _store(tmp_path)
    anarlog.handle_anarlog_item(s, {"event": "upsert", "ref_id": "a"},
                                db_path=str(p))
    ids = set(s.doc_ids_for_messages(["anarlog-a"]))
    assert "anarlog-a-summary-0" in ids
    assert "anarlog-a-transcript-0" in ids


def test_handle_remove_deletes_every_chunk_of_the_session(tmp_path):
    p = tmp_path / "app.db"
    _anarlog_db(p, [{"id": "a", "updated_at": "2026-09-17T01:00:00Z",
                     "summary": "We agreed.", "transcript": "Spoken."}])
    s = _store(tmp_path)
    anarlog.handle_anarlog_item(s, {"event": "upsert", "ref_id": "a"},
                                db_path=str(p))
    assert s.doc_ids_for_messages(["anarlog-a"]) != []
    anarlog.handle_anarlog_item(s, {"event": "remove", "ref_id": "a"},
                                db_path=str(p))
    assert s.doc_ids_for_messages(["anarlog-a"]) == []


def test_reprocessing_the_boundary_row_is_idempotent(tmp_path):
    p = tmp_path / "app.db"
    _anarlog_db(p, [{"id": "a", "updated_at": "2026-09-17T01:00:00Z",
                     "summary": "We agreed."}])
    s = _store(tmp_path)
    anarlog.handle_anarlog_item(s, {"event": "upsert", "ref_id": "a"},
                                db_path=str(p))
    first = sorted(s.doc_ids_for_messages(["anarlog-a"]))
    anarlog.handle_anarlog_item(s, {"event": "upsert", "ref_id": "a"},
                                db_path=str(p))
    assert sorted(s.doc_ids_for_messages(["anarlog-a"])) == first
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_anarlog_sync.py -v`
Expected: FAIL — `AttributeError: module 'mcpbrain.sync.anarlog' has no attribute 'discover_anarlog'`

- [ ] **Step 3: Write the minimal implementation**

Append to `mcpbrain/sync/anarlog.py` (adjust the enqueue call to the real helper name found in Step 0):

```python
from contextlib import nullcontext

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
    bulk_section = bulk_section or nullcontext
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
    with bulk_section():
        for r in rows:
            store.enqueue(_SOURCE, r["id"],
                          "remove" if r["deleted"] else "upsert")
        # Advance only after every row is durably enqueued — an interrupted
        # discovery costs a re-list next cycle, never lost work
        # (discover_calendar's contract).
        store.set_cursor(_SOURCE, rows[-1]["updated_at"])
    return len(rows)


def handle_anarlog_item(store, item, *, db_path, bulk_section=None) -> None:
    """Work one queued session. Raises on failure so the loop backs it off."""
    bulk_section = bulk_section or nullcontext
    sid = item["ref_id"]

    if item["event"] == "remove":
        with bulk_section():
            doc_ids = store.doc_ids_for_messages([f"anarlog-{sid}"])
            if doc_ids:
                store.delete_chunks(doc_ids)
        return

    with connect_ro(db_path) as db:
        session = read_session(db, sid)
    if session is None:
        # Deleted between discovery and handling: treat as a removal rather
        # than leaving orphaned chunks behind.
        with bulk_section():
            doc_ids = store.doc_ids_for_messages([f"anarlog-{sid}"])
            if doc_ids:
                store.delete_chunks(doc_ids)
        return

    chunks = normalise_session(session)
    with bulk_section():
        # Drop chunks that no longer exist (a note that shrank from 3 chunks to
        # 1 would otherwise leave two stale rows resolvable by session_id).
        live = {c.doc_id for c in chunks}
        for stale in set(store.doc_ids_for_messages([f"anarlog-{sid}"])) - live:
            store.delete_chunks([stale])
        for ch in chunks:
            store.upsert_chunk(ch.doc_id, ch.text, ch.content_hash, ch.metadata)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/test_anarlog_sync.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/sync/anarlog.py tests/test_anarlog_sync.py
git commit -m "feat(anarlog): discover/handle with watermark, deletions and drift guard

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01LvzZKvhmShfY3FHbUF2Gx8"
```

---

### Task 7: Block meeting claims from the org shared graph

**Files:**
- Modify: `mcpbrain/org_contrib.py:118` (`_source_kind` mapping), and the relation loop in `collect_from_drain` (~lines 141-152, beside the existing cold-provenance check)
- Test: `tests/test_anarlog_no_contribution.py`

**Interfaces:**
- Consumes: chunk metadata `source_type="anarlog"` from Task 5
- Produces: `_source_kind()` returns `"meeting"` for anarlog chunks; `collect_from_drain` emits nothing for them.

**Critical:** `_source_kind` currently maps only `{"gmail": "email", "drive": "drive", "calendar": "calendar"}` and returns `"unknown"` otherwise. A guard written as `== "anarlog"` would **never fire** — a silent no-op that would let meeting claims contribute after all. The mapping must gain the new source first.

- [ ] **Step 1: Write the failing test**

Create `tests/test_anarlog_no_contribution.py`:

```python
from mcpbrain.store import Store
from mcpbrain import org_contrib
from mcpbrain.fleet import FleetPin


def _store(tmp_path):
    return Store(str(tmp_path / "brain.sqlite3"), dim=8)


def _pin():
    return FleetPin(fleet_secret="s" * 32,
                    relation_allowlist=["works_at", "member_of"])


def test_source_kind_maps_anarlog_to_meeting(tmp_path):
    s = _store(tmp_path)
    s.upsert_chunk("anarlog-a-summary-0", "text", "h1",
                   {"source_type": "anarlog", "session_id": "a"})
    assert org_contrib._source_kind(s, "anarlog-a-summary-0") == "meeting"


def test_meeting_sourced_relation_never_contributes(tmp_path):
    s = _store(tmp_path)
    s.upsert_chunk("anarlog-a-summary-0", "text", "h1",
                   {"source_type": "anarlog", "session_id": "a"})
    delta = {
        "relations": [{"entity_a": "dana-okafor", "relation": "works_at",
                       "entity_b": "northgate-trust", "origin": "local",
                       "source_doc_id": "anarlog-a-summary-0",
                       "valid_from": "2026-09-17", "confidence": 1.0}],
        "entities": {
            "dana-okafor": {"type": "person", "origin": "local",
                            "email_addr": "dana@northgate.example"},
            "northgate-trust": {"type": "org", "origin": "local"},
        },
    }
    assert org_contrib.collect_from_drain(s, delta, _pin(), "me@example.com") == 0


def test_non_meeting_relation_still_contributes(tmp_path):
    s = _store(tmp_path)
    s.upsert_chunk("gmail-1", "text", "h1", {"source_type": "gmail"})
    delta = {
        "relations": [{"entity_a": "dana-okafor", "relation": "works_at",
                       "entity_b": "northgate-trust", "origin": "local",
                       "source_doc_id": "gmail-1",
                       "valid_from": "2026-09-17", "confidence": 1.0}],
        "entities": {
            "dana-okafor": {"type": "person", "origin": "local",
                            "email_addr": "dana@northgate.example"},
            "northgate-trust": {"type": "org", "origin": "local"},
        },
    }
    assert org_contrib.collect_from_drain(s, delta, _pin(), "me@example.com") > 0
```

If `FleetPin`'s constructor signature differs, confirm it with
`grep -n "class FleetPin" -A 12 mcpbrain/fleet.py` and match it — do not guess.

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_anarlog_no_contribution.py -v`
Expected: FAIL — `_source_kind` returns `"unknown"`, and the meeting relation contributes.

- [ ] **Step 3: Write the minimal implementation**

**3a.** In `_source_kind` (line 118), extend the mapping:

```python
    return {"gmail": "email", "drive": "drive", "calendar": "calendar",
            "anarlog": "meeting"}.get(st, "unknown")
```

**3b.** In `collect_from_drain`'s relation loop, immediately after the existing cold-provenance check:

```python
        if not doc_id or _is_cold(store, doc_id):
            continue                               # no/cold provenance — fail closed
        if _source_kind(store, doc_id) == "meeting":
            continue                               # meetings never contribute:
            # meeting content is personnel-adjacent by nature (staff meetings,
            # performance, grievance), so no meeting-derived claim leaves this
            # machine regardless of relation type. Transcripts were already
            # excluded by the cold check above; this covers the hot summary and
            # note. Deliberate policy, not an oversight — see the 2026-09-21
            # anarlog design doc §10.
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/test_anarlog_no_contribution.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Check existing contribution tests still pass**

Run: `pytest tests/test_org_contrib.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/org_contrib.py tests/test_anarlog_no_contribution.py
git commit -m "feat(org-contrib): meeting-sourced claims never contribute

_source_kind gains anarlog -> meeting so the guard can actually fire; a check
against \"anarlog\" would have matched nothing and silently contributed.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01LvzZKvhmShfY3FHbUF2Gx8"
```

---

### Task 8: Wire the source into the sync cycle and config

**Files:**
- Modify: `mcpbrain/sync/__init__.py` (imports at ~line 12-17; `discovered`/`handlers` block ~lines 165-180)
- Modify: `mcpbrain/config.py` (add `anarlog_db_path()`)
- Test: `tests/test_anarlog_wiring.py`

**Interfaces:**
- Consumes: `discover_anarlog`, `handle_anarlog_item` (Task 6)
- Produces: `config.anarlog_db_path() -> str | None`; source active in `run_sync_cycle` when the DB exists.

- [ ] **Step 1: Write the failing test**

Create `tests/test_anarlog_wiring.py`:

```python
from pathlib import Path
from mcpbrain import config


def test_db_path_defaults_to_the_anarlog_location(tmp_path, monkeypatch):
    home = tmp_path / "Library" / "Application Support" / "anarlog"
    home.mkdir(parents=True)
    (home / "app.db").write_text("")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    assert config.anarlog_db_path() == str(home / "app.db")


def test_db_path_is_none_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    assert config.anarlog_db_path() is None


def test_sync_module_exposes_the_source():
    from mcpbrain import sync
    assert hasattr(sync, "discover_anarlog")
    assert hasattr(sync, "handle_anarlog_item")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_anarlog_wiring.py -v`
Expected: FAIL — `AttributeError: module 'mcpbrain.config' has no attribute 'anarlog_db_path'`

- [ ] **Step 3: Write the minimal implementation**

**3a.** In `mcpbrain/config.py`:

```python
def anarlog_db_path() -> str | None:
    """Path to anarlog's app.db, or None when anarlog is not installed.

    Config key `anarlog.db_path` overrides the default. Returning None
    disables the source silently — most installs will not have anarlog, and a
    warning per cycle for a tool the user never installed is noise.
    """
    cfg = (load_config().get("anarlog") or {})
    explicit = (cfg.get("db_path") or "").strip()
    if explicit:
        return explicit if Path(explicit).exists() else None
    default = (Path.home() / "Library" / "Application Support"
               / "anarlog" / "app.db")
    return str(default) if default.exists() else None
```

Match the module's existing config-reading helper — confirm with
`grep -n "def load_config\|def _cfg" mcpbrain/config.py` and use whatever the
neighbouring accessors use rather than introducing a new pattern.

**3b.** In `mcpbrain/sync/__init__.py`, add to the top-level imports (module-level, matching the file's comment about monkeypatchability):

```python
from mcpbrain.sync.anarlog import discover_anarlog, handle_anarlog_item
```

**3c.** In `run_sync_cycle`, beside the other sources:

```python
    anarlog_db = config.anarlog_db_path()
    if anarlog_db:
        discovered["anarlog"] = discover_anarlog(
            store, db_path=anarlog_db, budget=disc_budget,
            bulk_section=bulk_section)
        handlers["anarlog"] = lambda it: handle_anarlog_item(
            store, it, db_path=anarlog_db, bulk_section=bulk_section)
```

Place the `discovered[...]` call with the other `discover_*` calls and the
`handlers[...]` assignment with the other handler registrations, following the
file's existing ordering.

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/test_anarlog_wiring.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Run the whole anarlog suite plus sync tests**

Run: `pytest tests/test_anarlog_*.py tests/test_sync_queue.py -q`
Expected: PASS

- [ ] **Step 6: Run ruff**

Run: `ruff check mcpbrain/ tests/test_anarlog_*.py`
Expected: no findings

- [ ] **Step 7: Commit**

```bash
git add mcpbrain/sync/__init__.py mcpbrain/config.py tests/test_anarlog_wiring.py
git commit -m "feat(anarlog): wire the meeting source into the sync cycle

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01LvzZKvhmShfY3FHbUF2Gx8"
```

---

### Task 9: Verify against the real anarlog database and the real store

**Files:**
- Create: none (verification task)
- Modify: `CLAUDE.md` (add the source to the project context)

This task exists because this repo's recorded failures were all found by
checking the RUNNING system, never by reading a diff or a green test run. It is
attended: do not run it unsupervised.

- [ ] **Step 1: Confirm the real anarlog DB is readable and its schema matches**

```bash
python3 -c "
from mcpbrain.sync import anarlog
from mcpbrain import config
p = config.anarlog_db_path()
print('db_path:', p)
with anarlog.connect_ro(p) as db:
    print('schema_status:', anarlog.schema_status(db))
    print('changed(all):', len(anarlog.changed_sessions(db, '', 200)))
"
```

Expected: `schema_status` → `(True, '20260909160300', [])`, and a non-zero
session count. If `ok` is False, STOP: anarlog has changed its schema and
`_REQUIRED_COLUMNS`/`_PINNED_SCHEMA_VERSION` need review before anything is
ingested.

- [ ] **Step 2: Dry-run normalisation against the two real meetings**

```bash
python3 -c "
from mcpbrain.sync import anarlog
from mcpbrain import config
p = config.anarlog_db_path()
with anarlog.connect_ro(p) as db:
    for row in anarlog.changed_sessions(db, '', 200):
        if row['deleted']:
            continue
        s = anarlog.read_session(db, row['id'])
        cs = anarlog.normalise_session(s)
        kinds = {}
        for c in cs:
            kinds[c.metadata['content_subtype']] = kinds.get(
                c.metadata['content_subtype'], 0) + 1
        print(s['title'][:40], '|', kinds, '| event_id:', bool(s['event_id']))
"
```

Expected: both ACC meetings listed, each with `summary`/`note`/`transcript`
counts, transcripts producing many chunks. Confirm titles are the real ones
(`ACC Staff Meeting`, `ACC State Secretaries & Managers Meeting`), not empty.

- [ ] **Step 3: Confirm the salience gate would cold-mark the transcripts**

```bash
python3 -c "
from mcpbrain.prepare import should_enrich
for st in ('summary','note','transcript'):
    print(st, should_enrich({'metadata':
        {'source_type':'anarlog','content_subtype':st}}))
"
```

Expected: `summary True`, `note True`, `transcript False`.

- [ ] **Step 4: Run one real sync cycle against the live store, with the daemon stopped**

Per this repo's rules, stop the daemon properly — `launchctl stop` is NOT
sufficient, KeepAlive relaunches it within seconds:

```bash
launchctl bootout gui/$(id -u)/com.mcpbrain
pgrep -fl mcpbrain || echo "daemon down (required before proceeding)"
```

Then run one discovery + handle pass against the real store, and afterwards:

```bash
sqlite3 "file:$HOME/Library/Application Support/mcpbrain/brain.sqlite3?mode=ro" \
  "SELECT json_extract(metadata,'\$.content_subtype') AS k, COUNT(*),
          SUM(enrich_state='cold') AS cold
   FROM chunks WHERE json_extract(metadata,'\$.source_type')='anarlog'
   GROUP BY k;"
```

Expected: `summary`/`note` rows with `cold = 0`, `transcript` rows with
`cold` equal to their count.

- [ ] **Step 5: Run `PRAGMA integrity_check` after the attended store operation**

```bash
sqlite3 "$HOME/Library/Application Support/mcpbrain/brain.sqlite3" "PRAGMA integrity_check;"
```

Expected: `ok`. This repo's 2026-09-10 corruption went undetected for hours
because nothing checked it.

- [ ] **Step 6: Restart the daemon and confirm it is serving**

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.mcpbrain.plist
curl -s -H "Authorization: Bearer $(cat "$MCPBRAIN_HOME/control_token")" \
  http://127.0.0.1:8765/api/status | head -c 400
```

`bootout` unregisters the login agent, so skipping the `bootstrap` leaves the
daemon gone across reboots.

- [ ] **Step 7: Record the source in CLAUDE.md**

Add a short entry under the project context describing the anarlog source,
that it is read-only, that transcripts are cold, and that meetings never
contribute to the org graph. Link the spec.

- [ ] **Step 8: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: record the anarlog meeting source in project context

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01LvzZKvhmShfY3FHbUF2Gx8"
```

---

## Out of scope (recorded in the spec's Follow-ups)

- Speaker attribution / diarization
- Writing back to anarlog via `propose_summary_edit`
- Fixing the `Busy` calendars (a calendar sharing setting, not code)
- Any version bump or release
