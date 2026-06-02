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

    platform = sys.platform
    if platform.startswith("linux"):
        platform = "linux"
    elif platform == "darwin":
        platform = "darwin"
    elif platform in ("win32", "cygwin"):
        platform = "win32"

    mcpbrain_bin = shutil.which("mcpbrain") or sys.argv[0] or "mcpbrain"
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
    ap.add_argument(
        "--repo-dir",
        default=None,
        help="path to the cloned mcpbrain repo; persisted so `mcpbrain update` can find it",
    )
    args = ap.parse_args(argv)

    home = str(app_dir())

    # Record where the clone lives so `mcpbrain update` can git pull it later.
    # Skip on --dry-run: dry-run must have no side effects.
    if args.repo_dir and not args.dry_run:
        from mcpbrain.config import write_config
        write_config(home, {"repo_dir": str(Path(args.repo_dir).resolve())})

    port = _ensure_daemon_running(home, dry_run=args.dry_run)
    url = f"http://127.0.0.1:{port}/"

    if args.dry_run:
        print(f"would open {url}")
        return 0

    print(f"Opening the mcpbrain setup wizard at {url}")
    print("If a browser does not open, paste that URL into one yourself.")
    webbrowser.open(url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
