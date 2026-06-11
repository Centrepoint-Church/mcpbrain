"""Author the Cowork enrichment scheduled-task SKILL.md (content only).

Claude Cowork stores a scheduled task's prompt as a plain SKILL.md under a
per-OS scheduled-tasks directory. The cadence/enabled state is app-managed and
NOT file-authorable, so this module only writes the prompt and detects it.
Every function degrades gracefully (returns None / False) rather than raising.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

ENRICHMENT_TASK = "mcpbrain-enrichment"
_DESCRIPTION = (
    "Reads a batch of email threads from ~/.mcpbrain/enrich_queue/pending.json, "
    "extracts structured knowledge (entities, actions, relations, org tags), and "
    "writes the result to ~/.mcpbrain/enrich_inbox/<batch_id>.json. No database "
    "access, no Gmail — two files in and out."
)


def _cowork_dir() -> Path:
    """Claude Cowork's scheduled-tasks dir (the product's target)."""
    return Path.home() / "Documents" / "Claude" / "Scheduled"


def _code_desktop_dir() -> Path:
    """Claude Code Desktop's scheduled-tasks dir (honours CLAUDE_CONFIG_DIR)."""
    cfg = os.getenv("CLAUDE_CONFIG_DIR")
    return (Path(cfg) if cfg else Path.home() / ".claude") / "scheduled-tasks"


def scheduled_dir() -> Path | None:
    """Resolve where to write the enrichment SKILL.md, biased to Cowork.

    mcpbrain targets Claude Cowork, whose tasks live under ``~/Documents/Claude/
    Scheduled``. We must NOT let ``~/.claude`` (which almost always exists for the
    Claude Code CLI) hijack the choice — a fresh Cowork install that hasn't yet
    created ``~/Documents/Claude`` would otherwise have its skill written to a dir
    Cowork never reads, silently. Resolution order:

    1. Cowork dir if it, or its ``~/Documents/Claude`` parent, already exists.
    2. Claude Code Desktop's dir only if that dir *itself* already exists
       (proof it is actually in use — not merely that ``~/.claude`` exists).
    3. Default to the Cowork dir (to be created) when ``~/Documents`` exists.
    4. Otherwise ``None`` (degrade — caller skips writing).
    """
    cowork = _cowork_dir()
    code_desktop = _code_desktop_dir()
    try:
        if cowork.exists() or cowork.parent.exists():
            return cowork
        if code_desktop.exists():
            return code_desktop
        if cowork.parent.parent.exists():  # ~/Documents exists -> create Cowork dir
            return cowork
    except OSError:
        return None
    return None


def _skill_body() -> str:
    return (Path(__file__).parent / "cowork" / "enrichment.md").read_text(encoding="utf-8")


def write_enrichment_skill(home: str) -> Path | None:
    """Write <scheduled_dir>/mcpbrain-enrichment/SKILL.md. Returns path or None.

    `home` is accepted for interface consistency with the other writers but is
    unused — the Cowork scheduled-tasks directory is always resolved from the OS
    home (Path.home()), not from the mcpbrain app dir.
    """
    d = scheduled_dir()
    if d is None:
        return None
    try:
        task_dir = d / ENRICHMENT_TASK
        task_dir.mkdir(parents=True, exist_ok=True)
        front = f"---\nname: {ENRICHMENT_TASK}\ndescription: {_DESCRIPTION}\n---\n\n"
        out = task_dir / "SKILL.md"
        out.write_text(front + _skill_body(), encoding="utf-8")
        return out
    except OSError as exc:
        log.debug("write_enrichment_skill degraded: %s", exc)
        return None


def enrichment_skill_present() -> bool:
    d = scheduled_dir()
    return bool(d and (d / ENRICHMENT_TASK / "SKILL.md").exists())
