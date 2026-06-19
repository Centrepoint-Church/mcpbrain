#!/usr/bin/env python3
"""Regenerate the embedded extraction-rules block in plugin agent files from the
single source of truth.

The canonical extraction rules live in `mcpbrain/enrich_prompt.md`
(SHARED-EXTRACTION-RULES block), exposed via `mcp_server._enrich_rules()`. The
`enrich-batch` subagent carries a *copy* of those rules in its SYSTEM PROMPT
(`plugin/agents/enrich-batch.md`) so that across the enrichment fan-out every sibling
subagent shares one byte-identical, cacheable prefix (prompt caching serves it at ~10%
after the first warms it).

That copy must never drift from the canonical rules the daemon/pull use.
`test_enrich_agent_rules_in_sync` enforces byte-equality; this script regenerates the
copy. Run it after editing the rules (`python bin/sync_agents.py`).
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BEGIN = "<!-- SHARED-EXTRACTION-RULES:BEGIN -->"
END = "<!-- SHARED-EXTRACTION-RULES:END -->"
AGENTS = [REPO / "plugin" / "agents" / "enrich-batch.md"]


def canonical_rules() -> str:
    sys.path.insert(0, str(REPO))
    from mcpbrain.mcp_server import _enrich_rules
    return _enrich_rules()


def splice(text: str, rules: str) -> str:
    i, j = text.index(BEGIN), text.index(END)
    return text[: i + len(BEGIN)] + "\n" + rules + "\n" + text[j:]


def main() -> int:
    rules = canonical_rules()
    if not rules:
        print("refusing to sync: _enrich_rules() returned empty", file=sys.stderr)
        return 1
    changed = []
    for path in AGENTS:
        old = path.read_text()
        new = splice(old, rules)
        if new != old:
            path.write_text(new)
            changed.append(path.name)
    print(f"synced: {', '.join(changed)}" if changed else "already in sync")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
