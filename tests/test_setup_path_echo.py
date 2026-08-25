"""mcpbrain setup --dry-run prints the resolved brain-folder path."""

import json

from mcpbrain import setup


def test_setup_dry_run_echoes_working_folder(monkeypatch, tmp_path, capsys):
    home = tmp_path / "mcpbrain-home"
    monkeypatch.setattr(setup, "app_dir", lambda: home)
    # Make _ensure_daemon_running a no-op that yields a port, so --dry-run
    # reaches the echo without touching the daemon/browser.
    monkeypatch.setattr(setup, "_ensure_daemon_running", lambda h, dry_run=False: 8765)

    rc = setup.main(["--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert str(home) in out
    assert "brain folder" in out.lower()


def test_setup_dry_run_registers_the_connector(monkeypatch, tmp_path, capsys):
    # setup registers the brain with every Claude surface present. --dry-run must
    # print each target path and the mcp-server command without writing anything.
    from mcpbrain import connector
    monkeypatch.setattr(setup, "app_dir", lambda: tmp_path / "home")
    monkeypatch.setattr(setup, "_ensure_daemon_running", lambda h, dry_run=False: 8765)
    monkeypatch.setattr(setup, "_mcpbrain_bin", lambda: "/abs/bin/mcpbrain")
    monkeypatch.setattr(connector, "desktop_config_paths",
                        lambda: [tmp_path / "claude_desktop_config.json"])
    monkeypatch.setattr(connector, "code_config_path", lambda: tmp_path / ".claude.json")

    assert setup.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "claude_desktop_config.json" in out and ".claude.json" in out
    assert "/abs/bin/mcpbrain" in out and "mcp-server" in out


def test_connect_main_writes_only_the_connector(tmp_path, monkeypatch):
    # `mcpbrain connect` registers the connector and nothing else — no daemon,
    # no wizard, no tray. It now quits/relaunches Claude Desktop around the
    # write (Task 8's fix), so fake that out rather than touching a real app:
    # the fake proves the on_quit callback (the connector write) actually runs.
    from mcpbrain import connector, desktop
    desktop_cfg = tmp_path / "claude_desktop_config.json"
    monkeypatch.setattr(setup, "_mcpbrain_bin", lambda: "/abs/bin/mcpbrain")
    monkeypatch.setattr(connector, "desktop_config_paths", lambda: [desktop_cfg])
    monkeypatch.setattr(connector, "code_config_path", lambda: tmp_path / ".claude.json")
    monkeypatch.setattr(desktop, "relaunch_claude_desktop",
                        lambda on_quit=None: (on_quit() if on_quit else None) or
                                              {"relaunched": True, "detail": "ok"})

    assert setup.connect_main([]) == 0
    data = json.loads(desktop_cfg.read_text())
    assert data["mcpServers"]["mcpbrain"] == {
        "command": "/abs/bin/mcpbrain", "args": ["mcp-server"]}


def test_connect_main_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    from mcpbrain import connector
    desktop_cfg = tmp_path / "claude_desktop_config.json"
    monkeypatch.setattr(setup, "_mcpbrain_bin", lambda: "/abs/bin/mcpbrain")
    monkeypatch.setattr(connector, "desktop_config_paths", lambda: [desktop_cfg])
    monkeypatch.setattr(connector, "code_config_path", lambda: tmp_path / ".claude.json")
    setup.connect_main(["--dry-run"])
    assert not desktop_cfg.exists()
    assert "would register" in capsys.readouterr().out
