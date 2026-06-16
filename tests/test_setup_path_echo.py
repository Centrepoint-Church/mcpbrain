"""mcpbrain setup --dry-run prints the resolved Cowork working-folder path."""

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
    assert "working folder" in out.lower()
