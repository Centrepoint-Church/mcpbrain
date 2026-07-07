"""Tests for Q2 schema-constrained extraction + grounding check.

Covers:
- ENTITY_TYPES / RELATION_TYPES constants exist and have correct values
- sanitize_extraction drops off-schema entity types
- sanitize_extraction drops off-schema relation types
- sanitize_extraction keeps valid entities and relations
- _grounding_filter drops entities not found in source text
- _grounding_filter keeps entities that appear in source text
- _grounding_filter drops relations when either endpoint not in source
- _grounding_filter keeps relations when both endpoints in source
- grounding filter is a no-op when flag off (via drain integration)
"""

import json
import tempfile
from pathlib import Path

import pytest

from mcpbrain.contract import (
    ENTITY_TYPES,
    RELATION_TYPES,
    sanitize_extraction,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

def test_entity_types_has_expected_values():
    assert ENTITY_TYPES == frozenset({"person", "org", "project", "meeting", "event", "topic"})


def test_relation_types_has_expected_values():
    # The five relation types the model may emit (attended is calendar-derived,
    # collaborates_with was removed as a dead synonym of coordinates_with).
    expected = {"works_at", "reports_to", "manages", "coordinates_with", "mentioned_with"}
    assert RELATION_TYPES == frozenset(expected)


# ---------------------------------------------------------------------------
# sanitize_extraction — entity type filtering
# ---------------------------------------------------------------------------

def _base_extraction(**overrides):
    d = {
        "thread_id": "t1",
        "org": "Centrepoint",
        "content_type": "fyi",
        "summary": "A test thread.",
        "entities": [],
        "relations": [],
        "actions": [],
        "topics": [],
        "messages": [{"message_id": "m1", "date": "2026-01-01", "sender": "alice@example.com"}],
    }
    d.update(overrides)
    return d


def test_sanitize_drops_off_schema_entity_type():
    d = _base_extraction(entities=[
        {"name": "Alice", "type": "person"},
        {"name": "Robot", "type": "ai_model"},  # invalid type
        {"name": "Acme", "type": "org"},
    ])
    cleaned, dropped = sanitize_extraction(d)
    assert dropped == 1
    types = [e["type"] for e in cleaned["entities"]]
    assert "ai_model" not in types
    assert "person" in types
    assert "org" in types


def test_sanitize_keeps_all_valid_entity_types():
    d = _base_extraction(entities=[
        {"name": "Alice", "type": "person"},
        {"name": "Acme", "type": "org"},
        {"name": "Widget v2", "type": "project"},
    ])
    cleaned, dropped = sanitize_extraction(d)
    assert dropped == 0
    assert len(cleaned["entities"]) == 3


def test_sanitize_drops_off_schema_relation_type():
    d = _base_extraction(relations=[
        {"source_name": "Alice", "type": "works_at", "target_name": "Acme"},
        {"source_name": "Alice", "type": "knows", "target_name": "Bob"},  # invalid
    ])
    cleaned, dropped = sanitize_extraction(d)
    assert dropped == 1
    types = [r["type"] for r in cleaned["relations"]]
    assert "knows" not in types
    assert "works_at" in types


def test_sanitize_keeps_all_valid_relation_types():
    d = _base_extraction(relations=[
        {"source_name": "Alice", "type": "works_at", "target_name": "Acme"},
        {"source_name": "Alice", "type": "reports_to", "target_name": "Bob"},
        {"source_name": "Bob", "type": "manages", "target_name": "Alice"},
        {"source_name": "Alice", "type": "coordinates_with", "target_name": "Carol"},
        {"source_name": "Alice", "type": "mentioned_with", "target_name": "Dave"},
    ])
    cleaned, dropped = sanitize_extraction(d)
    assert dropped == 0
    assert len(cleaned["relations"]) == 5


def test_sanitize_drops_relation_with_empty_type():
    d = _base_extraction(relations=[
        {"source_name": "Alice", "type": "", "target_name": "Bob"},
    ])
    cleaned, dropped = sanitize_extraction(d)
    assert dropped == 1
    assert cleaned["relations"] == []


# ---------------------------------------------------------------------------
# _grounding_filter
# ---------------------------------------------------------------------------

from mcpbrain.drain import _grounding_filter


def _extraction_with_text(entities, relations=None, text="Alice works at Acme Corp."):
    return {
        "thread_id": "t1",
        "org": "Centrepoint",
        "content_type": "fyi",
        "summary": "Test.",
        "entities": entities,
        "relations": relations or [],
        "actions": [],
        "topics": [],
        "messages": [
            {"message_id": "m1", "date": "2026-01-01",
             "sender": "alice@example.com", "text": text},
        ],
    }


def test_grounding_filter_keeps_entity_in_source():
    d = _extraction_with_text([
        {"name": "Alice", "type": "person"},
        {"name": "Acme Corp", "type": "org"},
    ], text="Alice works at Acme Corp.")
    filtered, dropped = _grounding_filter(d)
    assert dropped == 0
    assert len(filtered["entities"]) == 2


def test_grounding_filter_drops_entity_not_in_source():
    d = _extraction_with_text([
        {"name": "Alice", "type": "person"},
        {"name": "Fabricated Entity", "type": "org"},  # not in source
    ], text="Alice is a great person.")
    filtered, dropped = _grounding_filter(d)
    assert dropped == 1
    names = [e["name"] for e in filtered["entities"]]
    assert "Fabricated Entity" not in names
    assert "Alice" in names


def test_grounding_filter_case_insensitive():
    d = _extraction_with_text([
        {"name": "ALICE", "type": "person"},
    ], text="alice works at acme.")
    filtered, dropped = _grounding_filter(d)
    assert dropped == 0


def test_grounding_filter_drops_relation_with_missing_target():
    d = _extraction_with_text(
        [{"name": "Alice", "type": "person"}],
        relations=[
            {"source_name": "Alice", "type": "works_at", "target_name": "Ghost Corp"},
        ],
        text="Alice is mentioned here."
    )
    filtered, dropped = _grounding_filter(d)
    assert dropped == 1
    assert filtered["relations"] == []


def test_grounding_filter_keeps_relation_when_both_endpoints_present():
    d = _extraction_with_text(
        [{"name": "Alice", "type": "person"}, {"name": "Acme", "type": "org"}],
        relations=[
            {"source_name": "Alice", "type": "works_at", "target_name": "Acme"},
        ],
        text="Alice works at Acme and is responsible for the project."
    )
    filtered, dropped = _grounding_filter(d)
    assert dropped == 0
    assert len(filtered["relations"]) == 1


def test_grounding_filter_no_op_when_no_message_text():
    """No message text → source is empty → filter is a no-op (conservative)."""
    d = {
        "thread_id": "t1",
        "org": "Centrepoint",
        "content_type": "fyi",
        "summary": "Test.",
        "entities": [{"name": "Alice", "type": "person"}],
        "relations": [],
        "actions": [],
        "topics": [],
        "messages": [{"message_id": "m1", "date": "2026-01-01", "sender": "alice@example.com"}],
    }
    filtered, dropped = _grounding_filter(d)
    assert dropped == 0
    assert len(filtered["entities"]) == 1


# ---------------------------------------------------------------------------
# Integration: drain applies grounding filter only when flag is on
# ---------------------------------------------------------------------------

def test_drain_grounding_flag_off_does_not_filter(tmp_path):
    """With schema_grounding: false, fabricated entities pass through to apply."""
    from mcpbrain.store import Store
    from mcpbrain.drain import drain as drain_fn
    from mcpbrain.graph_write import apply as gw_apply

    db_path = tmp_path / "brain.sqlite3"
    store = Store(db_path, dim=4); store.init()

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "schema_grounding": False,
        "owner_name": "Josh",
        "owner_email": "josh@example.com",
        "orgs": [{"name": "Centrepoint"}],
    }))

    # Write an extraction with a fabricated entity not in the message text
    inbox = tmp_path / "enrich_inbox"
    inbox.mkdir()
    batch = {
        "unit_id": "u1",
        "extractions": [{
            "thread_id": "t1",
            "org": "Centrepoint",
            "content_type": "fyi",
            "summary": "Test.",
            "entities": [{"name": "Fabricated Person", "type": "person"}],
            "relations": [],
            "actions": [],
            "topics": [],
            "messages": [{"message_id": "m1", "date": "2026-01-01",
                          "sender": "alice@example.com", "text": "Hello world."}],
        }],
    }
    store.upsert_chunk("d1", "Hello world.", "hash-d1",
                       {"thread_id": "t1", "message_id": "m1"})  # thread has chunks
    (inbox / "b1.json").write_text(json.dumps(batch))

    applied = []

    def capture_apply(s, extraction, **kw):
        applied.append(extraction)
        return {"entities": 0, "relations": 0}

    drain_fn(store, home=str(tmp_path), apply=capture_apply)
    assert applied, "apply should have been called"
    # Fabricated entity not removed — flag is off
    entities = applied[0].get("entities", [])
    assert any(e.get("name") == "Fabricated Person" for e in entities)


