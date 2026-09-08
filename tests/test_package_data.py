"""Guard the non-.py files that must ship inside the mcpbrain wheel.

packages.find ships only .py modules, so package data (the onboarding wizard
HTML and the example OAuth client) needs an explicit package-data declaration in
pyproject.toml. This test documents that contract and asserts the source files
sit at their expected package-relative locations. It is offline and builds
nothing. The CI wheel-inspection step is what catches an actual wheel-exclusion;
this pairs with it as a cheap, fast guard.
"""

from pathlib import Path

import pytest

import mcpbrain


def _pkg_dir() -> Path:
    return Path(mcpbrain.__file__).parent


def test_wizard_html_present():
    html = _pkg_dir() / "wizard" / "index.html"
    assert html.is_file(), f"wizard HTML missing at {html}"


def test_oauth_client_example_present():
    example = _pkg_dir() / "google_oauth_client.json.example"
    assert example.is_file(), f"example OAuth client missing at {example}"


def test_shared_oauth_client_present():
    # The shared desktop client is no longer committed to source (it is
    # tenant-specific and gitignored) — it lands in the tree only after
    # `python bin/tenant.py use <tenant-dir>` stages it ahead of a build, and
    # bin/release.py::verify_wheel enforces its presence in the built wheel.
    # A fresh clone/fork has no client file yet, so skip rather than fail.
    client = _pkg_dir() / "google_oauth_client.json"
    if not client.is_file():
        pytest.skip(
            "OAuth client is no longer committed; run "
            "`python bin/tenant.py use <tenant-dir>` before a build. "
            "Wheel presence is enforced by bin/release.py::verify_wheel."
        )
    assert client.is_file()


def test_enrich_prompt_doc_present():
    # The extractor prompt is the canonical source mcp_server._enrich_rules()
    # reads (via Path(__file__).with_name) to serve the brain_enrich_pull MCP
    # tool. packages.find skips .md, so without the package-data declaration a
    # clean install has no prompt to serve.
    prompt = _pkg_dir() / "enrich_prompt.md"
    assert prompt.is_file(), f"extractor prompt missing at {prompt}"


def test_dashboard_html_present():
    html = _pkg_dir() / "wizard" / "dashboard.html"
    assert html.is_file(), f"dashboard HTML missing at {html}"


def test_records_claude_template_present():
    # The records-repo CLAUDE.md template is copied into a freshly-scaffolded
    # records repo. packages.find skips .md, so without the package-data
    # declaration a clean install has no template to seed the working space.
    tmpl = _pkg_dir() / "records_templates" / "CLAUDE.md"
    assert tmpl.is_file(), f"records CLAUDE.md template missing at {tmpl}"


def test_maintenance_subpackage_excluded_from_wheel():
    # mcpbrain/maintenance/ holds maintainer-only local backfill/cleanup
    # tooling (graph_cleanup, salience_backfill). It is reachable only from
    # tests, never from the shipped daemon, so pyproject's packages.find must
    # exclude it. Guard that exclusion is declared (offline, builds nothing —
    # pairs with the CI wheel-inspection step).
    import tomllib

    repo_root = Path(__file__).resolve().parents[1]
    # The subpackage exists in the source tree (importable from a checkout)...
    assert (repo_root / "mcpbrain" / "maintenance" / "__init__.py").is_file()
    # ...but pyproject's packages.find excludes it from the published wheel.
    with open(repo_root / "pyproject.toml", "rb") as f:
        cfg = tomllib.load(f)
    exclude = cfg["tool"]["setuptools"]["packages"]["find"].get("exclude", [])
    assert "mcpbrain.maintenance*" in exclude, (
        f"mcpbrain.maintenance* must be in packages.find exclude; got: {exclude}"
    )
