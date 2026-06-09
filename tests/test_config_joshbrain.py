"""Tests for records_dir() config helper (previously joshbrain_dir)."""
from mcpbrain import config


def test_records_dir_default(tmp_path):
    assert config.records_dir(str(tmp_path)).endswith("records")


def test_records_dir_configured(tmp_path):
    config.write_config(str(tmp_path), {"records_dir": "/custom/records"})
    assert config.records_dir(str(tmp_path)) == "/custom/records"
