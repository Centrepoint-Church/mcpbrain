"""The three MCP write tools spool envelopes; they never touch the store."""
import asyncio
import json
import pathlib

from mcpbrain.mcp_server import (
    make_brain_ingest, make_brain_action_create, make_brain_action_update,
    make_brain_decision, make_brain_note, make_brain_memory_write,
    _capture_envelope,
)


def _run(coro):
    return asyncio.run(coro)


def test_brain_ingest_spools_envelope(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    out = _run(make_brain_ingest()(title="T", content="C", tags="x,y",
                                   observation_type="memory", org="Acme"))
    assert out["queued"] is True
    files = list((tmp_path / "capture_inbox").glob("cap-*.json"))
    assert len(files) == 1
    env = json.loads(files[0].read_text())
    assert env["kind"] == "ingest" and env["observation_type"] == "memory"


def test_brain_action_create_spools(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    out = _run(make_brain_action_create()(text="Do thing", deadline="2026-07-01"))
    assert out["queued"] is True
    env = json.loads(next((tmp_path / "capture_inbox").glob("cap-*.json")).read_text())
    assert env["kind"] == "action_create" and env["deadline"] == "2026-07-01"


def test_brain_action_update_spools(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    out = _run(make_brain_action_update()(action_id=7, status="done"))
    assert out["queued"] is True
    env = json.loads(next((tmp_path / "capture_inbox").glob("cap-*.json")).read_text())
    assert env["action_id"] == 7


def test_invalid_input_returns_error_not_spool(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    out = _run(make_brain_ingest()(title="", content="C"))
    assert out["queued"] is False and "title" in out["error"]
    assert not list((tmp_path / "capture_inbox").glob("cap-*.json")) \
        if (tmp_path / "capture_inbox").exists() else True


# ---------------------------------------------------------------------------
# brain_decision
# ---------------------------------------------------------------------------

def test_decision_tool_writes_valid_envelope(tmp_path):
    from mcpbrain.capture import write_capture
    env = _capture_envelope("decision", text="Retire X", rationale="Y", owner="Sam")
    p = write_capture(str(tmp_path), env)
    data = json.loads(pathlib.Path(p).read_text())
    assert data["kind"] == "decision" and data["text"] == "Retire X" and data["owner"] == "Sam"


def test_brain_decision_spools_envelope(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    out = _run(make_brain_decision()(
        text="Adopt uv everywhere",
        rationale="Faster installs",
        owner="Sam",
        supersedes="old-decision",
        org="CP",
    ))
    assert out["queued"] is True
    files = list((tmp_path / "capture_inbox").glob("cap-*.json"))
    assert len(files) == 1
    env = json.loads(files[0].read_text())
    assert env["kind"] == "decision"
    assert env["text"] == "Adopt uv everywhere"
    assert env["rationale"] == "Faster installs"
    assert env["owner"] == "Sam"
    assert env["supersedes"] == "old-decision"
    assert env["org"] == "CP"


def test_brain_decision_empty_text_returns_error(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    out = _run(make_brain_decision()(text=""))
    assert out["queued"] is False and "error" in out


# ---------------------------------------------------------------------------
# brain_note
# ---------------------------------------------------------------------------

def test_brain_note_spools_envelope(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    out = _run(make_brain_note()(text="Remember to review CI config"))
    assert out["queued"] is True
    files = list((tmp_path / "capture_inbox").glob("cap-*.json"))
    assert len(files) == 1
    env = json.loads(files[0].read_text())
    assert env["kind"] == "continuity"
    assert env["text"] == "Remember to review CI config"


def test_brain_note_empty_text_returns_error(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    out = _run(make_brain_note()(text=""))
    assert out["queued"] is False and "error" in out


# ---------------------------------------------------------------------------
# brain_memory_write
# ---------------------------------------------------------------------------

def test_brain_memory_write_spools_envelope(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    out = _run(make_brain_memory_write()(
        slug="project_foo",
        description="Notes about Foo",
        body="# Foo\n\nDetails here.",
        memory_type="project",
    ))
    assert out["queued"] is True
    files = list((tmp_path / "capture_inbox").glob("cap-*.json"))
    assert len(files) == 1
    env = json.loads(files[0].read_text())
    assert env["kind"] == "memory"
    assert env["slug"] == "project_foo"
    assert env["description"] == "Notes about Foo"
    assert env["body"] == "# Foo\n\nDetails here."
    assert env["memory_type"] == "project"


def test_brain_memory_write_empty_slug_returns_error(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    out = _run(make_brain_memory_write()(slug="", description="D", body="B"))
    assert out["queued"] is False and "error" in out
