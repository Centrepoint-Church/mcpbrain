import mcpbrain.cli as cli

def test_dispatch_routes_each_subcommand(monkeypatch):
    called = {}
    monkeypatch.setattr(cli, "_daemon_main", lambda a: called.setdefault("daemon", a))
    monkeypatch.setattr(cli, "_auth_main",   lambda a: called.setdefault("auth", a))
    monkeypatch.setattr(cli, "_mcp_main",    lambda: called.setdefault("mcp", True))
    monkeypatch.setattr(cli, "_setup_main",  lambda a: called.setdefault("setup", a))
    monkeypatch.setattr(cli, "_update_main", lambda a: called.setdefault("update", a))
    monkeypatch.setattr(cli, "_register_main", lambda a: called.setdefault("register", a))
    cli.main(["daemon", "--once"]);     assert "daemon" in called
    assert called["daemon"] == ["--once"]
    cli.main(["mcp-server"]);           assert called["mcp"] is True
    cli.main(["auth", "--client-secrets", "x"]); assert "auth" in called
    assert called["auth"] == ["--client-secrets", "x"]
    cli.main(["setup"]);  cli.main(["update"]);  cli.main(["register"])
    assert {"setup","update","register"} <= set(called)
