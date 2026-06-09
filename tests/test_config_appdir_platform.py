"""app_dir() picks the OS path via sys.platform and never calls os.uname()."""
from pathlib import Path

from mcpbrain import config


def test_darwin_branch_without_os_uname(tmp_path, monkeypatch):
    # Simulate macOS, and DELETE os.uname to prove app_dir() does not use it
    # (os.uname does not exist on Windows; relying on it is the bug we're fixing).
    monkeypatch.delenv("MCPBRAIN_HOME", raising=False)
    monkeypatch.setattr(config.os, "name", "posix")
    monkeypatch.setattr(config.sys, "platform", "darwin")
    monkeypatch.delattr(config.os, "uname", raising=False)
    monkeypatch.setattr(config.Path, "home", classmethod(lambda cls: tmp_path))
    d = config.app_dir()
    assert d == tmp_path / "Library" / "Application Support" / "mcpbrain"


def test_linux_branch(tmp_path, monkeypatch):
    monkeypatch.delenv("MCPBRAIN_HOME", raising=False)
    monkeypatch.setattr(config.os, "name", "posix")
    monkeypatch.setattr(config.sys, "platform", "linux")
    monkeypatch.setattr(config.Path, "home", classmethod(lambda cls: tmp_path))
    assert config.app_dir() == tmp_path / ".mcpbrain"
