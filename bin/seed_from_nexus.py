#!/usr/bin/env python3
"""Seed mcpbrain's graph from a Nexus memory DB. Kills cold-start.

Copies Nexus's entities, bitemporal entity_relations, projects, and areas into
an mcpbrain store. Idempotent: re-running upserts in place rather than
duplicating, and refreshes the bitemporal fields on existing relations.

The mcpbrain store owns its schema — this script never issues DDL. It calls
store.init() to build the schema, then writes rows via store._connect() with
raw parameterised upserts (fine for a one-shot maintenance script).

The Nexus DB is opened READ-ONLY. Both DBs must be reachable when this runs
(the Nexus dev box, or the Mac in Phase 4) and the mcpbrain daemon must NOT be
running — the seed is the sole writer for its duration.

Nexus path convention (for reference, not hardcoded — pass it on the CLI):
the memory graph lives at db_for("<user>", "memory") per src/memory_db.py:80-97.

Usage:
    seed_from_nexus.py --nexus-db /path/to/memory.sqlite3 \\
                       --mcpbrain-home ~/.mcpbrain
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

# bin/ is not on sys.path when run as a script; add the package root so
# `from mcpbrain.store import Store` resolves both as a script and on import.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcpbrain.store import Store  # noqa: E402


def _open_nexus_ro(path: str) -> sqlite3.Connection:
    """Open the Nexus DB read-only (mode=ro URI) with a Row factory."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Column names present on a Nexus table (graceful schema drift handling)."""
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _get(row: sqlite3.Row, col: str, cols: set[str], default=None):
    """Read a column if the Nexus table carries it, else return default."""
    return row[col] if col in cols else default


# Columns mcpbrain's entities table accepts from Nexus (intersection of shapes).
_ENTITY_COLS = (
    "id", "name", "type", "org", "first_seen", "last_seen",
    "email_count", "degree", "aliases", "email_addr", "notes",
)
# Bitemporal + scalar relation columns to carry from Nexus.
_RELATION_TEMPORAL_COLS = (
    "valid_from", "valid_to", "invalidated_at", "invalidated_by_relation_id",
    "superseded_reason", "confidence", "evidence", "strength",
    "normalised_strength", "since", "last_seen",
)


def _seed_entities(nexus: sqlite3.Connection, db: sqlite3.Connection) -> tuple[int, set]:
    """Seed entities. Returns (count, set of seeded entity ids)."""
    if not _has_table(nexus, "entities"):
        return 0, set()
    cols = _table_columns(nexus, "entities")
    count = 0
    seeded_ids: set = set()
    for row in nexus.execute("SELECT * FROM entities"):
        vals = {c: _get(row, c, cols) for c in _ENTITY_COLS}
        db.execute(
            """INSERT INTO entities
                 (id, name, type, org, first_seen, last_seen, email_count,
                  degree, aliases, email_addr, notes)
               VALUES
                 (:id, :name, :type, :org, :first_seen, :last_seen, :email_count,
                  :degree, :aliases, :email_addr, :notes)
               ON CONFLICT(id) DO UPDATE SET
                 name        = excluded.name,
                 type        = excluded.type,
                 org         = excluded.org,
                 first_seen  = excluded.first_seen,
                 last_seen   = excluded.last_seen,
                 email_count = excluded.email_count,
                 degree      = excluded.degree,
                 aliases     = excluded.aliases,
                 email_addr  = excluded.email_addr,
                 notes       = excluded.notes""",
            {
                "id": vals["id"],
                "name": vals["name"] or "",
                "type": vals["type"] or "unknown",
                "org": vals["org"] or "",
                "first_seen": vals["first_seen"] or "",
                "last_seen": vals["last_seen"] or "",
                "email_count": vals["email_count"] or 0,
                "degree": vals["degree"] or 0,
                "aliases": vals["aliases"] or "",
                "email_addr": vals["email_addr"] or "",
                "notes": vals["notes"] or "",
            },
        )
        count += 1
        if vals["id"] is not None:
            seeded_ids.add(vals["id"])
    return count, seeded_ids


