"""draft._load_voice_rules reads the records repo's context/voice.md."""
import json

from mcpbrain import draft


def _home(tmp_path, data):
    (tmp_path / "config.json").write_text(json.dumps(data))
    return str(tmp_path)


def test_reads_voice_from_records_dir(tmp_path):
    home = _home(tmp_path, {"records_dir": str(tmp_path / "r")})
    (tmp_path / "r" / "context").mkdir(parents=True)
    (tmp_path / "r" / "context" / "voice.md").write_text("be warm and direct")
    assert draft._load_voice_rules(home) == "be warm and direct"


def test_missing_voice_returns_empty(tmp_path):
    home = _home(tmp_path, {})
    assert draft._load_voice_rules(home) == ""
