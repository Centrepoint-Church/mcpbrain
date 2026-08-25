"""Registering the mcpbrain stdio MCP server with every Claude surface.

The brain is served by ``mcpbrain mcp-server``. Two different config files can
carry that registration, and which ones exist depends on the machine:

* ``claude_desktop_config.json`` — the chat surface. On Windows this file is
  MSIX-virtualised: the app reads
  ``%LOCALAPPDATA%\\Packages\\Claude_pzs8sxrjxfjjc\\LocalCache\\Roaming\\Claude\\``
  while ``%APPDATA%\\Claude\\`` (the documented path, and the only one mcpbrain
  wrote before this module) is silently ignored.
* ``~/.claude.json`` — Claude Code's own config, user scope, which loads in every
  project. Not owned by the Desktop app, so it does not exhibit the
  clobber-on-quit behaviour the chat config does.

Both surfaces are in use, so registration writes to every config file present
rather than adjudicating between them. Every write merges into existing content
and is atomic; a file that will not parse is left untouched and reported.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# The MSIX package family Claude Desktop ships under. Its LocalCache\Roaming
# subtree is what the containerised app actually sees as %APPDATA%.
_MSIX_RELATIVE = Path("Packages") / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming" / "Claude"

_CONFIG_NAME = "claude_desktop_config.json"

# Capture the native Path type at module import time, before monkeypatching
_NATIVE_PATH_TYPE = type(Path("."))  # Will be PosixPath on Unix, WindowsPath on Windows


def desktop_config_paths() -> list[Path]:
    """Every chat-surface config file this machine could be reading, best first.

    Windows returns the MSIX-virtualised path ahead of ``%APPDATA%`` when it
    exists, and returns BOTH when both exist: a machine can carry an MSIX install
    and a non-MSIX one, the two are not reliably distinguishable from here, and
    writing the same entry twice is idempotent and cheap. When neither exists we
    return the ``%APPDATA%`` path so a first-time write has a destination.
    """
    if sys.platform == "darwin":
        return [Path.home() / "Library" / "Application Support" / "Claude" / _CONFIG_NAME]
    if os.name == "nt":
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        localappdata = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        # Build paths as strings to avoid Path type issues when os.name is monkeypatched
        # Derive MSIX path from _MSIX_RELATIVE to keep a single source of truth
        msix_relative_str = "/".join(_MSIX_RELATIVE.parts)
        msix_str = f"{localappdata}/{msix_relative_str}/{_CONFIG_NAME}"
        plain_str = f"{appdata}/Claude/{_CONFIG_NAME}"
        # Check existence and convert to native path type
        found = [_NATIVE_PATH_TYPE(p_str) for p_str in (msix_str, plain_str) if os.path.exists(p_str)]
        return found or [_NATIVE_PATH_TYPE(plain_str)]
    return [Path.home() / ".config" / "Claude" / _CONFIG_NAME]


def code_config_path() -> Path:
    """Claude Code's config file, honouring ``CLAUDE_CONFIG_DIR`` when set."""
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else Path.home()) / ".claude.json"


import json


def server_entry(mcpbrain_bin: str, *, typed: bool) -> dict:
    """The stdio server entry to register.

    ``typed`` selects the shape the target file already uses: Claude Code writes
    ``type``/``env`` into ~/.claude.json, while claude_desktop_config.json has
    carried the bare command/args form since mcpbrain first wrote it. Matching
    each file's existing convention keeps diffs minimal and avoids asking either
    reader to accept a shape it does not already produce itself.
    """
    if typed:
        return {"type": "stdio", "command": mcpbrain_bin, "args": ["mcp-server"], "env": {}}
    return {"command": mcpbrain_bin, "args": ["mcp-server"]}


def merge_server_into(path: Path, entry: dict, *, create: bool) -> tuple[bool, str]:
    """Merge ``entry`` in as ``mcpServers.mcpbrain``. Returns (wrote, detail).

    Never destructive. The file is parsed first and left byte-identical if it does
    not parse, or if its top level is not an object — both are states where a
    wholesale write would discard something we cannot interpret, and both have
    been observed in the wild. The write itself is tempfile + os.replace so an
    interrupted run cannot truncate a config, and the temp file is removed if the
    replace fails.

    ``create=False`` skips a file that does not exist, which is how a machine with
    only one of the two surfaces avoids gaining an empty config for the other.
    """
    if not path.exists():
        if not create:
            return False, f"not present: {path}"
        data: dict = {}
    else:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            return False, f"could not parse {path} ({exc}); left unchanged"
        if not isinstance(data, dict):
            return False, f"{path} is not a JSON object; left unchanged"

    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    if servers.get("mcpbrain") == entry and path.exists():
        return True, f"already registered in {path}"
    servers["mcpbrain"] = entry
    data["mcpServers"] = servers

    tmp = path.with_suffix(path.suffix + ".mcpbrain.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        os.replace(tmp, path)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        return False, f"could not write {path} ({exc})"
    return True, f"registered in {path}"
