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
        asserts_person_role=True, attribution_source="owner_statement",
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
    committed = rw.write_gardener_context(repo, "identity.md", content)
    assert committed is False


# Re-scoped guard: attribution is required ONLY when a person-role is asserted.

def test_preference_update_needs_no_attribution(tmp_path):
    """A non-role update (preferences) commits without any attribution_source."""
    repo = _fake_records_full(tmp_path)
    committed = rw.write_gardener_context(
        repo, "preferences.md", "# Preferences\n\n- Australian English, no em dashes.\n",
    )
    assert committed is True


def test_role_attribution_guard_rejects_self_sourced(tmp_path):
    repo = _fake_records_full(tmp_path)
    with pytest.raises(ValueError, match="not permitted"):
        rw.write_gardener_context(
            repo, "identity.md", "content",
            asserts_person_role=True, attribution_source="self_written",
        )


def test_role_attribution_guard_rejects_inferred(tmp_path):
    repo = _fake_records_full(tmp_path)
    with pytest.raises(ValueError, match="not permitted"):
        rw.write_gardener_context(
            repo, "identity.md", "content",
            asserts_person_role=True, attribution_source="inferred_from_context",
        )


def test_role_attribution_guard_rejects_missing_source_when_role_asserted(tmp_path):
    repo = _fake_records_full(tmp_path)
    with pytest.raises(ValueError, match="not permitted"):
        rw.write_gardener_context(
            repo, "identity.md", "content", asserts_person_role=True,
        )


def test_role_attribution_guard_accepts_owner_statement(tmp_path):
    repo = _fake_records_full(tmp_path)
    committed = rw.write_gardener_context(
        repo, "identity.md", "# Identity\n\nnew content\n",
        asserts_person_role=True, attribution_source="owner_statement",
    )
    assert committed is True


def test_role_attribution_guard_accepts_signature(tmp_path):
    repo = _fake_records_full(tmp_path)
    committed = rw.write_gardener_context(
        repo, "preferences.md", "# Preferences\n\nnew content\n",
        asserts_person_role=True, attribution_source="signature",
    )
    assert committed is True


def test_role_attribution_guard_accepts_owner_confirmation(tmp_path):
    repo = _fake_records_full(tmp_path)
    committed = rw.write_gardener_context(
        repo, "identity.md", "# Identity\n\nconfirmed content\n",
        asserts_person_role=True, attribution_source="owner_confirmation",
    )
    assert committed is True


def test_write_gardener_context_rejects_path_separator(tmp_path):
    repo = _fake_records_full(tmp_path)
    with pytest.raises(ValueError, match="basename"):
        rw.write_gardener_context(repo, "sub/identity.md", "content")


def test_write_gardener_context_rejects_missing_file(tmp_path):
    repo = _fake_records_full(tmp_path)
    with pytest.raises(FileNotFoundError):
        rw.write_gardener_context(repo, "nonexistent.md", "content")


# ---------------------------------------------------------------------------
# 2a: per-run change cap (deterministic backstop)
# ---------------------------------------------------------------------------

def test_change_cap_blocks_oversized_write(tmp_path):
    repo = _fake_records_full(tmp_path)
    big = "# Projects\n\n" + "\n".join(f"- line {i}" for i in range(50)) + "\n"
    with pytest.raises(ValueError, match="change cap exceeded"):
        rw.write_gardener_reference(repo, "projects.md", big, max_changed_lines=20)


def test_change_cap_allows_small_write(tmp_path):
    repo = _fake_records_full(tmp_path)
    base = (Path(repo) / "reference" / "projects.md").read_text()
    committed = rw.write_gardener_reference(
        repo, "projects.md", base + "\n## One new project\n- active\n",
        max_changed_lines=20,
    )
    assert committed is True


def test_change_cap_disabled_when_none(tmp_path):
    repo = _fake_records_full(tmp_path)
    big = "# Projects\n\n" + "\n".join(f"- line {i}" for i in range(50)) + "\n"
    assert rw.write_gardener_reference(repo, "projects.md", big, max_changed_lines=None) is True


def test_gardener_max_changed_lines_default(tmp_path):
    assert config.gardener_max_changed_lines(str(tmp_path)) == 20


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


# ---------------------------------------------------------------------------
# 2a: brain_gardener_apply MCP tool — wires the routine to the guarded writers
# ---------------------------------------------------------------------------

def _run(coro):
    import asyncio
    return asyncio.run(coro)


def _tool_with_home(tmp_path, monkeypatch):
    """Make the gardener_apply tool resolve to a fresh fake records repo."""
    from mcpbrain import mcp_server, config as _cfg
    _fake_records_full(tmp_path)  # creates <tmp_path>/records (== records_dir(tmp_path))
    monkeypatch.setattr(_cfg, "app_dir", lambda: tmp_path)
    return mcp_server.make_brain_gardener_apply()


def test_tool_reference_lane_applies(tmp_path, monkeypatch):
    tool = _tool_with_home(tmp_path, monkeypatch)
    out = _run(tool(lane="reference", filename="projects.md",
                    content="# Projects\n\n## New\n- active\n"))
    assert out == {"applied": True, "committed": True}


def test_tool_context_role_with_valid_source_applies(tmp_path, monkeypatch):
    tool = _tool_with_home(tmp_path, monkeypatch)
    out = _run(tool(lane="context", filename="identity.md",
                    content="# Identity\n\n**Role:** confirmed\n",
                    asserts_person_role=True, attribution_source="signature"))
    assert out["applied"] is True


def test_tool_context_role_bad_source_surfaces_error(tmp_path, monkeypatch):
    """Guard rejection comes back as a clean error dict, not an exception."""
    tool = _tool_with_home(tmp_path, monkeypatch)
    out = _run(tool(lane="context", filename="identity.md", content="x",
                    asserts_person_role=True, attribution_source="self_written"))
    assert out["applied"] is False
    assert "not permitted" in out["error"]


def test_tool_unknown_lane_errors(tmp_path, monkeypatch):
    tool = _tool_with_home(tmp_path, monkeypatch)
    out = _run(tool(lane="bogus", filename="projects.md", content="x"))
    assert out["applied"] is False
    assert "unknown lane" in out["error"]


def test_tool_change_cap_surfaces_error(tmp_path, monkeypatch):
    tool = _tool_with_home(tmp_path, monkeypatch)
    big = "# Projects\n\n" + "\n".join(f"- line {i}" for i in range(50)) + "\n"
    out = _run(tool(lane="reference", filename="projects.md", content=big))
    assert out["applied"] is False
    assert "change cap exceeded" in out["error"]
