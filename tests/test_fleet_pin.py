from mcpbrain import fleet, config


def test_org_pin_is_allowlisted():
    assert "org_pin" in fleet._ALLOWLIST
    assert "cadences" in fleet._ALLOWLIST


def test_non_allowlisted_keys_still_dropped():
    # A fabricated key must NOT be allowed through the overlay.
    assert not fleet._is_allowed("secrets")
    assert not fleet._is_allowed("owner_email")


def test_merge_stages_org_pin_into_config(tmp_path, monkeypatch):
    home = str(tmp_path)
    config.write_config(home, {"fleet": {"folder_id": "FID"}})
    monkeypatch.setattr(fleet, "read_org_config", lambda folder_id, svc: {
        "org_pin": {"fleet_secret": "s3cret", "dim": 384},
        "cadences": {"review_interval_s": 3600},
        "evil_key": {"x": 1}})
    allowed = fleet.merge_org_config(home, drive_service=object())
    assert "org_pin" in allowed and "cadences" in allowed
    assert "evil_key" not in allowed
    assert config.fleet_pin(home).fleet_secret == "s3cret"


def test_merge_falls_back_to_org_default_folder_when_unset(tmp_path, monkeypatch):
    """The common case: an install with NO fleet.folder_id set must STILL read
    org-config from the baked-in org default folder — else a fleet-wide pin/
    cadence change reaches nobody. (merge_org_config used to early-return here.)"""
    from mcpbrain import org_defaults
    home = str(tmp_path)   # config has no 'fleet' block at all
    seen = {}

    def _fake_read(folder_id, svc):
        seen["folder_id"] = folder_id
        return {"org_pin": {"fleet_secret": "s3cret", "dim": 384}}

    monkeypatch.setattr(fleet, "read_org_config", _fake_read)
    allowed = fleet.merge_org_config(home, drive_service=object())
    assert seen["folder_id"] == org_defaults.FLEET_FOLDER_ID   # fell back, didn't early-return
    assert "org_pin" in allowed
    assert config.fleet_pin(home).is_pinned is True
