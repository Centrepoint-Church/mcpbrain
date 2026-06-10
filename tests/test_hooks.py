"""Install SessionStart/SessionEnd hooks into ~/.claude/settings.json, mergefully."""
import json
import os
from pathlib import Path

import pytest

from mcpbrain import hooks


def _settings_path(tmp_path):
    return tmp_path / ".claude" / "settings.json"


def test_install_creates_both_hooks(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    p = hooks.install_session_hooks()
    data = json.loads(p.read_text())
    cmds = [h["command"] for grp in data["hooks"].values() for blk in grp for h in blk["hooks"]]
    assert any("session-start" in c for c in cmds)
    assert any("session-end" in c for c in cmds)
    assert hooks.hooks_status()["installed"] is True
    assert (os.stat(p).st_mode & 0o777) == 0o600


def test_install_is_idempotent_and_preserves_existing(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    sp = _settings_path(tmp_path)
    sp.parent.mkdir(parents=True)
    sp.write_text(json.dumps({"env": {"FOO": "bar"},
                              "hooks": {"SessionStart": [{"hooks": [{"type": "command",
                                        "command": "/usr/local/bin/other"}]}]}}))
    hooks.install_session_hooks()
    hooks.install_session_hooks()  # twice -> no duplicate
    data = json.loads(sp.read_text())
    assert data["env"] == {"FOO": "bar"}                       # preserved
    starts = [h["command"] for blk in data["hooks"]["SessionStart"] for h in blk["hooks"]]
    assert "/usr/local/bin/other" in starts                    # preserved
    assert sum("session-start" in c for c in starts) == 1      # no duplicate


def test_install_refuses_malformed(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    sp = _settings_path(tmp_path)
    sp.parent.mkdir(parents=True)
    sp.write_text("{not json")
    with pytest.raises(ValueError):
        hooks.install_session_hooks()


def test_uninstall_is_noop_when_absent(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    p = hooks.uninstall_session_hooks()  # must not raise
    assert not p.exists()


def test_uninstall_removes_only_ours(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    sp = _settings_path(tmp_path)
    sp.parent.mkdir(parents=True)
    sp.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [{"type": "command",
                              "command": "/usr/local/bin/other"}]}]}}))
    hooks.install_session_hooks()
    hooks.uninstall_session_hooks()
    data = json.loads(sp.read_text())
    starts = [h["command"] for blk in data["hooks"].get("SessionStart", []) for h in blk["hooks"]]
    assert "/usr/local/bin/other" in starts
    assert not any("session-start" in c for c in starts)
    assert hooks.hooks_status()["installed"] is False
