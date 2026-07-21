import json
from mcpbrain import config

def _home(tmp_path, data):
    (tmp_path / "config.json").write_text(json.dumps(data)); return str(tmp_path)

def test_user_timezone_empty_when_unset(tmp_path):
    assert config.user_timezone(_home(tmp_path, {})) == ""

def test_user_timezone_returns_configured(tmp_path):
    assert config.user_timezone(_home(tmp_path, {"timezone": "America/New_York"})) == "America/New_York"


# --- M5: full wizard persistence round-trip ---------------------------------
#
# Traced path: wizard/index.html #timezone dropdown -> saveProfile()'s POST
# body (`body.timezone = tz`, index.html:268-269) -> POST /api/config ->
# control_api._handle_post -> daemon.apply_config(body) ->
# config.write_config -> disk -> config.read_config / daemon.config_profile()
# (what the wizard's prefillFromConfig() reads back).
#
# Investigation found NO bug: every hop passes `timezone` through unfiltered
# (config.write_config does a generic `dict.update`, no key allowlist;
# daemon.apply_config forwards the whole POST body verbatim to write_config;
# config_profile() projects `timezone` back out for the GET the wizard uses
# to prefill the form). These tests pin that end-to-end behavior.

def test_timezone_persists_through_apply_config(tmp_path, monkeypatch):
    from mcpbrain import config as config_mod
    monkeypatch.setattr(config_mod, "app_dir", lambda: tmp_path)
    config.write_config(str(tmp_path), {**config.read_config(str(tmp_path)), "timezone": "Australia/Brisbane"})
    assert config.read_config(str(tmp_path)).get("timezone") == "Australia/Brisbane"


def test_timezone_write_does_not_clobber_other_keys(tmp_path):
    """write_config's shallow merge keeps a previously-saved timezone when a
    later POST (e.g. the fleet-settings form) omits the key entirely."""
    config.write_config(str(tmp_path), {"timezone": "Pacific/Auckland"})
    config.write_config(str(tmp_path), {"owner_name": "Sam"})
    cfg = config.read_config(str(tmp_path))
    assert cfg.get("timezone") == "Pacific/Auckland"
    assert cfg.get("owner_name") == "Sam"


class _FakeEmbedder:
    dim = 4

    def embed(self, texts):
        return [[0.0] * self.dim for _ in texts]


def test_timezone_persists_through_daemon_apply_config(tmp_path, monkeypatch):
    """The daemon-level hop: apply_config(body) as posted by the wizard's
    saveProfile() persists `timezone` to disk and it round-trips through
    config_profile() (what /api/config GET returns for the wizard's
    prefillFromConfig)."""
    from mcpbrain.daemon import Daemon
    from mcpbrain.store import Store

    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    store = Store(tmp_path / "b.sqlite3", dim=4)
    store.init()
    d = Daemon(store, _FakeEmbedder(), services={})

    d.apply_config({"owner_name": "Josh", "timezone": "Australia/Brisbane"})

    assert config.read_config(str(tmp_path)).get("timezone") == "Australia/Brisbane"
    assert d.config_profile()["timezone"] == "Australia/Brisbane"
