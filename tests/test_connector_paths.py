"""Where the connector entry has to land, per OS and per Claude install shape."""
from pathlib import Path

import pytest

from mcpbrain import connector

_MSIX_TAIL = Path("Packages") / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming" / "Claude"


def test_darwin_path(monkeypatch):
    monkeypatch.setattr(connector.sys, "platform", "darwin")
    monkeypatch.setattr(connector.os, "name", "posix")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/Users/x")))
    paths = connector.desktop_config_paths()
    assert paths == [Path("/Users/x/Library/Application Support/Claude/"
                          "claude_desktop_config.json")]


def test_windows_msix_path_comes_first_when_it_exists(monkeypatch, tmp_path):
    # MSIX virtualises %APPDATA%\Claude to %LOCALAPPDATA%\Packages\...\Roaming\Claude.
    # The app reads the virtualised copy; a write to %APPDATA% is silently ignored.
    appdata, localappdata = tmp_path / "Roaming", tmp_path / "Local"
    msix = localappdata / _MSIX_TAIL
    msix.mkdir(parents=True)
    (msix / "claude_desktop_config.json").write_text("{}")
    monkeypatch.setattr(connector.sys, "platform", "win32")
    monkeypatch.setattr(connector.os, "name", "nt")
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("LOCALAPPDATA", str(localappdata))

    paths = connector.desktop_config_paths()
    assert paths[0] == msix / "claude_desktop_config.json"


def test_windows_writes_both_when_both_exist(monkeypatch, tmp_path):
    appdata, localappdata = tmp_path / "Roaming", tmp_path / "Local"
    msix = localappdata / _MSIX_TAIL
    msix.mkdir(parents=True)
    (msix / "claude_desktop_config.json").write_text("{}")
    (appdata / "Claude").mkdir(parents=True)
    (appdata / "Claude" / "claude_desktop_config.json").write_text("{}")
    monkeypatch.setattr(connector.sys, "platform", "win32")
    monkeypatch.setattr(connector.os, "name", "nt")
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("LOCALAPPDATA", str(localappdata))

    paths = connector.desktop_config_paths()
    assert len(paths) == 2
    assert msix / "claude_desktop_config.json" in paths
    assert appdata / "Claude" / "claude_desktop_config.json" in paths


def test_windows_falls_back_to_appdata_when_no_msix(monkeypatch, tmp_path):
    monkeypatch.setattr(connector.sys, "platform", "win32")
    monkeypatch.setattr(connector.os, "name", "nt")
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    paths = connector.desktop_config_paths()
    assert paths == [tmp_path / "Roaming" / "Claude" / "claude_desktop_config.json"]


def test_code_config_path_honours_claude_config_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    assert connector.code_config_path() == tmp_path / "cfg" / ".claude.json"


def test_code_config_path_defaults_to_home(monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/Users/x")))
    assert connector.code_config_path() == Path("/Users/x/.claude.json")
