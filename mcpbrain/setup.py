"""``mcpbrain setup``: open the browser wizard, starting the daemon if needed.

This is the last step of the installers. By the time it runs, ``mcpbrain`` is
on PATH and the login agent has been registered. ``setup`` makes sure the
daemon is actually running (its control API serves the wizard), reads the
control port the daemon wrote, and opens ``http://127.0.0.1:<port>/`` in a
browser. On a headless box it prints the URL so the user can copy it across.

``--dry-run`` prints what it would do without starting anything or opening a
browser.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

from mcpbrain.config import app_dir

# How long to wait for the daemon to write control_port after we start the agent.
_PORT_WAIT_SECONDS = 30
_POLL_INTERVAL = 0.5

# Conventional port reported in dry-run when no daemon has written one yet.
_DRY_RUN_PORT = 53999


def _platform() -> str:
    p = sys.platform
    if p.startswith("linux"):
        return "linux"
    if p == "darwin":
        return "darwin"
    if p in ("win32", "cygwin"):
        return "win32"
    return p


def _mcpbrain_bin() -> str:
    found = shutil.which("mcpbrain") or sys.argv[0] or "mcpbrain"
    # Resolve to an absolute path: agent registration (launchd/schtasks) and the
    # MCP registration below both run later under a minimal login PATH, so a bare
    # name like "mcpbrain" would not resolve.
    p = Path(found)
    if p.exists():
        return str(p.resolve())
    return found


def _desktop_config_path() -> Path:
    """Path to the Claude **Desktop** MCP config for this OS.

    Claude Desktop — where the plugin runs and staff do their work — reads its
    MCP servers from this file, *not* from Claude Code's ``~/.claude.json``.
    """
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "Claude" / "claude_desktop_config.json"
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def _register_desktop_mcp(*, dry_run: bool = False) -> None:
    """Connect the brain to **Claude Desktop** by writing its MCP config.

    The brain is served through its stdio MCP server, ``mcpbrain mcp-server``.
    Setup writes the entry directly into ``claude_desktop_config.json`` using the
    *absolute* path to the installed binary — which only setup knows. A plain
    JSON edit with an absolute command is fully cross-platform (no shell/PATH/
    extension problem), and targets Claude Desktop rather than Claude Code. The
    plugin's own ``.mcp.json`` deliberately bundles no server. Best-effort: a
    write failure must never block onboarding.

    Merges into any existing config, preserving other servers; idempotent.
    """
    cfg = _desktop_config_path()
    entry = {"command": _mcpbrain_bin(), "args": ["mcp-server"]}
    if dry_run:
        print(f"would connect mcpbrain to Claude Desktop at {cfg}: {json.dumps(entry)}")
        return
    try:
        data = json.loads(cfg.read_text()) if cfg.exists() else {}
        if not isinstance(data, dict):
            data = {}
        servers = data.get("mcpServers")
        if not isinstance(servers, dict):
            servers = {}
            data["mcpServers"] = servers
        servers["mcpbrain"] = entry
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps(data, indent=2) + "\n")
        print(f"Wrote the mcpbrain MCP server to Claude Desktop's config:\n  {cfg}\n"
              "IMPORTANT: fully QUIT and REOPEN Claude Desktop to load the brain_* tools.\n"
              "Claude Desktop owns this file and overwrites edits made while it's running,\n"
              "so for a reliable result: quit Claude Desktop, run `mcpbrain connect` in a\n"
              "terminal, then reopen Claude Desktop.")
    except OSError as exc:
        print(f"Could not write the Claude Desktop MCP config ({exc}). Add this to "
              f"{cfg} under \"mcpServers\":\n  \"mcpbrain\": {json.dumps(entry)}",
              file=sys.stderr)


def _install_tray_best_effort(home: str) -> None:
    """Register the menu-bar tray login agent. Never fatal.

    The tray's GUI deps ship with the package (main dependencies), so the only
    thing that can fail here is registering the OS login agent on a machine
    without a desktop session (e.g. a headless server). That must not block
    onboarding, so a failure logs a hint and carries on.
    """
    from mcpbrain import agents
    try:
        agents.install_tray_agent(_platform(), mcpbrain_bin=_mcpbrain_bin(), home=home)
        print("Menu-bar tray installed; it appears at your next login (or run 'mcpbrain tray').")
    except Exception as exc:  # noqa: BLE001 - the tray is optional
        print(
            f"Skipped the menu-bar tray ({exc}). It is optional; the daemon runs without it. "
            f"On a desktop machine, run 'mcpbrain tray' to enable it.",
            file=sys.stderr,
        )


def _start_tray_now(home: str) -> None:
    """Launch the tray immediately so it appears without waiting for next login.
    Best-effort — the login agent still starts it at next login regardless."""
    kw = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.Popen([_mcpbrain_bin(), "tray"], **kw)
        print("Menu-bar tray started.")
    except Exception as exc:  # noqa: BLE001 — optional; never block onboarding
        print(f"Could not start the tray now ({exc}); it starts at next login.", file=sys.stderr)


def _read_port(home: str):
    """Return the int control port from <home>/control_port, or None if absent."""
    p = Path(home) / "control_port"
    if not p.exists():
        return None
    try:
        text = p.read_text().strip()
    except OSError:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _ensure_daemon_running(home: str, *, dry_run: bool = False) -> int:
    """Return the control port, starting the login agent first if needed.

    If the daemon already wrote a control_port we trust it and return that. The
    daemon is KeepAlive/Restart-managed by the OS agent, so a stale-but-present
    port file almost always means a live daemon. Otherwise we install + start
    the login agent for this platform, then poll for control_port to appear.

    In ``dry_run`` mode this never installs an agent or polls: it returns the
    existing control port if one is present, otherwise ``_DRY_RUN_PORT``.
    """
    existing = _read_port(home)
    if existing is not None:
        return existing

    if dry_run:
        # No port file and we're in dry-run: report the default without any
        # side effects (no agent install, no polling).
        return _DRY_RUN_PORT

    # No port file yet. Install and start the login agent so the daemon comes up.
    from mcpbrain import agents

    platform = _platform()
    mcpbrain_bin = _mcpbrain_bin()
    try:
        agents.install_agent(platform, mcpbrain_bin=mcpbrain_bin, home=home)
    except Exception as exc:  # noqa: BLE001 - degrade gracefully, the agent step is best-effort
        print(
            f"Could not start the mcpbrain login agent automatically ({exc}).\n"
            f"Start the daemon by hand with: {mcpbrain_bin} daemon",
            file=sys.stderr,
        )

    deadline = time.monotonic() + _PORT_WAIT_SECONDS
    while time.monotonic() < deadline:
        port = _read_port(home)
        if port is not None:
            return port
        time.sleep(_POLL_INTERVAL)

    raise SystemExit(
        f"Timed out after {_PORT_WAIT_SECONDS}s waiting for the daemon to start "
        f"(no {Path(home) / 'control_port'}). Run '{mcpbrain_bin} daemon' in a "
        f"terminal to see why it is not coming up."
    )


def connect_main(argv=None) -> int:
    """``mcpbrain connect``: (re)write ONLY the Claude Desktop MCP connector.

    Claude Desktop owns ``claude_desktop_config.json`` and overwrites entries
    added while it is running, so the reliable way to register the connector is
    to run this with **Claude Desktop quit**, then reopen it. Unlike ``setup``,
    this touches nothing else — no daemon, no wizard.
    """
    ap = argparse.ArgumentParser(prog="mcpbrain connect")
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would be written without writing")
    args = ap.parse_args(argv)
    _register_desktop_mcp(dry_run=args.dry_run)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="mcpbrain setup")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would happen without starting the daemon or opening a browser",
    )
    args = ap.parse_args(argv)

    home = str(app_dir())
    # The brain folder is only needed if the user later wants a dedicated project
    # pointed at it — the recurring tasks reach mcpbrain via its MCP tools, not the
    # filesystem, so they don't need this path. Surface it as optional info.
    print(f"Your brain folder (optional — for a dedicated project) is:\n  {home}")

    port = _ensure_daemon_running(home, dry_run=args.dry_run)
    url = f"http://127.0.0.1:{port}/"

    _register_desktop_mcp(dry_run=args.dry_run)

    if args.dry_run:
        print(f"would open {url}")
        return 0

    _install_tray_best_effort(home)
    _start_tray_now(home)

    try:
        from mcpbrain import agents
        agents.install_cadences(_platform(), mcpbrain_bin=_mcpbrain_bin(), home=home)
        print("Records cadences scheduled (prune daily, health weekly).")
    except Exception as exc:  # noqa: BLE001 — optional; never block onboarding
        print(f"Skipped scheduling records cadences ({exc}).", file=sys.stderr)

    print(f"Opening the mcpbrain setup wizard at {url}")
    print("If a browser does not open, paste that URL into one yourself.")
    print("Finish setup in the wizard (Google sign-in, your details), then click "
          "'Connect & restart Claude Desktop' as the LAST step — that loads the brain_* "
          "tools. Backup and recovery happen automatically.")
    webbrowser.open(url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
