"""MCP resources: context/*.md exposed so Desktop chats can @-mention them."""
import asyncio

from mcpbrain.mcp_server import list_context_resources, read_context_resource


def _run(coro):
    return asyncio.run(coro)


def test_lists_markdown_files(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    ctx = tmp_path / "context"
    ctx.mkdir()
    (ctx / "memory.md").write_text("# Memory Index\n")
    (ctx / "notes.txt").write_text("not exposed")
    resources = _run(list_context_resources())
    uris = [str(r.uri) for r in resources]
    assert any(u.endswith("memory.md") for u in uris)
    assert not any(u.endswith("notes.txt") for u in uris)


def test_read_returns_content(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    ctx = tmp_path / "context"
    ctx.mkdir()
    (ctx / "memory.md").write_text("# Memory Index\nhello")
    resources = _run(list_context_resources())
    content = _run(read_context_resource(resources[0].uri))
    assert "hello" in content


def test_read_rejects_paths_outside_context(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    (tmp_path / "context").mkdir()
    (tmp_path / "config.json").write_text("{}")
    with pytest.raises(ValueError):
        _run(read_context_resource(f"file://{tmp_path}/config.json"))


def test_missing_context_dir_lists_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    assert _run(list_context_resources()) == []
