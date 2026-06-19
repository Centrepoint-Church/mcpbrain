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
