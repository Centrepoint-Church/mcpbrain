import json
from pathlib import Path
_PLUGIN = Path(__file__).parent.parent / "plugin"

def test_plugin_json_valid():
    d = json.loads((_PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    assert d.get("name") == "mcpbrain"
    assert d.get("version") and d.get("description") and d.get("author")


def test_plugin_author_is_object_not_string():
    # The manifest schema requires `author` to be an OBJECT ({name, email?}). A
    # bare string fails validation and Claude Code rejects the ENTIRE plugin —
    # no skills/commands/hooks/monitors register at all (this happened: it's why
    # nothing in the plugin was reachable). Guard both manifests.
    for rel in (".claude-plugin/plugin.json", ".claude-plugin/marketplace.json"):
        d = json.loads((_PLUGIN / rel).read_text())
        authors = ([p.get("author") for p in d.get("plugins", [])]
                   if "plugins" in d else [d.get("author")])
        for a in authors:
            assert isinstance(a, dict) and a.get("name"), \
                f"{rel}: author must be an object with a name, got {a!r}"


def test_monitors_json_is_a_bare_array():
    # The monitors manifest must be a top-level ARRAY of monitor objects, not an
    # object wrapper ({"monitors": [...]}) — the wrapper fails component-load
    # validation ("expected array, received object").
    d = json.loads((_PLUGIN / "monitors" / "monitors.json").read_text())
    assert isinstance(d, list) and d and isinstance(d[0], dict) and d[0].get("command")

def test_marketplace_lists_plugin():
    d = json.loads((_PLUGIN / ".claude-plugin" / "marketplace.json").read_text())
    assert "mcpbrain" in [p.get("name") for p in d["plugins"]]

def test_mcp_json_bundles_no_server():
    # The mcpbrain MCP server is registered by `mcpbrain setup` at user scope
    # (absolute path, cross-platform). The plugin must NOT also bundle it, or a
    # duplicate "mcpbrain" server would be defined — and the bundled one can't
    # branch per-OS so it would fail under the minimal login PATH on macOS.
    d = json.loads((_PLUGIN / ".mcp.json").read_text())
    assert d.get("mcpServers") == {}

import subprocess
import os
import stat

def test_shims_executable():
    for name in ("mcpbrain-mcp", "mcpbrain-monitor"):
        shim = _PLUGIN / "bin" / name
        assert shim.exists() and (stat.S_IMODE(shim.stat().st_mode) & 0o111)

def test_mcp_shim_execs_mcp_server(tmp_path):
    fake = tmp_path / "mcpbrain"; fake.write_text('#!/bin/sh\necho "ARGS:$*"\n'); fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ.get('PATH','')}",
           "MCPBRAIN_HOME": str(tmp_path / ".mcpbrain")}
    r = subprocess.run(["/bin/sh", str(_PLUGIN / "bin" / "mcpbrain-mcp")], env=env,
                       capture_output=True, text=True, timeout=5)
    assert "mcp-server" in r.stdout

def test_mcp_shim_does_not_inject_dotmcpbrain_home(tmp_path):
    # Regression: the shim must NOT default MCPBRAIN_HOME to ~/.mcpbrain. On macOS
    # the daemon (and brain) live under ~/Library/Application Support/mcpbrain, so a
    # ~/.mcpbrain default pointed the MCP server at an empty store. With the env
    # unset, the shim must leave it unset and let mcpbrain resolve its own home.
    fake = tmp_path / "mcpbrain"
    fake.write_text('#!/bin/sh\necho "HOME_ENV:${MCPBRAIN_HOME:-UNSET}"\n'); fake.chmod(0o755)
    env = {"PATH": f"{tmp_path}:{os.environ.get('PATH','')}", "HOME": str(tmp_path)}
    r = subprocess.run(["/bin/sh", str(_PLUGIN / "bin" / "mcpbrain-mcp")], env=env,
                       capture_output=True, text=True, timeout=5)
    assert "HOME_ENV:UNSET" in r.stdout
    assert str(tmp_path / ".mcpbrain") not in r.stdout

def test_mcp_shim_honours_explicit_home(tmp_path):
    fake = tmp_path / "mcpbrain"
    fake.write_text('#!/bin/sh\necho "HOME_ENV:${MCPBRAIN_HOME:-UNSET}"\n'); fake.chmod(0o755)
    explicit = str(tmp_path / "custom-home")
    env = {"PATH": f"{tmp_path}:{os.environ.get('PATH','')}", "HOME": str(tmp_path),
           "MCPBRAIN_HOME": explicit}
    r = subprocess.run(["/bin/sh", str(_PLUGIN / "bin" / "mcpbrain-mcp")], env=env,
                       capture_output=True, text=True, timeout=5)
    assert f"HOME_ENV:{explicit}" in r.stdout

def test_monitor_shim_execs_monitor(tmp_path):
    fake = tmp_path / "mcpbrain"; fake.write_text('#!/bin/sh\necho "ARGS:$*"\n'); fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ.get('PATH','')}",
           "MCPBRAIN_HOME": str(tmp_path / ".mcpbrain")}
    r = subprocess.run(["/bin/sh", str(_PLUGIN / "bin" / "mcpbrain-monitor")], env=env,
                       capture_output=True, text=True, timeout=5)
    assert "monitor" in r.stdout

def test_mcp_shim_errors_when_binary_absent(tmp_path):
    env = {**os.environ, "PATH": str(tmp_path), "HOME": str(tmp_path),
           "MCPBRAIN_HOME": str(tmp_path / ".mcpbrain")}
    r = subprocess.run(["/bin/sh", str(_PLUGIN / "bin" / "mcpbrain-mcp")], env=env,
                       capture_output=True, text=True, timeout=5)
    assert r.returncode != 0 and "mcpbrain" in (r.stderr + r.stdout).lower()

def test_hooks_json_declares_both_events():
    d = json.loads((_PLUGIN / "hooks" / "hooks.json").read_text())
    assert {"SessionStart", "SessionEnd"} <= set(d["hooks"].keys())

def test_hooks_commands_reference_mcpbrain():
    d = json.loads((_PLUGIN / "hooks" / "hooks.json").read_text())
    def cmds(ev):
        return [h.get("command","") for blk in d["hooks"].get(ev, [])
                for h in blk.get("hooks", [])]
    assert any("session-start" in c for c in cmds("SessionStart"))
    assert any("session-end" in c for c in cmds("SessionEnd"))

def test_monitors_json_points_at_shim():
    d = json.loads((_PLUGIN / "monitors" / "monitors.json").read_text())
    assert isinstance(d, list) and len(d) >= 1
    assert "${CLAUDE_PLUGIN_ROOT}/bin/mcpbrain-monitor" in d[0]["command"]
