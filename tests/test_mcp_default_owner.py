"""brain_decision's owner default reads config, not 'Josh'."""
import json


def test_default_owner_reads_config(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(json.dumps({"owner_name": "Sam"}))
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import mcp_server
    assert mcp_server._default_owner() == "Sam"


def test_default_owner_empty_when_unconfigured(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(json.dumps({}))
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import mcp_server
    assert mcp_server._default_owner() == ""
