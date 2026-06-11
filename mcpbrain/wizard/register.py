"""Register the mcpbrain MCP server into a Claude Desktop config.

Claude Desktop spawns MCP servers from a JSON config with an ``mcpServers``
map. This helper merges the mcpbrain entry into that config idempotently,
preserving any other servers and refusing to clobber a malformed file.
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

_CONFIG_FILENAME = "claude_desktop_config.json"


def claude_desktop_config_path(platform=None) -> Path:
    """Return the per-OS path to the Claude Desktop config file."""
    platform = platform if platform is not None else sys.platform
    if platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "Claude"
            / _CONFIG_FILENAME
        )
    if platform == "win32":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base / "Claude" / _CONFIG_FILENAME
    return Path.home() / ".config" / "Claude" / _CONFIG_FILENAME


def register_mcpbrain(
    *,
    mcpbrain_home,
    mcpbrain_bin=None,
    config_path=None,
    server_name="mcpbrain",
) -> Path:
    """Merge the mcpbrain server entry into the Claude Desktop config.

    Points Claude Desktop at the installed ``mcpbrain`` console command
    (``mcpbrain mcp-server``), not a ``python -m`` invocation. Idempotent:
    re-running with the same args yields byte-identical output. Re-runnable:
    changed args update the entry in place. Other servers are preserved. A
    malformed existing config raises ValueError rather than being overwritten.

    config_path defaults to claude_desktop_config_path(); pass it explicitly to
    target a specific file (used by tests). mcpbrain_bin defaults to
    shutil.which("mcpbrain"); a clear error is raised if it cannot be resolved,
    so a broken ``"command": null`` entry is never written.
    """
    if mcpbrain_bin is not None:
        bin_path = str(mcpbrain_bin)
    else:
        # Prefer the binary actually running (correct in the `uv tool install` flow);
        # fall back to a PATH lookup. Avoids registering a different env's mcpbrain.
        _self = Path(sys.argv[0])
        bin_path = str(_self) if _self.name == "mcpbrain" and _self.exists() else shutil.which("mcpbrain")
    if not bin_path:
        raise RuntimeError(
            "Could not find the 'mcpbrain' executable on PATH. Install it "
            "(`uv tool install --from . mcpbrain`) or pass mcpbrain_bin explicitly."
        )
    config_path = Path(config_path) if config_path is not None else Path(claude_desktop_config_path())

    entry = {
        "command": bin_path,
        "args": ["mcp-server"],
        "env": {
            "MCPBRAIN_HOME": str(mcpbrain_home),
        },
    }

    config = {}
    if config_path.exists():
        raw = config_path.read_text()
        if raw.strip():
            try:
                config = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Existing Claude Desktop config at {config_path} is not "
                    f"valid JSON; refusing to overwrite it: {exc}"
                ) from exc
            if not isinstance(config, dict):
                raise ValueError(
                    f"Existing Claude Desktop config at {config_path} is not a JSON object "
                    f"(got {type(config).__name__}); refusing to overwrite it."
                )

    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    servers[server_name] = entry
    config["mcpServers"] = servers

    data = json.dumps(config, indent=2) + "\n"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    # Create the temp file 0600-from-creation (mkstemp default) in the same
    # directory so the atomic rename never crosses a device boundary and the
    # file is never briefly world-readable under a permissive umask.
    fd, tmp = tempfile.mkstemp(dir=str(config_path.parent), prefix=config_path.name + ".", suffix=".tmp")
    try:
        if hasattr(os, "fchmod"):  # POSIX-only; mkstemp is already owner-only on Windows
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(data)
        os.replace(tmp, config_path)  # atomic rename on POSIX; same-dir avoids cross-device failure
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return config_path


def main(argv=None):
    import argparse
    from mcpbrain.config import app_dir
    ap = argparse.ArgumentParser(prog="mcpbrain register")
    ap.add_argument("--home", default=None, help="MCPBRAIN_HOME (default: app_dir())")
    ap.add_argument("--mcpbrain-bin", default=None, help="path to the mcpbrain executable")
    args = ap.parse_args(argv)
    home = args.home if args.home is not None else str(app_dir())
    path = register_mcpbrain(mcpbrain_home=home, mcpbrain_bin=args.mcpbrain_bin)
    print(f"registered mcpbrain in {path}")
    return 0
