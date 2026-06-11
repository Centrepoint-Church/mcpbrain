"""config_profile projects non-secret fields; apply_config materialises assets."""
import json

from mcpbrain import daemon as daemon_mod


class _FakeStore:
    def chunk_count(self): return 0
    def enriched_count(self): return 0
    def open_findings_count(self): return 0


def test_config_profile_omits_secret(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(json.dumps(
        {"owner_full_name": "Dana", "owner_role": "Ops", "owner_email": "d@x.com",
         "orgs": [{"name": "Acme"}], "clickup_api_key": "pk_secret",
         "clickup_list_id": "L1", "timezone": "Australia/Perth"}))
    # config_profile() calls the `app_dir` name bound in daemon's namespace.
    monkeypatch.setattr(daemon_mod, "app_dir", lambda: tmp_path)
    d = daemon_mod.Daemon.__new__(daemon_mod.Daemon)  # bypass __init__ network work
    d._store = _FakeStore()
    prof = d.config_profile()
    assert prof["owner_full_name"] == "Dana"
    assert prof["clickup_api_key_set"] is True
    assert "clickup_api_key" not in prof
    assert prof["timezone"] == "Australia/Perth"
    assert prof["owner_name"] == ""           # absent in config -> normalised to ""
    assert prof["orgs"] == [{"name": "Acme"}]
    assert prof["clickup_list_id"] == "L1"


def test_config_profile_includes_project_instructions(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(json.dumps(
        {"owner_name": "Dana", "orgs": [{"name": "Acme"}, {"name": "Globex"}]}))
    monkeypatch.setattr(daemon_mod, "app_dir", lambda: tmp_path)
    d = daemon_mod.Daemon.__new__(daemon_mod.Daemon)
    d._store = _FakeStore()
    pi = d.config_profile()["project_instructions"]
    assert "Dana" in pi                       # rendered with the owner's name
    assert "Acme, Globex" in pi               # orgs interpolated
    assert "brain_search" in pi and "brain_decision" in pi
    assert "you don't tag anything" in pi     # classification is enrichment's job
