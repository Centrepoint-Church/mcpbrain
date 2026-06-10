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


def test_dispatch_enrich_backfill(monkeypatch):
    seen = {}
    monkeypatch.setattr("mcpbrain.enrich_backfill.main", lambda argv: seen.setdefault("hit", True) or 0)
    cli.main(["enrich-backfill"])
    assert seen.get("hit") is True


def test_dispatch_routes_each_subcommand(monkeypatch):
    called = {}
    monkeypatch.setattr(cli, "_daemon_main", lambda a: called.setdefault("daemon", a))
    monkeypatch.setattr(cli, "_auth_main",   lambda a: called.setdefault("auth", a))
    monkeypatch.setattr(cli, "_mcp_main",    lambda: called.setdefault("mcp", True))
    monkeypatch.setattr(cli, "_setup_main",  lambda a: called.setdefault("setup", a))
    monkeypatch.setattr(cli, "_update_main", lambda a: called.setdefault("update", a))
    monkeypatch.setattr(cli, "_register_main", lambda a: called.setdefault("register", a))
    monkeypatch.setattr(cli, "_tray_main",   lambda a: called.setdefault("tray", a))
    cli.main(["daemon", "--once"]);     assert "daemon" in called
    assert called["daemon"] == ["--once"]
    cli.main(["mcp-server"]);           assert called["mcp"] is True
    cli.main(["auth", "--client-secrets", "x"]); assert "auth" in called
    assert called["auth"] == ["--client-secrets", "x"]
    cli.main(["setup"]);  cli.main(["update"]);  cli.main(["register"]);  cli.main(["tray"])
    assert {"setup","update","register","tray"} <= set(called)
