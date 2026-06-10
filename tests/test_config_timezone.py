import json
from mcpbrain import config

def _home(tmp_path, data):
    (tmp_path / "config.json").write_text(json.dumps(data)); return str(tmp_path)

def test_user_timezone_empty_when_unset(tmp_path):
    assert config.user_timezone(_home(tmp_path, {})) == ""

def test_user_timezone_returns_configured(tmp_path):
    assert config.user_timezone(_home(tmp_path, {"timezone": "America/New_York"})) == "America/New_York"
