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
