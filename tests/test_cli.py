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


def test_gardener_meeting_subcommands_removed():
    import pytest
    for c in ("records-gardener", "meeting-packs"):
        with pytest.raises(SystemExit):
            cli.main([c])


def test_home_subcommand(capsys):
    import mcpbrain.cli as _cli
    import mcpbrain.config as _cfg
    import unittest.mock as mock
    with mock.patch.object(_cfg, "app_dir", return_value="/fake/home"):
        _cli.main(["home"])
    assert "/fake/home" in capsys.readouterr().out
