#!/usr/bin/env python3
"""Repoint rows still pointing at a merged-away entity onto its merge winner.

ATTENDED ONLY. Dry-run by default; --yes required. Stop the daemon first and
confirm a recent verified backup.

Why this exists: store.merge_entities repoints relations, observations and email
links correctly, but it is a NO-OP once the loser row is gone
(`if loser is None or win is None: return`), so it cannot clean residue after
the fact. Residue still appears two ways:

  * a write path re-creates the dead id after the merge. That is what happened
    on the live store — the calendar attendee pass minted `attended` edges from
    slugify(configured owner name) = 'joshua-kemp' seven weeks after it was
    merged into 'josh-kemp'. Fixed at the source by
    graph_write.resolve_owner_entity_id (PR #25, shipped 0.7.119); this sweep
    clears what was written before that landed.
  * entity_communities is never repointed by merge_entities at all — it relies
    on ON DELETE CASCADE, which cannot fire for a row inserted AFTER the entity
    was already deleted. replace_communities did exactly that while foreign_keys
    was off.

Consequences were real and silent: ONE dead id (`joshua-kemp`) made every
replace_communities transaction fail its FK and roll back, so community data
froze for five days while the stale rows kept being served.

The merge log is the authority for winners — never guess.
"""
import argparse
import sys

from mcpbrain import config
from mcpbrain.embed import embedder_dim
from mcpbrain.store import Store

# (table, column) pairs that reference entities.id and can hold residue.
# entity_observations and entity_relations.entity_b are covered too; the
# relation table needs both endpoint columns.
_RESIDUE = (
    ("entity_relations", "entity_a"),
    ("entity_relations", "entity_b"),
    ("entity_observations", "entity_id"),
    ("email_entities", "entity_id"),
    ("entity_communities", "entity_id"),
)


def scan(store) -> list[dict]:
    """Merge-log losers that no longer exist but are still referenced.

    Returns [{"loser_id", "winner_id", "counts": {table: n}}], only for losers
    with at least one referencing row and a winner that still exists.
    """
    out = []
    with store._connect() as db:
        pairs = db.execute(
            "SELECT DISTINCT loser_id, winner_id FROM entity_merge_log "
            "WHERE loser_id NOT IN (SELECT id FROM entities) "
            "  AND winner_id IN (SELECT id FROM entities)").fetchall()
        for p in pairs:
            counts: dict = {}
            for table, col in _RESIDUE:
                n = db.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {col}=?", (p["loser_id"],)
                ).fetchone()[0]
                if n:
                    counts[table] = counts.get(table, 0) + n
            if counts:
                out.append({"loser_id": p["loser_id"], "winner_id": p["winner_id"],
                            "counts": counts})
    return out


def apply(store, plan: list[dict]) -> int:
    """Repoint each planned loser's residue onto its winner. Returns rows moved.

    Mirrors merge_entities' semantics exactly: UPDATE OR IGNORE so a row that
    would duplicate an edge the winner already holds is dropped rather than
    violating UNIQUE(entity_a,relation,entity_b) (on the live store josh-kemp
    already carried `attended campus-pastors`), then the leftover loser rows are
    deleted, then any self-loop the repoint created on the winner.
    """
    moved = 0
    if not plan:
        return 0
    with store._connect(write=True) as db:
        for item in plan:
            loser, winner = item["loser_id"], item["winner_id"]
            for table, col in _RESIDUE:
                before = db.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {col}=?", (loser,)).fetchone()[0]
                if not before:
                    continue
                db.execute(f"UPDATE OR IGNORE {table} SET {col}=? WHERE {col}=?",
                           (winner, loser))
                db.execute(f"DELETE FROM {table} WHERE {col}=?", (loser,))  # admin-delete-ok
                moved += before
            db.execute(
                "DELETE FROM entity_relations WHERE entity_a=entity_b AND entity_a=?",  # admin-delete-ok
                (winner,))
    return moved


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--yes", action="store_true",
                    help="actually write; without it this is a dry run")
    args = ap.parse_args(argv)
    store = Store(config.store_path(), dim=embedder_dim("bge-small"))
    plan = scan(store)
    if not plan:
        print("[residue] none — no merge-log loser is still referenced")
        return 0
    for item in plan:
        print(f"[residue] {item['loser_id']} -> {item['winner_id']}: {item['counts']}")
    if not args.yes:
        print("[residue] DRY RUN — re-run with --yes to repoint")
        return 0
    print(f"[residue] repointed {apply(store, plan)} row(s)")
    with store._connect() as db:
        left = db.execute("PRAGMA foreign_key_check").fetchall()
    print(f"[residue] foreign_key_check now: {len(left)} violation(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
