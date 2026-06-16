"""daemon.main merges org-config on startup when fleet.folder_id is set."""
from mcpbrain import daemon


def test_maybe_merge_org_config_calls_fleet_when_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config
    config.write_config(str(tmp_path), {"fleet": {"folder_id": "FLEET1"}})
    calls = {}
    monkeypatch.setattr(daemon, "_build_drive_service", lambda: "SVC")
    monkeypatch.setattr("mcpbrain.fleet.merge_org_config",
                        lambda home, svc: calls.setdefault("args", (home, svc)) or {"ok": 1})
    daemon._maybe_merge_org_config(str(tmp_path))
    assert calls["args"] == (str(tmp_path), "SVC")


def test_maybe_merge_org_config_skips_when_unconfigured(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config
    config.write_config(str(tmp_path), {})  # no fleet.folder_id
    called = {"n": 0}
    monkeypatch.setattr("mcpbrain.fleet.merge_org_config",
                        lambda home, svc: called.update(n=called["n"] + 1))
    daemon._maybe_merge_org_config(str(tmp_path))
    assert called["n"] == 0


def test_maybe_merge_org_config_swallows_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config
    config.write_config(str(tmp_path), {"fleet": {"folder_id": "FLEET1"}})
    monkeypatch.setattr(daemon, "_build_drive_service",
                        lambda: (_ for _ in ()).throw(RuntimeError("no token")))
    # must not raise — org-config is best-effort
    daemon._maybe_merge_org_config(str(tmp_path))