def test_drain_grounding_flag_on_removes_fabricated_entity(tmp_path):
    """With schema_grounding: true, entity not in source text is dropped."""
    from mcpbrain.store import Store
    from mcpbrain.drain import drain as drain_fn

    db_path = tmp_path / "brain.sqlite3"
    store = Store(db_path, dim=4); store.init()

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "schema_grounding": True,
        "owner_name": "Josh",
        "owner_email": "josh@example.com",
        "orgs": [{"name": "Centrepoint"}],
    }))

    inbox = tmp_path / "enrich_inbox"
    inbox.mkdir()
    batch = {
        "unit_id": "u1",
        "extractions": [{
            "thread_id": "t1",
            "org": "Centrepoint",
            "content_type": "fyi",
            "summary": "Test.",
            "entities": [
                {"name": "Alice", "type": "person"},       # in source
                {"name": "Fabricated Person", "type": "person"},  # not in source
            ],
            "relations": [],
            "actions": [],
            "topics": [],
            "messages": [{"message_id": "m1", "date": "2026-01-01",
                          "sender": "alice@example.com",
                          "text": "Alice sent a message today."}],
        }],
    }
    store.upsert_chunk("d1", "Alice sent a message today.", "hash-d1",
                       {"thread_id": "t1", "message_id": "m1"})  # thread has chunks
    (inbox / "b1.json").write_text(json.dumps(batch))

    applied = []

    def capture_apply(s, extraction, **kw):
        applied.append(extraction)
        return {"entities": 0, "relations": 0}

    drain_fn(store, home=str(tmp_path), apply=capture_apply)
    assert applied, "apply should have been called"
    entities = applied[0].get("entities", [])
    names = [e.get("name") for e in entities]
    assert "Fabricated Person" not in names, "fabricated entity must be filtered"
    assert "Alice" in names, "grounded entity must survive"


# --- Q2 grounding fix: token-overlap keeps normalised names, drops hallucinations ---

def test_grounding_keeps_normalised_name_via_token():
    """A correctly-extracted entity whose FULL name isn't a substring (it was
    normalised) is kept when a distinctive token appears in the source."""
    from mcpbrain.drain import _grounding_filter
    ext = {
        "messages": [{"text": "Spoke with Ps Joel today about the budget."}],
        "entities": [{"name": "Joel Chelliah", "type": "person"}],  # full name not in text
        "relations": [],
    }
    out, dropped = _grounding_filter(ext)
    assert dropped == 0
    assert out["entities"] == [{"name": "Joel Chelliah", "type": "person"}]


def test_grounding_drops_hallucinated_entity():
    """An entity with no lexical anchor in the source is dropped."""
    from mcpbrain.drain import _grounding_filter
    ext = {
        "messages": [{"text": "Spoke with Ps Joel today about the budget."}],
        "entities": [{"name": "Acme Corporation", "type": "org"}],  # nothing matches
        "relations": [],
    }
    out, dropped = _grounding_filter(ext)
    assert dropped == 1
    assert out["entities"] == []
