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


def test_setup_dry_run_registers_desktop_mcp(monkeypatch, tmp_path, capsys):
    # setup connects the brain to Claude DESKTOP by writing its MCP config
    # (claude_desktop_config.json), using the ABSOLUTE mcpbrain path. --dry-run
    # must print the target config path and the mcp-server command.
    monkeypatch.setattr(setup, "app_dir", lambda: tmp_path / "home")
    monkeypatch.setattr(setup, "_ensure_daemon_running", lambda h, dry_run=False: 8765)
    monkeypatch.setattr(setup, "_mcpbrain_bin", lambda: "/abs/bin/mcpbrain")

    rc = setup.main(["--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Claude Desktop" in out and "claude_desktop_config.json" in out
    assert "/abs/bin/mcpbrain" in out and "mcp-server" in out


def test_register_desktop_mcp_merges_and_preserves(monkeypatch, tmp_path):
    # Writing the entry must create the file, preserve other servers, and be
    # idempotent (overwrite the mcpbrain entry, not duplicate it).
    cfg = tmp_path / "Claude" / "claude_desktop_config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text('{"mcpServers": {"other": {"command": "x"}}, "keep": 1}')
    monkeypatch.setattr(setup, "_desktop_config_path", lambda: cfg)
    monkeypatch.setattr(setup, "_mcpbrain_bin", lambda: "/abs/bin/mcpbrain")

    setup._register_desktop_mcp()
    setup._register_desktop_mcp()  # twice → idempotent

    import json
    data = json.loads(cfg.read_text())
    assert data["keep"] == 1                                   # untouched
    assert data["mcpServers"]["other"] == {"command": "x"}     # preserved
    assert data["mcpServers"]["mcpbrain"] == {
        "command": "/abs/bin/mcpbrain", "args": ["mcp-server"]}


def test_connect_main_writes_only_the_connector(tmp_path, monkeypatch):
    # `mcpbrain connect` registers the Desktop connector and nothing else (no
    # daemon, no wizard) — run with Claude Desktop quit so the entry survives.
    from mcpbrain import setup
    cfg = tmp_path / "Claude" / "claude_desktop_config.json"
    monkeypatch.setattr(setup, "_desktop_config_path", lambda: cfg)
    monkeypatch.setattr(setup, "_mcpbrain_bin", lambda: "/abs/bin/mcpbrain")
    assert setup.connect_main([]) == 0
    data = json.loads(cfg.read_text())
    assert data["mcpServers"]["mcpbrain"] == {
        "command": "/abs/bin/mcpbrain", "args": ["mcp-server"]}


def test_connect_main_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    from mcpbrain import setup
    cfg = tmp_path / "Claude" / "claude_desktop_config.json"
    monkeypatch.setattr(setup, "_desktop_config_path", lambda: cfg)
    monkeypatch.setattr(setup, "_mcpbrain_bin", lambda: "/abs/bin/mcpbrain")
    setup.connect_main(["--dry-run"])
    assert not cfg.exists()
    assert "would connect" in capsys.readouterr().out
