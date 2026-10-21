import pytest

from mcpbrain import backup_setup, restore


@pytest.fixture(autouse=True)
def _no_drive_key(monkeypatch):
    """Default: Drive definitively holds no escrow key. Tests that need one
    override restore._download_escrow_key."""
    monkeypatch.setattr(restore, "_download_escrow_key", lambda svc, folder, name: None)


def test_enable_writes_config_and_escrows(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    uploaded = {}
    monkeypatch.setattr(backup_setup, "_resolve_shared_drive", lambda svc, **kw: "SHARED1")
    monkeypatch.setattr(backup_setup, "_drive_id_of", lambda svc, fid: fid)
    monkeypatch.setattr(backup_setup, "_escrow_key_to_drive", lambda svc, uid, key, **kw: uploaded.setdefault("k", (uid, key)))
    cfg = backup_setup.enable_backup(str(tmp_path), drive_service=object(), user_id="dana@x.com")
    assert cfg["backup"]["escrow_key"] and cfg["backup"]["shared_drive_id"] == "SHARED1" and cfg["backup"]["user_id"] == "dana@x.com"
    assert uploaded["k"][0] == "dana@x.com"


def test_enable_idempotent_keeps_existing_key(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    monkeypatch.setattr(backup_setup, "_resolve_shared_drive", lambda svc, **kw: "S")
    monkeypatch.setattr(backup_setup, "_drive_id_of", lambda svc, fid: fid)
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
    from mcpbrain import config, tenant
    config.write_config(str(tmp_path), {"owner_email": "j@x.com"})  # no fleet block
    assert backup_setup._resolve_shared_drive(object(), home=str(tmp_path)) \
        == tenant.profile().escrow_folder_id


def test_enable_backup_escrows_to_configured_shared_drive(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config
    config.write_config(str(tmp_path), {"fleet": {"escrow_folder_id": "ESCROW1"}})
    captured = {}

    def _fake_escrow(svc, uid, key, *, folder_id=None):
        captured["folder_id"] = folder_id
        captured["uid"] = uid

    monkeypatch.setattr(backup_setup, "_escrow_key_to_drive", _fake_escrow)
    monkeypatch.setattr(backup_setup, "_drive_id_of", lambda svc, fid: "0Adrive")
    cfg = backup_setup.enable_backup(str(tmp_path), drive_service=object(), user_id="dana@x.com")
    assert captured["folder_id"] == "ESCROW1"
    assert cfg["backup"]["shared_drive_id"] == "0Adrive"


class _RecordingExec:
    """Records the num_retries every .execute() was called with."""

    def __init__(self, result, calls):
        self._r = result
        self._calls = calls

    def execute(self, num_retries=0):
        self._calls.append(num_retries)
        return self._r


def test_escrow_key_to_drive_passes_num_retries_on_list_and_create():
    """The escrow upload's Drive calls had no retry at all.

    This runs inside `mcpbrain setup`, so a transient Errno-49-class failure
    on the folder list (or on the key upload itself) failed backup-enable
    outright on a fresh install. The bodies are MediaInMemoryUpload buffers,
    not resumable streams, so a retry just resends the same bytes -- none of
    backup._MEDIA_NUM_RETRIES' "cannot re-seek a retried chunk" problem
    applies here.
    """
    calls: list = []

    class _Files:
        def list(self, **kw):
            return _RecordingExec({"files": []}, calls)

        def create(self, **kw):
            return _RecordingExec({"id": "new"}, calls)

        def update(self, **kw):
            raise AssertionError("nothing existed, update must not be called")

    class _Drive:
        def files(self):
            return _Files()

    backup_setup._escrow_key_to_drive(_Drive(), "u@x.com", b"key", folder_id="F1")

    assert calls == [backup_setup._NUM_RETRIES] * 2   # one list + one create


def test_escrow_key_to_drive_never_overwrites_an_existing_key():
    """R27: the escrowed key is canonical; a key that appeared since the check
    is refused, never updated."""
    calls: list = []

    class _Files:
        def list(self, **kw):
            return _RecordingExec({"files": [{"id": "existing"}]}, calls)

        def update(self, **kw):
            raise AssertionError("an escrowed key must never be overwritten")

        def create(self, **kw):
            raise AssertionError("a key already existed, create must not run")

    class _Drive:
        def files(self):
            return _Files()

    with pytest.raises(backup_setup.EscrowKeyMismatch):
        backup_setup._escrow_key_to_drive(_Drive(), "u@x.com", b"key", folder_id="F1")
    assert calls == [backup_setup._NUM_RETRIES]        # the list, with retries


def _setup_stubs(monkeypatch, uploads):
    monkeypatch.setattr(backup_setup, "_resolve_shared_drive", lambda svc, **kw: "ESC")
    monkeypatch.setattr(backup_setup, "_drive_id_of", lambda svc, fid: "0A")
    monkeypatch.setattr(backup_setup, "_escrow_key_to_drive",
                        lambda svc, name, key, **kw: uploads.append((name, key)))


def test_enable_backup_adopts_an_existing_drive_key(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config
    config.write_config(str(tmp_path), {"owner_email": "j@x.com"})
    uploads = []
    _setup_stubs(monkeypatch, uploads)
    monkeypatch.setattr(restore, "_download_escrow_key",
                        lambda svc, folder, name: b"DRIVEKEY=\n")
    cfg = backup_setup.enable_backup(str(tmp_path), drive_service=object(), user_id="u")
    assert cfg["backup"]["escrow_key"] == "DRIVEKEY="
    assert uploads == []                               # nothing written to Drive


def test_enable_backup_is_a_noop_when_drive_key_matches(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config
    config.write_config(str(tmp_path), {"owner_email": "j@x.com",
                                        "backup": {"escrow_key": "SAME="}})
    uploads = []
    _setup_stubs(monkeypatch, uploads)
    monkeypatch.setattr(restore, "_download_escrow_key", lambda svc, folder, name: b"SAME=")
    cfg = backup_setup.enable_backup(str(tmp_path), drive_service=object(), user_id="u")
    assert cfg["backup"]["escrow_key"] == "SAME=" and uploads == []


def test_enable_backup_refuses_a_mismatched_key_without_touching_anything(tmp_path,
                                                                          monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config
    config.write_config(str(tmp_path), {"owner_email": "j@x.com",
                                        "backup": {"escrow_key": "LOCAL="}})
    before = (tmp_path / "config.json").read_text()
    uploads = []
    _setup_stubs(monkeypatch, uploads)
    monkeypatch.setattr(restore, "_download_escrow_key", lambda svc, folder, name: b"DRIVE=")
    with pytest.raises(backup_setup.EscrowKeyMismatch):
        backup_setup.enable_backup(str(tmp_path), drive_service=object(), user_id="u")
    assert uploads == []
    assert (tmp_path / "config.json").read_text() == before


def test_enable_backup_key_download_failure_raises_and_generates_nothing(tmp_path,
                                                                         monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import backup, config
    config.write_config(str(tmp_path), {"owner_email": "j@x.com"})
    uploads = []
    _setup_stubs(monkeypatch, uploads)
    monkeypatch.setattr(backup, "generate_escrow_key",
                        lambda: (_ for _ in ()).throw(AssertionError("must not generate")))

    def boom(svc, folder, name):
        raise restore.EscrowKeyUnavailable("could not look up the escrow key")
    monkeypatch.setattr(restore, "_download_escrow_key", boom)
    with pytest.raises(restore.EscrowKeyUnavailable):
        backup_setup.enable_backup(str(tmp_path), drive_service=object(), user_id="u")
    assert uploads == []
    assert "backup" not in config.read_config(str(tmp_path))


def test_enable_backup_names_key_by_email(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config
    config.write_config(str(tmp_path), {"owner_email": "Dana.Okafor@Example.org"})
    seen = {}
    monkeypatch.setattr(backup_setup, "_resolve_shared_drive", lambda svc, **kw: "F")
    monkeypatch.setattr(backup_setup, "_drive_id_of", lambda svc, fid: "0A")
    monkeypatch.setattr(backup_setup, "_escrow_key_to_drive",
                        lambda svc, name, key, **kw: seen.setdefault("name", name))
    cfg = backup_setup.enable_backup(str(tmp_path), drive_service=object(),
                                     user_id="dana-okafor")
    assert seen["name"] == "dana.okafor@example.org"
    assert cfg["backup"]["user_id"] == "dana-okafor"  # kept for legacy discovery


def test_enable_backup_stores_drive_id_not_folder_id(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    monkeypatch.setattr(backup_setup, "_resolve_shared_drive", lambda svc, **kw: "1folder")
    monkeypatch.setattr(backup_setup, "_escrow_key_to_drive", lambda *a, **kw: None)

    class _Exec:
        def execute(self, num_retries=0):
            return {"driveId": "0Adrive"}

    class _Files:
        def get(self, fileId, **kw):
            assert fileId == "1folder"
            return _Exec()

    class _Svc:
        def files(self):
            return _Files()

    cfg = backup_setup.enable_backup(str(tmp_path), drive_service=_Svc(), user_id="u")
    assert cfg["backup"]["shared_drive_id"] == "0Adrive"


def test_enable_backup_reads_the_key_under_the_original_case_email(tmp_path, monkeypatch):
    """R30: enable_backup passes the email as-is, so _download_escrow_key can
    try <lower>.key and then an older install's <Original>.key; a new key is
    still written under the lower-cased name."""
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config
    config.write_config(str(tmp_path), {"owner_email": "Dana.Okafor@Example.org"})
    uploads, seen = [], []
    _setup_stubs(monkeypatch, uploads)
    monkeypatch.setattr(restore, "_download_escrow_key",
                        lambda svc, folder, email: seen.append(email))
    backup_setup.enable_backup(str(tmp_path), drive_service=object(), user_id="u")
    assert seen == ["Dana.Okafor@Example.org"]
    assert uploads[0][0] == "dana.okafor@example.org"
