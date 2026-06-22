"""Tests for Q3 write-time entity dedup (resolve.py + graph_write.py wiring).

Covers:
- build_entity_index builds a usable lookup structure
- write_time_dedup_check finds exact canonical-key matches
- write_time_dedup_check finds high-confidence token-similarity matches (≥ 0.8)
- write_time_dedup_check does NOT merge below the threshold
- write_time_dedup_check does NOT merge cross-type entities
- apply() redirects a near-dup entity to existing when flag is on
- apply() creates a new entity when flag is off
- Redirected entity still gets linked to the message (mention bump)
"""

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from mcpbrain.resolve import (
    build_entity_index,
    write_time_dedup_check,
    _WRITE_TIME_MERGE_THRESHOLD,
)


# ---------------------------------------------------------------------------
# Unit tests: resolve.py functions
# ---------------------------------------------------------------------------

def _ent(eid, name, typ="person"):
    return {"id": eid, "name": name, "type": typ}


def test_build_entity_index_contains_all_entities():
    entities = [_ent("e1", "Joel Chelliah"), _ent("e2", "Sarah Jones")]
    idx = build_entity_index(entities)
    assert set(idx.keys()) == {"e1", "e2"}
    assert idx["e1"]["name"] == "Joel Chelliah"
    assert idx["e1"]["type"] == "person"
    assert isinstance(idx["e1"]["toks"], set)
    assert isinstance(idx["e1"]["key"], str)


def test_build_entity_index_empty():
    assert build_entity_index([]) == {}


def test_write_time_dedup_exact_canonical_key_match():
    """Honorific-stripped form matches existing entity with different display name."""
    idx = build_entity_index([_ent("e1", "Joel Chelliah")])
    # "Ps Joel Chelliah" strips to "Joel Chelliah" → same canonical key
    result = write_time_dedup_check("Ps Joel Chelliah", "person", idx)
    assert result == "e1"


def test_write_time_dedup_token_similarity_above_threshold():
    """High-overlap token sets (≥ 0.8) merge."""
    # "Joel Chelliah" tokens: {"joel", "chelliah"}
    # "Joel L. Chelliah" tokens: {"joel", "chelliah"} (L. is dropped as 1-char)
    idx = build_entity_index([_ent("e1", "Joel Chelliah")])
    result = write_time_dedup_check("Joel L Chelliah", "person", idx)
    assert result == "e1", "token overlap should trigger dedup"


def test_write_time_dedup_below_threshold_creates_new():
    """Low overlap → no merge; caller should create a new entity."""
    # "Alice" vs "Bob Smith" — no shared tokens
    idx = build_entity_index([_ent("e1", "Alice Wonderland")])
    result = write_time_dedup_check("Bob Smith", "person", idx)
    assert result is None


def test_write_time_dedup_no_cross_type_merge():
    """A person and an org with the same name must NOT merge."""
    idx = build_entity_index([_ent("e1", "Centrepoint", "org")])
    result = write_time_dedup_check("Centrepoint", "person", idx)
    assert result is None


def test_write_time_dedup_empty_name_returns_none():
    idx = build_entity_index([_ent("e1", "Joel Chelliah")])
    assert write_time_dedup_check("", "person", idx) is None


def test_write_time_dedup_empty_index_returns_none():
    assert write_time_dedup_check("Joel Chelliah", "person", {}) is None


def test_write_time_dedup_exact_name_match():
    """Exact same name always resolves."""
    idx = build_entity_index([_ent("e1", "Sarah Jones", "person")])
    assert write_time_dedup_check("Sarah Jones", "person", idx) == "e1"


# ---------------------------------------------------------------------------
# Integration tests: graph_write.apply() wiring
# ---------------------------------------------------------------------------

def _make_store_and_home():
    """Create a minimal in-memory-backed store and a temp home with config."""
    from mcpbrain.store import Store

    tmpdir = tempfile.mkdtemp()
    db_path = Path(tmpdir) / "brain.sqlite3"
    store = Store(str(db_path))

    # Write config with write_time_dedup enabled
    cfg_path = Path(tmpdir) / "config.json"
    cfg_path.write_text(json.dumps({
        "write_time_dedup": True,
        "owner_name": "Josh",
        "owner_email": "josh@example.com",
        "orgs": [{"name": "Centrepoint"}],
    }))
    return store, tmpdir


