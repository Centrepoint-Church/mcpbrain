"""Tests for the daemon building a real Drive-backed BackupConfig from config.json.

Task 2.4: backup self-configures from config.json. The daemon's periodic
encrypted backup is OFF unless a BackupConfig is supplied; _backup_from_config
builds one from the config's `backup` block, degrading gracefully to (None, None)
when the block is incomplete or credentials/Drive scope are unavailable.
"""

import pytest

from mcpbrain import daemon as dmod
from mcpbrain.config import write_config


def _raise(**kw):
    raise AssertionError("build_google_services must not be called")


def test_daemon_builds_backup_from_config(tmp_path, monkeypatch):
    write_config(str(tmp_path), {"backup": {"escrow_key": "k" * 44, "shared_drive_id": "D",
                                            "user_id": "sam", "interval_s": 3600}})
    monkeypatch.setattr(dmod.auth, "build_google_services",
        lambda **kw: {"drive_service": "DRIVE", "gmail_service": None, "calendar_service": None})
    bc, interval = dmod._backup_from_config(str(tmp_path))
    assert bc is not None and bc.drive_service == "DRIVE"
    assert bc.shared_drive_id == "D" and interval == 3600


def test_no_backup_block_returns_none(tmp_path, monkeypatch):
    write_config(str(tmp_path), {})
    monkeypatch.setattr(dmod.auth, "build_google_services", _raise)
    assert dmod._backup_from_config(str(tmp_path)) == (None, None)


def test_incomplete_backup_block_returns_none(tmp_path, monkeypatch):
    # Missing user_id — incomplete. build_google_services must NOT be called.
    write_config(str(tmp_path), {"backup": {"escrow_key": "k" * 44, "shared_drive_id": "D"}})
    monkeypatch.setattr(dmod.auth, "build_google_services", _raise)
    assert dmod._backup_from_config(str(tmp_path)) == (None, None)


def test_missing_drive_scope_returns_none(tmp_path, monkeypatch):
    write_config(str(tmp_path), {"backup": {"escrow_key": "k" * 44, "shared_drive_id": "D",
                                            "user_id": "sam"}})
    monkeypatch.setattr(dmod.auth, "build_google_services",
        lambda **kw: {"gmail_service": None, "calendar_service": None})
    assert dmod._backup_from_config(str(tmp_path)) == (None, None)


def test_escrow_key_str_encoded_to_bytes(tmp_path, monkeypatch):
    write_config(str(tmp_path), {"backup": {"escrow_key": "k" * 44, "shared_drive_id": "D",
                                            "user_id": "sam"}})
    monkeypatch.setattr(dmod.auth, "build_google_services",
        lambda **kw: {"drive_service": "DRIVE"})
    bc, _ = dmod._backup_from_config(str(tmp_path))
    assert bc is not None and bc.key == b"k" * 44


def test_interval_defaults_when_omitted(tmp_path, monkeypatch):
    write_config(str(tmp_path), {"backup": {"escrow_key": "k" * 44, "shared_drive_id": "D",
                                            "user_id": "sam"}})
    monkeypatch.setattr(dmod.auth, "build_google_services",
        lambda **kw: {"drive_service": "DRIVE"})
    _, interval = dmod._backup_from_config(str(tmp_path))
    assert interval == dmod.DEFAULT_BACKUP_INTERVAL_S


def test_non_numeric_interval_falls_back_to_default(tmp_path, monkeypatch):
    write_config(str(tmp_path), {"backup": {"escrow_key": "k" * 44, "shared_drive_id": "D",
                                            "user_id": "sam", "interval_s": "weekly"}})
    monkeypatch.setattr(dmod.auth, "build_google_services",
        lambda **kw: {"drive_service": "DRIVE"})
    bc, interval = dmod._backup_from_config(str(tmp_path))
    assert bc is not None
    assert interval == dmod.DEFAULT_BACKUP_INTERVAL_S