def _seed_relations(
    nexus: sqlite3.Connection, db: sqlite3.Connection, seeded_ids: set
) -> tuple[int, int]:
    """Seed relations. Returns (count, orphaned count).

    An orphan is a relation whose entity_a or entity_b was not seeded into the
    mcpbrain entities table. mcpbrain enforces no FK on relations, so a partial
    or truncated Nexus DB would otherwise produce dangling relations silently.
    The rows are kept (a relation may legitimately precede its entity in some
    seeds) — the count is surfaced for observability only.
    """
    if not _has_table(nexus, "entity_relations"):
        return 0, 0
    cols = _table_columns(nexus, "entity_relations")
    count = 0
    orphaned = 0
    for row in nexus.execute("SELECT * FROM entity_relations"):
        entity_a, relation, entity_b = row["entity_a"], row["relation"], row["entity_b"]
        if entity_a not in seeded_ids or entity_b not in seeded_ids:
            orphaned += 1
        # Insert the triple if new (UNIQUE(entity_a,relation,entity_b) makes
        # re-seeds a no-op on the row itself), then refresh temporal fields on
        # the surviving row so a re-seed picks up Nexus's latest state without
        # duplicating. The mcpbrain id is its own autoincrement — Nexus ids are
        # not carried (relations are referenced by triple, not id).
        db.execute(
            "INSERT OR IGNORE INTO entity_relations(entity_a, relation, entity_b) "
            "VALUES (?,?,?)",
            (entity_a, relation, entity_b),
        )
        temporal = {c: _get(row, c, cols) for c in _RELATION_TEMPORAL_COLS}
        db.execute(
            """UPDATE entity_relations SET
                 valid_from                 = :valid_from,
                 valid_to                   = :valid_to,
                 invalidated_at             = :invalidated_at,
                 invalidated_by_relation_id = :invalidated_by_relation_id,
                 superseded_reason          = :superseded_reason,
                 confidence                 = :confidence,
                 evidence                   = :evidence,
                 strength                   = :strength,
                 normalised_strength        = :normalised_strength,
                 since                      = :since,
                 last_seen                  = :last_seen
               WHERE entity_a=:entity_a AND relation=:relation AND entity_b=:entity_b""",
            {
                **temporal,
                "confidence": temporal["confidence"] if temporal["confidence"] is not None else 1.0,
                "strength": temporal["strength"] if temporal["strength"] is not None else 1,
                "normalised_strength": temporal["normalised_strength"] or 0.0,
                "entity_a": entity_a,
                "relation": relation,
                "entity_b": entity_b,
            },
        )
        count += 1
    return count, orphaned


def _seed_areas(nexus: sqlite3.Connection, db: sqlite3.Connection) -> int:
    if not _has_table(nexus, "areas"):
        return 0
    cols = _table_columns(nexus, "areas")
    count = 0
    for row in nexus.execute("SELECT * FROM areas"):
        # Nexus areas.org_id is an FK to organisations(id); mcpbrain stores it
        # as a plain TEXT tag, so the value copies straight across.
        db.execute(
            """INSERT INTO areas
                 (id, org_id, name, description, standard, review_cadence,
                  last_reviewed_at, active, created_at, archived_at)
               VALUES
                 (:id, :org_id, :name, :description, :standard, :review_cadence,
                  :last_reviewed_at, :active, :created_at, :archived_at)
               ON CONFLICT(id) DO UPDATE SET
                 org_id           = excluded.org_id,
                 name             = excluded.name,
                 description      = excluded.description,
                 standard         = excluded.standard,
                 review_cadence   = excluded.review_cadence,
                 last_reviewed_at = excluded.last_reviewed_at,
                 active           = excluded.active,
                 created_at       = excluded.created_at,
                 archived_at      = excluded.archived_at""",
            {
                "id": row["id"],
                "org_id": (_get(row, "org_id", cols) or ""),
                "name": _get(row, "name", cols) or "",
                "description": _get(row, "description", cols),
                "standard": _get(row, "standard", cols),
                "review_cadence": _get(row, "review_cadence", cols),
                "last_reviewed_at": _get(row, "last_reviewed_at", cols),
                "active": _get(row, "active", cols, 1),
                "created_at": _get(row, "created_at", cols),
                "archived_at": _get(row, "archived_at", cols),
            },
        )
        count += 1
    return count


