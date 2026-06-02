import mcpbrain.setup as setup


def test_setup_dry_run_reports_wizard_url(capsys, monkeypatch):
    monkeypatch.setattr(setup, "_ensure_daemon_running", lambda home, *, dry_run=False: 53999)
    setup.main(["--dry-run"])
    out = capsys.readouterr().out
    assert "127.0.0.1:53999" in out and "would open" in out.lower()


def test_ensure_daemon_running_dry_run_has_no_side_effects(tmp_path, monkeypatch):
    """The real _ensure_daemon_running dry-run path installs/polls nothing."""
    calls = []

    from mcpbrain import agents

    monkeypatch.setattr(agents, "install_agent", lambda *a, **k: calls.append((a, k)))

    port = setup._ensure_daemon_running(str(tmp_path), dry_run=True)

    assert port == setup._DRY_RUN_PORT
    assert not (tmp_path / "control_port").exists()
    assert calls == []