@pytest.mark.parametrize("bad_interval", [0, -5])
def test_non_positive_interval_falls_back_to_default(tmp_path, monkeypatch, bad_interval):
    write_config(str(tmp_path), {"backup": {"escrow_key": "k" * 44, "shared_drive_id": "D",
                                            "user_id": "sam", "interval_s": bad_interval}})
    monkeypatch.setattr(dmod.auth, "build_google_services",
        lambda **kw: {"drive_service": "DRIVE"})
    bc, interval = dmod._backup_from_config(str(tmp_path))
    assert bc is not None
    assert interval == dmod.DEFAULT_BACKUP_INTERVAL_S


def test_credentials_unavailable_disables_backup(tmp_path, monkeypatch):
    write_config(str(tmp_path), {"backup": {"escrow_key": "k" * 44, "shared_drive_id": "D",
                                            "user_id": "sam"}})

    def _boom(**kw):
        raise RuntimeError("no token")

    monkeypatch.setattr(dmod.auth, "build_google_services", _boom)
    # Graceful degradation: no exception propagates.
    assert dmod._backup_from_config(str(tmp_path)) == (None, None)


# -- incremental backup: settings + upload transport (Task 9) -----------------

def _cfg(tmp_path, **extra):
    write_config(str(tmp_path), {"backup": {"escrow_key": "k" * 44, "shared_drive_id": "D",
                                            "user_id": "sam", **extra}})


def test_settings_come_from_the_backup_block(tmp_path, monkeypatch):
    _cfg(tmp_path, capture_interval_s=1800, retain=5, base_threshold=0.25,
         upload_budget_s=30, outbox_max_bytes=1000)
    monkeypatch.setattr(dmod.auth, "build_google_services",
        lambda **kw: {"drive_service": "DRIVE"})
    bc, interval = dmod._backup_from_config(str(tmp_path))
    s = bc.settings
    assert interval == 1800.0 and s.capture_interval_s == 1800.0
    assert s.retain_days == 5.0          # old `retain` count read as days
    assert s.base_threshold == 0.25 and s.upload_budget_s == 30.0
    assert s.outbox_max_bytes == 1000
    assert not hasattr(bc, "out_path")


@pytest.mark.parametrize("key,bad", [("capture_interval_s", "hourly"),
                                     ("upload_budget_s", 0), ("retain_days", -1),
                                     ("outbox_max_bytes", None)])
def test_invalid_setting_falls_back_to_its_default(tmp_path, monkeypatch, key, bad):
    from mcpbrain import backup_outbox
    _cfg(tmp_path, **{key: bad})
    monkeypatch.setattr(dmod.auth, "build_google_services",
        lambda **kw: {"drive_service": "DRIVE"})
    bc, _ = dmod._backup_from_config(str(tmp_path))
    assert bc is not None
    assert bc.settings == backup_outbox.Settings()


def test_transport_is_none_without_a_token_and_backup_stays_on(tmp_path, monkeypatch):
    _cfg(tmp_path)
    monkeypatch.setattr(dmod.auth, "build_google_services",
        lambda **kw: {"drive_service": "DRIVE"})

    def no_token(*a, **k):
        raise FileNotFoundError("google_token.json")
    monkeypatch.setattr(dmod.auth, "load_credentials", no_token)
    bc, _ = dmod._backup_from_config(str(tmp_path))
    assert bc is not None and bc.transport is None


def test_transport_built_from_the_same_token_with_the_upload_timeout(tmp_path, monkeypatch):
    _cfg(tmp_path)
    monkeypatch.setattr(dmod.auth, "build_google_services",
        lambda **kw: {"drive_service": "DRIVE"})
    creds = object()
    seen = {}

    def load(*a, token_file=None, **k):
        seen["token_file"] = token_file
        return creds

    def transport(c, timeout_s):
        seen["creds"], seen["timeout"] = c, timeout_s
        return "TRANSPORT"
    monkeypatch.setattr(dmod.auth, "load_credentials", load)
    monkeypatch.setattr(dmod.drive_resumable, "authorized_transport", transport)
    bc, _ = dmod._backup_from_config(str(tmp_path))
    assert bc.transport == "TRANSPORT"
    assert seen == {"token_file": tmp_path / "google_token.json", "creds": creds,
                    "timeout": dmod.auth.DEFAULT_HTTP_TIMEOUT_S}
