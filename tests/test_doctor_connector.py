"""doctor must notice a connector that never landed — the MSIX failure mode is
silent: the write succeeds, into a file the app does not read."""
import json

from mcpbrain import connector, doctor


def _lines(monkeypatch, desktop_cfg, code_cfg):
    monkeypatch.setattr(connector, "desktop_config_paths", lambda: [desktop_cfg])
    monkeypatch.setattr(connector, "code_config_path", lambda: code_cfg)
    return doctor.connector_lines(mcpbrain_bin="/abs/bin/mcpbrain")


def test_reports_ok_when_registered_and_binary_exists(tmp_path, monkeypatch):
    binary = tmp_path / "mcpbrain"
    binary.write_text("#!/bin/sh\n")
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {
        "mcpbrain": {"command": str(binary), "args": ["mcp-server"]}}}))
    monkeypatch.setattr(connector, "desktop_config_paths", lambda: [cfg])
    monkeypatch.setattr(connector, "code_config_path", lambda: tmp_path / "absent.json")

    lines = doctor.connector_lines(mcpbrain_bin=str(binary))
    assert any(line.startswith("✅") and "Connector" in line for line in lines)


def test_reports_missing_entry(tmp_path, monkeypatch):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {}}))
    lines = _lines(monkeypatch, cfg, tmp_path / "absent.json")
    assert any("⚠️" in line and "not registered" in line for line in lines)


def test_reports_stale_command_path(tmp_path, monkeypatch):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {
        "mcpbrain": {"command": str(tmp_path / "gone"), "args": ["mcp-server"]}}}))
    lines = _lines(monkeypatch, cfg, tmp_path / "absent.json")
    assert any("⚠️" in line and "does not exist" in line for line in lines)


def test_absent_config_file_is_informational_not_a_fault(tmp_path, monkeypatch):
    lines = _lines(monkeypatch, tmp_path / "absent1.json",
                   tmp_path / "absent2.json")
    assert lines and all(not line.startswith("❌") for line in lines)


def test_repair_registers_the_connector(tmp_path, monkeypatch):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {}}))
    monkeypatch.setattr(connector, "desktop_config_paths", lambda: [cfg])
    monkeypatch.setattr(connector, "code_config_path", lambda: tmp_path / "absent.json")

    repairs = doctor._default_repairs(str(tmp_path), "darwin", "/abs/bin/mcpbrain")
    assert "connector" in repairs
    repairs["connector"]()
    assert json.loads(cfg.read_text())["mcpServers"]["mcpbrain"]["command"] == "/abs/bin/mcpbrain"
