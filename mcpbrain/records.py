"""Create and scaffold the per-user records repo (local git, no remote).

The daemon writes structured records (decisions, continuity, memories) into this
repo via joshbrain_write, committing by name. The repo is a plain local git repo
under the user's app dir. This module creates it and stamps the minimal scaffold
the writers expect (the decisions/hot anchors, MEMORY.md, memory/, voice.md),
idempotently — existing files are never clobbered and an existing repo's git
identity is left as-is.
"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

_DECISIONS_MD = """# Decisions

Decisions that supersede earlier behaviour. Newest first.

Append new decisions at the top. One line per decision.

| Date | Decision | Rationale | Owner | Status | Supersedes |
|------|----------|-----------|-------|--------|------------|
"""

_HOT_MD = """# Hot — active continuity

## Just decided

## Open
"""

_MEMORY_MD = "# Memory Index\n"

_VOICE_MD = "# Voice & style\n\n(Describe the owner's writing voice here.)\n"

# Relative path -> initial content. memory/ is created as a directory separately.
_SCAFFOLD = {
    "state/decisions.md": _DECISIONS_MD,
    "state/hot.md": _HOT_MD,
    "MEMORY.md": _MEMORY_MD,
    "context/voice.md": _VOICE_MD,
}


def _git(repo: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = {**os.environ, "LC_ALL": "C", "LANGUAGE": ""}
    return subprocess.run(["git", "-C", repo, *args], check=check,
                          capture_output=True, env=env)


def ensure_records_repo(repo_dir: str, *, git_name: str = "mcpbrain",
                        git_email: str = "mcpbrain@localhost") -> str:
    """Ensure repo_dir is a git repo with the scaffold the writers expect.

    git-inits the directory if absent, sets a local git identity only if none is
    configured (never overrides the user's), stamps any missing scaffold files
    (never clobbers existing ones), and commits the scaffold on first creation.
    Idempotent; safe to call every cycle. Returns repo_dir.
    """
    repo = Path(repo_dir)
    repo.mkdir(parents=True, exist_ok=True)
    fresh = not (repo / ".git").is_dir()
    if fresh:
        _git(repo_dir, "init")
    if _git(repo_dir, "config", "--local", "user.name", check=False).returncode != 0:
        _git(repo_dir, "config", "user.name", git_name)
    if _git(repo_dir, "config", "--local", "user.email", check=False).returncode != 0:
        _git(repo_dir, "config", "user.email", git_email)
    (repo / "memory").mkdir(exist_ok=True)
    wrote = False
    for rel, content in _SCAFFOLD.items():
        p = repo / rel
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
            wrote = True
    if fresh or wrote:
        _git(repo_dir, "add", "-A")
        staged = _git(repo_dir, "diff", "--cached", "--quiet",
                      check=False).returncode != 0
        if staged:
            _git(repo_dir, "commit", "-m", "scaffold: initialize records repo")
    return repo_dir
