"""The wizard's fleet prefill has never worked: config_profile() returns no
`fleet` key, so index.html's prefill branch is dead and its hardcoded IDs are the
only source — a silent duplicate of the tenant profile.

Tested through a pure resolver rather than a live Daemon: config_profile() also
renders project instructions and resolves the records dir, none of which this
behaviour depends on.
"""
import pytest

from mcpbrain import config, tenant


@pytest.fixture(autouse=True)
def _clear():
    tenant._clear_cache()
    yield
    tenant._clear_cache()


def test_empty_config_falls_back_to_the_tenant_profile():
    prof = tenant.profile()
    fleet = config.fleet_defaults({})
    assert fleet["folder_id"] == (prof.fleet_folder_id if prof else "")
    assert fleet["escrow_folder_id"] == (prof.escrow_folder_id if prof else "")


def test_no_tenant_profile_yields_empty_not_someone_elses_folders(monkeypatch, tmp_path):
    from mcpbrain import config, tenant
    monkeypatch.delenv("MCPBRAIN_TENANT", raising=False)
    monkeypatch.setattr(tenant, "_bundled_path", lambda: tmp_path / "absent.json")
    tenant._clear_cache()
    assert config.fleet_defaults({}) == {"folder_id": "", "escrow_folder_id": ""}


def test_saved_values_win():
    fleet = config.fleet_defaults(
        {"fleet": {"folder_id": "SAVED_FOLDER", "escrow_folder_id": "SAVED_ESCROW"}})
    assert fleet["folder_id"] == "SAVED_FOLDER"
    assert fleet["escrow_folder_id"] == "SAVED_ESCROW"


def test_partial_config_fills_only_the_missing_half():
    prof = tenant.profile()
    fleet = config.fleet_defaults({"fleet": {"folder_id": "SAVED_FOLDER"}})
    assert fleet["folder_id"] == "SAVED_FOLDER"
    assert fleet["escrow_folder_id"] == (prof.escrow_folder_id if prof else "")


def test_empty_string_is_treated_as_unset():
    # The wizard clears a field to opt out of the org fleet; an empty string must
    # not be mistaken for a saved value, or the default could never come back.
    prof = tenant.profile()
    fleet = config.fleet_defaults({"fleet": {"folder_id": ""}})
    assert fleet["folder_id"] == (prof.fleet_folder_id if prof else "")


def test_config_profile_exposes_the_fleet_block():
    from mcpbrain import daemon as daemon_mod
    import inspect
    src = inspect.getsource(daemon_mod.Daemon.config_profile)
    assert "fleet_defaults" in src, "config_profile must serve the fleet block"