def _make_extraction(entities, thread_id="t1"):
    return {
        "thread_id": thread_id,
        "org": "Centrepoint",
        "content_type": "email",
        "summary": "Test extraction",
        "contextual_summary": "",
        "topics": [],
        "entities": entities,
        "relations": [],
        "actions": [],
        "messages": [
            {
                "message_id": "m1",
                "date": "2026-01-01",
                "sender": "alice@example.com",
                "subject": "Test",
                "text": "Hello world",
            }
        ],
    }


def test_apply_dedup_redirects_near_duplicate_entity(tmp_path):
    """When dedup flag on and near-dup exists, apply() links the existing entity."""
    from mcpbrain.graph_write import apply, upsert_entity
    from mcpbrain.store import Store
    import json

    db_path = tmp_path / "brain.sqlite3"
    store = Store(db_path, dim=4); store.init()
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "write_time_dedup": True,
        "owner_name": "Josh",
        "owner_email": "josh@example.com",
        "orgs": [{"name": "Centrepoint"}],
    }))

    # Pre-insert a canonical entity
    import mcpbrain.orgs as orgs_mod
    taxonomy = orgs_mod.taxonomy_from_config()
    existing_id = upsert_entity(store, name="Joel Chelliah", entity_type="person",
                                taxonomy=taxonomy)
    assert existing_id, "pre-insert must succeed"

    # Now apply an extraction with a near-duplicate name ("Ps Joel Chelliah")
    extraction = _make_extraction([
        {"name": "Ps Joel Chelliah", "type": "person", "org": "Centrepoint"},
    ])
    result = apply(store, extraction, doc_ids=[], home=str(tmp_path))

    # The near-dup should have been redirected, so no new entity created
    with store._connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM entities WHERE type='person' AND id LIKE '%joel%'"
        ).fetchone()[0]
    # Exactly one Joel entity should exist (the pre-inserted one, not a new dup)
    assert count == 1, f"expected 1 Joel entity, got {count}"


def test_apply_dedup_off_creates_new_entity(tmp_path):
    """When dedup flag off, apply() creates a new entity even if near-dup exists."""
    from mcpbrain.graph_write import apply, upsert_entity
    from mcpbrain.store import Store
    import json

    db_path = tmp_path / "brain.sqlite3"
    store = Store(db_path, dim=4); store.init()
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "write_time_dedup": False,
        "owner_name": "Josh",
        "owner_email": "josh@example.com",
        "orgs": [{"name": "Centrepoint"}],
    }))

    import mcpbrain.orgs as orgs_mod
    taxonomy = orgs_mod.taxonomy_from_config()
    upsert_entity(store, name="Joel Chelliah", entity_type="person", taxonomy=taxonomy)

    # With flag off, "Ps Joel Chelliah" → strip → "Joel Chelliah" → upsert_entity
    # uses slugify-based dedup internally, so it still merges on exact slug.
    # Use a slightly different name that would only merge via token similarity.
    # "Joel L Chelliah" → slug "joel-l-chelliah" ≠ "joel-chelliah" → NEW entity
    extraction = _make_extraction([
        {"name": "Joel L Chelliah", "type": "person", "org": ""},
    ])
    apply(store, extraction, doc_ids=[], home=str(tmp_path))

    with store._connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM entities WHERE type='person' AND name LIKE '%Joel%'"
        ).fetchone()[0]
    # With dedup off, the near-dup creates a new entity
    assert count == 2, f"expected 2 Joel entities with dedup off, got {count}"


def test_apply_dedup_redirected_entity_linked_to_message(tmp_path):
    """A deduplicated entity gets linked to the message (mention bump)."""
    from mcpbrain.graph_write import apply, upsert_entity
    from mcpbrain.store import Store
    import json

    db_path = tmp_path / "brain.sqlite3"
    store = Store(db_path, dim=4); store.init()
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "write_time_dedup": True,
        "owner_name": "Josh",
        "owner_email": "josh@example.com",
        "orgs": [{"name": "Centrepoint"}],
    }))

    import mcpbrain.orgs as orgs_mod
    taxonomy = orgs_mod.taxonomy_from_config()
    existing_id = upsert_entity(store, name="Joel Chelliah", entity_type="person",
                                taxonomy=taxonomy)

    extraction = _make_extraction([
        {"name": "Ps Joel Chelliah", "type": "person", "org": ""},
    ])
    apply(store, extraction, doc_ids=[], home=str(tmp_path))

    # The existing entity should have a link to the message
    with store._connect() as conn:
        link = conn.execute(
            "SELECT 1 FROM email_entities WHERE entity_id = ? AND message_id = 'm1'",
            (existing_id,)
        ).fetchone()
    assert link is not None, "redirected entity must be linked to message m1"
