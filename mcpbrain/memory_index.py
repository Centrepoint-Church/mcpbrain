"""memory.md: the one-line-per-memory index every surface reads.

Mechanically regenerated from memory-typed capture-note chunks (no LLM).
Lives at MCPBRAIN_HOME/context/memory.md; exposed to Claude Desktop as an MCP
resource, read directly by Cowork and Claude Code. The memory_distil enrich
block curates the underlying notes (expire/merge); this module just renders
the live set.
"""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

_HEADER = (
    "# Memory Index\n\n"
    "One line per memory. Full notes are in the brain: search by title or\n"
    "read the chunk id with brain_read. Maintained automatically; do not edit.\n\n"
)


def regenerate(store, home) -> Path:
    """Rewrite context/memory.md from live memory notes. Atomic overwrite."""
    notes = store.note_chunks(observation_type="memory")
    lines = []
    for n in notes:
        title = n["metadata"].get("title") or n["doc_id"]
        body = n["text"].split("\n\n", 1)[-1].strip().splitlines()
        hook = body[0][:120] if body else ""
        captured = (n["metadata"].get("captured_at") or "")[:10]
        lines.append(f"- **{title}** ({n['doc_id']}, {captured}) — {hook}")
    target_dir = Path(home) / "context"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "memory.md"
    fd, tmp = tempfile.mkstemp(dir=str(target_dir), prefix=".memory.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(_HEADER + "\n".join(lines) + ("\n" if lines else ""))
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target
