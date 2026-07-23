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


def test_maybe_merge_org_config_uses_fallback_when_folder_id_unset(tmp_path, monkeypatch):
    # The common case: fleet.folder_id is NOT set at setup. merge_org_config falls
    # back to org_defaults.FLEET_FOLDER_ID, so the daemon MUST still call it —
    # otherwise the fleet-wide org_pin/flags reach nobody (the caller-guard half of
    # the 0.7.90 fallback bug).
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config
    config.write_config(str(tmp_path), {})  # no fleet.folder_id
    monkeypatch.setattr("mcpbrain.org_defaults.FLEET_FOLDER_ID", "BAKED_IN")
    calls = {"n": 0}
    monkeypatch.setattr(daemon, "_build_drive_service", lambda: "SVC")
    monkeypatch.setattr("mcpbrain.fleet.merge_org_config",
                        lambda home, svc: calls.update(n=calls["n"] + 1))
    daemon._maybe_merge_org_config(str(tmp_path))
    assert calls["n"] == 1  # merged via the baked-in fallback


def test_maybe_merge_org_config_skips_when_no_folder_resolves(tmp_path, monkeypatch):
    # Only skip when NEITHER fleet.folder_id NOR the baked-in default resolves
    # (e.g. a fork with no org folder).
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config
    config.write_config(str(tmp_path), {})
    monkeypatch.setattr("mcpbrain.org_defaults.FLEET_FOLDER_ID", "")
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
