"""Tests for mcpbrain.extractor_io — the shared pure extractor helpers."""
import json
import subprocess

import pytest

from mcpbrain import extractor_io


# ---------------------------------------------------------------------------
# claude_runner — builds expected argv, pipes prompt via stdin, returns stdout
# ---------------------------------------------------------------------------

def test_claude_runner_invokes_cli(monkeypatch):
    """claude_runner must shell to the claude CLI with the right flags and
    return raw stdout. Mirrors test_enrich_backfill.py::test_local_claude_runner_invokes_cli."""
    seen = {}

    class _Result:
        stdout = '{"result": "hello"}'
        returncode = 0

    def fake_run(cmd, *, input=None, capture_output=None, text=None,
                 timeout=None, check=None):
        seen["cmd"] = cmd
        seen["input"] = input
        seen["timeout"] = timeout
        return _Result()

    monkeypatch.setattr("mcpbrain.draft._find_claude", lambda: "/usr/bin/claude")
    monkeypatch.setattr(extractor_io.subprocess, "run", fake_run)

    out = extractor_io.claude_runner("MY PROMPT", model="sonnet", timeout=120)

    assert out == '{"result": "hello"}'
    assert seen["cmd"][0] == "/usr/bin/claude"
    assert "--print" in seen["cmd"]
    assert "--model" in seen["cmd"]
    assert "sonnet" in seen["cmd"]
    assert "--output-format" in seen["cmd"]
    assert "json" in seen["cmd"]
    assert seen["input"] == "MY PROMPT"
    assert seen["timeout"] == 120


def test_claude_runner_honours_explicit_claude_bin(monkeypatch):
    """When claude_bin is provided, it is used instead of _find_claude."""
    class _Result:
        stdout = "raw"
        returncode = 0

    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd[0])
        return _Result()

    monkeypatch.setattr(extractor_io.subprocess, "run", fake_run)
    extractor_io.claude_runner("P", claude_bin="/custom/claude")
    assert calls == ["/custom/claude"]


def test_claude_runner_raises_on_nonzero(monkeypatch):
    """Non-zero exit must propagate as CalledProcessError."""
    monkeypatch.setattr("mcpbrain.draft._find_claude", lambda: "/usr/bin/claude")

    def bad_run(cmd, **kw):
        raise subprocess.CalledProcessError(1, cmd, stderr="boom")

    monkeypatch.setattr(extractor_io.subprocess, "run", bad_run)
    with pytest.raises(subprocess.CalledProcessError):
        extractor_io.claude_runner("P")


# ---------------------------------------------------------------------------
# extract_answer — round-trip on {"result": "..."} envelope
# ---------------------------------------------------------------------------

def test_extract_answer_result_envelope():
    """Newer CLI envelope: {"type": "result", "result": "..."}."""
    payload = json.dumps({"type": "result", "result": "the answer"})
    assert extractor_io.extract_answer(payload) == "the answer"


def test_extract_answer_content_list_envelope():
    """Older CLI envelope: {"content": [{"type": "text", "text": "..."}]}."""
    payload = json.dumps({"content": [{"type": "text", "text": "hello"}]})
    assert extractor_io.extract_answer(payload) == "hello"


def test_extract_answer_passthrough_on_invalid_json():
    """Non-JSON stdout is returned verbatim."""
    assert extractor_io.extract_answer("not json") == "not json"


# ---------------------------------------------------------------------------
# parse_extractor_json — strips fences
# ---------------------------------------------------------------------------

def test_parse_extractor_json_strips_fences():
    """Fenced JSON must be parsed correctly."""
    fenced = '```json\n{"batch_id": "b1", "extractions": []}\n```'
    result = extractor_io.parse_extractor_json(fenced)
    assert result == {"batch_id": "b1", "extractions": []}


def test_parse_extractor_json_plain():
    """Plain JSON (no fences) is parsed directly."""
    plain = '{"batch_id": "b2", "extractions": []}'
    assert extractor_io.parse_extractor_json(plain) == {"batch_id": "b2", "extractions": []}


def test_parse_extractor_json_raises_on_garbage():
    """Completely unparseable input raises JSONDecodeError."""
    import json
    with pytest.raises(json.JSONDecodeError):
        extractor_io.parse_extractor_json("not json at all")