def _seed_projects(nexus: sqlite3.Connection, db: sqlite3.Connection) -> int:
    if not _has_table(nexus, "projects"):
        return 0
    cols = _table_columns(nexus, "projects")
    count = 0
    for row in nexus.execute("SELECT * FROM projects"):
        db.execute(
            """INSERT INTO projects
                 (id, name, org_tag, status_line, status_updated_at, created_at,
                  archived_at, notes_path, outcome, status, target_date,
                  actual_done_date, priority, area_id, owner_entity_id, updated_at)
               VALUES
                 (:id, :name, :org_tag, :status_line, :status_updated_at,
                  :created_at, :archived_at, :notes_path, :outcome, :status,
                  :target_date, :actual_done_date, :priority, :area_id,
                  :owner_entity_id, :updated_at)
               ON CONFLICT(id) DO UPDATE SET
                 name              = excluded.name,
                 org_tag           = excluded.org_tag,
                 status_line       = excluded.status_line,
                 status_updated_at = excluded.status_updated_at,
                 created_at        = excluded.created_at,
                 archived_at       = excluded.archived_at,
                 notes_path        = excluded.notes_path,
                 outcome           = excluded.outcome,
                 status            = excluded.status,
                 target_date       = excluded.target_date,
                 actual_done_date  = excluded.actual_done_date,
                 priority          = excluded.priority,
                 area_id           = excluded.area_id,
                 owner_entity_id   = excluded.owner_entity_id,
                 updated_at        = excluded.updated_at""",
            {
                "id": row["id"],
                "name": _get(row, "name", cols) or "",
                "org_tag": _get(row, "org_tag", cols),
                "status_line": _get(row, "status_line", cols),
                "status_updated_at": _get(row, "status_updated_at", cols),
                "created_at": _get(row, "created_at", cols),
                "archived_at": _get(row, "archived_at", cols),
                "notes_path": _get(row, "notes_path", cols),
                "outcome": _get(row, "outcome", cols),
                "status": _get(row, "status", cols, "active") or "active",
                "target_date": _get(row, "target_date", cols),
                "actual_done_date": _get(row, "actual_done_date", cols),
                "priority": _get(row, "priority", cols),
                "area_id": _get(row, "area_id", cols),
                "owner_entity_id": _get(row, "owner_entity_id", cols),
                "updated_at": _get(row, "updated_at", cols),
            },
        )
        count += 1
    return count


def seed(nexus_memory_db: str, store: Store) -> dict:
    """Copy Nexus's graph into the mcpbrain store. Returns a counts summary.

    Idempotent: entities/projects/areas upsert by id; relations upsert by the
    UNIQUE(entity_a,relation,entity_b) triple then refresh their bitemporal
    fields. Order matters only loosely (no FK enforcement on the mcpbrain side),
    but areas are seeded before projects to mirror the natural dependency.
    """
    nexus = _open_nexus_ro(nexus_memory_db)
    try:
        with store._connect() as db:
            entities, seeded_ids = _seed_entities(nexus, db)
            relations, orphaned_relations = _seed_relations(nexus, db, seeded_ids)
            areas = _seed_areas(nexus, db)
            projects = _seed_projects(nexus, db)
    finally:
        nexus.close()
    if orphaned_relations > 0:
        print(
            f"WARNING: {orphaned_relations} relation(s) reference an entity not "
            "present in the mcpbrain entities table (dangling). Kept, not dropped "
            "— check for a partial or truncated Nexus DB.",
            file=sys.stderr,
        )
    return {
        "entities": entities,
        "relations": relations,
        "orphaned_relations": orphaned_relations,
        "areas": areas,
        "projects": projects,
    }


def _existing_store_dim(store_path: Path) -> int | None:
    """Read the vector dim a store was built with, or None if it doesn't exist."""
    if not Path(store_path).exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT v FROM meta WHERE k='dim'").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return int(row[0]) if row and row[0] else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Seed mcpbrain's graph from a Nexus memory DB (idempotent).")
    parser.add_argument(
        "--nexus-db", required=True,
        help="Path to the Nexus memory sqlite DB (db_for('<user>','memory')).")
    parser.add_argument(
        "--mcpbrain-home", default=None,
        help="mcpbrain home dir (default: MCPBRAIN_HOME or the OS default). "
             "The store resolves to <home>/brain.sqlite3.")
    args = parser.parse_args(argv)

    from mcpbrain import config
    if args.mcpbrain_home:
        store_path = Path(args.mcpbrain_home) / "brain.sqlite3"
    else:
        store_path = config.store_path()

    # The vec table is dimensioned at first init() and can't change after, so
    # prefer the dim the store already recorded in meta. Only fall back to the
    # configured embedder's dim for a brand-new store (loading the embedder is
    # heavy — avoid it when the store already exists).
    dim = _existing_store_dim(store_path)
    if dim is None:
        from mcpbrain.embed import get_embedder
        dim = get_embedder(config.EMBEDDER).dim
    store = Store(store_path, dim=dim)
    store.init()

    summary = seed(args.nexus_db, store)
    print(
        f"Seeded from {args.nexus_db} into {store_path}:\n"
        f"  entities:  {summary['entities']}\n"
        f"  relations: {summary['relations']} "
        f"({summary['orphaned_relations']} orphaned)\n"
        f"  areas:     {summary['areas']}\n"
        f"  projects:  {summary['projects']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
