"""Tests for Q2 schema-constrained extraction.

Covers:
- ENTITY_TYPES / RELATION_TYPES constants exist and have correct values
- sanitize_extraction drops off-schema entity types
- sanitize_extraction drops off-schema relation types
- sanitize_extraction keeps valid entities and relations
"""



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
        "org": "Northgate Trust",
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
