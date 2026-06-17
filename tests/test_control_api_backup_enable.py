"""backup/enable resolves the escrow folder via config → org default.

Previously, enable_backup raised when fleet.escrow_folder_id was unset. That
broke the wizard's automatic backup-enable, which runs *before* the wizard
writes the fleet folder IDs. It now falls back to the baked-in org default
(matching restore._escrow_folder), so first-run backup succeeds.
"""
from mcpbrain import backup_setup


def test_enable_backup_falls_back_to_org_default_without_fleet(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config, org_defaults
    config.write_config(str(tmp_path), {})  # no fleet.escrow_folder_id
    captured = {}
    monkeypatch.setattr(backup_setup, "_escrow_key_to_drive",
                        lambda svc, uid, key, *, folder_id=None: captured.update(folder=folder_id))

    cfg = backup_setup.enable_backup(str(tmp_path), drive_service=object(), user_id="u")

    assert captured["folder"] == org_defaults.ESCROW_FOLDER_ID      # fell back, no raise
    assert cfg["backup"]["shared_drive_id"] == org_defaults.ESCROW_FOLDER_ID
