"""Reinstall this working tree on THIS Mac and prove the running daemon has it.

    python bin/dev_reinstall.py

Replaces the hand procedure that three install traps kept breaking:

1. `launchctl stop` is not enough: KeepAlive relaunches the daemon within
   seconds, mid-install, so the "restarted" process runs the old code.
   This boots the daemon AND the tray out, and waits until no process is left.
2. `uv tool install --force` reuses a cached wheel when the version string
   has not changed. This always passes `--reinstall`.
3. A stale `__pycache__` under the tool's site-packages can outlive the
   reinstall. This deletes every one.

Then it checks the ARTIFACT, not the command's exit: every .py file in the
installed package must be byte-identical to the working tree, and the
restarted daemon's /api/status must report this tree's version. Anything
else exits 1.

This machine only. It does not release anything to other users.
Running MCP servers keep the code they started with until their client
(Claude Desktop / Claude Code session) restarts; they are listed, not killed.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
AGENTS = ("com.mcpbrain", "com.mcpbrain.tray")
TOOL_ROOT = Path.home() / ".local/share/uv/tools/mcpbrain"
_ROLES = ("daemon", "tray")


def mcpbrain_pids(ps_lines: list[str], roles=_ROLES) -> list[int]:
    """Pids of mcpbrain processes whose subcommand is one of `roles`.

    Parsed from `ps -axo pid=,command=` lines. Matches on the LAST argument so
    `mcpbrain mcp-server` (owned by a client, not launchd) is never counted.
    """
    out = []
    for line in ps_lines:
        parts = line.split()
        if len(parts) < 3 or not parts[0].isdigit():
            continue
        if any(Path(p).name == "mcpbrain" for p in parts[1:-1]) and parts[-1] in roles:
            out.append(int(parts[0]))
    return out


def tree_digest(root: Path) -> dict[str, str]:
    """{relative path: sha256} for every .py file under `root`."""
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*.py")) if "__pycache__" not in p.parts}


def wheel_excludes(pyproject: Path) -> tuple[str, ...]:
    """Path prefixes (relative to mcpbrain/) that pyproject keeps out of the wheel,
    from [tool.setuptools.packages.find] exclude, e.g. "mcpbrain.devtools*"
    -> "devtools"."""
    import tomllib
    find = (tomllib.loads(pyproject.read_text()).get("tool", {}).get("setuptools", {})
            .get("packages", {}).get("find", {}))
    out = []
    for pat in find.get("exclude", []):
        parts = pat.rstrip("*").rstrip(".").split(".")
        if parts[0] == "mcpbrain" and len(parts) > 1:
            out.append("/".join(parts[1:]))
    return tuple(out)


def diff_trees(source: dict[str, str], installed: dict[str, str]) -> list[str]:
    """Human-readable differences; empty means the install matches."""
    problems = [f"missing from install: {k}" for k in sorted(source.keys() - installed.keys())]
    problems += [f"differs: {k}" for k in sorted(source.keys() & installed.keys())
                 if source[k] != installed[k]]
    problems += [f"stale extra file in install: {k}"
                 for k in sorted(installed.keys() - source.keys())]
    return problems


def _ps() -> list[str]:
    return subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True,
                          text=True, check=True).stdout.splitlines()


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def _site_packages() -> Path:
    found = sorted(TOOL_ROOT.glob("lib/python3*/site-packages/mcpbrain"))
    if not found:
        sys.exit(f"no installed mcpbrain package under {TOOL_ROOT}")
    return found[-1]


def _step(msg: str) -> None:
    print(f"• {msg}", flush=True)


def main() -> int:
    if sys.platform != "darwin":
        sys.exit("dev_reinstall is macOS-only (it drives launchd)")
    uid = os.getuid()
    domain = f"gui/{uid}"
    agents_dir = Path.home() / "Library/LaunchAgents"

    _step("booting out daemon and tray")
    for label in AGENTS:
        _launchctl("bootout", f"{domain}/{label}")      # not loaded is fine
    deadline = time.monotonic() + 30
    while (left := mcpbrain_pids(_ps())) and time.monotonic() < deadline:
        time.sleep(1)
    if left:
        sys.exit(f"still running after bootout: pids {left}; stop them, then re-run")

    _step("uv tool install --reinstall --force .[daemon]")
    r = subprocess.run(["uv", "tool", "install", "--reinstall", "--force", ".[daemon]"],
                       cwd=REPO, capture_output=True, text=True)
    if r.returncode:
        print(r.stdout + r.stderr, file=sys.stderr)
        print("install FAILED; daemon left booted out. Fix, then re-run.", file=sys.stderr)
        return 1

    site = _site_packages()
    _step(f"clearing __pycache__ under {site}")
    for cache in site.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)

    _step("comparing installed files with the working tree")
    skip = wheel_excludes(REPO / "pyproject.toml")
    source = {k: v for k, v in tree_digest(REPO / "mcpbrain").items()
              if not k.startswith(skip)} if skip else tree_digest(REPO / "mcpbrain")
    problems = diff_trees(source, tree_digest(site))

    _step("bootstrapping daemon and tray")
    for label in AGENTS:
        plist = agents_dir / f"{label}.plist"
        if plist.exists():
            b = _launchctl("bootstrap", domain, str(plist))
            if b.returncode:
                problems.append(f"bootstrap {label}: {b.stderr.strip()}")

    sys.path.insert(0, str(REPO))
    from mcpbrain import __version__, config
    from mcpbrain.control_client import ControlClient
    _step("waiting for the daemon to answer /api/status")
    status, deadline = None, time.monotonic() + 90
    while status is None and time.monotonic() < deadline:
        try:
            status = ControlClient(str(config.app_dir()), timeout=5).status()
        except Exception:  # noqa: BLE001 — not up yet
            time.sleep(2)
    if status is None:
        problems.append("daemon did not answer /api/status within 90 s")
    elif status.get("version") != __version__:
        problems.append(f"daemon reports {status.get('version')}, tree is {__version__}")

    servers = [ln.split()[0] for ln in _ps() if ln.rstrip().endswith("mcp-server")
               and "mcpbrain" in ln and "disclaimer" not in ln]
    if servers:
        print(f"  note: {len(servers)} MCP server process(es) keep their start-time code "
              "until their Claude client restarts")

    if problems:
        print("\nNOT VERIFIED:", *problems, sep="\n  ", file=sys.stderr)
        return 1
    print(f"\nOK: daemon and tray run this tree ({__version__}), "
          "installed files identical to source")
    return 0


if __name__ == "__main__":
    sys.exit(main())
