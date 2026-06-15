import mcpbrain.cli as cli


def test_dispatch_records_cadences(monkeypatch):
    import mcpbrain.cli as cli
    seen = {}
    def _fake(argv):
        seen["argv"] = argv
        return 0
    monkeypatch.setattr("mcpbrain.records_cadences.main", _fake)
    cli.main(["records-prune", "--days", "7"])
    assert seen["argv"][0] == "records-prune" and "--days" in seen["argv"]
    cli.main(["records-health"])
    assert seen["argv"][0] == "records-health"


def test_dispatch_home_prints_app_dir(tmp_path, monkeypatch, capsys):
    # `mcpbrain home` is the single source of truth shims + Cowork skills resolve
    # the home dir through, so it must print exactly config.app_dir().
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    cli.main(["home"])
    out = capsys.readouterr().out.strip()
    from mcpbrain.config import app_dir
    assert out == str(app_dir())


def test_enrich_backfill_subcommand_removed():
    import pytest
    with pytest.raises(SystemExit):
        cli.main(["enrich-backfill"])


def test_gardener_meeting_subcommands_removed():
    import pytest
    for c in ("records-gardener", "meeting-packs"):
        with pytest.raises(SystemExit):
            cli.main([c])


def test_dispatch_routes_each_subcommand(monkeypatch):
    called = {}
    monkeypatch.setattr(cli, "_daemon_main", lambda a: called.setdefault("daemon", a))
    monkeypatch.setattr(cli, "_auth_main",   lambda a: called.setdefault("auth", a))
    monkeypatch.setattr(cli, "_mcp_main",    lambda: called.setdefault("mcp", True))
    monkeypatch.setattr(cli, "_setup_main",  lambda a: called.setdefault("setup", a))
    monkeypatch.setattr(cli, "_update_main", lambda a: called.setdefault("update", a))
    monkeypatch.setattr(cli, "_tray_main",   lambda a: called.setdefault("tray", a))
    cli.main(["daemon", "--once"]);     assert "daemon" in called
    assert called["daemon"] == ["--once"]
    cli.main(["mcp-server"]);           assert called["mcp"] is True
    cli.main(["auth", "--client-secrets", "x"]); assert "auth" in called
    assert called["auth"] == ["--client-secrets", "x"]
    cli.main(["setup"]);  cli.main(["update"]);  cli.main(["tray"])
    assert {"setup","update","tray"} <= set(called)
