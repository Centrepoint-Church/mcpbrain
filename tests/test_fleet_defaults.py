"""The wizard's fleet prefill has never worked: config_profile() returns no
`fleet` key, so index.html's prefill branch is dead and its hardcoded IDs are the
only source — a silent duplicate of org_defaults.

Tested through a pure resolver rather than a live Daemon: config_profile() also
renders project instructions and resolves the records dir, none of which this
behaviour depends on.
"""
from mcpbrain import config, org_defaults


def test_empty_config_falls_back_to_org_defaults():
    fleet = config.fleet_defaults({})
    assert fleet["folder_id"] == org_defaults.FLEET_FOLDER_ID
    assert fleet["escrow_folder_id"] == org_defaults.ESCROW_FOLDER_ID


def test_saved_values_win():
    fleet = config.fleet_defaults(
        {"fleet": {"folder_id": "SAVED_FOLDER", "escrow_folder_id": "SAVED_ESCROW"}})
    assert fleet["folder_id"] == "SAVED_FOLDER"
    assert fleet["escrow_folder_id"] == "SAVED_ESCROW"


def test_partial_config_fills_only_the_missing_half():
    fleet = config.fleet_defaults({"fleet": {"folder_id": "SAVED_FOLDER"}})
    assert fleet["folder_id"] == "SAVED_FOLDER"
    assert fleet["escrow_folder_id"] == org_defaults.ESCROW_FOLDER_ID


def test_empty_string_is_treated_as_unset():
    # The wizard clears a field to opt out of the org fleet; an empty string must
    # not be mistaken for a saved value, or the default could never come back.
    fleet = config.fleet_defaults({"fleet": {"folder_id": ""}})
    assert fleet["folder_id"] == org_defaults.FLEET_FOLDER_ID


def test_config_profile_exposes_the_fleet_block():
    from mcpbrain import daemon as daemon_mod
    import inspect
    src = inspect.getsource(daemon_mod.Daemon.config_profile)
    assert "fleet_defaults" in src, "config_profile must serve the fleet block"
