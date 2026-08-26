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


def test_mcpservers_non_dict_does_not_crash_and_reports_not_registered(tmp_path, monkeypatch):
    """Bug 2: a malformed-but-valid-JSON config (mcpServers is a list) must not
    raise AttributeError out of connector_lines."""
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": [1, 2]}))
    lines = _lines(monkeypatch, cfg, tmp_path / "absent.json")
    assert any("⚠️" in line and "not registered" in line for line in lines)


def test_entry_non_dict_does_not_crash_and_reports_malformed(tmp_path, monkeypatch):
    """Bug 2: mcpServers.mcpbrain being a bare string (hand-edited) must not
    raise AttributeError out of connector_lines, and must be distinguishable
    from the plain 'not registered' case."""
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {"mcpbrain": "not-a-dict"}}))
    lines = _lines(monkeypatch, cfg, tmp_path / "absent.json")
    assert any("⚠️" in line and "malformed" in line for line in lines)
    assert not any("not registered" in line for line in lines)


def test_reports_stale_binary_path_when_command_differs_from_current_install(tmp_path, monkeypatch):
    """Bug 3: a command that exists on disk but is not the CURRENT mcpbrain_bin
    (e.g. an old uv-tool-venv resolved path that still exists after Task 7
    switched to the stable shim path) must not report ✅."""
    old_binary = tmp_path / "old-mcpbrain"
    old_binary.write_text("#!/bin/sh\n")
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {
        "mcpbrain": {"command": str(old_binary), "args": ["mcp-server"]}}}))
    monkeypatch.setattr(connector, "desktop_config_paths", lambda: [cfg])
    monkeypatch.setattr(connector, "code_config_path", lambda: tmp_path / "absent.json")

    lines = doctor.connector_lines(mcpbrain_bin=str(tmp_path / "new-mcpbrain"))
    assert any("⚠️" in line and "differs from the current install" in line for line in lines)
    assert not any(line.startswith("✅") and "Connector" in line for line in lines)


def test_run_doctor_dispatches_connector_repair(tmp_path, monkeypatch):
    """Bug 1: run_doctor must actually CALL the injected connector repair when
    a connector problem is present, not just report it -- the pre-fix bug was
    that '_repair_connector' was registered but never dispatched anywhere."""
    from mcpbrain import doctor as doctor_mod

    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {}}))
    monkeypatch.setattr(connector, "desktop_config_paths", lambda: [cfg])
    monkeypatch.setattr(connector, "code_config_path", lambda: tmp_path / "absent.json")

    new_bin_path = tmp_path / "new-mcpbrain"
    new_bin_path.write_text("#!/bin/sh\n")
    new_bin = str(new_bin_path)
    calls = {"n": 0}

    def fake_repair():
        calls["n"] += 1
        data = json.loads(cfg.read_text())
        data.setdefault("mcpServers", {})["mcpbrain"] = {
            "command": new_bin, "args": ["mcp-server"]}
        cfg.write_text(json.dumps(data))

    conns = {k: {"state": "ok", "detail": "Connected", "last_verified": None}
             for k in ("google", "claude", "backup", "records", "enrichment")}
    repairs = {"daemon": lambda: None, "agent": lambda: None,
               "records": lambda: None, "connector": fake_repair}

    code, msg = doctor_mod.run_doctor(str(tmp_path), model_present=lambda h: True,
                                      conns=conns, repairs=repairs,
                                      mcpbrain_bin=new_bin)
    assert calls["n"] == 1, "the connector repair closure was never dispatched"
    assert "✅ Connector" in msg


def test_run_doctor_connector_repair_still_broken_counts_as_need_action(tmp_path, monkeypatch):
    """Bug 1 continued: if the repair is attempted and the post-repair state
    still shows a warning, that must count toward need_action / exit code."""
    from mcpbrain import doctor as doctor_mod

    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {}}))
    monkeypatch.setattr(connector, "desktop_config_paths", lambda: [cfg])
    monkeypatch.setattr(connector, "code_config_path", lambda: tmp_path / "absent.json")

    conns = {k: {"state": "ok", "detail": "Connected", "last_verified": None}
             for k in ("google", "claude", "backup", "records", "enrichment")}
    repairs = {"daemon": lambda: None, "agent": lambda: None,
               "records": lambda: None, "connector": lambda: None}  # no-op repair

    code, msg = doctor_mod.run_doctor(str(tmp_path), model_present=lambda h: True,
                                      conns=conns, repairs=repairs,
                                      mcpbrain_bin="/abs/bin/mcpbrain")
    assert "⚠️" in msg
    assert "Connector" in msg
    assert code == 1


def test_successful_connector_repair_is_reported_as_fixed(tmp_path, monkeypatch):
    """A repair that WORKED must be counted in the summary's "fixed" tally.

    The block increments need_action when the post-repair state is still bad,
    but originally incremented nothing when the repair succeeded — so doctor
    silently healed the connector and then printed "0 fixed automatically, 0
    need your action". Its own comment says it mirrors the embedder block, and
    that block does `fixed += 1`.
    """
    from mcpbrain import doctor as doctor_mod

    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {}}))          # entry missing -> ⚠️
    monkeypatch.setattr(connector, "desktop_config_paths", lambda: [cfg])
    monkeypatch.setattr(connector, "code_config_path", lambda: tmp_path / "absent.json")

    new_bin_path = tmp_path / "new-mcpbrain"
    new_bin_path.write_text("#!/bin/sh\n")
    new_bin = str(new_bin_path)

    def fake_repair():
        data = json.loads(cfg.read_text())
        data.setdefault("mcpServers", {})["mcpbrain"] = {
            "command": new_bin, "args": ["mcp-server"]}
        cfg.write_text(json.dumps(data))

    conns = {k: {"state": "ok", "detail": "Connected", "last_verified": None}
             for k in ("google", "claude", "backup", "records", "enrichment")}
    repairs = {"daemon": lambda: None, "agent": lambda: None,
               "records": lambda: None, "connector": fake_repair}

    code, msg = doctor_mod.run_doctor(str(tmp_path), model_present=lambda h: True,
                                      conns=conns, repairs=repairs,
                                      mcpbrain_bin=new_bin)

    assert "✅ Connector" in msg
    assert "0 fixed automatically" not in msg, \
        "a successful connector repair must not report zero fixes"
    assert "1 fixed automatically" in msg
    assert code == 0
