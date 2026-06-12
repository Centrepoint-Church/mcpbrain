import json
from mcpbrain.daemon import _cadences_from_config

def test_communities_runs_on_fresh_config(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({}))
    c = _cadences_from_config(str(tmp_path))
    assert c["communities_interval_s"] is not None and c["communities_interval_s"] > 0

def test_communities_explicit_zero_disables(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"cadences": {"communities_interval_s": 0}}))
    assert _cadences_from_config(str(tmp_path))["communities_interval_s"] is None
