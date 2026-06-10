"""Tests for mcpbrain.waiting_on — Phase 3 Task 5.

Sub-task 5.1: _matches / reconcile (match + clear logic).
Sub-task 5.2: store.inbound_chunks_since + run entry point.
"""

import json
import pytest
from datetime import datetime, timedelta, timezone

from mcpbrain import graph_write as gw
from mcpbrain.store import Store
from mcpbrain.waiting_on import _normalise, _matches, reconcile, run


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db", dim=64)
    s.init()
    return s


def _add_action(store, waiting_on="Taryn Hamilton", waiting_on_entity_id="",
                waiting_on_set_at=None, status="open"):
    """Insert an action with waiting_on set. Returns the new action id."""
    set_at = waiting_on_set_at or (
        datetime.now(timezone.utc) - timedelta(days=1)
    ).isoformat()
    with store._connect() as db:
        db.execute(
            "INSERT INTO actions(text, status, waiting_on, waiting_on_entity_id, waiting_on_set_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("Test action", status, waiting_on, waiting_on_entity_id, set_at),
        )
        return db.execute("SELECT last_insert_rowid()").fetchone()[0]


def _make_chunk(doc_id="chunk-1", sender_name="Taryn Hamilton", sender_entity_id="",
                labels=None, date="2026-06-01"):
    meta = {
        "sender": sender_name,
        "sender_entity_id": sender_entity_id,
        "labels": labels or [],
        "date": date,
    }
    return {"doc_id": doc_id, "text": "test", "metadata": meta}


def _insert_chunk(store, doc_id, date="2026-06-01", sender="Taryn Hamilton", labels=None):
    """Insert a chunk directly into the store's chunks table."""
    meta = json.dumps({"sender": sender, "date": date, "labels": labels or []})
    with store._connect() as db:
        db.execute(
            "INSERT INTO chunks(doc_id, text, content_hash, metadata) VALUES(?,?,?,?)",
            (doc_id, "test text", f"hash-{doc_id}", meta),
        )


# ---------------------------------------------------------------------------
# Sub-task 5.1 — _matches tests
# ---------------------------------------------------------------------------

def test_matches_by_entity_id():
    """A chunk with sender_entity_id matching waiting_on_entity_id -> match."""
    chunk = _make_chunk(sender_entity_id="ent-taryn-001")
    assert _matches(chunk, waiting_on="Taryn Hamilton", entity_id="ent-taryn-001")


def test_matches_by_normalised_name():
    """waiting_on='Taryn Hamilton', chunk sender_name 'taryn  hamilton!' -> match."""
    chunk = _make_chunk(sender_name="taryn  hamilton!")
    assert _matches(chunk, waiting_on="Taryn Hamilton", entity_id=None)


def test_no_match_different_sender():
    """Different person -> no match."""
    chunk = _make_chunk(sender_name="Joel Chelliah", sender_entity_id="ent-joel-001")
    assert not _matches(chunk, waiting_on="Taryn Hamilton", entity_id="ent-taryn-001")


def test_normalise_strips_punctuation_and_collapses_whitespace():
    """_normalise strips non-word chars, lowercases, collapses whitespace."""
    assert _normalise("Taryn  Hamilton!") == "taryn hamilton"
    # "taryn.hamilton" -> dot stripped (not a word char or space), no space inserted
    assert _normalise("  taryn.hamilton  ") == "tarynhamilton"


def test_normalise_empty_inputs():
    assert _normalise(None) == ""
    assert _normalise("") == ""
    assert _normalise("   ") == ""


# ---------------------------------------------------------------------------
# Sub-task 5.1 — reconcile tests
# ---------------------------------------------------------------------------

