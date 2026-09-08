"""config_profile carries the tenant so the wizard can name it in its own UI."""
import pytest

from mcpbrain import tenant


@pytest.fixture(autouse=True)
def _clear_tenant_cache():
    # tenant.profile() memoises; without this, a test here that monkeypatches
    # _bundled_path leaves the cache stale for the next module in the session
    # (same convention as tests/test_fleet_defaults.py).
    tenant._clear_cache()
    yield
    tenant._clear_cache()


def test_config_profile_exposes_the_tenant(monkeypatch, tmp_path):
    from mcpbrain.daemon import Daemon
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text("{}")
    tenant._clear_cache()
    prof = Daemon.config_profile(_StubDaemon())
    assert prof["tenant"]["display_name"] == "Centrepoint Church"
    assert prof["tenant"]["tenant_id"] == "centrepoint"


def test_config_profile_tenant_is_none_when_unconfigured(monkeypatch, tmp_path):
    from mcpbrain.daemon import Daemon
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text("{}")
    monkeypatch.delenv("MCPBRAIN_TENANT", raising=False)
    monkeypatch.setattr(tenant, "_bundled_path", lambda: tmp_path / "absent.json")
    tenant._clear_cache()
    assert Daemon.config_profile(_StubDaemon())["tenant"] is None


class _StubDaemon:
    """config_profile reads only module-level config, never self."""
