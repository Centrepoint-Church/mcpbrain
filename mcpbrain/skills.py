"""Write mcpbrain's personal Claude skills into ~/.claude/skills/.

Personal skills (a folder with a SKILL.md) are invokable across Cowork, Claude
chat and Claude Code. We ship two:

- ``mcpbrain-enrichment`` — the extraction prompt (body from cowork/enrichment.md).
  A scheduled task runs this hourly to drain the enrich queue.
- ``mcpbrain-setup`` — run once in Cowork; it asks Claude to create the hourly
  scheduled task that runs the enrichment skill (the cadence the daemon can't set).

Every function degrades gracefully (returns [] / False) rather than raising, so a
settings save is never failed by skill materialisation.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

ENRICHMENT_SKILL = "mcpbrain-enrichment"
SETUP_SKILL = "mcpbrain-setup"

_ENRICHMENT_DESC = (
    "Extract structured knowledge (entities, actions, relations, org tags) from a "
    "batch of email threads: reads ~/.mcpbrain/enrich_queue/pending.json and writes "
    "~/.mcpbrain/enrich_inbox/<batch_id>.json. Two files in, two files out."
)
_SETUP_DESC = (
    "One-time setup: create the hourly mcpbrain enrichment scheduled task. Run this "
    "once in Cowork after installing mcpbrain."
)

_SETUP_BODY = """\
Set up mcpbrain's background enrichment for this user. Enrichment is what turns
their mail into structured memory; it should run every hour.

Do this:

1. Find the mcpbrain home. Use ~/.mcpbrain if it exists, otherwise the OS default
   app dir (macOS: ~/Library/Application Support/mcpbrain). You can read
   <home>/config.json to confirm. Call this HOME.
2. Confirm the `mcpbrain-enrichment` skill is installed (a SKILL.md exists under
   the personal skills directory). If it is missing, tell the user to reinstall
   mcpbrain, and stop.
3. Create a scheduled task:
   - Name: mcpbrain-enrichment
   - Schedule: hourly
   - Working folder: HOME
   - What it does: "Run the mcpbrain-enrichment skill to process the current
     pending batch."
   Create it the normal way you create a scheduled task when asked.
4. Confirm to the user that the hourly task is set up, and that it will quietly
   keep their brain current. Nothing else is needed.

Do not edit any files in HOME yourself — the enrichment skill and the mcpbrain
daemon own those.
"""


def skills_dir() -> Path:
    """Personal Claude skills directory (honours CLAUDE_CONFIG_DIR)."""
    base = os.getenv("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else Path.home() / ".claude") / "skills"


def _enrichment_body() -> str:
    return (Path(__file__).parent / "cowork" / "enrichment.md").read_text(encoding="utf-8")


def _write_skill(name: str, description: str, body: str) -> Path:
    d = skills_dir() / name
    d.mkdir(parents=True, exist_ok=True)
    front = f"---\nname: {name}\ndescription: {description}\n---\n\n"
    out = d / "SKILL.md"
    out.write_text(front + body, encoding="utf-8")
    return out


def write_personal_skills() -> list[Path]:
    """Write both personal skills. Idempotent. Degrades to [] on error (never raises)."""
    try:
        return [
            _write_skill(ENRICHMENT_SKILL, _ENRICHMENT_DESC, _enrichment_body()),
            _write_skill(SETUP_SKILL, _SETUP_DESC, _SETUP_BODY),
        ]
    except OSError as exc:
        log.debug("write_personal_skills degraded: %s", exc)
        return []


def enrichment_skill_present() -> bool:
    return (skills_dir() / ENRICHMENT_SKILL / "SKILL.md").exists()


def setup_skill_present() -> bool:
    return (skills_dir() / SETUP_SKILL / "SKILL.md").exists()
