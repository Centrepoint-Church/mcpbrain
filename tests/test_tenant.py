"""The tenant profile: resolution, optional-field semantics, and the one rule
that matters — an unconfigured build degrades to disabled and NEVER falls back
to another organisation's infrastructure."""
import json

import pytest

from mcpbrain import tenant


@pytest.fixture(autouse=True)
def _clear():
    tenant._clear_cache()
    yield
    tenant._clear_cache()


def _write(tmp_path, **overrides):
    data = {
        "tenant_id": "acme",
        "display_name": "Acme Corporation",
        "oauth_project_id": "acme-brain-123",
        "fleet_folder_id": "FLEET1",
        "escrow_folder_id": "ESCROW1",
        "index_url": "https://acme.github.io/mcpbrain-dist/simple/",
        "marketplace_owner": "Acme-Org",
        "marketplace_repo": "mcpbrain-plugin",
        "marketplace_name": "acme-org",
    }
    data.update(overrides)
    p = tmp_path / "tenant.json"
    p.write_text(json.dumps(data))
    return p


def test_load_reads_every_field(tmp_path):
    p = tenant.load(_write(tmp_path))
    assert p.tenant_id == "acme"
    assert p.display_name == "Acme Corporation"
    assert p.oauth_project_id == "acme-brain-123"
    assert p.fleet_folder_id == "FLEET1"
    assert p.escrow_folder_id == "ESCROW1"
    assert p.index_url == "https://acme.github.io/mcpbrain-dist/simple/"


def test_marketplace_helpers_derive_from_owner_and_repo(tmp_path):
    p = tenant.load(_write(tmp_path))
    assert p.marketplace_slug == "Acme-Org/mcpbrain-plugin"
    assert p.plugin_homepage == "https://github.com/Acme-Org/mcpbrain-plugin"


@pytest.mark.parametrize("field", ["fleet_folder_id", "escrow_folder_id", "index_url"])
def test_empty_string_means_unset_not_empty_string(tmp_path, field):
    """The wizard clears a field to opt out of the org fleet; config.fleet_defaults
    has always treated "" as unset. An optional field must normalise to None so
    callers can test truthiness without knowing which convention applies."""
    p = tenant.load(_write(tmp_path, **{field: ""}))
    assert getattr(p, field) is None


@pytest.mark.parametrize("field", tenant.REQUIRED_FIELDS)
def test_required_field_empty_is_rejected(tmp_path, field):
    with pytest.raises(ValueError, match=field):
        tenant.load(_write(tmp_path, **{field: ""}))


def test_missing_key_is_rejected(tmp_path):
    data = json.loads(_write(tmp_path).read_text())
    del data["display_name"]
    (tmp_path / "tenant.json").write_text(json.dumps(data))
    with pytest.raises(ValueError, match="display_name"):
        tenant.load(tmp_path / "tenant.json")


def test_env_override_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_TENANT", str(_write(tmp_path, tenant_id="fromenv")))
    assert tenant.profile().tenant_id == "fromenv"


def test_env_set_but_missing_warns_and_falls_through(tmp_path, monkeypatch, caplog):
    """Mirrors auth.embedded_client_config's MCPBRAIN_GOOGLE_CLIENT behaviour: a
    typo'd override must not silently mean 'no tenant'."""
    monkeypatch.setenv("MCPBRAIN_TENANT", str(tmp_path / "nope.json"))
    with caplog.at_level("WARNING"):
        got = tenant.profile()
    assert got is not None and got.tenant_id == "centrepoint"   # fell through to bundled
    assert "MCPBRAIN_TENANT" in caplog.text


def test_no_profile_anywhere_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_TENANT", str(tmp_path / "nope.json"))
    monkeypatch.setattr(tenant, "_bundled_path", lambda: tmp_path / "absent.json")
    assert tenant.profile() is None


def test_require_raises_when_unconfigured(tmp_path, monkeypatch):
    monkeypatch.setattr(tenant, "_bundled_path", lambda: tmp_path / "absent.json")
    monkeypatch.delenv("MCPBRAIN_TENANT", raising=False)
    with pytest.raises(tenant.TenantNotConfigured):
        tenant.require()


def test_the_bundled_centrepoint_profile_is_valid():
    """The shipped profile must parse and validate — a broken tenant.json is a
    fleet-wide outage, not a local inconvenience."""
    p = tenant.profile()
    assert p is not None
    assert p.tenant_id == "centrepoint"
    assert p.marketplace_slug == "Centrepoint-Church/mcpbrain-plugin"
