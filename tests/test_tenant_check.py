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