def test_reconcile_clears_waiting(store):
    """Open action with waiting_on='Taryn Hamilton' + matching inbound chunk
    -> action's waiting_on nulled, reply_received=1, waiting_on_cleared_by_doc_id set."""
    action_id = _add_action(store, waiting_on="Taryn Hamilton")
    chunk = _make_chunk(doc_id="chunk-taryn-1", sender_name="Taryn Hamilton")
    now = datetime.now(timezone.utc).isoformat()

    cleared = reconcile(store, [chunk], now=now)

    assert cleared == 1
    with store._connect() as db:
        row = db.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
    assert row["waiting_on"] is None
    assert row["reply_received"] == 1
    assert row["waiting_on_cleared_by_doc_id"] == "chunk-taryn-1"


def test_reconcile_respects_window(store):
    """Waiting action set 40 days ago with window=30 -> not cleared."""
    old_set_at = (
        datetime.now(timezone.utc) - timedelta(days=40)
    ).isoformat()
    action_id = _add_action(store, waiting_on="Taryn Hamilton",
                            waiting_on_set_at=old_set_at)
    chunk = _make_chunk(sender_name="Taryn Hamilton")
    now = datetime.now(timezone.utc).isoformat()

    cleared = reconcile(store, [chunk], now=now, window_days=30)

    assert cleared == 0
    with store._connect() as db:
        row = db.execute("SELECT waiting_on FROM actions WHERE id=?", (action_id,)).fetchone()
    # waiting_on must still be set
    assert row["waiting_on"] == "Taryn Hamilton"


def test_reconcile_ignores_outbound(store):
    """A SENT chunk does not clear waiting_on."""
    action_id = _add_action(store, waiting_on="Taryn Hamilton")
    # Chunk from Taryn but labelled SENT (outbound)
    chunk = _make_chunk(sender_name="Taryn Hamilton", labels=["SENT"])
    now = datetime.now(timezone.utc).isoformat()

    cleared = reconcile(store, [chunk], now=now)

    assert cleared == 0
    with store._connect() as db:
        row = db.execute("SELECT waiting_on FROM actions WHERE id=?", (action_id,)).fetchone()
    assert row["waiting_on"] == "Taryn Hamilton"


def test_reconcile_only_clears_once_per_action(store):
    """Two matching chunks -> action cleared once (break after first match)."""
    _add_action(store, waiting_on="Taryn Hamilton")
    chunks = [
        _make_chunk(doc_id="chunk-a", sender_name="Taryn Hamilton"),
        _make_chunk(doc_id="chunk-b", sender_name="taryn hamilton"),
    ]
    now = datetime.now(timezone.utc).isoformat()

    cleared = reconcile(store, chunks, now=now)

    assert cleared == 1


def test_reconcile_no_actions_no_op(store):
    """No waiting actions -> nothing cleared."""
    chunk = _make_chunk(sender_name="Taryn Hamilton")
    cleared = reconcile(store, [chunk])
    assert cleared == 0


def test_reconcile_no_chunks_no_op(store):
    """Waiting actions but no chunks -> nothing cleared."""
    _add_action(store)
    cleared = reconcile(store, [])
    assert cleared == 0


# ---------------------------------------------------------------------------
# Sub-task 5.2 — inbound_chunks_since tests
# ---------------------------------------------------------------------------

def test_recent_inbound_chunks_since_returns_newer(store):
    """inbound_chunks_since(cursor) returns chunks with date > cursor only."""
    _insert_chunk(store, "old-1", date="2026-05-01")
    _insert_chunk(store, "new-1", date="2026-06-01")
    _insert_chunk(store, "new-2", date="2026-06-02")

    results = store.inbound_chunks_since("2026-05-31")
    doc_ids = {r["doc_id"] for r in results}
    assert "new-1" in doc_ids
    assert "new-2" in doc_ids
    assert "old-1" not in doc_ids


def test_recent_inbound_chunks_since_none_returns_all_inbound(store):
    """cursor=None returns all inbound chunks (no SENT filter)."""
    _insert_chunk(store, "a", date="2026-01-01")
    _insert_chunk(store, "b", date="2026-02-01")
    _insert_chunk(store, "c-sent", date="2026-03-01", labels=["SENT"])

    results = store.inbound_chunks_since(None)
    doc_ids = {r["doc_id"] for r in results}
    assert "a" in doc_ids
    assert "b" in doc_ids
    # SENT excluded
    assert "c-sent" not in doc_ids


