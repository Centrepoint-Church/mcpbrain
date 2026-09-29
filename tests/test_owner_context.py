"""brain_owner_context: the owner's standing context, readable from ANY client.

Why a tool and not just the resources: Claude Desktop and Cowork give the model
no way to read an MCP resource itself -- one reaches the model only when the
user attaches it by hand. A Cowork session asked to apply the owner's voice
searched the workspace, found no voice file, and fell back on settings. The
resources stay; this tool is what makes "apply my voice" work everywhere.
"""
import asyncio
import json

from mcpbrain import config, owner_context, session_hooks
from mcpbrain.mcp_server import list_context_resources


def _records(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    repo = tmp_path / "records"
    for sub in ("context", "reference", "state"):
        (repo / sub).mkdir(parents=True)
    (repo / "context" / "identity.md").write_text("I am Dana.")
    (repo / "context" / "voice.md").write_text("Never say leverage.")
    (repo / "context" / "preferences.md").write_text("Short replies.")
    (repo / "state" / "decisions.md").write_text("Decided X.")
    (repo / "reference" / "projects.md").write_text("Project list.")
    (repo / "MEMORY.md").write_text("index")
    return repo


def test_bare_call_returns_core_files_in_full_and_lists_the_rest(tmp_path, monkeypatch):
    _records(tmp_path, monkeypatch)
    out = owner_context.owner_context()
    assert out["files"] == {
        "context/identity.md": "I am Dana.",
        "context/voice.md": "Never say leverage.",
        "context/preferences.md": "Short replies.",
        "state/decisions.md": "Decided X.",
    }
    names = {m["name"] for m in out["more"]}
    assert names == {"reference/projects.md", "MEMORY.md"}
    assert "missing" not in out


def test_named_fetch_returns_one_file(tmp_path, monkeypatch):
    _records(tmp_path, monkeypatch)
    assert owner_context.owner_context("reference/projects.md") == {
        "name": "reference/projects.md", "text": "Project list."}


def test_unknown_or_outside_name_is_refused_with_the_list(tmp_path, monkeypatch):
    _records(tmp_path, monkeypatch)
    (tmp_path / "config.json").write_text("{}")
    for bad in ("config.json", "../config.json", "reference/nope.md"):
        out = owner_context.owner_context(bad)
        assert "error" in out and "text" not in out
        assert "context/voice.md" in out["available"]


def test_missing_voice_is_reported_not_silent(tmp_path, monkeypatch):
    repo = _records(tmp_path, monkeypatch)
    (repo / "context" / "voice.md").unlink()
    out = owner_context.owner_context()
    assert out["missing"] == ["context/voice.md"]


def test_tool_and_resources_expose_the_same_files(tmp_path, monkeypatch):
    _records(tmp_path, monkeypatch)
    listed = {r.name for r in asyncio.run(list_context_resources())}
    reachable = set(owner_context.owner_context()["files"]) | {
        m["name"] for m in owner_context.owner_context()["more"]}
    assert listed == reachable


def test_instructions_point_at_the_tool_not_attach_only_resources():
    # Both texts used to say "read ... from the @-resources" / "no tool call
    # needed", which is false in Desktop and Cowork.
    text = config.render_project_instructions({"owner_name": "Dana"})
    assert "brain_owner_context" in text
    assert "@-resources" not in text
    assert "brain_owner_context" in session_hooks._TOOL_REMINDER
    assert "no tool call" not in session_hooks._TOOL_REMINDER


def test_callable_over_the_protocol(protocol_session):
    async def go():
        async with protocol_session() as (session, _stderr):
            res = await session.call_tool("brain_owner_context", {})
            assert not res.is_error
            body = json.loads(res.content[0].text)
            assert "files" in body and "more" in body
    asyncio.run(go())
