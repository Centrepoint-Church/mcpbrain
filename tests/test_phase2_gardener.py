"""Phase 2 gardener auto-apply, org-context scaffold, and weekly digest tests."""
import subprocess
from pathlib import Path

import pytest

from mcpbrain import config, records, records_write as rw


def _fake_records_full(tmp_path):
    """Records repo with full template scaffold (context/ + reference/ files)."""
    repo = str(tmp_path / "records")
    records.ensure_records_repo(
        repo, git_name="t", git_email="t@t",
        profile={"owner_full_name": "Test User", "owner_role": "Tester",
                 "orgs": [{"name": "TestOrg"}]},
    )
    return repo


# ---------------------------------------------------------------------------
# 2a: config flag
# ---------------------------------------------------------------------------

def test_gardener_auto_apply_disabled_default(tmp_path):
    assert config.gardener_auto_apply_enabled(str(tmp_path)) is False


def test_gardener_auto_apply_enabled_via_config(tmp_path):
    config.write_config(str(tmp_path), {"gardener_auto_apply": True})
    assert config.gardener_auto_apply_enabled(str(tmp_path)) is True


# ---------------------------------------------------------------------------
# 2a: write_gardener_reference — drift lane
# ---------------------------------------------------------------------------

def test_write_gardener_reference_commits_drift_tag(tmp_path):
    repo = _fake_records_full(tmp_path)
    committed = rw.write_gardener_reference(
        repo, "projects.md", "# Projects\n\n## New project\n- Status: active\n"
    )
    assert committed is True
    log = subprocess.run(
        ["git", "-C", repo, "log", "--oneline", "-1"],
        capture_output=True, text=True,
    ).stdout
    assert "gardener: apply drift (reference/projects.md)" in log


def test_write_gardener_reference_noop_on_same_content(tmp_path):
    repo = _fake_records_full(tmp_path)
    content = (Path(repo) / "reference" / "projects.md").read_text()
    committed = rw.write_gardener_reference(repo, "projects.md", content)
    assert committed is False


def test_write_gardener_reference_rejects_path_separator(tmp_path):
    repo = _fake_records_full(tmp_path)
    with pytest.raises(ValueError, match="basename"):
        rw.write_gardener_reference(repo, "sub/projects.md", "content")


def test_write_gardener_reference_rejects_missing_file(tmp_path):
    repo = _fake_records_full(tmp_path)
    with pytest.raises(FileNotFoundError):
        rw.write_gardener_reference(repo, "nonexistent.md", "content")


def test_write_gardener_reference_ensures_trailing_newline(tmp_path):
    repo = _fake_records_full(tmp_path)
    rw.write_gardener_reference(repo, "projects.md", "# Projects\n\ncontent")
    assert (Path(repo) / "reference" / "projects.md").read_text().endswith("\n")


# ---------------------------------------------------------------------------
# 2a: write_gardener_context — constitution lane + role-attribution guard
# ---------------------------------------------------------------------------

def test_write_gardener_context_commits_constitution_tag(tmp_path):
    repo = _fake_records_full(tmp_path)
    committed = rw.write_gardener_context(
        repo, "identity.md",
        "# Identity\n\n**Name:** Test User\n**Role:** Tester\n",
        attribution_source="owner_statement",
    )
    assert committed is True
    log = subprocess.run(
        ["git", "-C", repo, "log", "--oneline", "-1"],
        capture_output=True, text=True,
    ).stdout
    assert "gardener: update identity/preferences" in log


def test_write_gardener_context_noop_on_same_content(tmp_path):
    repo = _fake_records_full(tmp_path)
    content = (Path(repo) / "context" / "identity.md").read_text()
    committed = rw.write_gardener_context(
        repo, "identity.md", content, attribution_source="owner_statement"
    )
    assert committed is False


def test_role_attribution_guard_rejects_self_sourced(tmp_path):
    repo = _fake_records_full(tmp_path)
    with pytest.raises(ValueError, match="not permitted"):
        rw.write_gardener_context(
            repo, "identity.md", "content", attribution_source="self_written"
        )


def test_role_attribution_guard_rejects_inferred(tmp_path):
    repo = _fake_records_full(tmp_path)
    with pytest.raises(ValueError, match="not permitted"):
        rw.write_gardener_context(
            repo, "identity.md", "content", attribution_source="inferred_from_context"
        )


def test_role_attribution_guard_accepts_owner_statement(tmp_path):
    repo = _fake_records_full(tmp_path)
    committed = rw.write_gardener_context(
        repo, "identity.md", "# Identity\n\nnew content\n",
        attribution_source="owner_statement",
    )
    assert committed is True


def test_role_attribution_guard_accepts_signature(tmp_path):
    repo = _fake_records_full(tmp_path)
    committed = rw.write_gardener_context(
        repo, "preferences.md", "# Preferences\n\nnew content\n",
        attribution_source="signature",
    )
    assert committed is True


def test_role_attribution_guard_accepts_owner_confirmation(tmp_path):
    repo = _fake_records_full(tmp_path)
    committed = rw.write_gardener_context(
        repo, "identity.md", "# Identity\n\nconfirmed content\n",
        attribution_source="owner_confirmation",
    )
    assert committed is True


def test_write_gardener_context_rejects_path_separator(tmp_path):
    repo = _fake_records_full(tmp_path)
    with pytest.raises(ValueError, match="basename"):
        rw.write_gardener_context(
            repo, "sub/identity.md", "content", attribution_source="owner_statement"
        )


def test_write_gardener_context_rejects_missing_file(tmp_path):
    repo = _fake_records_full(tmp_path)
    with pytest.raises(FileNotFoundError):
        rw.write_gardener_context(
            repo, "nonexistent.md", "content", attribution_source="owner_statement"
        )


# ---------------------------------------------------------------------------
# 2b: org-context.md scaffolded by ensure_records_repo
# ---------------------------------------------------------------------------

def test_org_context_scaffolded_with_profile(tmp_path):
    repo = _fake_records_full(tmp_path)
    assert (Path(repo) / "reference" / "org-context.md").exists()


def test_org_context_not_clobbered_on_re_ensure(tmp_path):
    repo = _fake_records_full(tmp_path)
    org_ctx = Path(repo) / "reference" / "org-context.md"
    org_ctx.write_text("# Custom org content\n")
    # Force re-stamp by clearing the cache
    records._ENSURED.discard(str(Path(repo).resolve()))
    records.ensure_records_repo(
        repo, git_name="t", git_email="t@t",
        profile={"owner_full_name": "Test User", "owner_role": "Tester",
                 "orgs": [{"name": "TestOrg"}]},
    )
    assert org_ctx.read_text() == "# Custom org content\n"


def test_org_context_contains_org_routing_rules(tmp_path):
    repo = _fake_records_full(tmp_path)
    content = (Path(repo) / "reference" / "org-context.md").read_text()
    assert "TestOrg" in content