def test_recent_inbound_chunks_excludes_sent(store):
    """SENT chunks are excluded even when newer than cursor."""
    _insert_chunk(store, "inbound-1", date="2026-06-01")
    _insert_chunk(store, "sent-1", date="2026-06-02", labels=["SENT"])

    results = store.inbound_chunks_since("2026-05-31")
    doc_ids = {r["doc_id"] for r in results}
    assert "inbound-1" in doc_ids
    assert "sent-1" not in doc_ids


# ---------------------------------------------------------------------------
# Sub-task 5.2 — run entry point tests
# ---------------------------------------------------------------------------

def test_waiting_on_run_clears_and_advances_cursor(store):
    """run(store) reconciles new chunks then advances waiting_on_cursor meta."""
    # Insert a chunk (inbound) with a date
    _insert_chunk(store, "chunk-taryn", date="2026-06-01", sender="Taryn Hamilton")
    # Add an open waiting action
    _add_action(store, waiting_on="Taryn Hamilton")
    now = datetime.now(timezone.utc).isoformat()

    result = run(store, now=now)

    assert result["cleared"] == 1
    # Cursor should now be set
    cursor = store.get_meta("waiting_on_cursor")
    assert cursor == "2026-06-01"


def test_waiting_on_run_advances_cursor_so_second_run_sees_no_new(store):
    """Second run with no new chunks after cursor advanced -> cleared=0."""
    _insert_chunk(store, "chunk-taryn", date="2026-06-01", sender="Taryn Hamilton")
    # Insert a new waiting action for the second run check
    _add_action(store, waiting_on="Taryn Hamilton")
    now = datetime.now(timezone.utc).isoformat()

    # First run: clears
    run(store, now=now)
    # Second run: cursor is at 2026-06-01, chunk date == cursor, not > cursor -> no new chunks
    result = run(store, now=now)
    assert result["cleared"] == 0


def test_waiting_on_run_returns_cleared_zero_no_matches(store):
    """run with no matching chunks returns {"cleared": 0}."""
    _insert_chunk(store, "chunk-joel", date="2026-06-01", sender="Joel Chelliah")
    _add_action(store, waiting_on="Taryn Hamilton")
    now = datetime.now(timezone.utc).isoformat()

    result = run(store, now=now)
    assert result == {"cleared": 0}


def test_apply_to_reconcile_round_trip(store):
    """End-to-end producer -> consumer: apply() flags an action waiting on a
    person, and the reconciler clears it when that person's chunk arrives."""
    ext = {
        "thread_id": "t-wait", "org": "Acme", "content_type": "request",
        "summary": "Sam asks Taryn to confirm the venue.", "contextual_summary": "",
        "entities": [{"name": "Taryn Hamilton", "type": "person",
                      "org": "Acme", "role": ""}],
        "topics": [], "reply_needed": True, "reply_reason": "",
        "resolved_action_ids": [], "updated_actions": [], "relations": [],
        "actions": [{"description": "Confirm the venue booking.",
                     "owner_name": "Sam Chen", "owner_fallback": "",
                     "due_date": "", "waiting_on": "Taryn Hamilton"}],
        "messages": [{"message_id": "wait-m1",
                      "sender": "Sam Chen <sam@example.org>",
                      "date": "2026-06-01", "labels": "INBOX", "subject": "Venue"}],
    }
    gw.apply(store, ext, doc_ids=["t-wait"])

    # The action is now awaiting Taryn. Her reply chunk arrives.
    _insert_chunk(store, "chunk-taryn", date="2026-06-02", sender="Taryn Hamilton")
    result = run(store, now="2026-06-03T00:00:00Z")

    assert result == {"cleared": 1}
    with store._connect() as db:
        row = db.execute(
            "SELECT waiting_on, reply_received FROM actions "
            "WHERE text='Confirm the venue booking.'").fetchone()
    assert row["waiting_on"] is None
    assert row["reply_received"] == 1
