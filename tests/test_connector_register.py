"""register_connector writes every surface present, and reports each outcome."""
import json

from mcpbrain import connector


def test_registers_both_surfaces(tmp_path, monkeypatch):
    desktop = tmp_path / "claude_desktop_config.json"
    desktop.write_text(json.dumps({"preferences": {"a": 1}}))
    code = tmp_path / ".claude.json"
    code.write_text(json.dumps({"projects": {"/p": {"allowedTools": []}}}))
    monkeypatch.setattr(connector, "desktop_config_paths", lambda: [desktop])
    monkeypatch.setattr(connector, "code_config_path", lambda: code)

    results = connector.register_connector(mcpbrain_bin="/abs/bin/mcpbrain")

    assert all(ok for _, ok, _, _ in results)
    d = json.loads(desktop.read_text())
    assert d["preferences"] == {"a": 1}
    assert d["mcpServers"]["mcpbrain"] == {
        "command": "/abs/bin/mcpbrain", "args": ["mcp-server"]}
    c = json.loads(code.read_text())
    assert c["projects"] == {"/p": {"allowedTools": []}}      # untouched
    assert c["mcpServers"]["mcpbrain"]["type"] == "stdio"


def test_desktop_config_is_created_when_absent(tmp_path, monkeypatch):
    # A first-ever install has no chat config yet; that surface must still work.
    desktop = tmp_path / "Claude" / "claude_desktop_config.json"
    monkeypatch.setattr(connector, "desktop_config_paths", lambda: [desktop])
    monkeypatch.setattr(connector, "code_config_path", lambda: tmp_path / ".claude.json")
    connector.register_connector(mcpbrain_bin="/abs/bin/mcpbrain")
    assert json.loads(desktop.read_text())["mcpServers"]["mcpbrain"]


def test_code_config_is_not_created_when_absent(tmp_path, monkeypatch):
    # ~/.claude.json missing means Claude Code has never run here. Do not
    # fabricate one: an empty config we invented is a file the real client may
    # later disagree with, and the chat surface already covers this machine.
    code = tmp_path / ".claude.json"
    monkeypatch.setattr(connector, "desktop_config_paths",
                        lambda: [tmp_path / "claude_desktop_config.json"])
    monkeypatch.setattr(connector, "code_config_path", lambda: code)
    connector.register_connector(mcpbrain_bin="/abs/bin/mcpbrain")
    assert not code.exists()


def test_one_broken_file_does_not_stop_the_other(tmp_path, monkeypatch):
    desktop = tmp_path / "claude_desktop_config.json"
    desktop.write_text("{ broken")
    code = tmp_path / ".claude.json"
    code.write_text("{}")
    monkeypatch.setattr(connector, "desktop_config_paths", lambda: [desktop])
    monkeypatch.setattr(connector, "code_config_path", lambda: code)

    results = connector.register_connector(mcpbrain_bin="/abs/bin/mcpbrain")

    assert any(ok for _, ok, _, _ in results) and any(not ok for _, ok, _, _ in results)
    assert desktop.read_text() == "{ broken"
    assert json.loads(code.read_text())["mcpServers"]["mcpbrain"]


def test_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    desktop = tmp_path / "claude_desktop_config.json"
    monkeypatch.setattr(connector, "desktop_config_paths", lambda: [desktop])
    monkeypatch.setattr(connector, "code_config_path", lambda: tmp_path / ".claude.json")
    connector.register_connector(mcpbrain_bin="/abs/bin/mcpbrain", dry_run=True)
    assert not desktop.exists()
    assert "would" in capsys.readouterr().out.lower()
