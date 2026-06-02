"""Guard the non-.py files that must ship inside the mcpbrain wheel.

packages.find ships only .py modules, so package data (the onboarding wizard
HTML and the example OAuth client) needs an explicit package-data declaration in
pyproject.toml. This test documents that contract and asserts the source files
sit at their expected package-relative locations. It is offline and builds
nothing. The CI wheel-inspection step is what catches an actual wheel-exclusion;
this pairs with it as a cheap, fast guard.
"""

from pathlib import Path

import mcpbrain


def _pkg_dir() -> Path:
    return Path(mcpbrain.__file__).parent


def test_wizard_html_present():
    html = _pkg_dir() / "wizard" / "index.html"
    assert html.is_file(), f"wizard HTML missing at {html}"


def test_oauth_client_example_present():
    example = _pkg_dir() / "google_oauth_client.json.example"
    assert example.is_file(), f"example OAuth client missing at {example}"
