"""The three MCP write tools spool envelopes; they never touch the store."""
import asyncio
import json

from mcpbrain.mcp_server import (
    make_brain_ingest, make_brain_action_create, make_brain_action_update,
)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


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
