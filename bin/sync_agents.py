#!/usr/bin/env python3
"""Regenerate embedded copies of canonical mcpbrain/ content inside plugin/
files, keeping each pair byte-identical to its single source of truth.

Two independent pairs, each with its own delimited region and canonical
source:

1. Extraction rules. The canonical rules live in `mcpbrain/enrich_prompt.md`
   (SHARED-EXTRACTION-RULES block), exposed via `tools._enrich_rules()`.
   The `enrich-batch` subagent carries a *copy* of those rules in its SYSTEM
   PROMPT (`plugin/agents/enrich-batch.md`) so that across the enrichment
   fan-out every sibling subagent shares one byte-identical, cacheable prefix
   (prompt caching serves it at ~10% after the first warms it).
   `test_enrich_agent_rules_in_sync` enforces byte-equality.

2. The draft-reply prompt body. The canonical body lives in
   `mcpbrain/prompts/draft-reply.md` (it ships in the wheel via
   package-data; `plugin/` does not). The `mcpbrain-draft-reply` plugin
   skill (`plugin/skills/mcpbrain-draft-reply/SKILL.md`) carries a *copy* of
   that body between its own markers, below its YAML frontmatter (which the
   plugin loader needs and this script never touches).
   `test_draft_reply_skill_in_sync` enforces byte-equality.

Both copies must never drift from their canonical source. Run this script
after editing either canonical file (`python bin/sync_agents.py`).
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

RULES_BEGIN = "<!-- SHARED-EXTRACTION-RULES:BEGIN -->"
RULES_END = "<!-- SHARED-EXTRACTION-RULES:END -->"
RULES_TARGETS = [REPO / "plugin" / "agents" / "enrich-batch.md"]

DRAFT_REPLY_BEGIN = "<!-- DRAFT-REPLY-BODY:BEGIN -->"
DRAFT_REPLY_END = "<!-- DRAFT-REPLY-BODY:END -->"
DRAFT_REPLY_TARGETS = [REPO / "plugin" / "skills" / "mcpbrain-draft-reply" / "SKILL.md"]


def canonical_rules() -> str:
    sys.path.insert(0, str(REPO))
    from mcpbrain.tools import _enrich_rules
    return _enrich_rules()


def canonical_draft_reply_body() -> str:
    return (REPO / "mcpbrain" / "prompts" / "draft-reply.md").read_text().strip()


def splice(text: str, begin: str, end: str, body: str) -> str:
    i, j = text.index(begin), text.index(end)
    return text[: i + len(begin)] + "\n" + body + "\n" + text[j:]


def _sync(targets: list[Path], begin: str, end: str, body: str) -> list[str]:
    changed = []
    for path in targets:
        old = path.read_text()
        new = splice(old, begin, end, body)
        if new != old:
            path.write_text(new)
            changed.append(path.name)
    return changed


def main() -> int:
    rules = canonical_rules()
    if not rules:
        print("refusing to sync: _enrich_rules() returned empty", file=sys.stderr)
        return 1
    draft_reply_body = canonical_draft_reply_body()
    if not draft_reply_body:
        print("refusing to sync: draft-reply.md is empty", file=sys.stderr)
        return 1
    changed = _sync(RULES_TARGETS, RULES_BEGIN, RULES_END, rules)
    changed += _sync(DRAFT_REPLY_TARGETS, DRAFT_REPLY_BEGIN, DRAFT_REPLY_END, draft_reply_body)
    print(f"synced: {', '.join(changed)}" if changed else "already in sync")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
