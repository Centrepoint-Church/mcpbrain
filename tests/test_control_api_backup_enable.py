"""backup/enable resolves the escrow folder via config → tenant profile.

Previously, enable_backup raised when fleet.escrow_folder_id was unset. That
broke the wizard's automatic backup-enable, which runs *before* the wizard
writes the fleet folder IDs. It now falls back to this build's tenant profile
(matching restore._escrow_folder), so first-run backup succeeds.
"""
from mcpbrain import backup_setup


class _Exec:
    def __init__(self, value):
        self._value = value

    def execute(self, num_retries=0):
        return self._value


class _NoKeyDrive:
    """Drive with no escrow key yet: every files().list is empty (so
    enable_backup, which reads the Drive key first, goes on to mint and upload
    one), and files().create records what it is asked to create."""

    def __init__(self):
        self.lists, self.creates = [], []

    def files(self):
        outer = self

        class F:
            def list(self, **kw):
                assert kw.get("supportsAllDrives") is True
                outer.lists.append(kw["q"])
                return _Exec({"files": []})

            def create(self, **kw):
                outer.creates.append(kw)
                return _Exec({"id": f"new-{len(outer.creates)}"})
        return F()


def test_enable_backup_falls_back_to_org_default_without_fleet(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config, tenant
    config.write_config(str(tmp_path), {})  # no fleet.escrow_folder_id
    captured = {}
    monkeypatch.setattr(backup_setup, "_escrow_key_to_drive",
                        lambda svc, uid, key, *, folder_id=None: captured.update(folder=folder_id))

    monkeypatch.setattr(backup_setup, "_drive_id_of", lambda svc, fid: "0Adrive")

    drive = _NoKeyDrive()
    cfg = backup_setup.enable_backup(str(tmp_path), drive_service=drive, user_id="u")

    assert captured["folder"] == tenant.profile().escrow_folder_id  # fell back, no raise
    assert cfg["backup"]["shared_drive_id"] == "0Adrive"  # the drive, not the folder
    # It looked for an existing key in the fallback folder before minting one.
    assert drive.lists and all(f"'{tenant.profile().escrow_folder_id}' in parents" in q
                               for q in drive.lists)
