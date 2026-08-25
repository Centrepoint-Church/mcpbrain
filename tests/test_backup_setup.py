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


def test_escrow_key_to_drive_passes_num_retries_on_update():
    """The update() branch (a key already escrowed for this user) retries too."""
    calls: list = []

    class _Files:
        def list(self, **kw):
            return _RecordingExec({"files": [{"id": "existing"}]}, calls)

        def update(self, **kw):
            return _RecordingExec({"id": "existing"}, calls)

        def create(self, **kw):
            raise AssertionError("a key already existed, create must not run")

    class _Drive:
        def files(self):
            return _Files()

    backup_setup._escrow_key_to_drive(_Drive(), "u@x.com", b"key", folder_id="F1")

    assert calls == [backup_setup._NUM_RETRIES] * 2   # one list + one update
