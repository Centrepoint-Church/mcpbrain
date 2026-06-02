"""mcpbrain update — git pull + reinstall + restart.

Pulls the latest commits from the ops-brain repo using --ff-only (aborting on
divergence), reinstalls the mcpbrain package via uv, then restarts the login
agent so the new version takes effect immediately.
"""

import os
import subprocess
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _repo_dir() -> str:
    """Return the directory containing pyproject.toml (the cloned mcpbrain repo).

    Resolution order:
      (a) MCPBRAIN_REPO env var, if set and it contains a pyproject.toml;
      (b) the repo_dir persisted in config by the installer, if it still
          contains a pyproject.toml;
      (c) walk up from this file's location until pyproject.toml is found;
      (d) raise RuntimeError with a user-facing message.

    After `uv tool install` the package lives in an isolated tool venv with no
    pyproject.toml, so (c) fails on a normal install — (a) and (b) are how
    `mcpbrain update` finds the clone the installer ran from.
    """
    override = os.environ.get("MCPBRAIN_REPO")
    if override and (Path(override) / "pyproject.toml").exists():
        return override

    # Persisted by the installer via `mcpbrain setup --repo-dir`.
    try:
        from mcpbrain.config import read_config, app_dir
        persisted = read_config(str(app_dir())).get("repo_dir")
    except Exception:  # noqa: BLE001 - config read must never break update
        persisted = None
    if persisted and (Path(persisted) / "pyproject.toml").exists():
        return persisted

    candidate = Path(__file__).resolve().parent
    for _ in range(10):  # guard against runaway traversal
        if (candidate / "pyproject.toml").exists():
            return str(candidate)
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent

    raise RuntimeError(
        "Could not locate the mcpbrain repo to update. Re-run the installer from "
        "your cloned mcpbrain checkout, or set MCPBRAIN_REPO to the directory "
        "containing the cloned repo (where pyproject.toml lives)."
    )


def _run(cmd: list) -> tuple[str, int]:
    """Run cmd (list form, shell=False), return (combined_output, returncode).

    No timeout is set intentionally: this is a user-invoked interactive command
    and the user can Ctrl-C if needed. A generous timeout large enough to cover
    slow connections would add little safety value.
    """
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return result.stdout or "", result.returncode


def _restart_agent() -> None:
    """Restart the mcpbrain login agent for the current platform."""
    from mcpbrain import agents
    agents.restart_agent(sys.platform)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list) -> int:
    """Pull, reinstall, restart. Returns 0 on success, nonzero on failure."""
    repo = _repo_dir()

    # Capture current HEAD so we can show the log of what changed.
    old_head_out, _ = _run(["git", "-C", repo, "rev-parse", "HEAD"])
    old_head = old_head_out.strip()

    # Step 1: pull.
    pull_out, pull_rc = _run(["git", "-C", repo, "pull", "--ff-only"])
    if pull_rc != 0:
        print(
            "Update aborted: git pull --ff-only failed.\n"
            f"{pull_out.strip()}\n\n"
            "This usually means there are local commits or the branch has diverged.\n"
            "To resolve: stash or reset local changes, then run `mcpbrain update` again.\n"
            "Or pull manually: git -C " + repo + " pull",
            file=sys.stderr,
        )
        return pull_rc

    # Step 2: reinstall.
    uv_out, uv_rc = _run(["uv", "tool", "install", "--from", repo, "mcpbrain", "--force"])
    if uv_rc != 0:
        print(
            "Update aborted: uv tool install failed.\n"
            f"{uv_out.strip()}\n\n"
            "The daemon has NOT been restarted. Fix the error above and run "
            "`mcpbrain update` again.",
            file=sys.stderr,
        )
        return uv_rc

    # Step 3: restart.
    _restart_agent()

    # Best-effort: show what changed.
    if old_head:
        log_out, log_rc = _run(
            ["git", "-C", repo, "log", "--oneline", f"{old_head}..HEAD"]
        )
        if log_rc == 0 and log_out.strip():
            print("Updated:\n" + log_out.strip())
        elif log_rc == 0:
            print("Already up to date.")
        else:
            print("Updated (could not determine the change range).")

    return 0
