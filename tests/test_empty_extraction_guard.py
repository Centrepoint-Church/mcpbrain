"""Tests for Q8 stricter validate_extraction (no empty 'done').

An extraction with all lists empty AND blank summary is invalid.
drain skips it without calling mark_enriched → unit stays re-queueable.
"""

import json
from pathlib import Path

import pytest

from mcpbrain.contract import validate_extraction


# ---------------------------------------------------------------------------
# validate_extraction: empty guard
# ---------------------------------------------------------------------------

def _base():
    return {
        "thread_id": "t1",
        "org": "Centrepoint",
        "content_type": "fyi",
        "summary": "",
        "entities": [],
        "relations": [],
        "actions": [],
        "topics": [],
        "messages": [{"message_id": "m1", "date": "2026-01-01",
                      "sender": "alice@example.com"}],
    }


def test_all_empty_blank_summary_is_invalid():
    problems = validate_extraction(_base())
    assert any("no content" in p for p in problems), \
        f"expected 'no content' problem, got: {problems}"


def test_non_empty_entities_is_valid_despite_blank_summary():
    d = _base()
    d["entities"] = [{"name": "Alice", "type": "person"}]
    problems = validate_extraction(d)
    content_problems = [p for p in problems if "no content" in p]
    assert not content_problems, f"should not flag content problem: {content_problems}"


def test_non_blank_summary_is_valid_despite_empty_lists():
    d = _base()
    d["summary"] = "This thread discussed the project timeline."
    problems = validate_extraction(d)
    content_problems = [p for p in problems if "no content" in p]
    assert not content_problems, f"should not flag: {content_problems}"


def test_topics_only_prevents_empty_flag():
    d = _base()
    d["topics"] = ["budget"]
    problems = validate_extraction(d)
    content_problems = [p for p in problems if "no content" in p]
    assert not content_problems


def test_actions_only_prevents_empty_flag():
    d = _base()
    d["actions"] = [{"description": "Follow up with Alice"}]
    problems = validate_extraction(d)
    content_problems = [p for p in problems if "no content" in p]
    assert not content_problems


def test_whitespace_summary_counts_as_blank():
    d = _base()
    d["summary"] = "   "
    problems = validate_extraction(d)
    assert any("no content" in p for p in problems)


# ---------------------------------------------------------------------------
# Integration: drain does NOT mark_enriched on empty extraction
# ---------------------------------------------------------------------------

def test_drain_skips_mark_enriched_for_empty_extraction(tmp_path):
    """Empty extraction: drain skips it and does NOT call mark_enriched.

    The unit stays re-queueable (enriched=0 on those chunks).
    """
    from mcpbrain.store import Store
    from mcpbrain.drain import drain as drain_fn

    db_path = tmp_path / "brain.sqlite3"
    store = Store(db_path, dim=4); store.init()

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "owner_name": "Josh",
        "owner_email": "josh@example.com",
        "orgs": [{"name": "Centrepoint"}],
    }))

    # Add a chunk to the store so doc_ids_for_messages returns something
    store.upsert_chunk("doc1", "m1", "Hello world", {"message_id": "m1",
                        "source_type": "gmail", "thread_id": "t1"})

    inbox = tmp_path / "enrich_inbox"
    inbox.mkdir()
    batch = {
        "unit_id": "u1",
        "extractions": [{
            "thread_id": "t1",
            "org": "Centrepoint",
            "content_type": "fyi",
            "summary": "",        # blank
            "entities": [],
            "relations": [],
            "actions": [],
            "topics": [],
            "messages": [{"message_id": "m1", "date": "2026-01-01",
                          "sender": "alice@example.com"}],
        }],
    }
    (inbox / "b1.json").write_text(json.dumps(batch))

    apply_called = []

    def capture_apply(s, extraction, **kw):
        apply_called.append(extraction)
        return {"entities": 0, "relations": 0}

    result = drain_fn(store, home=str(tmp_path), apply=capture_apply)

    # apply() must NOT have been called — the extraction was skipped
    assert not apply_called, "apply should not be called for empty extraction"

    # The chunk must still be enriched=0 (not marked done)
    with store._connect() as conn:
        row = conn.execute("SELECT enriched FROM chunks WHERE doc_id='doc1'").fetchone()
    assert row is not None, "chunk doc1 must exist in store"
    assert row["enriched"] == 0, \
        f"chunk must not be marked enriched; got enriched={row['enriched']!r}"

    # drain should report the extraction as skipped
    assert result.get("skipped", 0) >= 1, f"expected skipped >= 1, got {result}"
