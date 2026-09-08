"""bin/tenant.py — `use` copies a private profile into the tree, `check` validates it."""
import importlib.util
import json
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent


def _load_cli():
    spec = importlib.util.spec_from_file_location("_tenant_cli", _ROOT / "bin" / "tenant.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _client(project_id="acme-brain-123", kind="installed"):
    return {kind: {"client_id": "123-abc.apps.googleusercontent.com",
                   "project_id": project_id,
                   "client_secret": "GOCSPX-fake",
                   "redirect_uris": ["http://localhost"]}}


def _profile(**overrides):
    data = {"tenant_id": "acme", "display_name": "Acme Corporation",
            "oauth_project_id": "acme-brain-123",
            "fleet_folder_id": "FLEET1", "escrow_folder_id": "ESCROW1",
            "index_url": "https://acme.github.io/mcpbrain-dist/simple/",
            "marketplace_owner": "Acme-Org", "marketplace_repo": "mcpbrain-plugin",
            "marketplace_name": "acme-org"}
    data.update(overrides)
    return data


@pytest.fixture
def tenant_repo(tmp_path):
    d = tmp_path / "tenant-repo"
    d.mkdir()
    (d / "google_oauth_client.json").write_text(json.dumps(_client()))
    (d / "tenant.json").write_text(json.dumps(_profile()))
    return d


@pytest.fixture
def fake_repo(tmp_path):
    r = tmp_path / "repo"
    (r / "mcpbrain").mkdir(parents=True)
    return r


def test_use_copies_the_oauth_client_into_the_package(tenant_repo, fake_repo):
    cli = _load_cli()
    written = cli.use_profile(tenant_repo, fake_repo)
    dest = fake_repo / "mcpbrain" / "google_oauth_client.json"
    assert dest.exists()
    assert dest in written
    assert json.loads(dest.read_text())["installed"]["project_id"] == "acme-brain-123"


def test_use_also_copies_tenant_json_when_present(tenant_repo, fake_repo):
    cli = _load_cli()
    cli.use_profile(tenant_repo, fake_repo)
    assert (fake_repo / "mcpbrain" / "tenant.json").exists()


def test_use_refuses_a_directory_with_no_oauth_client(tmp_path, fake_repo):
    cli = _load_cli()
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(FileNotFoundError, match="google_oauth_client.json"):
        cli.use_profile(empty, fake_repo)


from mcpbrain import tenant


def _repo_with(tmp_path, profile=None, client=None) -> Path:
    """A fake source tree carrying just what check_offline reads."""
    r = tmp_path / "repo"
    (r / "mcpbrain").mkdir(parents=True)
    (r / "plugin" / ".claude-plugin").mkdir(parents=True)
    (r / "plugin" / "scripts").mkdir(parents=True)
    (r / "plugin" / "commands").mkdir(parents=True)
    prof = _profile() if profile is None else profile
    (r / "mcpbrain" / "tenant.json").write_text(json.dumps(prof))
    if client is not None:
        (r / "mcpbrain" / "google_oauth_client.json").write_text(json.dumps(client))
    (r / "plugin" / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps({"name": prof["marketplace_name"], "plugins": [{"version": "0.0.0"}]}))
    (r / "plugin" / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"version": "0.0.0",
                    "homepage": f"https://github.com/{prof['marketplace_owner']}"
                                f"/{prof['marketplace_repo']}"}))
    (r / "plugin" / "scripts" / "install.ps1").write_text(
        f'$INDEX = "mcpbrain={prof["index_url"]}"\n')
    (r / "plugin" / "commands" / "install.md").write_text(
        f'uv tool install --python 3.12 --index "mcpbrain={prof["index_url"]}" '
        f'"mcpbrain[daemon]" --force\n')
    return r


def test_a_complete_profile_passes(tmp_path):
    assert tenant.check_offline(_repo_with(tmp_path, client=_client())) == []


def test_missing_oauth_client_is_reported(tmp_path):
    problems = tenant.check_offline(_repo_with(tmp_path))
    assert any("google_oauth_client.json" in p for p in problems)


def test_placeholder_values_are_rejected(tmp_path):
    """The example template's own values must fail, so a fork that copies it and
    forgets to edit fails loudly instead of half-working."""
    repo = _repo_with(tmp_path, profile=_profile(tenant_id="your-org",
                                                 oauth_project_id="REPLACE-gcp-project-id"),
                      client=_client())
    problems = tenant.check_offline(repo)
    assert any("your-org" in p for p in problems)
    assert any("REPLACE" in p for p in problems)


def test_a_web_oauth_client_is_rejected(tmp_path):
    """A `web` client fails later with an opaque redirect_uri_mismatch."""
    repo = _repo_with(tmp_path, client=_client(kind="web"))
    assert any("Desktop" in p or "installed" in p for p in tenant.check_offline(repo))


def test_reusing_the_upstream_oauth_client_is_rejected(tmp_path):
    """The likeliest fork mistake. Expressed as an agreement between tenant.json's
    oauth_project_id and the client's own project_id, so no upstream identifier is
    hardcoded and the check survives a fork of a fork."""
    repo = _repo_with(tmp_path, client=_client(project_id="someone-elses-project"))
    problems = tenant.check_offline(repo)
    assert any("project_id" in p for p in problems)


def test_a_client_id_of_the_wrong_shape_is_rejected(tmp_path):
    bad = _client()
    bad["installed"]["client_id"] = "not-a-google-client"
    assert any("client_id" in p for p in tenant.check_offline(_repo_with(tmp_path, client=bad)))


def test_marketplace_name_drift_is_reported(tmp_path):
    repo = _repo_with(tmp_path, client=_client())
    mk = repo / "plugin" / ".claude-plugin" / "marketplace.json"
    mk.write_text(json.dumps({"name": "stale-name", "plugins": [{"version": "0.0.0"}]}))
    assert any("marketplace.json" in p for p in tenant.check_offline(repo))


def test_index_url_drift_in_install_ps1_is_reported(tmp_path):
    repo = _repo_with(tmp_path, client=_client())
    (repo / "plugin" / "scripts" / "install.ps1").write_text(
        '$INDEX = "mcpbrain=https://stale.example/simple/"\n')
    assert any("install.ps1" in p for p in tenant.check_offline(repo))
