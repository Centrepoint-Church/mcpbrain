"""Phase 3 Task 3 — synthesis file contract tests.

Covers:
  3.1  build_synthesis_requests shape and omission rules
  3.2  attach_synthesis_block (prepare-side)
  3.3  drain_synthesis (drain-side) + round-trip acceptance test
"""


from mcpbrain.store import Store
from mcpbrain.synthesise_threads import (
    attach_synthesis_block,
    build_synthesis_requests,
    drain_synthesis,
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

def _store(tmp_path, name="synth.db"):
    s = Store(tmp_path / name, dim=4)
    s.init()
    return s


def _add_email_context(store, message_id, thread_id, summary="", date_iso="2026-01-01",
                       content_type="update"):
    with store._connect() as db:
        db.execute(
            "INSERT OR REPLACE INTO email_context"
            "(message_id, thread_id, summary, date_iso, content_type) "
            "VALUES (?,?,?,?,?)",
            (message_id, thread_id, summary, date_iso, content_type),
        )


def _add_thread_context(store, thread_id, email_count=10, summary="", subject="Test",
                        contextual_summary=""):
    with store._connect() as db:
        db.execute(
            "INSERT OR REPLACE INTO thread_context"
            "(thread_id, email_count, summary, subject, contextual_summary) "
            "VALUES (?,?,?,?,?)",
            (thread_id, email_count, summary, subject, contextual_summary),
        )


# ---------------------------------------------------------------------------
# Sub-task 3.1 — build_synthesis_requests shape
# ---------------------------------------------------------------------------

def test_synthesis_block_shape(tmp_path):
    """build_synthesis_requests returns correct item shape for a thread with summaries."""
    store = _store(tmp_path)

    # Thread with two messages that have summaries.
    _add_thread_context(store, "t-1", email_count=10, subject="Budget discussion")
    _add_email_context(store, "m-1a", "t-1", summary="Intro", date_iso="2026-01-01",
                       content_type="request")
    _add_email_context(store, "m-1b", "t-1", summary="Reply with figures",
                       date_iso="2026-01-02", content_type="update")

    requests = build_synthesis_requests(store, min_emails=5)

    assert len(requests) == 1
    item = requests[0]
    assert item["thread_id"] == "t-1"
    assert item["subject"] == "Budget discussion"
    assert item["email_count"] == 10
    assert item["first_date"] == "2026-01-01"
    assert item["last_date"] == "2026-01-02"
    # email_summaries is chronological "- date [ctype]: summary" lines
    lines = item["email_summaries"].splitlines()
    assert len(lines) == 2
    assert lines[0] == "- 2026-01-01 [request]: Intro"
    assert lines[1] == "- 2026-01-02 [update]: Reply with figures"


def test_synthesis_block_skips_threads_without_summaries(tmp_path):
    """Threads whose email_context rows all have empty summary are omitted."""
    store = _store(tmp_path)

    _add_thread_context(store, "t-empty", email_count=10)
    _add_email_context(store, "m-e1", "t-empty", summary="", date_iso="2026-01-01")
    _add_email_context(store, "m-e2", "t-empty", summary="", date_iso="2026-01-02")

    requests = build_synthesis_requests(store, min_emails=5)
    thread_ids = {r["thread_id"] for r in requests}
    assert "t-empty" not in thread_ids


def test_synthesis_block_respects_min_emails(tmp_path):
    """Threads below min_emails are excluded by threads_needing_summary."""
    store = _store(tmp_path)

    # Below threshold.
    _add_thread_context(store, "t-small", email_count=3, subject="Small")
    _add_email_context(store, "m-s1", "t-small", summary="A summary")

    # Above threshold.
    _add_thread_context(store, "t-big", email_count=10, subject="Big")
    _add_email_context(store, "m-b1", "t-big", summary="A summary")

    requests = build_synthesis_requests(store, min_emails=5)
    thread_ids = {r["thread_id"] for r in requests}
    assert "t-small" not in thread_ids
    assert "t-big" in thread_ids


def test_synthesis_block_skips_already_synthesised(tmp_path):
    """Threads that already have a deep contextual_summary are excluded.

    A headline `summary` (which apply() always writes) does NOT exclude a thread —
    the synthesis pass's job is the deeper contextual_summary, so only that
    excludes.
    """
    store = _store(tmp_path)

    _add_thread_context(store, "t-done", email_count=10, summary="Headline",
                        contextual_summary="Already synthesised in depth")
    _add_email_context(store, "m-d1", "t-done", summary="Message summary")

    requests = build_synthesis_requests(store, min_emails=5)
    thread_ids = {r["thread_id"] for r in requests}
    assert "t-done" not in thread_ids


def test_synthesis_block_partial_summaries(tmp_path):
    """Messages without a summary are omitted from email_summaries; thread is kept."""
    store = _store(tmp_path)

    _add_thread_context(store, "t-partial", email_count=8, subject="Partial")
    _add_email_context(store, "m-p1", "t-partial", summary="Has summary",
                       date_iso="2026-02-01", content_type="update")
    _add_email_context(store, "m-p2", "t-partial", summary="",
                       date_iso="2026-02-02", content_type="fyi")

    requests = build_synthesis_requests(store, min_emails=5)
    assert len(requests) == 1
    lines = requests[0]["email_summaries"].splitlines()
    assert len(lines) == 1  # Only the message with a summary.
    assert "Has summary" in lines[0]


# ---------------------------------------------------------------------------
# Sub-task 3.2 — attach_synthesis_block (prepare-side)
# ---------------------------------------------------------------------------

def test_attach_synthesis_block_adds_key():
    """attach_synthesis_block adds the synthesis key; existing keys are untouched."""
    requests = [{"thread_id": "t-1", "subject": "Test"}]
    pending = {"threads": [], "merge_review": []}

    result = attach_synthesis_block(pending, requests)

    assert "synthesis" in result
    assert result["synthesis"] == requests
    assert result["threads"] == []
    assert result["merge_review"] == []


def test_attach_synthesis_block_empty_noop():
    """Empty requests list -> no synthesis key added."""
    pending = {"threads": [], "merge_review": []}

    result = attach_synthesis_block(pending, [])

    assert "synthesis" not in result
    assert result is pending or result == pending


def test_attach_synthesis_block_does_not_mutate_input():
    """attach_synthesis_block returns a new dict; input is unchanged."""
    pending = {"threads": [], "merge_review": []}
    requests = [{"thread_id": "t-x"}]

    result = attach_synthesis_block(pending, requests)

    assert result is not pending
    assert "synthesis" not in pending


# ---------------------------------------------------------------------------
# Sub-task 3.3 — drain_synthesis (drain-side)
# ---------------------------------------------------------------------------

def test_drain_synthesis_writes_thread_context(tmp_path):
    """drain_synthesis writes the narrative into thread_context.contextual_summary.

    The answer carries it under the back-compat `summary` key here; drain routes
    it to contextual_summary (the deep narrative apply() does not produce).
    """
    store = _store(tmp_path)
    _add_thread_context(store, "t-1", email_count=10)

    inbox_obj = {"synthesis": [{"thread_id": "t-1", "summary": "Full narrative text"}]}
    result = drain_synthesis(store, inbox_obj)

    assert result == {"thread_context_written": 1}

    with store._connect() as db:
        row = db.execute(
            "SELECT contextual_summary FROM thread_context WHERE thread_id='t-1'"
        ).fetchone()
    assert row is not None
    assert row["contextual_summary"] == "Full narrative text"


def test_drain_synthesis_skips_empty_summary(tmp_path):
    """Items with no narrative text are not written."""
    store = _store(tmp_path)
    _add_thread_context(store, "t-2", email_count=10)

    inbox_obj = {"synthesis": [{"thread_id": "t-2", "contextual_summary": ""}]}
    result = drain_synthesis(store, inbox_obj)

    assert result == {"thread_context_written": 0}

    with store._connect() as db:
        row = db.execute(
            "SELECT contextual_summary FROM thread_context WHERE thread_id='t-2'"
        ).fetchone()
    # Row exists but contextual_summary is still empty (not written).
    assert row is not None
    assert not row["contextual_summary"]


def test_drain_synthesis_skips_missing_thread_id(tmp_path):
    """Items without thread_id are skipped without crashing."""
    store = _store(tmp_path)

    inbox_obj = {"synthesis": [{"summary": "Orphan summary"}]}
    result = drain_synthesis(store, inbox_obj)

    assert result == {"thread_context_written": 0}


def test_drain_synthesis_ignores_unknown_thread(tmp_path):
    """thread_id not in store is handled via upsert (INSERT OR equivalent); no crash."""
    store = _store(tmp_path)
    # Do NOT pre-insert t-unknown.

    inbox_obj = {"synthesis": [{"thread_id": "t-unknown", "contextual_summary": "New narrative"}]}
    result = drain_synthesis(store, inbox_obj)

    assert result == {"thread_context_written": 1}

    with store._connect() as db:
        row = db.execute(
            "SELECT contextual_summary FROM thread_context WHERE thread_id='t-unknown'"
        ).fetchone()
    assert row is not None
    assert row["contextual_summary"] == "New narrative"


def test_drain_synthesis_empty_inbox(tmp_path):
    """No synthesis key in inbox -> returns zero written."""
    store = _store(tmp_path)
    result = drain_synthesis(store, {})
    assert result == {"thread_context_written": 0}


# ---------------------------------------------------------------------------
# Acceptance test: round-trip build -> stub filler -> drain
# ---------------------------------------------------------------------------

def test_synthesis_round_trip(tmp_path):
    """Build requests -> stub filler -> drain -> thread_context has the summaries."""
    store = _store(tmp_path)

    # Set up two threads needing synthesis.
    _add_thread_context(store, "rt-1", email_count=10, subject="Thread One")
    _add_email_context(store, "rt-1a", "rt-1", summary="First message summary",
                       date_iso="2026-03-01", content_type="request")
    _add_email_context(store, "rt-1b", "rt-1", summary="Second message summary",
                       date_iso="2026-03-02", content_type="update")

    _add_thread_context(store, "rt-2", email_count=12, subject="Thread Two")
    _add_email_context(store, "rt-2a", "rt-2", summary="Only message with summary",
                       date_iso="2026-03-05", content_type="fyi")

    # Build synthesis requests.
    requests = build_synthesis_requests(store, min_emails=5)
    assert len(requests) == 2

    # Stub filler: maps each request to a deterministic narrative.
    filled = [
        {"thread_id": r["thread_id"], "contextual_summary": f"Narrative for {r['thread_id']}"}
        for r in requests
    ]

    # Drain the filled responses.
    inbox_obj = {"synthesis": filled}
    result = drain_synthesis(store, inbox_obj)
    assert result["thread_context_written"] == 2

    # Verify thread_context holds the synthesised narratives.
    with store._connect() as db:
        rows = {
            r["thread_id"]: r["contextual_summary"]
            for r in db.execute(
                "SELECT thread_id, contextual_summary FROM thread_context").fetchall()
        }

    assert rows["rt-1"] == "Narrative for rt-1"
    assert rows["rt-2"] == "Narrative for rt-2"
