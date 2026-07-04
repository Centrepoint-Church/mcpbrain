"""Subsystem B4 — consumer import.

Fetches the curator's versioned snapshot and imports it as origin='org' rows
that coexist with local data. Wholesale-replace: org rows (entities AND
relations) absent from a newer snapshot are removed — but an org ENTITY is
DEMOTED to origin='local' rather than deleted when local relations/
observations hang off it, so import never orphans the user's own knowledge.
Tombstones re-point local references onto merge survivors so a stale import
can't resurrect a merged-away node; a tombstone with no valid merge target
falls back to the same demote-if-attached rule as wholesale-replace.

Everything (upsert + wholesale-replace + tombstones + version bump) happens
inside ONE transaction via a single `store._connect()` handle. Reads/writes
inside that transaction MUST go through that same `db` handle — calling
store convenience methods (which each open their own connection) from inside
an open write transaction would self-deadlock against SQLite's single-writer
lock (or silently read pre-transaction state under WAL). origin='local' rows
are never touched, at any step.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging

from mcpbrain.org_contracts import SnapshotManifest, Tombstone

log = logging.getLogger(__name__)

MANIFEST_PATH = "org-graph/manifest.json"
SNAPSHOT_PATH = "org-graph/snapshot.jsonl.gz"
TOMBSTONES_PATH = "org-graph/tombstones.jsonl"

_META_VERSION_KEY = "org_snapshot_version"


def _parse_snapshot(gz: bytes):
    entities, relations, taxonomy = [], [], []
    for line in gzip.decompress(gz).decode("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        kind = obj.get("kind")
        if kind == "entity":
            entities.append(obj)
        elif kind == "relation":
            relations.append(obj)
        elif kind == "org_taxonomy":
            taxonomy = obj.get("names", [])
    return entities, relations, taxonomy


def _has_local_attachments(db, entity_id) -> bool:
    """True if any origin='local' relation or any observation hangs off entity_id."""
    r = db.execute(
        "SELECT 1 FROM entity_relations WHERE (entity_a=? OR entity_b=?) AND origin='local' LIMIT 1",
        (entity_id, entity_id)).fetchone()
    if r:
        return True
    return db.execute(
        "SELECT 1 FROM entity_observations WHERE entity_id=? LIMIT 1", (entity_id,)).fetchone() is not None


def _entity_exists(db, entity_id) -> bool:
    return db.execute("SELECT 1 FROM entities WHERE id=?", (entity_id,)).fetchone() is not None


def _repoint_local_refs(db, from_id, to_id) -> None:
    """Re-point local relations/observations from a tombstoned id onto its survivor.

    UPDATE OR IGNORE moves what it can; a residual local relation that still
    references from_id afterwards must have collided with the UNIQUE
    (entity_a, relation, entity_b) constraint on a row already touching
    to_id — i.e. the fact is already represented against the survivor, so the
    duplicate is dropped rather than left dangling on a node about to vanish.
    """
    db.execute("UPDATE OR IGNORE entity_relations SET entity_a=? WHERE entity_a=? AND origin='local'",
               (to_id, from_id))
    db.execute("UPDATE OR IGNORE entity_relations SET entity_b=? WHERE entity_b=? AND origin='local'",
               (to_id, from_id))
    db.execute("DELETE FROM entity_relations WHERE (entity_a=? OR entity_b=?) AND origin='local'",
               (from_id, from_id))
    db.execute("UPDATE entity_observations SET entity_id=? WHERE entity_id=?", (to_id, from_id))


def _remove_org_entity(db, entity_id) -> None:
    db.execute("DELETE FROM entity_relations WHERE (entity_a=? OR entity_b=?) AND origin='org'",
               (entity_id, entity_id))
    db.execute("DELETE FROM entities WHERE id=?", (entity_id,))


def import_snapshot(store, fleet_storage) -> dict:
    """See module docstring. Signature + status vocabulary frozen — consumed by C onboarding too."""
    raw_manifest = fleet_storage.get_bytes(MANIFEST_PATH)
    if not raw_manifest:
        return {"status": "no_snapshot"}
    manifest = SnapshotManifest.from_dict(json.loads(raw_manifest))
    local_version = int(store.get_meta(_META_VERSION_KEY) or 0)
    if manifest.version <= local_version:
        return {"status": "unchanged", "version": local_version}

    gz = fleet_storage.get_bytes(SNAPSHOT_PATH)
    if not gz or hashlib.sha256(gz).hexdigest() != manifest.snapshot_sha256:
        log.warning("org_import: snapshot sha256 mismatch (v%s); aborting, previous layer intact",
                    manifest.version)
        return {"status": "error", "reason": "sha_mismatch"}

    entities, relations, _taxonomy = _parse_snapshot(gz)
    raw_tombs = fleet_storage.get_bytes(TOMBSTONES_PATH) or b""
    tombstones = [Tombstone.from_dict(json.loads(x))
                  for x in raw_tombs.decode("utf-8").splitlines() if x.strip()]

    snapshot_entity_ids = {e["id"] for e in entities}
    snapshot_relation_keys = {(r["entity_a"], r["relation"], r["entity_b"]) for r in relations}
    demoted = tombstoned = 0

    with store._connect() as db:
        # (1) Upsert snapshot entities as origin='org'. A same-id row already
        # marked origin='local' (a demoted former org node, or a genuine local
        # collision) is NEVER touched — the WHERE clause on DO UPDATE makes the
        # upsert a no-op for it, per the "local rows never touched" invariant.
        for e in entities:
            db.execute(
                "INSERT INTO entities(id,name,type,org,email_addr,aliases,origin,first_seen,last_seen) "
                "VALUES(?,?,?,?,?,?, 'org', '', '') "
                "ON CONFLICT(id) DO UPDATE SET "
                "  name=excluded.name, type=excluded.type, org=excluded.org, "
                "  email_addr=excluded.email_addr, "
                "  aliases=CASE WHEN entities.aliases='' THEN excluded.aliases ELSE entities.aliases END, "
                "  origin='org' "
                "WHERE entities.origin != 'local'",
                (e["id"], e.get("name", ""), e.get("type", "person"), e.get("org", ""),
                 e.get("email_addr", ""), e.get("aliases", "")))

        # (2) Upsert snapshot relations as origin='org'. Same local-never-touched
        # guard: a relation triple already claimed by a local edit keeps its
        # origin='local' row untouched (the UNIQUE(entity_a,relation,entity_b)
        # constraint means there is only ever one row per triple).
        for r in relations:
            db.execute(
                "INSERT INTO entity_relations"
                "(entity_a,relation,entity_b,valid_from,valid_to,confidence,origin,source_doc_id) "
                "VALUES(?,?,?,?,?,?, 'org', 'org-snapshot') "
                "ON CONFLICT(entity_a,relation,entity_b) DO UPDATE SET "
                "  valid_from=excluded.valid_from, valid_to=excluded.valid_to, "
                "  confidence=excluded.confidence, source_doc_id=excluded.source_doc_id, "
                "  origin='org' "
                "WHERE entity_relations.origin != 'local'",
                (r["entity_a"], r["relation"], r["entity_b"], r.get("valid_from", ""),
                 r.get("valid_to", ""), r.get("confidence", 1.0)))

        # (3) Wholesale-replace, relation level: an org relation whose triple no
        # longer appears in the snapshot is dropped. Relations carry no
        # "attached local data" of their own (a local edit already lives under
        # origin='local', a distinct row by the unique-triple constraint), so
        # no demote case applies here.
        for row in db.execute(
                "SELECT id, entity_a, relation, entity_b FROM entity_relations WHERE origin='org'").fetchall():
            if (row["entity_a"], row["relation"], row["entity_b"]) not in snapshot_relation_keys:
                db.execute("DELETE FROM entity_relations WHERE id=?", (row["id"],))

        # (4) Wholesale-replace, entity level: an org entity absent from the
        # snapshot is removed — but DEMOTED to origin='local' instead when local
        # relations/observations are attached, so import never orphans the
        # user's own knowledge (spec B4).
        for eid in [row["id"] for row in
                    db.execute("SELECT id FROM entities WHERE origin='org'").fetchall()]:
            if eid in snapshot_entity_ids:
                continue
            if _has_local_attachments(db, eid):
                db.execute("UPDATE entities SET origin='local' WHERE id=?", (eid,))
                demoted += 1
            else:
                _remove_org_entity(db, eid)

        # (5) Tombstones: re-point local references onto the merge survivor
        # (logged to org_repoint_log so a later curator split can restore local
        # flesh — spec B4a rule 4), then remove the tombstoned node. A
        # tombstone with no valid merge target falls back to the same
        # demote-if-attached rule as step (4) rather than destroying local data.
        for t in tombstones:
            if not _entity_exists(db, t.entity_id):
                continue
            if t.merged_into and _entity_exists(db, t.merged_into):
                _repoint_local_refs(db, t.entity_id, t.merged_into)
                db.execute(
                    "INSERT INTO org_repoint_log(from_entity_id, to_entity_id, snapshot_version, reason) "
                    "VALUES(?,?,?,?)",
                    (t.entity_id, t.merged_into, manifest.version, "tombstone"))
                _remove_org_entity(db, t.entity_id)
                tombstoned += 1
            elif _has_local_attachments(db, t.entity_id):
                db.execute("UPDATE entities SET origin='local' WHERE id=?", (t.entity_id,))
                demoted += 1
            else:
                _remove_org_entity(db, t.entity_id)
                tombstoned += 1

        # (6) Version bump, inside the same transaction as the data mutations
        # above: a crash between them must never leave the store thinking it
        # has ingested a version whose rows didn't actually land (or vice versa).
        db.execute("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)",
                   (_META_VERSION_KEY, str(manifest.version)))

    return {"status": "imported", "version": manifest.version,
            "entities": len(entities), "relations": len(relations),
            "tombstoned": tombstoned, "demoted": demoted}
