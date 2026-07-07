"""Tests for the shared extraction-contract validator and golden fixtures.

The contract is the single seam both halves of the enrich loop import: the
prepare/extractor side produces it, the drain/apply side consumes it. These
tests pin the envelope shape so the two cannot drift.
"""

import json
from pathlib import Path


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "extractions"
EXTRACTION_FIXTURES = ["thread_simple", "thread_multi_message", "thread_self"]


def _load(name):
    return json.loads((FIXTURE_DIR / f"{name}.json").read_text())


# --- 1.1: fixtures are well-formed (no validator code yet) ----------------

def test_fixtures_are_self_consistent():
    for name in EXTRACTION_FIXTURES:
        d = _load(name)
        assert d["thread_id"], f"{name}: thread_id must be non-empty"
        assert d["messages"], f"{name}: messages must be non-empty"
        for action in d["actions"]:
            assert action.get("description"), f"{name}: every action needs a description"
        for relation in d["relations"]:
            assert relation.get("source_name"), f"{name}: relation needs source_name"
            assert relation.get("type"), f"{name}: relation needs type"
            assert relation.get("target_name"), f"{name}: relation needs target_name"


# --- 1.2: the validator ---------------------------------------------------

def test_validate_extraction_accepts_fixtures():
    from mcpbrain.contract import validate_extraction
    for name in EXTRACTION_FIXTURES:
        problems = validate_extraction(_load(name))
        assert problems == [], f"{name}: expected valid, got {problems}"


def test_validate_rejects_missing_thread_id():
    from mcpbrain.contract import validate_extraction
    d = _load("thread_simple")
    del d["thread_id"]
    problems = validate_extraction(d)
    assert any("thread_id" in p for p in problems)


def test_validate_rejects_non_list_messages():
    from mcpbrain.contract import validate_extraction
    d = _load("thread_simple")
    d["messages"] = "not a list"
    problems = validate_extraction(d)
    assert any("messages" in p for p in problems)


def test_validate_rejects_non_string_org():
    from mcpbrain.contract import validate_extraction
    d = _load("thread_simple")
    d["org"] = None
    problems = validate_extraction(d)
    assert any("org" in p for p in problems)


def test_validate_accepts_unconfigured_org_string():
    # Enum membership is no longer structural: an unconfigured org is
    # recoverable drift, coerced by normalise_org in drain (with a proactive
    # finding) rather than quarantining the whole extraction.
    from mcpbrain.contract import validate_extraction
    d = _load("thread_simple")
    d["org"] = "WORSHIP"
    assert validate_extraction(d) == []


def test_validate_rejects_action_without_description():
    from mcpbrain.contract import validate_extraction
    d = _load("thread_simple")
    d["actions"] = [{"owner_name": "Sam Chen", "description": ""}]
    problems = validate_extraction(d)
    assert any("description" in p for p in problems)


def test_validate_allows_empty_optionals():
    from mcpbrain.contract import validate_extraction
    d = _load("thread_self")
    d["contextual_summary"] = ""
    d["topics"] = []
    d["resolved_action_ids"] = []
    d["updated_actions"] = []
    problems = validate_extraction(d)
    assert problems == [], f"expected empty optionals to be valid, got {problems}"


def test_validate_does_not_mutate_input():
    from mcpbrain.contract import validate_extraction
    d = _load("thread_simple")
    before = json.dumps(d, sort_keys=True)
    validate_extraction(d)
    assert json.dumps(d, sort_keys=True) == before


def test_validate_rejects_non_int_resolved_action_ids():
    from mcpbrain.contract import validate_extraction
    d = _load("thread_multi_message")
    d["resolved_action_ids"] = ["not-an-int"]
    problems = validate_extraction(d)
    assert any("resolved_action_ids" in p for p in problems)


def test_validate_rejects_relation_without_endpoints():
    from mcpbrain.contract import validate_extraction
    d = _load("thread_simple")
    d["relations"] = [{"source_name": "Joel Chelliah", "type": "works_at"}]
    problems = validate_extraction(d)
    assert any("target_name" in p for p in problems)


# --- 1.3: the inbox batch-file wrapper ------------------------------------

def _batch(extractions, merge_answers=None):
    return {
        "batch_id": "batch-20260602-093000",
        "extractions": extractions,
        "merge_answers": merge_answers or [],
    }


def test_validate_batch_file():
    from mcpbrain.contract import validate_batch_file
    batch = _batch([_load(n) for n in EXTRACTION_FIXTURES],
                   merge_answers=[{"pair_id": "a|b", "same": True, "canonical": "Joel Chelliah"}])
    assert validate_batch_file(batch) == []


def test_validate_batch_file_reports_failing_index():
    from mcpbrain.contract import validate_batch_file
    good = _load("thread_simple")
    bad = _load("thread_multi_message")
    bad["thread_id"] = ""
    problems = validate_batch_file(_batch([good, bad]))
    assert problems, "expected the bad extraction to be reported"
    assert any("1" in p for p in problems), f"expected index 1 to be named, got {problems}"


def test_validate_batch_file_rejects_missing_batch_id():
    from mcpbrain.contract import validate_batch_file
    batch = _batch([_load("thread_simple")])
    del batch["batch_id"]
    problems = validate_batch_file(batch)
    assert any("batch_id" in p for p in problems)


