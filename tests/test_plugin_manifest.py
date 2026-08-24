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
    # no skills/commands/hooks register at all (this happened: it's why
    # nothing in the plugin was reachable). Guard both manifests.
    for rel in (".claude-plugin/plugin.json", ".claude-plugin/marketplace.json"):
        d = json.loads((_PLUGIN / rel).read_text())
        authors = ([p.get("author") for p in d.get("plugins", [])]
                   if "plugins" in d else [d.get("author")])
        for a in authors:
            assert isinstance(a, dict) and a.get("name"), \
                f"{rel}: author must be an object with a name, got {a!r}"


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

def test_no_toplevel_bin_dir():
    # claude.ai-hosted plugins may NOT ship a top-level bin/ directory: its
    # executables are added to PATH on the CLI but are invisible on the admin
    # approval surface, so the marketplace fails the ENTIRE plugin
    # ("Plugin contains a top-level bin/ directory"). Executable entry points must
    # be declared via hooks/commands/mcpServers instead. This guards against a
    # bin/ shim (or the removed bin/mcpbrain-monitor) creeping back in.
    assert not (_PLUGIN / "bin").exists(), \
        "plugin/bin/ must not exist — it fails claude.ai marketplace validation"

def test_no_mcpb_extension_dir():
    # The Desktop Extension (.mcpb) was REMOVED, deliberately. It registered a
    # server under the same name "mcpbrain" as the connector `mcpbrain setup`
    # writes into claude_desktop_config.json, WON that collision, and then
    # failed to launch: mcpb's `server.type: "uv"` drops the `--from mcpbrain
    # mcpbrain` argv, so Desktop ran a bare `uv mcp-server` ->
    # "unrecognized subcommand". Net effect was to shadow a working connector
    # with a broken one (observed live 2026-08-24). The manifest was also
    # unresolvable regardless: it lacked `--index`, and mcpbrain is not on PyPI.
    # Do not reintroduce this directory -- `mcpbrain setup` is the only
    # supported way the connector is registered.
    assert not (_PLUGIN / "mcpb").exists(), \
        "plugin/mcpb/ must not exist -- the .mcpb extension shadows the working connector"


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
