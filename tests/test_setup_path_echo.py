"""mcpbrain setup --dry-run prints the resolved brain-folder path."""

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


def test_setup_dry_run_registers_mcp_server(monkeypatch, tmp_path, capsys):
    # setup connects the brain by registering the `mcpbrain` MCP server with the
    # `claude` CLI at user scope, using the ABSOLUTE mcpbrain path (so it resolves
    # under the minimal login PATH). --dry-run must print that registration.
    from mcpbrain import config
    monkeypatch.setattr(setup, "app_dir", lambda: tmp_path / "home")
    monkeypatch.setattr(setup, "_ensure_daemon_running", lambda h, dry_run=False: 8765)
    monkeypatch.setattr(setup, "_mcpbrain_bin", lambda: "/abs/bin/mcpbrain")
    monkeypatch.setattr(config, "find_claude", lambda: "/abs/bin/claude")

    rc = setup.main(["--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "mcp add mcpbrain --scope user -- /abs/bin/mcpbrain mcp-server" in out
