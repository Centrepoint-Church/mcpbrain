"""config.records_dir() — new key, default."""
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


def test_empty_config_returns_default(tmp_path):
    assert config.records_dir(str(tmp_path)) == str(tmp_path / "records")
