"""Tests for config.is_configured() — the enrichment gate predicate."""
import json
from pathlib import Path

from mcpbrain.config import is_configured


def _home(tmp_path: Path, data: dict) -> str:
    (tmp_path / "config.json").write_text(json.dumps(data))
    return str(tmp_path)


def test_empty_config_is_not_configured(tmp_path):
    assert is_configured(_home(tmp_path, {})) is False


def test_identity_without_org_is_not_configured(tmp_path):
    home = _home(tmp_path, {"owner_name": "Sam", "owner_email": "sam@x.org"})
    assert is_configured(home) is False


def test_org_without_identity_is_not_configured(tmp_path):
    home = _home(tmp_path, {"orgs": [{"name": "Org", "domains": ["x.org"]}]})
    assert is_configured(home) is False


def test_identity_and_org_is_configured(tmp_path):
    home = _home(tmp_path, {
        "owner_name": "Sam", "owner_email": "sam@x.org",
        "orgs": [{"name": "Org", "domains": ["x.org"]}],
    })
    assert is_configured(home) is True


def test_blank_identity_strings_are_not_configured(tmp_path):
    home = _home(tmp_path, {
        "owner_name": "  ", "owner_email": "",
        "orgs": [{"name": "Org"}],
    })
    assert is_configured(home) is False


def test_orgs_list_of_nameless_entries_is_not_configured(tmp_path):
    home = _home(tmp_path, {
        "owner_name": "Sam", "owner_email": "sam@x.org",
        "orgs": [{"domains": ["x.org"]}, {"name": ""}],
    })
    assert is_configured(home) is False
