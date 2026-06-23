"""Tests for mcpbrain.lessons — S5 outcome-grounded lessons-learned writer.

Acceptance criteria (#21):
  - Lessons are written ONLY when grounded in observed outcomes (external signal).
  - No outcomes → no lessons written.
  - Independent verification check required before writing.
  - Duplicate lessons (same content hash) are not re-written.
  - write_lessons() never raises even with a broken store.
  - Flag-off path returns early without touching the store.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

import pytest

from mcpbrain.lessons import (
    init_lessons_table,
    read_recent_outcomes,
    write_lessons,
    _content_hash,
    _already_written,
    _write_lesson,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_home(tmp_path, **flags):
    (tmp_path / "config.json").write_text(json.dumps(flags))
    return str(tmp_path)


class _InMemoryStore:
    """Minimal store with in-memory SQLite + mock recall_feedback."""
    def __init__(self, feedback_rows=None):
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row
        self._feedback = feedback_rows or []

    def _connect(self):
        return self._conn

    def all_feedback_rows(self):
        return self._feedback


def _make_store_with_outcomes(events: list[dict]) -> _InMemoryStore:
    """Create store with recall_feedback rows already populated."""
    store = _InMemoryStore()
    # Seed recall_feedback table
    with store._connect() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS recall_feedback (
                id INTEGER PRIMARY KEY,
                doc_id TEXT, session_id TEXT, event_type TEXT, ts TEXT
            )
        """)
        now = datetime.now(timezone.utc)
        for i, e in enumerate(events):
            ts = (now - timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
            db.execute(
                "INSERT INTO recall_feedback(doc_id, session_id, event_type, ts) VALUES(?,?,?,?)",
                (e["doc_id"], "session", e["event_type"], ts),
            )
    return store


# ---------------------------------------------------------------------------
# init_lessons_table
# ---------------------------------------------------------------------------

def test_init_lessons_table_creates_schema():
    store = _InMemoryStore()
    init_lessons_table(store)
    rows = store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='recall_lessons'"
    ).fetchall()
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# read_recent_outcomes
# ---------------------------------------------------------------------------

def test_read_recent_outcomes_filters_to_used_edited():
    store = _make_store_with_outcomes([
        {"doc_id": "d1", "event_type": "used"},
        {"doc_id": "d2", "event_type": "exposure"},
        {"doc_id": "d3", "event_type": "edited"},
    ])
    outcomes = read_recent_outcomes(store, days=30)
    event_types = {o["event_type"] for o in outcomes}
    assert "used" in event_types
    assert "edited" in event_types
    assert "exposure" not in event_types


def test_read_recent_outcomes_empty_table():
    store = _InMemoryStore()
    # No feedback table at all — should return [] gracefully
    outcomes = read_recent_outcomes(store, days=7)
    assert outcomes == []


def test_read_recent_outcomes_broken_store():
    store = MagicMock()
    store._connect.side_effect = RuntimeError("broken")
    outcomes = read_recent_outcomes(store, days=7)
    assert outcomes == []


# ---------------------------------------------------------------------------
# _content_hash and _already_written
# ---------------------------------------------------------------------------

def test_content_hash_stable():
    h1 = _content_hash("Recall is helpful when users ask about recent decisions.")
    h2 = _content_hash("Recall is helpful when users ask about recent decisions.")
    assert h1 == h2


def test_content_hash_case_insensitive():
    h1 = _content_hash("Recall helps.")
    h2 = _content_hash("RECALL HELPS.")
    assert h1 == h2


def test_already_written_false_when_empty():
    store = _InMemoryStore()
    init_lessons_table(store)
    assert _already_written(store, "abc123") is False


def test_already_written_true_after_write():
    store = _InMemoryStore()
    init_lessons_table(store)
    _write_lesson(store, "Test lesson.", [{"doc_id": "d1"}])
    h = _content_hash("Test lesson.")
    assert _already_written(store, h) is True


# ---------------------------------------------------------------------------
# write_lessons() — integration
# ---------------------------------------------------------------------------

def test_write_lessons_flag_off(tmp_path):
    """Acceptance: lessons_enabled=false → skip immediately."""
    home = _make_home(tmp_path, lessons=False)
    store = MagicMock()
    result = write_lessons(store, home)
    assert result["written"] == 0
    assert result["skipped"] == "lessons_enabled=false"
    store._connect.assert_not_called()


