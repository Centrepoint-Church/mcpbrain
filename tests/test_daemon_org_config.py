"""daemon.main merges org-config on startup when a Google token is present."""
from mcpbrain import daemon


def _write_token(tmp_path):
    # _maybe_merge_org_config gates on auth.token_path().exists() — the content
    # is never parsed in these tests (they monkeypatch _build_drive_service
    # directly), only presence matters.
    (tmp_path / "google_token.json").write_text("{}")


def test_maybe_merge_org_config_calls_fleet_when_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config
    config.write_config(str(tmp_path), {"fleet": {"folder_id": "FLEET1"}})
    _write_token(tmp_path)
    calls = {}
    monkeypatch.setattr(daemon, "_build_drive_service", lambda: "SVC")
    monkeypatch.setattr("mcpbrain.fleet.merge_org_config",
                        lambda home, svc: calls.setdefault("args", (home, svc)) or {"ok": 1})
    daemon._maybe_merge_org_config(str(tmp_path))
    assert calls["args"] == (str(tmp_path), "SVC")


def test_maybe_merge_org_config_uses_fallback_when_folder_id_unset(tmp_path, monkeypatch):
    # The common case: fleet.folder_id is NOT set at setup. merge_org_config falls
    # back to org_defaults.FLEET_FOLDER_ID internally (it owns folder resolution),
    # so the daemon must still call it whenever a token is present.
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config
    config.write_config(str(tmp_path), {})  # no fleet.folder_id
    _write_token(tmp_path)
    monkeypatch.setattr("mcpbrain.org_defaults.FLEET_FOLDER_ID", "BAKED_IN")
    calls = {"n": 0}
    monkeypatch.setattr(daemon, "_build_drive_service", lambda: "SVC")
    monkeypatch.setattr("mcpbrain.fleet.merge_org_config",
                        lambda home, svc: calls.update(n=calls["n"] + 1))
    daemon._maybe_merge_org_config(str(tmp_path))
    assert calls["n"] == 1  # merged via the baked-in fallback (inside merge_org_config)


def test_maybe_merge_org_config_swallows_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config
    config.write_config(str(tmp_path), {"fleet": {"folder_id": "FLEET1"}})
    _write_token(tmp_path)
    monkeypatch.setattr(daemon, "_build_drive_service",
                        lambda: (_ for _ in ()).throw(RuntimeError("no token")))
    # must not raise — org-config is best-effort
    daemon._maybe_merge_org_config(str(tmp_path))


def test_maybe_merge_org_config_skips_when_no_token_file(tmp_path, monkeypatch):
    # #6: no stored Google credentials -> no Drive/auth attempt at all, and no
    # per-boot warning log. This is now the FIRST gate, ahead of any folder
    # resolution (which merge_org_config owns).
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config
    config.write_config(str(tmp_path), {"fleet": {"folder_id": "FLEET1"}})
    assert not (tmp_path / "google_token.json").exists()
    called = {"build": 0, "merge": 0}
    monkeypatch.setattr(daemon, "_build_drive_service",
                        lambda: called.update(build=called["build"] + 1) or "SVC")
    monkeypatch.setattr("mcpbrain.fleet.merge_org_config",
                        lambda home, svc: called.update(merge=called["merge"] + 1))
    daemon._maybe_merge_org_config(str(tmp_path))
    assert called == {"build": 0, "merge": 0}


def test_maybe_merge_org_config_runs_when_token_present(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config
    config.write_config(str(tmp_path), {"fleet": {"folder_id": "FLEET1"}})
    _write_token(tmp_path)
    calls = {"n": 0}
    monkeypatch.setattr(daemon, "_build_drive_service", lambda: "SVC")
    monkeypatch.setattr("mcpbrain.fleet.merge_org_config",
                        lambda home, svc: calls.update(n=calls["n"] + 1))
    daemon._maybe_merge_org_config(str(tmp_path))
    assert calls["n"] == 1
