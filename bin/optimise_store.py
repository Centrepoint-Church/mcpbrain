"""Attended, backup-gated store rebuild. NEVER run automatically."""
import sqlite3
from pathlib import Path

# (child table, child column) -> parent table. entity_observations already
# DECLARES REFERENCES entities(id), but foreign_keys was OFF so it never
# enforced anything.
_REFS = [
    ("entity_relations", "entity_a", "entities", "id"),
    ("entity_relations", "entity_b", "entities", "id"),
    ("email_entities", "entity_id", "entities", "id"),
    ("entity_observations", "entity_id", "entities", "id"),
    ("entity_communities", "entity_id", "entities", "id"),
]


def report_orphans(path) -> dict[str, int]:
    db = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True)
    try:
        out = {}
        for child, col, parent, pcol in _REFS:
            try:
                n = db.execute(
                    f'SELECT count(*) FROM "{child}" c '
                    f'LEFT JOIN "{parent}" p ON p."{pcol}" = c."{col}" '
                    f'WHERE p."{pcol}" IS NULL'
                ).fetchone()[0]
            except sqlite3.OperationalError:
                continue
            out[f"{child}.{col}"] = n
        return out
    finally:
        db.close()
