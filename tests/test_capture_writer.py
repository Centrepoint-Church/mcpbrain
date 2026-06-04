"""write_capture: atomic envelope writes into capture_inbox/. The MCP server
process calls this — it must never touch the database (single-writer invariant)."""
import json

from mcpbrain.capture import write_capture


def test_writes_valid_json_into_capture_inbox(tmp_path):
    env = {"kind": "ingest", "title": "T", "content": "C",
           "captured_at": "2026-06-04T12:00:00Z", "source": "code"}
    path = write_capture(str(tmp_path), env)
    assert path.parent == tmp_path / "capture_inbox"
    assert path.name.startswith("cap-") and path.suffix == ".json"
    assert json.loads(path.read_text())["title"] == "T"


def test_two_writes_get_distinct_filenames(tmp_path):
    env = {"kind": "ingest", "title": "T", "content": "C"}
    p1 = write_capture(str(tmp_path), env)
    p2 = write_capture(str(tmp_path), env)
    assert p1 != p2


def test_invalid_envelope_raises_value_error(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        write_capture(str(tmp_path), {"kind": "telepathy"})
    assert not (tmp_path / "capture_inbox").exists() or \
        not list((tmp_path / "capture_inbox").glob("*.json"))
