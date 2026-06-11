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


def test_read_rejects_nested_subdir_path(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    sub = tmp_path / "context" / "sub"
    sub.mkdir(parents=True)
    (sub / "x.md").write_text("nested")
    with pytest.raises(ValueError):
        _run(read_context_resource(f"file://{sub}/x.md"))


def test_read_rejects_parent_escape_path(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    (tmp_path / "context").mkdir()
    (tmp_path / "escape.md").write_text("escaped")
    with pytest.raises(ValueError):
        _run(read_context_resource(f"file://{tmp_path}/context/../escape.md"))


def test_missing_context_dir_lists_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    assert _run(list_context_resources()) == []


def _seed_records(tmp_path):
    rec = tmp_path / "records"
    (rec / "context").mkdir(parents=True)
    (rec / "reference").mkdir(parents=True)
    (rec / "state").mkdir(parents=True)
    (rec / "CLAUDE.md").write_text("# project manual\n")
    (rec / "MEMORY.md").write_text("# Memory Index\n")
    (rec / "context" / "identity.md").write_text("# Identity\nDana\n")
    (rec / "reference" / "systems.md").write_text("# Systems\n")
    (rec / "state" / "decisions.md").write_text("# Decisions\n")
    return rec


def test_records_repo_files_listed(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    (tmp_path / "context").mkdir()
    (tmp_path / "context" / "memory.md").write_text("# memory\n")
    _seed_records(tmp_path)
    names = {r.name for r in _run(list_context_resources())}
    assert "memory.md" in names                       # app-dir context
    assert "CLAUDE.md" in names                        # records repo
    assert "context/identity.md" in names
    assert "reference/systems.md" in names
    assert "state/decisions.md" in names
    assert "MEMORY.md" in names


def test_records_resource_readable(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    rec = _seed_records(tmp_path)
    uri = f"file://{(rec / 'context' / 'identity.md').resolve()}"
    assert "Dana" in _run(read_context_resource(uri))


def test_degrades_to_app_dir_only_without_records(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    (tmp_path / "context").mkdir()
    (tmp_path / "context" / "memory.md").write_text("# memory\n")
    names = {r.name for r in _run(list_context_resources())}
    assert names == {"memory.md"}  # no records repo -> just the app-dir context
