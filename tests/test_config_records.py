"""config.records_dir() — new key, legacy joshbrain_dir fallback, default."""
import json
from pathlib import Path

from mcpbrain import config


def _home(tmp_path: Path, data: dict) -> str:
    (tmp_path / "config.json").write_text(json.dumps(data))
    return str(tmp_path)


def test_default_is_records_under_home(tmp_path):
    assert config.records_dir(_home(tmp_path, {})) == str(tmp_path / "records")


def test_explicit_records_dir_key(tmp_path):
    assert config.records_dir(_home(tmp_path, {"records_dir": "/x/y"})) == "/x/y"


def test_legacy_joshbrain_dir_key_still_honored(tmp_path):
    assert config.records_dir(_home(tmp_path, {"joshbrain_dir": "/old/jb"})) == "/old/jb"


def test_records_dir_key_wins_over_legacy(tmp_path):
    home = _home(tmp_path, {"records_dir": "/new", "joshbrain_dir": "/old"})
    assert config.records_dir(home) == "/new"


def test_joshbrain_dir_alias_matches_records_dir(tmp_path):
    home = _home(tmp_path, {})
    assert config.joshbrain_dir(home) == config.records_dir(home)
