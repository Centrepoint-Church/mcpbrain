import json
import os
import stat

from mcpbrain import config
from mcpbrain.config import read_config, write_config


def test_app_dir_is_absolute_and_created(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path / "data"))
    d = config.app_dir()
    assert d.is_absolute()
    assert d.exists()


def test_store_path_under_app_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path / "data"))
    assert config.store_path().parent == config.app_dir()
    assert config.store_path().name == "brain.sqlite3"


def test_spool_thread_cap_default_and_override(tmp_path):
    home = str(tmp_path)
    assert config.spool_thread_cap(home) == 2000           # default when unset
    write_config(home, {"spool_thread_cap": 800})
    assert config.spool_thread_cap(home) == 800            # explicit override
    write_config(home, {"spool_thread_cap": "garbage"})
    assert config.spool_thread_cap(home) == 2000           # invalid -> default
    write_config(home, {"spool_thread_cap": 0})
    assert config.spool_thread_cap(home) == 1              # floored at 1


def test_recall_excludes_cold_defaults_off(tmp_path):
    # The salience gate is an enrichment-cost optimization, not a retrieval filter:
    # cold chunks stay searchable by default. Excluding them from recall halved gold
    # recall (0.75->0.35) once the salience backfill grew the cold set, so this is
    # decoupled from tiered_memory and defaults OFF.
    home = str(tmp_path)
    assert config.recall_excludes_cold(home) is False          # default: cold stays in recall
    write_config(home, {"recall_excludes_cold": True})
    assert config.recall_excludes_cold(home) is True           # explicit opt-in


def test_write_is_0600_and_roundtrips(tmp_path):
    write_config(str(tmp_path), {"gemini_key": "k", "backup": {"shared_drive_id": "d"}})
    p = tmp_path / "config.json"
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    cfg = read_config(str(tmp_path))
    assert cfg["gemini_key"] == "k" and cfg["backup"]["shared_drive_id"] == "d"


def test_read_missing_returns_empty(tmp_path):
    assert read_config(str(tmp_path)) == {}


def test_read_corrupt_config_returns_empty(tmp_path):
    (tmp_path / "config.json").write_text("{ not valid json }")
    assert read_config(str(tmp_path)) == {}


def test_spool_home_default_is_app_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain.config import spool_home, app_dir
    assert spool_home() == app_dir()


def test_spool_home_override(tmp_path):
    from mcpbrain.config import spool_home
    assert spool_home(str(tmp_path / "x")) == (tmp_path / "x")


def test_reextract_enabled_default_and_pause(tmp_path):
    import json
    from mcpbrain.config import reextract_enabled
    # default True when key absent
    (tmp_path / "config.json").write_text(json.dumps({"owner_name": "A"}))
    assert reextract_enabled(str(tmp_path)) is True
    # explicit pause
    (tmp_path / "config.json").write_text(json.dumps({"reextract": False}))
    assert reextract_enabled(str(tmp_path)) is False


def test_review_max_apply_per_run_default_and_override(tmp_path):
    home = str(tmp_path)
    assert config.review_max_apply_per_run(home) == 50          # default when unset
    write_config(home, {"review_max_apply_per_run": 100})
    assert config.review_max_apply_per_run(home) == 100         # explicit override
    write_config(home, {"review_max_apply_per_run": "garbage"})
    assert config.review_max_apply_per_run(home) == 50          # invalid -> default
    write_config(home, {"review_max_apply_per_run": 0})
    assert config.review_max_apply_per_run(home) == 1           # floored at 1


def test_meeting_series_enabled_default_true(tmp_path):
    assert config.meeting_series_enabled(str(tmp_path)) is True


def test_topic_consolidation_enabled_default_true(tmp_path):
    assert config.topic_consolidation_enabled(str(tmp_path)) is True


def test_meeting_series_can_be_disabled(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"meeting_series_enabled": False}))
    assert config.meeting_series_enabled(str(tmp_path)) is False


def test_topic_consolidation_can_be_disabled(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"topic_consolidation_enabled": False}))
    assert config.topic_consolidation_enabled(str(tmp_path)) is False


def test_write_config_without_fchmod(tmp_path, monkeypatch):
    """On platforms lacking os.fchmod (Windows), write_config must still persist."""
    monkeypatch.delattr(os, "fchmod", raising=False)
    config.write_config(str(tmp_path), {"owner_full_name": "Nakia Busby"})
    got = config.read_config(str(tmp_path))
    assert got["owner_full_name"] == "Nakia Busby"


def test_embed_skip_tabular_defaults_off(tmp_path):
    assert config.embed_skip_tabular_enabled(str(tmp_path)) is False


def test_importance_weights_defaults_and_override(tmp_path):
    """Defaults re-measured 2026-10-14 (see config.importance_weights); the
    config.json override path must keep working."""
    w = config.importance_weights(str(tmp_path))
    assert w == {"recency_weight": 0.05, "importance_weight": 0.15,
                 "decay_weight": 0.0, "recency_alpha": 0.01}
    write_config(str(tmp_path), {"importance_weights": {"recency_weight": 0.2,
                                                        "decay_weight": 0.1}})
    w = config.importance_weights(str(tmp_path))
    assert w["recency_weight"] == 0.2 and w["decay_weight"] == 0.1
    assert w["importance_weight"] == 0.15


def test_importance_weights_warns_once_on_a_no_op_decay_weight(tmp_path, monkeypatch, caplog):
    """decay_weight does nothing until a real decay factor reaches ranking; a
    configured value must say so — once, not on every recall."""
    import logging
    monkeypatch.setattr(config, "_DECAY_WEIGHT_WARNED", False)
    write_config(str(tmp_path), {"importance_weights": {"decay_weight": 0.1}})
    with caplog.at_level(logging.WARNING, logger=config.log.name):
        config.importance_weights(str(tmp_path))
        config.importance_weights(str(tmp_path))
    hits = [r for r in caplog.records if "decay_weight has no effect" in r.getMessage()]
    assert len(hits) == 1


def test_importance_weights_no_warning_at_default(tmp_path, monkeypatch, caplog):
    import logging
    monkeypatch.setattr(config, "_DECAY_WEIGHT_WARNED", False)
    with caplog.at_level(logging.WARNING, logger=config.log.name):
        config.importance_weights(str(tmp_path))
    assert not [r for r in caplog.records if "decay_weight" in r.getMessage()]
