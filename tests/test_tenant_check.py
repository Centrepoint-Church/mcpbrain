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
    (r / "pyproject.toml").write_text('[project]\nversion = "0.0.0"\n')
    (r / "mcpbrain" / "__init__.py").write_text('__version__ = "0.0.0"\n')
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


class _FakeFiles:
    def __init__(self, table): self._t = table
    def get(self, *, fileId, fields, supportsAllDrives):
        class _Req:
            def __init__(self, val): self._v = val
            def execute(self):
                if isinstance(self._v, Exception):
                    raise self._v
                return self._v
        return _Req(self._t.get(fileId, KeyError(fileId)))


class _FakeDrive:
    def __init__(self, table): self._f = _FakeFiles(table)
    def files(self): return self._f


_FOLDER = {"mimeType": "application/vnd.google-apps.folder",
           "driveId": "0ABC", "capabilities": {"canAddChildren": True}}


def test_online_passes_when_folders_and_index_are_good(tmp_path):
    prof = tenant.load_dict(_profile())
    drive = _FakeDrive({"FLEET1": _FOLDER, "ESCROW1": _FOLDER})
    def fetch(url):
        return '<a href="mcpbrain-0.1.0-py3-none-any.whl">x</a>' if "mcpbrain" in url else "<a href=\"mcpbrain/\">mcpbrain</a>"
    assert tenant.check_online(prof, drive=drive, fetch=fetch,
                               repo_probe=lambda o, r: True)[0] == []


def test_a_folder_id_that_is_not_a_folder_is_reported(tmp_path):
    prof = tenant.load_dict(_profile())
    drive = _FakeDrive({"FLEET1": {"mimeType": "application/pdf", "driveId": "0ABC",
                                   "capabilities": {"canAddChildren": False}},
                        "ESCROW1": _FOLDER})
    problems, _ = tenant.check_online(prof, drive=drive, fetch=lambda u: "mcpbrain",
                                      repo_probe=lambda o, r: True)
    assert any("fleet_folder_id" in p and "folder" in p for p in problems)


def test_a_folder_on_my_drive_not_a_shared_drive_is_reported(tmp_path):
    """drive.file cannot write to My Drive, so this fails backups silently later."""
    prof = tenant.load_dict(_profile())
    mydrive = {"mimeType": "application/vnd.google-apps.folder",
               "capabilities": {"canAddChildren": True}}      # no driveId
    drive = _FakeDrive({"FLEET1": _FOLDER, "ESCROW1": mydrive})
    problems, _ = tenant.check_online(prof, drive=drive, fetch=lambda u: "mcpbrain",
                                      repo_probe=lambda o, r: True)
    assert any("escrow_folder_id" in p and "Shared Drive" in p for p in problems)


def test_an_index_that_does_not_list_mcpbrain_is_reported(tmp_path):
    prof = tenant.load_dict(_profile())
    drive = _FakeDrive({"FLEET1": _FOLDER, "ESCROW1": _FOLDER})
    problems, _ = tenant.check_online(prof, drive=drive, fetch=lambda u: "<html></html>",
                                      repo_probe=lambda o, r: True)
    assert any("index_url" in p for p in problems)


def test_blank_optional_fields_are_skipped_not_failed(tmp_path):
    """A tenant that runs without fleet or backup is a valid tenant.

    fetch is a no-op stub, not None: marketplace_owner/marketplace_repo are
    REQUIRED fields (never blank), so check_online's marketplace-reachability
    check always runs regardless of the optional fields under test here — passing
    fetch=None would fall back to _default_fetch and hit the real network for
    Acme-Org/mcpbrain-plugin (a fictional repo, reliably 404), which is exactly
    what an injectable fetch exists to avoid in a test.
    """
    prof = tenant.load_dict(_profile(fleet_folder_id="", escrow_folder_id="",
                                     index_url=""))
    assert tenant.check_online(prof, drive=None, fetch=lambda u: "ok",
                               repo_probe=lambda o, r: True)[0] == []


# --- marketplace reachability: a private repo must not be a permanent failure ---

def test_an_unreachable_marketplace_is_a_note_not_a_failure():
    """mcpbrain-plugin is PRIVATE by design, so an unauthenticated fetch always
    404s. Treating that as a failure made `check --online` exit 1 for the normal,
    correct configuration — and a check that is always red is one people learn to
    ignore. Found live on 2026-09-09 against the real Centrepoint profile.
    """
    prof = tenant.load_dict(_profile())
    drive = _FakeDrive({"FLEET1": _FOLDER, "ESCROW1": _FOLDER})

    def fetch(url):
        if "github.com" in url:
            raise Exception("HTTP Error 404: Not Found")
        return "mcpbrain"

    problems, notes = tenant.check_online(prof, drive=drive, fetch=fetch,
                                          repo_probe=lambda o, r: None)
    assert problems == [], f"a private-repo 404 must not fail the check: {problems}"
    assert any("marketplace" in n for n in notes), "but it must still be reported"


