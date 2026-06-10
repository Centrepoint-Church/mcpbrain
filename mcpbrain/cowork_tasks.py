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


def _candidate_dirs() -> list[Path]:
    home = Path.home()
    cands = [home / "Documents" / "Claude" / "Scheduled"]
    cfg = os.getenv("CLAUDE_CONFIG_DIR")
    cands.append((Path(cfg) if cfg else home / ".claude") / "scheduled-tasks")
    return cands


def scheduled_dir() -> Path | None:
    """First candidate whose PARENT exists (so we may create the task subdir)."""
    for d in _candidate_dirs():
        try:
            if d.exists() or d.parent.exists():
                return d
        except OSError:
            continue
    return None


def _skill_body() -> str:
    return (Path(__file__).parent / "cowork" / "enrichment.md").read_text()


def write_enrichment_skill(home: str) -> Path | None:
    """Write <scheduled_dir>/mcpbrain-enrichment/SKILL.md. Returns path or None."""
    d = scheduled_dir()
    if d is None:
        return None
    try:
        task_dir = d / ENRICHMENT_TASK
        task_dir.mkdir(parents=True, exist_ok=True)
        front = f"---\nname: {ENRICHMENT_TASK}\ndescription: {_DESCRIPTION}\n---\n\n"
        out = task_dir / "SKILL.md"
        out.write_text(front + _skill_body())
        return out
    except OSError as exc:
        log.debug("write_enrichment_skill degraded: %s", exc)
        return None


def enrichment_skill_present() -> bool:
    d = scheduled_dir()
    return bool(d and (d / ENRICHMENT_TASK / "SKILL.md").exists())
