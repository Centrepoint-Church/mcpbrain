"""Capture-envelope validation: the contract between the MCP write tools
(which spool envelopes to capture_inbox/) and the daemon drain that applies them."""
from mcpbrain.contract import validate_capture


def _ingest(**over):
    d = {"kind": "ingest", "captured_at": "2026-06-04T12:00:00Z", "source": "desktop",
         "title": "T", "content": "C", "tags": "", "observation_type": "note", "org": ""}
    d.update(over)
    return d


def test_valid_ingest_passes():
    assert validate_capture(_ingest()) == []


def test_ingest_requires_title_and_content():
    assert any("title" in p for p in validate_capture(_ingest(title="")))
    assert any("content" in p for p in validate_capture(_ingest(content="")))


def test_ingest_observation_type_enum():
    assert validate_capture(_ingest(observation_type="memory")) == []
    assert any("observation_type" in p
               for p in validate_capture(_ingest(observation_type="vibes")))


def test_action_create_requires_text():
    d = {"kind": "action_create", "captured_at": "x", "source": "code", "text": ""}
    assert any("text" in p for p in validate_capture(d))
    d["text"] = "Do the thing"
    assert validate_capture(d) == []


def test_action_update_requires_int_id_and_status_enum():
    d = {"kind": "action_update", "captured_at": "x", "source": "desktop",
         "action_id": "42", "status": "done"}
    assert any("action_id" in p for p in validate_capture(d))
    d["action_id"] = 42
    assert validate_capture(d) == []
    d["status"] = "snoozed"
    assert any("status" in p for p in validate_capture(d))


def test_unknown_kind_rejected():
    assert any("kind" in p for p in validate_capture({"kind": "telepathy"}))
    assert any("kind" in p for p in validate_capture("not a dict"))


def test_action_id_must_be_positive():
    assert validate_capture({"kind": "action_update", "action_id": 0, "status": "done"})
    assert validate_capture({"kind": "action_update", "action_id": -1, "status": "done"})


def test_cross_kind_contamination_rejected():
    # action_id on an ingest envelope is a misrouted client bug
    problems = validate_capture({"kind": "ingest", "title": "T", "content": "C",
                                  "action_id": 5})
    assert any("action_id" in p for p in problems)


def test_decision_envelope_valid():
    assert validate_capture({"kind": "decision", "text": "Retire X", "rationale": "Y", "owner": "Josh"}) == []


def test_decision_requires_text():
    assert validate_capture({"kind": "decision", "rationale": "Y"})  # non-empty problems list


def test_continuity_envelope_valid():
    assert validate_capture({"kind": "continuity", "text": "Shipped the parity audit today"}) == []


def test_memory_envelope_valid():
    assert validate_capture({"kind": "memory", "slug": "cowork-traps",
                             "description": "Cowork gotchas", "body": "..."}) == []


def test_memory_requires_slug_and_body():
    probs = validate_capture({"kind": "memory", "description": "d"})
    assert any("slug" in p for p in probs) and any("body" in p for p in probs)
