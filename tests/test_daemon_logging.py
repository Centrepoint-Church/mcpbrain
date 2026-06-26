"""Daemon logging: rotating file on Windows; stdout-only on macOS."""
import logging

from mcpbrain import daemon, config


def test_windows_logging_attaches_file_handler(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon.sys, "platform", "win32")
    monkeypatch.setattr(daemon.config, "app_dir", lambda: tmp_path)
    root = logging.getLogger("mcpbrain.test-isolated-win")
    root.handlers.clear()
    daemon._configure_logging(root)
    paths = [getattr(h, "baseFilename", "") for h in root.handlers]
    assert any(str(tmp_path / "com.mcpbrain.log") == p for p in paths)


def test_non_windows_logging_no_file_handler(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon.sys, "platform", "darwin")
    root = logging.getLogger("mcpbrain.test-isolated-mac")
    root.handlers.clear()
    daemon._configure_logging(root)
    assert not any(getattr(h, "baseFilename", None) for h in root.handlers)
