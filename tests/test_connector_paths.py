"""Where the connector entry has to land, per OS and per Claude install shape."""
from pathlib import Path

from mcpbrain import connector

_MSIX_TAIL = Path("Packages") / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming" / "Claude"


def test_darwin_path(monkeypatch):
    monkeypatch.setattr(connector.sys, "platform", "darwin")
    monkeypatch.setattr(connector.os, "name", "posix")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/Users/x")))
    paths = connector.desktop_config_paths()
    assert paths == [Path("/Users/x/Library/Application Support/Claude/"
                          "claude_desktop_config.json")]


def test_windows_msix_package_dir_present_targets_msix_even_without_config_file(tmp_path):
    # The exact fresh-install bug: MSIX is installed (package dir exists) but
    # no config file has ever been written in either location yet.
    localappdata = tmp_path / "Local"
    (localappdata / "Packages" / "Claude_pzs8sxrjxfjjc").mkdir(parents=True)
    appdata = tmp_path / "Roaming"

    paths = connector._windows_desktop_paths(str(appdata), str(localappdata))

    assert paths == [localappdata / _MSIX_TAIL / "claude_desktop_config.json"]


def test_windows_writes_both_when_msix_present_and_plain_config_exists(tmp_path):
    localappdata = tmp_path / "Local"
    (localappdata / "Packages" / "Claude_pzs8sxrjxfjjc").mkdir(parents=True)
    appdata = tmp_path / "Roaming"
    (appdata / "Claude").mkdir(parents=True)
    (appdata / "Claude" / "claude_desktop_config.json").write_text("{}")

    paths = connector._windows_desktop_paths(str(appdata), str(localappdata))

    assert len(paths) == 2
    assert localappdata / _MSIX_TAIL / "claude_desktop_config.json" in paths
    assert appdata / "Claude" / "claude_desktop_config.json" in paths


def test_windows_falls_back_to_appdata_when_no_msix_package_dir(tmp_path):
    # No MSIX install on this machine at all (package dir absent) — target
    # the plain path regardless of whether its config file exists yet.
    appdata = tmp_path / "Roaming"
    localappdata = tmp_path / "Local"

    paths = connector._windows_desktop_paths(str(appdata), str(localappdata))

    assert paths == [appdata / "Claude" / "claude_desktop_config.json"]


def test_windows_msix_path_comes_first_when_both_present(tmp_path):
    localappdata = tmp_path / "Local"
    (localappdata / "Packages" / "Claude_pzs8sxrjxfjjc").mkdir(parents=True)
    appdata = tmp_path / "Roaming"
    (appdata / "Claude").mkdir(parents=True)
    (appdata / "Claude" / "claude_desktop_config.json").write_text("{}")

    paths = connector._windows_desktop_paths(str(appdata), str(localappdata))

    assert paths[0] == localappdata / _MSIX_TAIL / "claude_desktop_config.json"


def test_desktop_config_paths_dispatches_to_windows_helper(monkeypatch):
    monkeypatch.setattr(connector.sys, "platform", "win32")
    monkeypatch.setattr(connector.os, "name", "nt")
    monkeypatch.setenv("APPDATA", r"C:\Users\x\AppData\Roaming")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\x\AppData\Local")
    captured = {}

    def fake_windows_desktop_paths(appdata, localappdata, **kw):
        captured["args"] = (appdata, localappdata)
        return [Path("stub")]

    monkeypatch.setattr(connector, "_windows_desktop_paths", fake_windows_desktop_paths)

    result = connector.desktop_config_paths()

    assert result == [Path("stub")]
    assert captured["args"] == (r"C:\Users\x\AppData\Roaming", r"C:\Users\x\AppData\Local")


def test_code_config_path_honours_claude_config_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    assert connector.code_config_path() == tmp_path / "cfg" / ".claude.json"


def test_code_config_path_defaults_to_home(monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/Users/x")))
    assert connector.code_config_path() == Path("/Users/x/.claude.json")
