"""Create and scaffold the per-user records repo (local git, no remote).

The daemon writes structured records (decisions, continuity, memories) into this
repo via the write module, committing by name. The repo is a plain local git repo
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

from mcpbrain import config

log = logging.getLogger(__name__)

_TEMPLATES = Path(__file__).parent / "records_templates"

# Relative target path in the repo -> template filename in records_templates/.
_TEMPLATE_FILES = {
    "CLAUDE.md": "CLAUDE.md",
    "context/identity.md": "context_identity.md",
    "context/preferences.md": "context_preferences.md",
    "reference/systems.md": "reference_systems.md",
    "reference/projects.md": "reference_projects.md",
}


def _render_template(name: str, profile: dict) -> str:
    text = (_TEMPLATES / name).read_text(encoding="utf-8")
    orgs = [str(o.get("name") or "").strip() for o in (profile.get("orgs") or [])
            if isinstance(o, dict) and str(o.get("name") or "").strip()]
    org_list = ", ".join(orgs) if orgs else "(none configured yet)"
    org_block = "\n".join(f"- Items for {o} must be tagged clearly and kept separate." for o in orgs)
    repl = {
        "{{OWNER_FULL_NAME}}": profile.get("owner_full_name") or "(your name)",
        "{{OWNER_ROLE}}": profile.get("owner_role") or "(your role)",
        "{{ORG_LIST}}": org_list,
        "{{ORG_BLOCK}}": org_block,
    }
    for k, v in repl.items():
        text = text.replace(k, v)
    return text


# Per-process cache: tracks repo paths that have been fully ensured this process
# lifetime.  A new daemon process re-verifies once; subsequent cycles are no-ops.
_ENSURED: set[str] = set()

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

_BIN_README = (
    "# bin/\n\nPlace cadence scripts here (prune_hot_md.py, context_health.py, "
    "run_memory_gardener.sh, build_meeting_packs.sh).\n"
)

# Relative path -> initial content. memory/ is created as a directory separately.
_SCAFFOLD = {
    "state/decisions.md": _DECISIONS_MD,
    "state/hot.md": _HOT_MD,
    "MEMORY.md": _MEMORY_MD,
    "context/voice.md": _VOICE_MD,
    "bin/README.md": _BIN_README,
}


def _git(repo: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = {**os.environ, "LC_ALL": "C", "LANGUAGE": ""}
    try:
        return subprocess.run(["git", "-C", repo, *args], check=check,
                              capture_output=True, env=env)
    except FileNotFoundError:
        raise RuntimeError(
            "git is required but was not found in PATH — install git and ensure it is on the PATH used by launchd"
        )


def ensure_records_repo(repo_dir: str, *, git_name: str = "mcpbrain",
                        git_email: str = "mcpbrain@localhost",
                        profile: dict | None = None) -> str:
    """Ensure repo_dir is a git repo with the scaffold the writers expect.

    git-inits the directory if absent, sets a local git identity only if none is
    configured (never overrides the user's), stamps any missing scaffold files
    (never clobbers existing ones), and commits the scaffold on first creation.
    Idempotent; safe to call every cycle. Returns repo_dir.
    """
    repo = Path(repo_dir).resolve()
    repo_key = str(repo)
    if repo_key in _ENSURED:
        return repo_dir
    repo.mkdir(parents=True, exist_ok=True)
    fresh = not (repo / ".git").is_dir()
    if fresh:
        _git(repo_dir, "init")
    if _git(repo_dir, "config", "--local", "user.name", check=False).returncode != 0:
        _git(repo_dir, "config", "--local", "user.name", git_name)
    if _git(repo_dir, "config", "--local", "user.email", check=False).returncode != 0:
        _git(repo_dir, "config", "--local", "user.email", git_email)
    (repo / "memory").mkdir(exist_ok=True)
    newly_written: list[str] = []
    for rel, content in _SCAFFOLD.items():
        p = repo / rel
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
            newly_written.append(rel)
    if profile is not None:
        for rel, tmpl in _TEMPLATE_FILES.items():
            p = repo / rel
            if not p.exists():
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(_render_template(tmpl, profile), encoding="utf-8")
                newly_written.append(rel)
    if fresh:
        _git(repo_dir, "add", "-A")
        staged = _git(repo_dir, "diff", "--cached", "--quiet", check=False).returncode != 0
        if staged:
            _git(repo_dir, "commit", "-m", "scaffold: initialize records repo")
    elif newly_written:
        _git(repo_dir, "add", *newly_written)
        staged = _git(repo_dir, "diff", "--cached", "--quiet", check=False).returncode != 0
        if staged:
            _git(repo_dir, "commit", "-m", "scaffold: add missing scaffold files")
    _ENSURED.add(repo_key)
    return repo_dir


def scaffold_records(home: str) -> list[str]:
    """Ensure + stamp the records repo from the saved profile. Degrades to [].

    Best-effort: any failure (no git, unwritable dir) returns [] and never raises,
    so a settings POST is never failed by scaffolding.
    """
    try:
        repo = config.records_dir(home)
        profile = {
            "owner_full_name": config.owner_full_name(home),
            "owner_role": config.owner_role(home),
            "orgs": config.read_config(home).get("orgs") or [],
        }
        _ENSURED.discard(str(Path(repo).resolve()))  # force a re-stamp pass
        ensure_records_repo(repo, profile=profile)
        return [str(Path(repo) / rel) for rel in _TEMPLATE_FILES]
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.debug("scaffold_records degraded: %s", exc)
        return []
