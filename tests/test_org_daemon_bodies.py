from mcpbrain import daemon as d


def _daemon():
    # A bare Daemon-like object is heavy to build; assert the bodies gate on the
    # module-level seams by patching them. Use the real class with a minimal shim.
    class Shim(d.Daemon):
        def __init__(self):
            self._org_contrib_upload_interval_s = 1.0
            self._last_org_contrib_upload = None
            self._org_import_interval_s = 1.0
            self._last_org_import = None
            self._org_curate_interval_s = 1.0
            self._last_org_curate = None
            self._clock = lambda: 1000.0

        def ensure_services(self):          # real daemon resolves services here
            return {"drive_service": None}
    return Shim()


def test_contrib_upload_skips_when_unpinned(tmp_path, monkeypatch):
    from mcpbrain import config
    monkeypatch.setattr(config, "app_dir", lambda: tmp_path)
    dm = _daemon()
    res = dm._run_org_contrib_upload()
    assert res == {"skipped": "unpinned"} or res == {"skipped": "disabled"}
    assert dm._last_org_contrib_upload == 1000.0   # advanced despite skip


def test_curate_skips_when_not_curator(tmp_path, monkeypatch):
    from mcpbrain import config
    monkeypatch.setattr(config, "app_dir", lambda: tmp_path)   # role defaults to 'member'
    dm = _daemon()
    assert dm._run_org_curate() == {"skipped": "not_curator"}


def test_import_noops_without_fleet_storage(tmp_path, monkeypatch):
    # Simulate the pre-A state: subsystem A's fleet_storage module either isn't
    # importable, or its factory returns None (no Drive service). Inject a fake
    # that returns None so the assertion holds regardless of whether A has landed.
    import sys
    import types
    from mcpbrain import config
    monkeypatch.setattr(config, "app_dir", lambda: tmp_path)
    fake = types.ModuleType("mcpbrain.fleet_storage")
    fake.fleet_folder_storage = lambda home, drive_service=None: None
    monkeypatch.setitem(sys.modules, "mcpbrain.fleet_storage", fake)
    dm = _daemon()
    assert dm._run_org_import() == {"skipped": "no_fleet_storage"}
    assert dm._last_org_import == 1000.0           # advanced despite skip
