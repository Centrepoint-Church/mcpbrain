"""bin/release.py must not be able to ship a tenant-less wheel.

A wheel missing the OAuth client is a silent, fleet-wide auth outage: every install
picks it up on the next daily auto-update and consent simply stops working."""
import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent


def _load_release():
    spec = importlib.util.spec_from_file_location("_release", _ROOT / "bin" / "release.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _wheel(tmp_path, *, tenant=True, client=True) -> Path:
    w = tmp_path / "mcpbrain-9.9.9-py3-none-any.whl"
    with zipfile.ZipFile(w, "w") as z:
        z.writestr("mcpbrain/__init__.py", "__version__ = '9.9.9'\n")
        if tenant:
            z.writestr("mcpbrain/tenant.json", json.dumps({"tenant_id": "acme"}))
        if client:
            z.writestr("mcpbrain/google_oauth_client.json",
                       json.dumps({"installed": {"client_id": "123-abc.apps.googleusercontent.com"}}))
    return w


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "mcpbrain").mkdir(parents=True)
    (r / "mcpbrain" / "google_oauth_client.json").write_text(
        json.dumps({"installed": {"client_id": "123-abc.apps.googleusercontent.com"}}))
    return r


def test_a_complete_wheel_verifies(tmp_path, repo):
    assert _load_release().verify_wheel(_wheel(tmp_path), repo) == []


def test_a_wheel_without_the_oauth_client_fails(tmp_path, repo):
    problems = _load_release().verify_wheel(_wheel(tmp_path, client=False), repo)
    assert any("google_oauth_client.json" in p for p in problems)


def test_a_wheel_without_the_tenant_profile_fails(tmp_path, repo):
    problems = _load_release().verify_wheel(_wheel(tmp_path, tenant=False), repo)
    assert any("tenant.json" in p for p in problems)


def test_a_wheel_carrying_a_different_client_than_the_tree_fails(tmp_path, repo):
    """Guards the stale-wheel gotcha: release.py globs dist/ and an older wheel may
    predate the profile entirely, so verification must target THIS build."""
    w = tmp_path / "mcpbrain-9.9.9-py3-none-any.whl"
    with zipfile.ZipFile(w, "w") as z:
        z.writestr("mcpbrain/tenant.json", "{}")
        z.writestr("mcpbrain/google_oauth_client.json",
                   json.dumps({"installed": {"client_id": "999-stale.apps.googleusercontent.com"}}))
    assert any("client_id" in p for p in _load_release().verify_wheel(w, repo))
