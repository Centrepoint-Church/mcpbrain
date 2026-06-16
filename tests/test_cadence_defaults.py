import json
from mcpbrain.daemon import _cadences_from_config


def test_empty_config_yields_defaults(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({}))
    r = _cadences_from_config(str(tmp_path))
    assert r["communities_interval_s"] == 86400.0
    assert r["blocks_interval_s"] == 86400.0
    assert r["proactive_interval_s"] == 86400.0
    assert r["waiting_on_interval_s"] == 86400.0
    assert r["lint_interval_s"] == 86400.0
    assert r["stale_reextract_interval_s"] == 86400.0
    assert r["synthesise_interval_s"] == 604800.0
    assert r["audit_interval_s"] == 604800.0
    assert r["verify_interval_s"] == 3600.0
    assert r["auto_update_interval_s"] == 86400.0


def test_missing_config_file_yields_defaults(tmp_path):
    r = _cadences_from_config(str(tmp_path))
    assert r["communities_interval_s"] == 86400.0


def test_explicit_override_wins(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"cadences": {"communities_interval_s": 7200}}))
    r = _cadences_from_config(str(tmp_path))
    assert r["communities_interval_s"] == 7200.0
    assert r["proactive_interval_s"] == 86400.0


def test_explicit_zero_disables_pass(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"cadences": {"lint_interval_s": 0}}))
    assert _cadences_from_config(str(tmp_path))["lint_interval_s"] is None