def test_a_marketplace_repo_that_definitively_does_not_exist_is_a_failure():
    """When an authenticated probe can tell the difference, a missing repo is a
    real problem — a fork that typo'd marketplace_owner should hear about it."""
    prof = tenant.load_dict(_profile())
    drive = _FakeDrive({"FLEET1": _FOLDER, "ESCROW1": _FOLDER})
    problems, _ = tenant.check_online(prof, drive=drive, fetch=lambda u: "mcpbrain",
                                      repo_probe=lambda o, r: False)
    assert any("marketplace" in p for p in problems)


def test_a_confirmed_marketplace_repo_is_silent():
    prof = tenant.load_dict(_profile())
    drive = _FakeDrive({"FLEET1": _FOLDER, "ESCROW1": _FOLDER})
    problems, notes = tenant.check_online(prof, drive=drive, fetch=lambda u: "mcpbrain",
                                          repo_probe=lambda o, r: True)
    assert problems == [] and notes == []


def test_no_online_test_falls_through_to_the_real_gh(monkeypatch):
    """repo_probe defaults to shelling out to `gh`. Every test above injects it;
    this pins that, because a default that reaches the network turns a unit suite
    into a flaky integration suite (two tests here did exactly that before the
    injection was added)."""
    def _explode(*a, **k):
        raise AssertionError("a test reached the real gh probe")
    monkeypatch.setattr(tenant, "_gh_repo_probe", _explode)
    prof = tenant.load_dict(_profile())
    problems, notes = tenant.check_online(
        prof, drive=_FakeDrive({"FLEET1": _FOLDER, "ESCROW1": _FOLDER}),
        fetch=lambda u: "mcpbrain", repo_probe=lambda o, r: True)
    assert problems == [] and notes == []


# --- the gold eval set is tenant data, not product ---

def test_use_copies_the_gold_set_into_tests_eval(tenant_repo, fake_repo):
    """tests/eval/golden_retrieval_set*.yaml held REAL Centrepoint content in a
    PUBLIC repo — named staff tied to employment agreements, an EOY review, and
    WWCC/Safer-Churches training status. The literal guard could never catch it:
    the spec deliberately excludes tests/ as "fixtures and history", which is
    right for a slugify assertion and wrong for a curated corpus.

    A gold set is tenant data by definition — its chunk ids point at one
    organisation's own store, so a fork cannot use another's anyway. It now
    travels the same path as the OAuth client: private source, copied in, ignored.
    """
    (tenant_repo / "eval").mkdir()
    (tenant_repo / "eval" / "golden_retrieval_set.yaml").write_text("- id: x\n")
    cli = _load_cli()
    cli.use_profile(tenant_repo, fake_repo)
    dest = fake_repo / "tests" / "eval" / "golden_retrieval_set.yaml"
    assert dest.exists(), "gold set must land where load_gold_cases() looks"
    assert dest.read_text() == "- id: x\n"


def test_use_creates_missing_destination_directories(tenant_repo, fake_repo):
    """fake_repo has no tests/eval/; a fresh clone has no tests/eval/gold either.
    copy2 into a missing parent raises, so `use` must create the tree."""
    (tenant_repo / "eval").mkdir()
    (tenant_repo / "eval" / "golden_retrieval_set_mcpbrain_candidate.yaml").write_text("- id: y\n")
    cli = _load_cli()
    cli.use_profile(tenant_repo, fake_repo)
    assert (fake_repo / "tests" / "eval" /
            "golden_retrieval_set_mcpbrain_candidate.yaml").exists()


def test_use_succeeds_when_the_tenant_repo_has_no_gold_set(tenant_repo, fake_repo):
    """The gold set is OPTIONAL — a fork with no curated cases still gets a
    working build, and load_gold_cases() already returns [] so the floor test
    skips honestly rather than failing."""
    cli = _load_cli()
    written = cli.use_profile(tenant_repo, fake_repo)
    assert (fake_repo / "mcpbrain" / "google_oauth_client.json").exists()
    assert not any("golden_retrieval_set" in str(p) for p in written)


# --- a minimal fork should need ONE repo, not four ---

def test_marketplace_fields_are_optional(tmp_path):
    """A fork that distributes nothing needs no marketplace repo.

    index_url was already optional (blank => no auto-update). Requiring the three
    marketplace_* fields forced an org that wants none of it to invent values and
    create a repo, when `mcpbrain setup` registers the MCP connector — the actual
    brain — with no marketplace involved. Minimum viable fork: their own source
    fork plus a private home for the OAuth client.
    """
    prof = tenant.load_dict(_profile(marketplace_owner="", marketplace_repo="",
                                     marketplace_name=""))
    assert prof.marketplace_owner is None
    assert prof.marketplace_slug is None
    assert prof.plugin_homepage is None


def test_install_surface_is_not_checked_when_no_marketplace_is_configured(tmp_path):
    """With no marketplace there is nothing for the install docs to agree WITH,
    so the consistency check must skip rather than invent a failure."""
    prof = _profile(marketplace_owner="", marketplace_repo="", marketplace_name="")
    repo = _repo_with(tmp_path, profile=prof, client=_client())
    assert tenant.check_offline(repo) == []


def test_a_partially_configured_marketplace_is_still_an_error(tmp_path):
    """All three or none. An owner with no repo is a typo, not a choice."""
    repo = _repo_with(tmp_path, profile=_profile(marketplace_repo=""),
                      client=_client())
    problems = tenant.check_offline(repo)
    assert any("marketplace" in p.lower() for p in problems), problems
