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
import shutil
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


def _register_mcp_server(*, dry_run: bool = False) -> None:
    """Register the mcpbrain MCP server with Claude Code (user scope).

    The brain is served through its stdio MCP server, ``mcpbrain mcp-server``.
    We register it here, from setup, instead of via the plugin's ``.mcp.json``
    because only setup knows the *absolute* path to the installed ``mcpbrain``
    binary. That matters: a plugin ``.mcp.json`` cannot branch per-OS, and the
    daemon/desktop app is launched at login (launchd on macOS) with a minimal
    PATH that excludes ``~/.local/bin`` — so a bare ``mcpbrain`` command would
    not resolve. An absolute path resolves the same on macOS and Windows.

    User scope makes the ``brain_*`` tools available in every Claude Code session
    (including scheduled tasks), which is what we want. Best-effort: a missing
    ``claude`` CLI must never block onboarding.
    """
    from mcpbrain import config
    mcpbrain_bin = _mcpbrain_bin()
    manual = f"  claude mcp add mcpbrain --scope user -- {mcpbrain_bin} mcp-server"
    try:
        claude = config.find_claude()
    except Exception as exc:  # noqa: BLE001 - registration is best-effort
        print(f"Skipped connecting the mcpbrain MCP server ({exc}). Once the "
              f"`claude` CLI is available, run:\n{manual}", file=sys.stderr)
        return

    add_cmd = [claude, "mcp", "add", "mcpbrain", "--scope", "user", "--",
               mcpbrain_bin, "mcp-server"]
    if dry_run:
        print(f"would register MCP server: {' '.join(add_cmd)}")
        return

    import subprocess
    # Idempotent: drop any prior registration (ignore if absent), then add.
    subprocess.run([claude, "mcp", "remove", "mcpbrain", "--scope", "user"],
                   capture_output=True, text=True)
    res = subprocess.run(add_cmd, capture_output=True, text=True)
    if res.returncode == 0:
        print("Connected the mcpbrain MCP server to Claude Code (user scope) — the "
              "brain_* tools are now available in every session.")
    else:
        print(f"Could not auto-connect the mcpbrain MCP server "
              f"({(res.stderr or res.stdout).strip()}). Run this yourself:\n{manual}",
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

    _register_mcp_server(dry_run=args.dry_run)

    if args.dry_run:
        print(f"would open {url}")
        return 0

    _install_tray_best_effort(home)

    try:
        from mcpbrain import agents
        agents.install_cadences(_platform(), mcpbrain_bin=_mcpbrain_bin(), home=home)
        print("Records cadences scheduled (prune daily, health weekly).")
    except Exception as exc:  # noqa: BLE001 — optional; never block onboarding
        print(f"Skipped scheduling records cadences ({exc}).", file=sys.stderr)

    print(f"Opening the mcpbrain setup wizard at {url}")
    print("If a browser does not open, paste that URL into one yourself.")
    print("Finish setup in the wizard (Google sign-in, your details). Backup and "
          "recovery happen automatically. Then create the four Local scheduled tasks "
          "from this Claude Code session (see the install prompt).")
    webbrowser.open(url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
