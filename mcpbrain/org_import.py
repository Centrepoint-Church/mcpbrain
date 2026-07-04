"""Subsystem B4 — consumer import.

Fetches the curator's versioned snapshot and imports it as origin='org' rows
that coexist with local data. Wholesale-replace: org rows (entities AND
relations) absent from a newer snapshot are removed — but an org ENTITY is
DEMOTED to origin='local' rather than deleted when local relations/
observations hang off it, so import never orphans the user's own knowledge.
Tombstones re-point local references onto merge survivors so a stale import
can't resurrect a merged-away node; a tombstone with no valid merge target
falls back to the same demote-if-attached rule as wholesale-replace.

Before any of that, slug-drift reconciliation (_reconcile_slug_drift) folds
existing LOCAL entities into their incoming org twin — on a shared,
non-role email or an unambiguous alias/name-token match — so a curator
re-keying/renaming a node doesn't strand the user's local observations on an
orphaned duplicate; the org id always survives. Each such merge, and every
tombstone repoint, is logged to org_repoint_log so a later curator SPLIT can
be recovered by _restore_from_repoint_log, which re-attaches local flesh from
the merge target back onto a resurrected id.

Everything from step (1) on (upsert + wholesale-replace + tombstones +
version bump) happens inside ONE transaction via a single `store._connect()`
handle. Reads/writes inside that transaction MUST go through that same `db`
handle — calling store convenience methods (which each open their own
connection) from inside an open write transaction would self-deadlock
against SQLite's single-writer lock (or silently read pre-transaction state
under WAL). The slug-drift/restore step runs BEFORE that transaction opens
for exactly this reason: it calls store.merge_entities/get_entity, which
manage their own connections. origin='local' rows are never touched, at any
step, except to be merged (never deleted outright) into their org twin by
slug-drift reconciliation — the one intentional exception, and even then the
local row's flesh is carried forward onto the survivor, never dropped.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging

from mcpbrain.org_contracts import SnapshotManifest, Tombstone
from mcpbrain.resolve import canonical_key, is_role_address, _token_set_ratio, _tokens

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


def _is_org_entity(db, entity_id) -> bool:
    """True only if entity_id currently names an origin='org' row.

    Entity ids are deterministic name-slugs, so a local extraction and an
    org-snapshot entity can legitimately collide on id — a locally-created
    row at that id is never touched by the upsert steps (their WHERE
    origin != 'local' guard), so it can be sitting there unpromoted when a
    tombstone naming that same id arrives. Every tombstone-loop identity
    check MUST go through this (never a plain existence check), or a
    genuinely local entity that happens to share an id with a tombstoned
    org id gets repointed-away-from and permanently deleted.
    """
    return db.execute(
        "SELECT 1 FROM entities WHERE id=? AND origin='org'", (entity_id,)).fetchone() is not None


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


def _resolve_tombstone_chains(tombstones: list[Tombstone]) -> list[Tombstone]:
    """Collapse transitive merge chains (A->B, B->C) into direct pointers
    (A->C) before any tombstone is applied.

    Because org_curate._tombstones() republishes the full entity_merge_log
    history on every publish, a single snapshot can carry both links of a
    chain formed by two separate curator merges over time. Applying them as
    given makes the outcome depend on which link the processing loop happens
    to reach first: if B->C runs before A->B, then by the time A->B is
    processed B (its merge target) has ALREADY been removed, so the
    org='org'-gated check correctly refuses to repoint into a dead target —
    but the fallback then DEMOTES A instead of consolidating it onto C,
    stranding A's local flesh on an orphaned duplicate rather than the node
    it actually belongs on. Pre-resolving every entity_id to its ultimate,
    non-tombstoned target makes the result identical regardless of list
    order, and keeps org_repoint_log's entries consistent with where the
    data actually landed (so a later _restore_from_repoint_log lookup for
    the ORIGINAL id still finds its way to the correct final survivor)."""
    chain = {t.entity_id: t.merged_into for t in tombstones if t.merged_into}
    resolved = []
    for t in tombstones:
        target = t.merged_into
        seen = {t.entity_id}
        while target in chain and target not in seen:
            seen.add(target)
            target = chain[target]
        resolved.append(Tombstone(entity_id=t.entity_id, merged_into=target))
    return resolved


def _log_repoint(store, from_id, to_id, version, reason) -> None:
    with store._connect() as db:
        db.execute(
            "INSERT INTO org_repoint_log(from_entity_id,to_entity_id,snapshot_version,reason) "
            "VALUES(?,?,?,?)", (from_id, to_id, version, reason))


def _reconcile_slug_drift(store, entities, version) -> int:
    """Reconcile incoming org entities against existing LOCAL entities whose id
    drifted from the org slug (curator renamed/re-keyed the same real-world
    identity). The LOCAL variant merges INTO the org node — the org id always
    survives (spec B4a rule 2) — so local observations/relations don't end up
    stranded on an orphaned duplicate once the org id lands separately.

    Two match strategies, both required to be unambiguous (exactly one
    candidate) before auto-merging:
      (a) shared, non-empty, non-role email address — deterministic identity.
      (b) same type + (canonical-key match, OR local name is an org-supplied
          alias, OR token-set similarity >= 0.8) — a fuzzy but still
          same-type, high-confidence name match.
    A local row sharing the incoming id itself is a plain id collision (the
    upsert step already guards that case), not a drift candidate, and is
    excluded here. Anything else — role-address pairs, or an ambiguous
    (multiple-candidate or weak) name-only match — is left untouched for the
    local fuzzy-review queue; auto-merging an ambiguous pair risks folding two
    distinct people together, which is unrecoverable without curator help.

    Fan-in is ALSO ambiguity: if two different org entities in this same
    snapshot both independently match the same local candidate (an upstream
    data-quality hiccup — e.g. two org entities sharing an email that the
    curator's own dedup should have caught but hasn't yet), the local
    candidate's true identity can't be determined by this consumer either.
    Matches are collected in a first pass and only applied in a second pass
    when a local candidate maps to exactly one org entity — a candidate
    claimed by 2+ org entities is left for the local fuzzy-review queue on
    every one of them, the same as any other ambiguous match, rather than
    silently letting whichever org entity happens to be processed first win.

    Each applied merge is logged to org_repoint_log (reason='slug_drift') so
    a later curator SPLIT can restore the local flesh (see
    _restore_from_repoint_log).
    """
    with store._connect() as db:
        local = [dict(r) for r in db.execute(
            "SELECT id,name,type,email_addr,aliases FROM entities WHERE origin='local'").fetchall()]
    local_by_email = {}
    for l in local:
        em = (l.get("email_addr") or "").strip().lower()
        if em and not is_role_address(em):
            local_by_email.setdefault(em, []).append(l)

    # Pass 1: collect each org entity's candidate match, without applying any
    # merge yet, so fan-in (multiple org entities matching one local id) can
    # be detected before anything is claimed.
    candidate_for: dict = {}   # org_id -> local candidate id
    for e in entities:
        org_id = e["id"]
        target = None

        # (a) email-equality — deterministic, role-address guarded.
        em = (e.get("email_addr") or "").strip().lower()
        if em and not is_role_address(em):
            cands = [l for l in local_by_email.get(em, []) if l["id"] != org_id]
            if len(cands) == 1:
                target = cands[0]

        # (b) alias / canonical-key / token-set match, same type only. Also
        # role-guarded on the ORG side (not just the local side below): an
        # org entity that is itself a role inbox must never absorb a real
        # local person's flesh via a name/token match either — "role-address
        # pairs never auto-merge" holds regardless of which match method
        # would otherwise fire.
        if target is None and not is_role_address(em):
            org_toks = _tokens(e.get("name", ""))
            org_key = canonical_key(e.get("name", ""))
            org_aliases = {a.strip().lower() for a in (e.get("aliases") or "").split(",") if a.strip()}
            matches = []
            for l in local:
                if l["id"] == org_id or l["type"] != e.get("type", "person"):
                    continue
                if is_role_address(l.get("email_addr") or ""):
                    continue
                lk = canonical_key(l["name"])
                lname = l["name"].strip().lower()
                if lk and lk == org_key:
                    matches.append(l)
                elif lname in org_aliases:
                    matches.append(l)
                elif _token_set_ratio(org_toks, _tokens(l["name"])) >= 0.8:
                    matches.append(l)
            if len(matches) == 1:            # single unambiguous match only
                target = matches[0]

        if target is not None:
            candidate_for[org_id] = target["id"]

    # Fan-in check: a local candidate matched by 2+ org entities is ambiguous
    # from this consumer's point of view, same as any other multi-candidate
    # match — drop every org entity's claim on it rather than letting
    # whichever happens to be processed first silently win.
    claim_count: dict = {}
    for local_id in candidate_for.values():
        claim_count[local_id] = claim_count.get(local_id, 0) + 1
    unambiguous = {org_id: local_id for org_id, local_id in candidate_for.items()
                  if claim_count[local_id] == 1}

    merged = 0
    for org_id, local_id in unambiguous.items():
        # Re-check both ids still exist right before merging: import_snapshot
        # stubs org ids before calling this, so org_id should already be
        # materialised; local_id is re-verified in case something upstream
        # already touched it. Only log/count a repoint that actually happens.
        if store.get_entity(local_id) is not None and store.get_entity(org_id) is not None:
            # Local merges INTO org (org id survives — B4a rule 2). The org row
            # must already be materialised (import_snapshot stubs it before
            # calling this) for merge_entities to have a winner to fold into.
            store.merge_entities(local_id, org_id, method="slug_drift")
            _log_repoint(store, local_id, org_id, version, "slug_drift")
            merged += 1
    return merged


def _restore_from_repoint_log(store, entities) -> int:
    """Curator-SPLIT recovery (spec B4a rule 4): when a snapshot re-introduces
    an id that an earlier repoint (tombstone OR slug-drift merge) had folded
    away, re-materialise the resurrected node and move the local flesh that
    had landed on the merge target back onto it, via the same
    _repoint_local_refs helper the tombstone step uses (just with from/to
    swapped). Must run BEFORE the generic org-id stub step, since that stub
    would otherwise pre-create the resurrected id and make it look
    already-present here.

    Bounded observation restore: entity_observations carry no per-row
    provenance, so once a merge has happened there is no way to tell which of
    the target's observations originated on the resurrected node versus
    natively on the target. Restoring ALL of them (the naive approach) can
    move back an observation that was always native to the target, added
    well after the original merge. Since org_repoint_log already stamps the
    merge time (`at`, default CURRENT_TIMESTAMP) and entity_observations
    already carries `valid_from`, this restores only observations recorded
    at-or-before the original repoint — anything the target accrued
    natively AFTER the merge stays put. This is an approximation where
    `valid_from` is date-only and `at` is a full timestamp (a same-day
    observation compares as "at or before" a same-day repoint), not exact
    provenance, but is a real improvement over unconditionally moving
    everything; true per-row provenance would be a schema change, out of
    scope here. Local relations carry no such accumulation-ambiguity (a
    relation is a single fact, not an append-only log of a person's
    knowledge, and the unique-triple constraint prevents duplication), so
    they are still moved back wholesale.
    """
    incoming = {e["id"]: e for e in entities}
    restored = 0
    with store._connect() as db:
        logs = [dict(r) for r in db.execute(
            "SELECT from_entity_id, to_entity_id, at FROM org_repoint_log").fetchall()]
        for lg in logs:
            resurrected, target, repoint_at = lg["from_entity_id"], lg["to_entity_id"], lg["at"]
            if resurrected not in incoming:
                continue
            if db.execute("SELECT 1 FROM entities WHERE id=?", (resurrected,)).fetchone() is not None:
                continue  # already present (either never removed, or already restored)
            e = incoming[resurrected]
            db.execute(
                "INSERT INTO entities(id,name,type,origin,first_seen,last_seen) "
                "VALUES(?,?,?, 'org', '', '')",
                (resurrected, e.get("name", ""), e.get("type", "person")))
            db.execute("UPDATE OR IGNORE entity_relations SET entity_a=? WHERE entity_a=? AND origin='local'",
                       (resurrected, target))
            db.execute("UPDATE OR IGNORE entity_relations SET entity_b=? WHERE entity_b=? AND origin='local'",
                       (resurrected, target))
            db.execute("DELETE FROM entity_relations WHERE (entity_a=? OR entity_b=?) AND origin='local'",
                       (target, target))
            db.execute("UPDATE entity_observations SET entity_id=? "
                       "WHERE entity_id=? AND (valid_from IS NULL OR valid_from='' OR valid_from<=?)",
                       (resurrected, target, repoint_at))
            restored += 1
    return restored


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
    tombstones = _resolve_tombstone_chains(tombstones)

    snapshot_entity_ids = {e["id"] for e in entities}
    snapshot_relation_keys = {(r["entity_a"], r["relation"], r["entity_b"]) for r in relations}
    demoted = tombstoned = 0

    # (0) Slug-drift reconciliation + curator-split restore (spec B4/B4a),
    # each its OWN sequence of short transactions, run BEFORE the single big
    # transaction below. store.merge_entities/get_entity open their own
    # connection, so they must never be called from inside that transaction's
    # `with store._connect() as db:` block (self-deadlock / stale-WAL-read
    # risk — see module docstring); running them here, before that block
    # opens, is what keeps this safe.
    #
    # Order matters: restore MUST run before the generic org-id stub below,
    # or the stub's blanket INSERT OR IGNORE would pre-create a resurrected
    # id and make it look already-present to the restore check. Reconcile
    # runs after stubbing so merge_entities always has a materialised org
    # winner to fold the local loser into.
    restored = _restore_from_repoint_log(store, entities)
    with store._connect() as db:
        for e in entities:                          # stub org ids so reconcile can merge into them
            db.execute("INSERT OR IGNORE INTO entities(id,name,type,origin,first_seen,last_seen) "
                       "VALUES(?,?,?, 'org', '', '')",
                       (e["id"], e.get("name", ""), e.get("type", "person")))
    reconciled = _reconcile_slug_drift(store, entities, manifest.version)

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

        # (4) Tombstones: re-point local references onto the merge survivor
        # (logged to org_repoint_log so a later curator split can restore local
        # flesh — spec B4a rule 4), then remove the tombstoned node. A
        # tombstone with no valid merge target falls back to the same
        # demote-if-attached rule as step (5) rather than destroying local data.
        # This MUST run before step (5)'s generic wholesale-replace: a
        # tombstoned entity is also "absent from the new snapshot", and step
        # (5) demoting it first (its only recourse, since it carries no merge
        # target) would make it origin='local' before this step's org-gated
        # checks ever see it — silently skipping the repoint the tombstone
        # specifically asked for. Every identity check here is
        # origin='org'-gated (_is_org_entity), not a plain existence check: a
        # tombstoned id can currently belong to an unrelated origin='local' row
        # (entity ids are deterministic name-slugs, so local/org collisions are
        # the common case, not an edge case), and such a row must be left
        # completely untouched, per the "local rows never touched, under any
        # circumstance" invariant.
        for t in tombstones:
            if not _is_org_entity(db, t.entity_id):
                continue
            if t.merged_into and _is_org_entity(db, t.merged_into):
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

        # (5) Wholesale-replace, entity level: an org entity absent from the
        # snapshot is removed — but DEMOTED to origin='local' instead when local
        # relations/observations are attached, so import never orphans the
        # user's own knowledge (spec B4). Tombstoned entities were already
        # handled by step (4) and are gone by now, so this only ever catches
        # entities that are simply absent, not merged away.
        for eid in [row["id"] for row in
                    db.execute("SELECT id FROM entities WHERE origin='org'").fetchall()]:
            if eid in snapshot_entity_ids:
                continue
            if _has_local_attachments(db, eid):
                db.execute("UPDATE entities SET origin='local' WHERE id=?", (eid,))
                demoted += 1
            else:
                _remove_org_entity(db, eid)

        # (6) Version bump, inside the same transaction as the data mutations
        # above: a crash between them must never leave the store thinking it
        # has ingested a version whose rows didn't actually land (or vice versa).
        db.execute("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)",
                   (_META_VERSION_KEY, str(manifest.version)))

    return {"status": "imported", "version": manifest.version,
            "entities": len(entities), "relations": len(relations),
            "tombstoned": tombstoned, "demoted": demoted,
            "reconciled": reconciled, "restored": restored}
