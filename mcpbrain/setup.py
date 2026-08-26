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
    """Absolute path to the installed mcpbrain launcher.

    Agent registration (launchd/schtasks) and connector registration both run
    later under a minimal login PATH, so a bare name would not resolve — this
    must be absolute.

    Prefer the path `which` reports (uv's shim, ~/.local/bin/mcpbrain) WITHOUT
    resolving it. Resolving follows the symlink into uv's tool venv, which is an
    internal layout detail rather than a supported entry point. Only fall back to
    resolving argv[0] when there is no shim on PATH at all.
    """
    found = shutil.which("mcpbrain")
    if found:
        return str(Path(found).absolute())
    fallback = Path(sys.argv[0] or "mcpbrain")
    return str(fallback.resolve()) if fallback.exists() else "mcpbrain"


def _register_connector(*, dry_run: bool = False) -> None:
    """Register the brain with every Claude surface, reporting each outcome.

    Deliberately terse. This used to print a five-line block instructing the user
    to quit Claude Desktop and re-run `mcpbrain connect` — advice that contradicted
    both the install command and the wizard's final step, and which is obsolete now
    that the wizard's Connect button writes inside the quit/relaunch window.
    """
    from mcpbrain import connector
    for path, ok, status, detail in connector.register_connector(
            mcpbrain_bin=_mcpbrain_bin(), dry_run=dry_run):
        if status == connector.STATUS_DRY_RUN:
            continue  # register_connector already printed "would register ..."
        if status == connector.STATUS_SKIPPED:
            # Intentional (e.g. ~/.claude.json on a Claude-Desktop-only machine)
            # — informational, never stderr. Branching on the status rather than
            # on the detail text means rewording a message cannot turn this back
            # into a spurious error.
            print(f"Skipped: {detail}")
        elif ok:
            print(f"Connected the brain: {detail}")
        else:
            print(f"Could not connect the brain here: {detail}", file=sys.stderr)


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
    """``mcpbrain connect``: re-register the brain's MCP connector on every
    Claude surface present, quitting and relaunching Claude Desktop around the
    write so Desktop's own clobber-on-quit behaviour can't discard it.

    This FORCE-QUITS Claude Desktop (``taskkill /IM Claude.exe /F`` on Windows,
    ``osascript -e 'quit app "Claude"'`` on macOS). Since Claude Code Desktop
    *is* Claude Desktop, running this from a terminal inside a Claude Code
    Desktop session will close that very session. Unlike ``setup``, this
    touches nothing else — no daemon, no wizard.
    """
    ap = argparse.ArgumentParser(prog="mcpbrain connect")
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would be written without writing")
    args = ap.parse_args(argv)
    if args.dry_run:
        # A dry-run must never take a real destructive/disruptive action
        # against the user's actual running Claude Desktop.
        _register_connector(dry_run=True)
        return 0
    print("This will quit and relaunch Claude Desktop (force-quit if needed) "
          "to register the connector. If you are running this from a terminal "
          "inside Claude Code Desktop, that session will close.")
    from mcpbrain import desktop
    desktop.relaunch_claude_desktop(on_quit=lambda: _register_connector())
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

    _register_connector(dry_run=args.dry_run)

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
          "'Connect & restart Claude Desktop' as the LAST step — that reloads Claude "
          "so the brain_* tools appear. Backup and recovery happen automatically.")
    webbrowser.open(url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