def test_validate_batch_file_rejects_non_list_extractions():
    from mcpbrain.contract import validate_batch_file
    batch = _batch([])
    batch["extractions"] = "nope"
    problems = validate_batch_file(batch)
    assert any("extractions" in p for p in problems)


def test_validate_rejects_merge_answer_string_same():
    # A merge collapses two entities irreversibly. The model emitting "same" as a
    # string ("false") must NOT slip through truthiness and trigger a merge — the
    # whole file is rejected (quarantined) rather than mis-merged.
    from mcpbrain.contract import validate_batch_file
    batch = _batch([], merge_answers=[{"pair_id": "a|b", "same": "false"}])
    problems = validate_batch_file(batch)
    assert any("same" in p for p in problems)


def test_validate_rejects_merge_answer_missing_pair_id():
    from mcpbrain.contract import validate_batch_file
    batch = _batch([], merge_answers=[{"same": True}])
    problems = validate_batch_file(batch)
    assert any("pair_id" in p for p in problems)


def test_validate_accepts_well_formed_merge_answers():
    from mcpbrain.contract import validate_batch_file
    batch = _batch([], merge_answers=[
        {"pair_id": "a|b", "same": True, "canonical": "Joel Chelliah"},
        {"pair_id": "c|d", "same": False},
    ])
    assert validate_batch_file(batch) == []


# --- importability: constants must be reachable from their canonical homes ---

def test_valid_content_types_importable_from_contract():
    from mcpbrain.contract import _VALID_CONTENT_TYPES
    assert "request" in _VALID_CONTENT_TYPES
    assert "decision" in _VALID_CONTENT_TYPES


def test_valid_types_importable_from_chunking():
    from mcpbrain.chunking import _VALID_TYPES, _is_junk_entity
    assert "person" in _VALID_TYPES
    assert _is_junk_entity("Re: subject", "person") is True


def test_parse_first_json_object_importable_from_chunking():
    from mcpbrain.chunking import _parse_first_json_object
    assert _parse_first_json_object('{"a": 1}') == {"a": 1}


def test_messages_not_required():
    from mcpbrain.contract import validate_extraction
    ext = {"thread_id": "t1", "org": "unknown", "content_type": "update",
           "summary": "s", "entities": [{"name": "Sam", "type": "person"}],
           "relations": [], "actions": [], "topics": ["x"]}
    # messages intentionally absent — the daemon supplies them.
    assert validate_extraction(ext) == []


# --- 1.4: extended entity and relation types (Task 4.1) --------------------

def test_entity_types_include_meeting():
    from mcpbrain.contract import ENTITY_TYPES
    assert "meeting" in ENTITY_TYPES


def test_entity_types_include_event():
    from mcpbrain.contract import ENTITY_TYPES
    assert "event" in ENTITY_TYPES


def test_entity_types_include_topic():
    from mcpbrain.contract import ENTITY_TYPES
    assert "topic" in ENTITY_TYPES


def test_relation_types_is_the_five_model_types():
    """The model may emit exactly the five types the enrich prompt documents.
    `attended` is calendar-derived (never model-emitted); `collaborates_with` was
    a dead synonym of coordinates_with — both are excluded from the model set."""
    from mcpbrain.contract import RELATION_TYPES
    assert RELATION_TYPES == frozenset({
        "works_at", "reports_to", "manages", "coordinates_with", "mentioned_with"})
    assert "collaborates_with" not in RELATION_TYPES
    assert "attended" not in RELATION_TYPES


def test_relation_vocab_in_sync():
    """The two model-relation allowlists must stay identical, and match the five
    types the enrich prompt tells the model to emit — a guard against future drift
    like the collaborates_with/attended superset this replaced."""
    from pathlib import Path
    from mcpbrain.contract import RELATION_TYPES
    from mcpbrain.graph_write import VALID_RELATION_TYPES
    five = {"works_at", "reports_to", "manages", "coordinates_with", "mentioned_with"}
    assert set(RELATION_TYPES) == set(VALID_RELATION_TYPES) == five
    prompt = (Path(__file__).resolve().parents[1] / "mcpbrain" / "enrich_prompt.md").read_text()
    for t in five:
        assert f"`{t}`" in prompt, f"{t} missing from enrich prompt"
    # the removed synonym must not reappear as a documented model relation
    assert "`collaborates_with`" not in prompt


# --- Task 7: meeting series_name/occurrence_date entity fields --------------

def test_meeting_series_fields_pass_validation():
    from mcpbrain.contract import validate_extraction
    ext = {"thread_id": "t1", "org": "Acme", "content_type": "update",
           "summary": "s", "entities": [
               {"name": "Board 12 May", "type": "meeting",
                "series_name": "Board", "occurrence_date": "2026-05-12"}],
           "topics": [], "actions": [], "relations": []}
    assert validate_extraction(ext) == []


def test_meeting_series_fields_survive_sanitize():
    from mcpbrain.contract import sanitize_extraction
    ext = {"thread_id": "t1", "org": "Acme", "content_type": "update",
           "summary": "s", "entities": [
               {"name": "Board 12 May", "type": "meeting",
                "series_name": "Board", "occurrence_date": "2026-05-12"}],
           "topics": [], "actions": [], "relations": []}
    cleaned, dropped = sanitize_extraction(ext)
    assert dropped == 0
    ent = cleaned["entities"][0]
    assert ent["series_name"] == "Board"
    assert ent["occurrence_date"] == "2026-05-12"
