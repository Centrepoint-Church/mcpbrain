from mcpbrain import backup_setup


def test_enable_writes_config_and_escrows(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    uploaded = {}
    monkeypatch.setattr(backup_setup, "_resolve_shared_drive", lambda svc: "SHARED1")
    monkeypatch.setattr(backup_setup, "_escrow_key_to_drive", lambda svc, uid, key, **kw: uploaded.setdefault("k", (uid, key)))
    cfg = backup_setup.enable_backup(str(tmp_path), drive_service=object(), user_id="josh@x.com")
    assert cfg["backup"]["escrow_key"] and cfg["backup"]["shared_drive_id"] == "SHARED1" and cfg["backup"]["user_id"] == "josh@x.com"
    assert uploaded["k"][0] == "josh@x.com"


def test_enable_idempotent_keeps_existing_key(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    monkeypatch.setattr(backup_setup, "_resolve_shared_drive", lambda svc: "S")
    monkeypatch.setattr(backup_setup, "_escrow_key_to_drive", lambda *a, **kw: None)
    a = backup_setup.enable_backup(str(tmp_path), drive_service=object(), user_id="u")["backup"]["escrow_key"]
    b = backup_setup.enable_backup(str(tmp_path), drive_service=object(), user_id="u")["backup"]["escrow_key"]
    assert a == b  # never rotates silently