def test_write_lessons_no_outcomes(tmp_path):
    """Acceptance: no 'used'/'edited' events → no lessons written (external signal gate)."""
    home = _make_home(tmp_path, lessons=True)
    store = _make_store_with_outcomes([
        {"doc_id": "d1", "event_type": "exposure"},
        {"doc_id": "d2", "event_type": "exposure"},
    ])
    init_lessons_table(store)
    result = write_lessons(store, home)
    assert result["written"] == 0
    assert result["skipped"] == "no observed outcomes"


def test_write_lessons_llm_unavailable(tmp_path):
    """When claude CLI is absent, skip gracefully — don't write unverified."""
    home = _make_home(tmp_path, lessons=True)
    store = _make_store_with_outcomes([{"doc_id": "d1", "event_type": "used"}])
    init_lessons_table(store)
    with patch("mcpbrain.lessons._call_claude", return_value=""):
        result = write_lessons(store, home)
    assert result["written"] == 0
    assert "LLM unavailable" in (result["skipped"] or "")


def test_write_lessons_verification_fails(tmp_path):
    """Acceptance: lesson that fails independent check is NOT written."""
    home = _make_home(tmp_path, lessons=True)
    store = _make_store_with_outcomes([{"doc_id": "d1", "event_type": "used"}])
    init_lessons_table(store)

    extract_response = json.dumps({"lesson": "Memory recall is sometimes helpful."})
    verify_response = json.dumps({"grounded": False, "reason": "Too vague, not specific to observations."})

    responses = iter([extract_response, verify_response])
    with patch("mcpbrain.lessons._call_claude", side_effect=lambda *a, **k: next(responses)):
        result = write_lessons(store, home)

    assert result["written"] == 0
    assert result["skipped"] == "failed independent check"


def test_write_lessons_writes_when_verified(tmp_path):
    """Acceptance: grounded lesson with positive verification IS written."""
    home = _make_home(tmp_path, lessons=True)
    store = _make_store_with_outcomes([
        {"doc_id": "d1", "event_type": "used"},
        {"doc_id": "d2", "event_type": "edited"},
    ])
    init_lessons_table(store)

    lesson_text = "Recall of decision docs is valuable when users ask about commitments."
    extract_response = json.dumps({"lesson": lesson_text})
    verify_response = json.dumps({"grounded": True, "reason": "Directly supported by the 'used'/'edited' events."})

    responses = iter([extract_response, verify_response])
    with patch("mcpbrain.lessons._call_claude", side_effect=lambda *a, **k: next(responses)):
        result = write_lessons(store, home)

    assert result["written"] == 1
    assert result["lesson"] == lesson_text
    # Check it's actually in the table
    with store._connect() as db:
        rows = db.execute("SELECT lesson_text FROM recall_lessons").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == lesson_text


def test_write_lessons_deduplicates(tmp_path):
    """Acceptance: same lesson is not written twice (content hash dedup)."""
    home = _make_home(tmp_path, lessons=True)
    store = _make_store_with_outcomes([{"doc_id": "d1", "event_type": "used"}])
    init_lessons_table(store)

    lesson_text = "Recall helps with commitments."
    extract_response = json.dumps({"lesson": lesson_text})
    verify_response = json.dumps({"grounded": True, "reason": "Grounded."})

    # First write succeeds
    with patch("mcpbrain.lessons._call_claude",
               side_effect=[extract_response, verify_response]):
        r1 = write_lessons(store, home)
    assert r1["written"] == 1

    # Second call returns same lesson — should be deduped
    with patch("mcpbrain.lessons._call_claude", return_value=extract_response):
        r2 = write_lessons(store, home)
    assert r2["written"] == 0
    assert r2["skipped"] == "duplicate lesson"


def test_write_lessons_never_raises(tmp_path):
    """write_lessons() must not raise even with a completely broken store."""
    home = _make_home(tmp_path, lessons=True)
    store = MagicMock()
    store._connect.side_effect = RuntimeError("store broken")

    result = write_lessons(store, home)
    assert isinstance(result, dict)
    assert result["written"] == 0
