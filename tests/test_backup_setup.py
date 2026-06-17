from mcpbrain import backup_setup


def test_enable_writes_config_and_escrows(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    uploaded = {}
    monkeypatch.setattr(backup_setup, "_resolve_shared_drive", lambda svc, **kw: "SHARED1")
    monkeypatch.setattr(backup_setup, "_escrow_key_to_drive", lambda svc, uid, key, **kw: uploaded.setdefault("k", (uid, key)))
    cfg = backup_setup.enable_backup(str(tmp_path), drive_service=object(), user_id="josh@x.com")
    assert cfg["backup"]["escrow_key"] and cfg["backup"]["shared_drive_id"] == "SHARED1" and cfg["backup"]["user_id"] == "josh@x.com"
    assert uploaded["k"][0] == "josh@x.com"


def test_enable_idempotent_keeps_existing_key(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    monkeypatch.setattr(backup_setup, "_resolve_shared_drive", lambda svc, **kw: "S")
    monkeypatch.setattr(backup_setup, "_escrow_key_to_drive", lambda *a, **kw: None)
    a = backup_setup.enable_backup(str(tmp_path), drive_service=object(), user_id="u")["backup"]["escrow_key"]
    b = backup_setup.enable_backup(str(tmp_path), drive_service=object(), user_id="u")["backup"]["escrow_key"]
    assert a == b  # never rotates silently


def test_resolve_shared_drive_uses_configured_folder_not_search(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config
    config.write_config(str(tmp_path), {"fleet": {"escrow_folder_id": "ESCROW1"}})

    class _Drive:
        def files(self):
            raise AssertionError("must not touch Drive — folder id comes from config")

    assert backup_setup._resolve_shared_drive(_Drive(), home=str(tmp_path)) == "ESCROW1"


def test_resolve_shared_drive_falls_back_to_org_default(tmp_path, monkeypatch):
    # Regression: the wizard calls enable_backup (auto) BEFORE it writes the
    # fleet folder IDs. Without a fallback, _resolve_shared_drive raised and
    # first-run backup always failed. It must fall back to the org default.
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config, org_defaults
    config.write_config(str(tmp_path), {"owner_email": "j@x.com"})  # no fleet block
    assert backup_setup._resolve_shared_drive(object(), home=str(tmp_path)) \
        == org_defaults.ESCROW_FOLDER_ID


def test_enable_backup_escrows_to_configured_shared_drive(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config
    config.write_config(str(tmp_path), {"fleet": {"escrow_folder_id": "ESCROW1"}})
    captured = {}

    def _fake_escrow(svc, uid, key, *, folder_id=None):
        captured["folder_id"] = folder_id
        captured["uid"] = uid

    monkeypatch.setattr(backup_setup, "_escrow_key_to_drive", _fake_escrow)
    cfg = backup_setup.enable_backup(str(tmp_path), drive_service=object(), user_id="josh@x.com")
    assert captured["folder_id"] == "ESCROW1"
    assert cfg["backup"]["shared_drive_id"] == "ESCROW1"
