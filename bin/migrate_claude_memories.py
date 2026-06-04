#!/usr/bin/env python3
"""One-time migration: Claude Code memory files -> mcpbrain capture envelopes.

Usage:
    python3 bin/migrate_claude_memories.py --src ~/.claude/projects/<proj>/memory \\
        --out /path/to/staging
    # then copy the cap-*.json files into the TARGET machine's
    # ~/.mcpbrain/capture_inbox/ — the daemon applies them on its next cycle.

Skips MEMORY.md (it is an index; mcpbrain regenerates its own). Maps memory
types: feedback/user/project -> memory; reference -> reference. Delete this
script after the migration (spec: one-time scripts do not live on).
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

_TYPE_MAP = {"feedback": "memory", "user": "memory", "project": "memory",
             "reference": "reference"}


def _parse(path: Path) -> dict | None:
    text = path.read_text()
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
    front, body = (m.group(1), m.group(2)) if m else ("", text)
    title = path.stem.replace("_", " ").replace("-", " ")
    dm = re.search(r"^description:\s*(.+)$", front, re.MULTILINE)
    if dm:
        title = dm.group(1).strip().strip('"')[:120]
    tm = re.search(r"^\s*type:\s*(\w+)", front, re.MULTILINE)
    mtype = _TYPE_MAP.get(tm.group(1) if tm else "project", "memory")
    body = body.strip()
    if not body:
        return None
    return {"kind": "ingest",
            "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": "migration", "title": title, "content": body,
            "tags": "migrated", "observation_type": mtype, "org": ""}


def migrate(src: Path, out: Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    n = 0
    for path in sorted(Path(src).glob("*.md")):
        if path.name == "MEMORY.md":
            continue
        env = _parse(path)
        if env is None:
            continue
        n += 1
        (out / f"cap-migrate-{n:03d}.json").write_text(
            json.dumps(env, ensure_ascii=False))
    return n


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    count = migrate(args.src, args.out)
    print(f"wrote {count} envelopes to {args.out} — copy them into the target "
          f"machine's ~/.mcpbrain/capture_inbox/")
