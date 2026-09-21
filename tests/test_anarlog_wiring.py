from pathlib import Path
from mcpbrain import config


def test_db_path_defaults_to_the_anarlog_location(tmp_path, monkeypatch):
    anar = tmp_path / "Library" / "Application Support" / "anarlog"
    anar.mkdir(parents=True)
    (anar / "app.db").write_text("")
    mcphome = tmp_path / "mcpbrain-home"
    mcphome.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    assert config.anarlog_db_path(str(mcphome)) == str(anar / "app.db")


def test_db_path_is_none_when_absent(tmp_path, monkeypatch):
    mcphome = tmp_path / "mcpbrain-home"
    mcphome.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    assert config.anarlog_db_path(str(mcphome)) is None


def test_explicit_config_override_wins(tmp_path):
    mcphome = tmp_path / "mcpbrain-home"
    mcphome.mkdir()
    db = tmp_path / "elsewhere.db"
    db.write_text("")
    (mcphome / "config.json").write_text(
        '{"anarlog": {"db_path": "%s"}}' % db)
    assert config.anarlog_db_path(str(mcphome)) == str(db)


def test_sync_module_exposes_the_source():
    from mcpbrain import sync
    assert hasattr(sync, "discover_anarlog")
    assert hasattr(sync, "handle_anarlog_item")
