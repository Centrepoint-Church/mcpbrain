import json
from mcpbrain import config, fleet

def _write(tmp_path, obj):
    (tmp_path / "config.json").write_text(json.dumps(obj))

def test_fleet_flag_org_overlay_wins(tmp_path):
    _write(tmp_path, {"retrieval_expand": False,
                      "org_config": {"flags": {"retrieval_expand": True}}})
    assert config.fleet_flag(str(tmp_path), "retrieval_expand", False) is True

def test_fleet_flag_top_level_fallback(tmp_path):
    _write(tmp_path, {"retrieval_expand": True})
    assert config.fleet_flag(str(tmp_path), "retrieval_expand", False) is True

def test_fleet_flag_default(tmp_path):
    _write(tmp_path, {})
    assert config.fleet_flag(str(tmp_path), "retrieval_expand", False) is False

def test_retrieval_expand_enabled_delegates_to_fleet_flag(tmp_path):
    _write(tmp_path, {"org_config": {"flags": {"retrieval_expand": True}}})
    assert config.retrieval_expand_enabled(str(tmp_path)) is True

def test_flags_is_allowlisted():
    assert "flags" in fleet._ALLOWLIST
