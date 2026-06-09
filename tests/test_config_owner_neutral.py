"""Owner identity helpers default to empty (neutral), never Josh."""
import json
from pathlib import Path

from mcpbrain.config import (
    owner_name, owner_full_name, owner_role, owner_email, owner_aliases,
)


def _home(tmp_path: Path, data: dict) -> str:
    (tmp_path / "config.json").write_text(json.dumps(data))
    return str(tmp_path)


def test_owner_defaults_are_empty(tmp_path):
    home = _home(tmp_path, {})
    assert owner_name(home) == ""
    assert owner_full_name(home) == ""
    assert owner_role(home) == ""
    assert owner_email(home) == ""


def test_owner_aliases_empty_when_unconfigured(tmp_path):
    home = _home(tmp_path, {})
    assert owner_aliases(home) == frozenset()


def test_owner_values_read_from_config(tmp_path):
    home = _home(tmp_path, {
        "owner_name": "Sam", "owner_full_name": "Sam Jones",
        "owner_role": "office manager", "owner_email": "sam@x.org",
    })
    assert owner_name(home) == "Sam"
    assert owner_full_name(home) == "Sam Jones"
    assert owner_role(home) == "office manager"
    assert owner_email(home) == "sam@x.org"
    assert owner_aliases(home) == frozenset({"sam", "sam jones"})
