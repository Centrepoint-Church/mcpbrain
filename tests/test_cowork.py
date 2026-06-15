"""Tests for cowork cadence prompts and the headless-claude runner."""
from pathlib import Path

import mcpbrain
from mcpbrain import cowork


def _cowork_dir():
    return Path(mcpbrain.__file__).parent / "cowork"


def test_prompts_are_shipped():
    for name in ("memory-gardener.md", "meeting-packs.md"):
        assert (_cowork_dir() / name).exists()


def test_prompts_are_generic():
    for name in ("memory-gardener.md", "meeting-packs.md"):
        text = (_cowork_dir() / name).read_text().lower()
        assert "joshbrain" not in text
        assert "centrepoint" not in text
        assert "josh" not in text


def test_run_cowork_builds_claude_command(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    monkeypatch.setattr("mcpbrain.config.find_claude", lambda: "/usr/bin/claude")
    monkeypatch.setattr(cowork, "_mcpbrain_bin", lambda: "/usr/bin/mcpbrain")
    seen = {}

    class _R:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, *, input=None, capture_output=None, text=None, timeout=None, cwd=None):
        seen.update(cmd=cmd, input=input, cwd=cwd, timeout=timeout)
        return _R()

    monkeypatch.setattr(cowork.subprocess, "run", fake_run)
    rc = cowork.run_cowork("memory-gardener.md", tools="Bash,Read,Edit,Write",
                           extra_context="CTX", log_name="memory_gardener.log",
                           cwd=str(tmp_path / "records"), timeout=120)
    assert rc == 0
    assert seen["cmd"][0] == "/usr/bin/claude" and "-p" in seen["cmd"]
    assert "Bash,Read,Edit,Write" in seen["cmd"]
    assert "--dangerously-skip-permissions" in seen["cmd"]
    assert any("/usr/bin/mcpbrain" in c and "mcp-server" in c for c in seen["cmd"])
    assert "CTX" in seen["input"] and seen["input"].startswith("#")
    assert seen["cwd"] == str(tmp_path / "records")
    assert (tmp_path / "logs" / "memory_gardener.log").exists()


def test_meeting_packs_skips_when_daemon_down(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))  # no control_port/token
    called = {"n": 0}
    monkeypatch.setattr(cowork, "run_cowork", lambda *a, **k: called.__setitem__("n", 1))
    assert cowork.meeting_packs_main([]) == 0
    assert called["n"] == 0  # never invoked claude
