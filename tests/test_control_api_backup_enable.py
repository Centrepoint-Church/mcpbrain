"""backup/enable surfaces a clear error when fleet.escrow_folder_id is unset."""
from mcpbrain import backup_setup


def test_enable_backup_raises_without_escrow_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config
    config.write_config(str(tmp_path), {})  # no fleet.escrow_folder_id
    try:
        backup_setup.enable_backup(str(tmp_path), drive_service=object(), user_id="u")
        raised = False
    except RuntimeError as exc:
        raised = "escrow_folder_id" in str(exc)
    assert raised
