"""Tests for joshbrain_dir() config helper."""
from mcpbrain import config


def test_joshbrain_dir_default(tmp_path):
    assert config.joshbrain_dir(str(tmp_path)).endswith("records")


def test_joshbrain_dir_configured(tmp_path):
    config.write_config(str(tmp_path), {"joshbrain_dir": "/custom/jb"})
    assert config.joshbrain_dir(str(tmp_path)) == "/custom/jb"
