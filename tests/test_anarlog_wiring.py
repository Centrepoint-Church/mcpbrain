from pathlib import Path
from mcpbrain import config


def test_disabled_by_default_even_when_the_default_path_exists(tmp_path, monkeypatch):
    """Hermetic property: with `anarlog.enabled` absent, the function must not
    even LOOK at Path.home() -- this test asserts None using the REAL
    Path.home() (no monkeypatch), so it proves the opt-in gate short-circuits
    before any filesystem probe outside `home`, regardless of whether anarlog
    is actually installed on the machine running it."""
    mcphome = tmp_path / "mcpbrain-home"
    mcphome.mkdir()
    assert config.anarlog_db_path(str(mcphome)) is None


def test_enabled_defaults_to_the_anarlog_location(tmp_path, monkeypatch):
    anar = tmp_path / "Library" / "Application Support" / "anarlog"
    anar.mkdir(parents=True)
    (anar / "app.db").write_text("")
    mcphome = tmp_path / "mcpbrain-home"
    mcphome.mkdir()
    (mcphome / "config.json").write_text('{"anarlog": {"enabled": true}}')
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    assert config.anarlog_db_path(str(mcphome)) == str(anar / "app.db")


def test_enabled_but_no_db_anywhere_is_none(tmp_path, monkeypatch):
    mcphome = tmp_path / "mcpbrain-home"
    mcphome.mkdir()
    (mcphome / "config.json").write_text('{"anarlog": {"enabled": true}}')
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    assert config.anarlog_db_path(str(mcphome)) is None


def test_enabled_with_explicit_config_override_wins(tmp_path):
    mcphome = tmp_path / "mcpbrain-home"
    mcphome.mkdir()
    db = tmp_path / "elsewhere.db"
    db.write_text("")
    (mcphome / "config.json").write_text(
        '{"anarlog": {"enabled": true, "db_path": "%s"}}' % db)
    assert config.anarlog_db_path(str(mcphome)) == str(db)


def test_enabled_with_explicit_override_missing_is_none(tmp_path):
    mcphome = tmp_path / "mcpbrain-home"
    mcphome.mkdir()
    missing = tmp_path / "nowhere.db"
    (mcphome / "config.json").write_text(
        '{"anarlog": {"enabled": true, "db_path": "%s"}}' % missing)
    assert config.anarlog_db_path(str(mcphome)) is None


def test_sync_module_exposes_the_source():
    from mcpbrain import sync
    assert hasattr(sync, "discover_anarlog")
    assert hasattr(sync, "handle_anarlog_item")
