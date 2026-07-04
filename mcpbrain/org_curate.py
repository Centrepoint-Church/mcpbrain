"""Subsystem B3 — the curator.

A standard install with config.role='org_curator'. It curates claims, it does
not extract. Pipeline (daily cadence): ingest contribution JSONL from the fleet
into staging, deterministically merge (reusing resolve.py, role-address
guarded), count corroboration (distinct source_ref / contributor), adjudicate
what determinism can't settle on STRUCTURAL evidence only (verdict 'pending'
when it can't decide), and publish a versioned snapshot (manifest written LAST).
Reversible + capped, per the 0.7.84 brain-review hardening.

This module implements ingest, materialise (deterministic merge + corroboration
+ role-address guards), the adjudication seam (fuzzy structural-only candidate
packets, an injectable adjudicate() defaulting to all-pending, and a hardened
capped merge applier), and publish (versioned snapshot + tombstones written to
the fleet folder, manifest written last) plus run() — the full ingest ->
materialise -> dedup -> adjudicate -> apply -> publish pipeline.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone

from mcpbrain import config, graph_write, orgs, resolve
from mcpbrain.org_contracts import ContributionRecord, SnapshotManifest, Tombstone
from mcpbrain.resolve import (_NAME_MERGEABLE_TYPES, _candidate_pairs, _pick_winner,
                              is_role_address)

log = logging.getLogger(__name__)

# Fleet-relative paths the published snapshot lives at (spec B3.4/B5.4).
SNAPSHOT_PATH = "org-graph/snapshot.jsonl.gz"
TOMBSTONES_PATH = "org-graph/tombstones.jsonl"
MANIFEST_PATH = "org-graph/manifest.json"

# Relations that need independent-source corroboration before entering layer 1
# (spec B3.2): co-occurrence claimed by only one contributor never surfaces
# org-wide off a single fleet member's say-so.
_CORROBORATION_GUARDED = frozenset({"mentioned_with"})

# Within _CORROBORATION_GUARDED, these additionally require >=2 DISTINCT
# source_ref specifically — independent SOURCES, not merely independent
# people repeating the same single-source observation.
_STRICT_SOURCE_GUARDED = frozenset({"mentioned_with"})


def _ingest(store, fleet_storage) -> dict:
    """Read every contrib/**/*.jsonl batch into org_contrib_staging.

    Idempotent via the UNIQUE(contributor_email, source_ref, claim) constraint
    on org_contrib_staging: re-ingesting the same batch is a no-op. Malformed
    lines are logged and skipped rather than aborting the whole batch; an
    undecodable batch file is logged and skipped rather than aborting the
    whole run — one bad contributor must never block every other
    contributor's batch in the same cadence pass. A FleetStorage I/O error
    (list_paths or get_bytes raising — a real Drive backend can legitimately
    throw transiently) is treated the same way: log and continue rather than
    letting it propagate and abort ingestion of every OTHER contributor's
    already-enumerated batch.

    Returns {"batches": n, "ingested": rows_new}.
    """
    batches = 0
    ingested = 0
    try:
        paths = fleet_storage.list_paths("contrib/")
    except Exception as exc:  # noqa: BLE001 — one storage hiccup must not crash the cadence
        log.warning("curate: fleet_storage.list_paths failed, aborting this ingest pass: %s",
                    exc, exc_info=True)
        return {"batches": 0, "ingested": 0}
    for path in paths:
        if not path.endswith(".jsonl"):
            continue
        try:
            blob = fleet_storage.get_bytes(path)
        except Exception as exc:  # noqa: BLE001 — one bad path must not abort every other one
            log.warning("curate: fleet_storage.get_bytes failed for %s, skipping: %s",
                        path, exc, exc_info=True)
            continue
        if not blob:
            continue
        try:
            text = blob.decode("utf-8")
        except UnicodeDecodeError as exc:
            log.warning("curate: skipping undecodable contrib batch %s: %s", path, exc)
            continue
        batches += 1
        with store._connect() as db:
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = ContributionRecord.from_dict(json.loads(line))
                except (ValueError, KeyError, TypeError) as exc:
                    log.warning("curate: skipping malformed contrib line in %s: %s", path, exc)
                    continue
                cur = db.execute(
                    "INSERT OR IGNORE INTO org_contrib_staging"
                    "(contributor_email, source_ref, claim, confidence, valid_from, "
                    " valid_to, source_kind, batch_file) VALUES(?,?,?,?,?,?,?,?)",
                    (rec.contributor_email, rec.source_ref,
                     json.dumps(rec.claim, sort_keys=True), rec.confidence,
                     rec.valid_from, rec.valid_to, rec.source_kind, path))
                ingested += cur.rowcount
    return {"batches": batches, "ingested": ingested}


def _staged_claims(store) -> list[dict]:
    with store._connect() as db:
        return [dict(r) for r in db.execute(
            "SELECT contributor_email, source_ref, claim, confidence, valid_from, valid_to "
            "FROM org_contrib_staging ORDER BY id").fetchall()]


def _stamp_origin(store, *, entity_ids=(), relation_ids=()) -> None:
    with store._connect() as db:
        for eid in entity_ids:
            db.execute("UPDATE entities SET origin='org' WHERE id=?", (eid,))
        for rid in relation_ids:
            db.execute("UPDATE entity_relations SET origin='org' WHERE id=?", (rid,))


def _apply_org_skeleton(store, entity_id, aggregated_claim) -> None:
    """Authoritative org/email_addr write for a materialised org entity.

    graph_write.upsert_entity's B4a rule-3 guard exists to stop LOCAL writes
    from overwriting an org row's skeleton — but it can't distinguish "a
    local write" from "the curator's own re-materialise", so once an entity
    is first promoted to origin='org', every SUBSEQUENT _materialise run's
    upsert_entity call silently drops any org/email_addr change (a
    later-arriving email, a job-change org update). The curator IS the
    authority for the org layer, so this writes the current best-known
    aggregate (built from ALL staged claims, not just new ones —
    org_contrib_staging accumulates permanently) directly, bypassing that
    guard entirely. Confined to this module; the shared guard other writers
    rely on is untouched.

    Deliberately scoped to org/email_addr only — NOT name/type. Unlike
    org/email_addr, upsert_entity's existing-row branches never overwrite
    name/type after creation for ANY caller (org or local), so there is no
    equivalent gap to close there; overwriting them here would instead
    UNDO upsert_entity's own canonicalisation (canonical_org, strip_title)
    and could flip-flop a name across id-dedup boundaries where `entity_id`
    (the real, possibly-deduped store id) differs from what this claim_id's
    own name would naively produce. Blank org/email_addr in the aggregate
    are skipped rather than written, so this can only add/update skeleton
    knowledge, never blank out a value some earlier claim established."""
    updates = {}
    if aggregated_claim.get("org"):
        updates["org"] = aggregated_claim["org"]
    if aggregated_claim.get("email_addr"):
        updates["email_addr"] = aggregated_claim["email_addr"]
    if not updates:
        return
    set_clause = ", ".join(f"{k}=?" for k in updates)
    with store._connect() as db:
        db.execute(f"UPDATE entities SET {set_clause} WHERE id=?",
                   list(updates.values()) + [entity_id])


def _corroborated(relation: str, agg: dict) -> bool:
    """Corroboration rule (spec B3.2): a claim is corroborated by >=2 distinct
    source_ref OR >=2 distinct contributor_email — except relations in
    _STRICT_SOURCE_GUARDED (currently just mentioned_with), which require >=2
    distinct source_ref specifically: independent sources, not just
    independent people repeating one person's single-source observation."""
    distinct_sources = len(agg["srefs"])
    if relation in _STRICT_SOURCE_GUARDED:
        return distinct_sources >= 2
    return distinct_sources >= 2 or len(agg["contribs"]) >= 2


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _materialise(store, pin_allowlist=None) -> dict:
    """Materialise corroborated staged claims into origin='org' graph rows.

    Entities are written first (so relation endpoints exist), then relations
    under the corroboration guard (`_CORROBORATION_GUARDED`) and the
    role-address guard. A claim's local "id" (assigned by the contributing
    extractor) is NOT the store's real entity id — graph_write.upsert_entity
    may dedupe/rename it via email match, alias match, or org-name
    canonicalisation — so every materialised entity's claim-id is recorded in
    `id_map` and every relation's endpoints are resolved through that map. A
    relation whose endpoint claim never materialised (role-address guard,
    junk-name rejection, ...) is silently dropped rather than counted as
    pending: `pending` counts only guard-withheld corroboration, not dangling
    endpoints.

    Role-address guard: a person claim keyed on a shared/role inbox
    (resolve.is_role_address) is never materialised at all, not merely
    stripped of its email — claims are merged by claim-local id, and a role
    inbox may legitimately belong to different real people across different
    contributors, so merging their claims under one id would silently
    conflate distinct people (0.7.77's is_role_address rationale, applied one
    step earlier here: before identity is ever established, not just before
    it's keyed).

    pin_allowlist, when given, restricts materialisation to relation types the
    fleet has pinned (org_contracts.FleetPin.relation_allowlist); any other
    relation type is dropped without affecting the relations/pending counts.

    Supersessions: the valid_to from whichever staged claim has the most
    recent valid_from is applied to the materialised relation row — not the
    max valid_to ever staged, so a later claim reasserting the fact as
    ongoing (valid_to="") correctly overrides an earlier claim's reported end
    date. Applied only after ALL relation triples for this pass have been
    upserted (two-pass), and only onto a row graph_write's own cross-triple
    singleton-supersession left still current — a row already retired by
    that mechanism is never overwritten with a contributed value.

    Returns {"entities": n, "relations": n, "pending": n}.
    """
    rows = _staged_claims(store)
    entity_claims: dict = {}     # claim-id -> merged entity claim fields
    relation_claims: dict = {}   # (claim-id a, relation, claim-id b) -> aggregate
    for r in rows:
        claim = json.loads(r["claim"])
        kind = claim.get("kind")
        if kind == "entity":
            eid = claim.get("id")
            if not eid:
                continue
            if claim.get("type") == "person" and is_role_address(claim.get("email_addr", "")):
                continue
            prev = entity_claims.get(eid, {})
            entity_claims[eid] = {**prev, **{k: (claim.get(k) or prev.get(k) or "")
                                             for k in ("name", "type", "org", "email_addr", "aliases")}}
        elif kind == "relation":
            a, relation, b = claim.get("entity_a"), claim.get("relation"), claim.get("entity_b")
            if not (a and relation and b):
                continue
            key = (a, relation, b)
            agg = relation_claims.setdefault(key, {"srefs": set(), "contribs": set(),
                                                   "valid_from": "", "conf": 0.0,
                                                   "latest_vf": "", "latest_vf_valid_to": ""})
            agg["srefs"].add(r["source_ref"])
            agg["contribs"].add(r["contributor_email"])
            agg["conf"] = max(agg["conf"], float(r["confidence"] or 1.0))
            vf = r["valid_from"] or ""
            if vf and (not agg["valid_from"] or vf < agg["valid_from"]):
                agg["valid_from"] = vf
            # Track valid_to from whichever claim has the MOST RECENT valid_from,
            # not the max valid_to ever seen: a later claim reasserting the fact
            # as ongoing (valid_to="") must correctly override an earlier claim's
            # reported end date, rather than an old end-date claim sticking
            # forever once contributed (the original max-ever approach could
            # never "un-supersede" a relation once any contributor had reported
            # any end date for it, even after a newer contribution said it was
            # still current).
            vt = r["valid_to"] or ""
            if vf >= agg["latest_vf"]:
                agg["latest_vf"] = vf
                agg["latest_vf_valid_to"] = vt

    id_map: dict = {}   # claim-id -> real store entity id
    n_ent = 0
    for claim_id, e in entity_claims.items():
        got = graph_write.upsert_entity(
            store, name=e.get("name") or claim_id, entity_type=e.get("type") or "person",
            org=e.get("org", ""), email_addr=e.get("email_addr", ""), aliases=e.get("aliases", ""))
        if not got:
            continue
        id_map[claim_id] = got
        _stamp_origin(store, entity_ids=[got])
        _apply_org_skeleton(store, got, e)
        n_ent += 1

    # Pass 1: upsert every relation triple first, letting graph_write's own
    # cross-triple singleton-supersession cascade fully settle (e.g. a newer
    # "works_at beta" upsert retiring an older "works_at acme" row) BEFORE any
    # contributed valid_to is applied. Deferring the valid_to write to pass 2
    # avoids an ordering bug: applying a triple's own contributed valid_to
    # immediately (interleaved with pass 1) could be silently overwritten
    # moments later by upsert_relation's OWN supersession of that same row,
    # triggered by a *different* triple processed afterward in the same run.
    n_rel = 0
    pending = 0
    contributed_valid_to: list[tuple[int, str]] = []   # (rid, valid_to) to apply in pass 2
    for (a, relation, b), agg in relation_claims.items():
        if pin_allowlist is not None and relation not in pin_allowlist:
            continue
        if relation in _CORROBORATION_GUARDED and not _corroborated(relation, agg):
            pending += 1
            continue
        real_a, real_b = id_map.get(a), id_map.get(b)
        if not real_a or not real_b:
            continue
        rid = graph_write.upsert_relation(
            store, real_a, relation, real_b, valid_from=agg["valid_from"] or _today(),
            confidence=agg["conf"] or 1.0, source_doc_id="org-curated")
        if rid is None:
            continue
        _stamp_origin(store, relation_ids=[rid])
        n_rel += 1
        if agg["latest_vf_valid_to"]:
            contributed_valid_to.append((rid, agg["latest_vf_valid_to"]))

    # Pass 2: apply each triple's own contributed end-date ONLY if graph_write's
    # supersession cascade above left that row still current (valid_to blank).
    # If something else already retired it via the natural cross-triple
    # mechanism, that decision is based on comparing actual dated facts and is
    # trusted over blindly stomping it with a possibly-stale contributed value.
    if contributed_valid_to:
        with store._connect() as db:
            for rid, valid_to in contributed_valid_to:
                db.execute(
                    "UPDATE entity_relations SET valid_to=? "
                    "WHERE id=? AND (valid_to IS NULL OR valid_to='')",
                    (valid_to, rid))
    return {"entities": n_ent, "relations": n_rel, "pending": pending}


def _org_entities(store) -> list[dict]:
    with store._connect() as db:
        return [dict(r) for r in db.execute(
            "SELECT id, name, type, org, email_addr, aliases, mentions "
            "FROM entities WHERE origin='org' ORDER BY id").fetchall()]


def _build_adjudication_units(store) -> list[dict]:
    """Fuzzy same-type name-pair candidates among org entities, structural-only.

    Reuses resolve._candidate_pairs (blocking + token-set similarity, restricted
    to _NAME_MERGEABLE_TYPES) — the exact machinery the local fuzzy merge-review
    queue uses — so this only ever surfaces pairs a merge applier could act on.
    Each unit carries names/types/emails/aliases only, never message content:
    the curator adjudicates STRUCTURAL evidence, it never sees claim payloads.
    """
    ents = _org_entities(store)
    units = []
    for a, b in _candidate_pairs(ents):
        pair_id = "|".join(sorted((a["id"], b["id"])))
        units.append({"pair_id": pair_id,
                      "a": {k: a.get(k, "") for k in ("id", "name", "type", "email_addr", "aliases")},
                      "b": {k: b.get(k, "") for k in ("id", "name", "type", "email_addr", "aliases")}})
    return units


def adjudicate(units, *, home=None) -> list[dict]:
    """Adjudication seam (spec B3.3). Default: return no verdicts, so every unit
    stays 'pending' — the safe default when nothing has wired in a real curator.
    Tests and a future Haiku-wired curator monkeypatch/replace this to return
    [{"pair_id", "verdict": "merge"|"pending"|"skip", "canonical"?}, ...]."""
    return []


def _apply_merge_verdicts(store, verdicts, *, cap) -> dict:
    """Apply curator merge verdicts with the 0.7.84 brain-review hardening:
    re-fetch both entities from the store by their OWN id (never trust a
    verdict's embedded data), missing -> skip; enforce the _NAME_MERGEABLE_TYPES
    and role-address guards; cap the number of merges actually applied; and
    treat anything that isn't strictly "merge" (including "pending") as a
    no-op. Returns counts: {"merged", "guarded", "capped", "pending", "skipped"}.
    """
    result = {"merged": 0, "guarded": 0, "capped": 0, "pending": 0, "skipped": 0}
    for v in verdicts or []:
        verdict = v.get("verdict")
        ids = (v.get("pair_id") or "").split("|")
        if len(ids) != 2 or not all(ids) or ids[0] == ids[1]:
            result["skipped"] += 1
            continue
        if verdict == "pending":
            result["pending"] += 1
            continue
        if verdict != "merge":
            result["skipped"] += 1
            continue
        a = store.get_entity(ids[0])
        b = store.get_entity(ids[1])
        if a is None or b is None:
            result["skipped"] += 1
            continue
        if a["type"] not in _NAME_MERGEABLE_TYPES or b["type"] not in _NAME_MERGEABLE_TYPES:
            result["guarded"] += 1
            continue
        if is_role_address(a.get("email_addr", "")) or is_role_address(b.get("email_addr", "")):
            result["guarded"] += 1
            continue
        if result["merged"] >= cap:
            result["capped"] += 1
            continue
        winner, loser = _pick_winner(a, b)
        store.merge_entities(loser["id"], winner["id"],
                             canonical_name=v.get("canonical") or None, method="curator")
        result["merged"] += 1
    return result


def _snapshot_lines(store, home) -> list[str]:
    """Serialise the org layer to JSONL lines: entities, then relations, then a
    trailing org_taxonomy line — everything a consumer needs to rebuild the
    org graph and classify against the same taxonomy the curator used."""
    lines = []
    with store._connect() as db:
        for e in db.execute(
                "SELECT id, name, type, org, email_addr, aliases FROM entities "
                "WHERE origin='org' ORDER BY id").fetchall():
            lines.append(json.dumps({"kind": "entity", **dict(e)}, sort_keys=True))
        for r in db.execute(
                "SELECT entity_a, relation, entity_b, valid_from, valid_to, confidence "
                "FROM entity_relations WHERE origin='org' AND invalidated_at IS NULL "
                "ORDER BY id").fetchall():
            lines.append(json.dumps({"kind": "relation", **dict(r)}, sort_keys=True))
    lines.append(json.dumps({"kind": "org_taxonomy",
                             "names": list(orgs.taxonomy_from_config(home).names)}, sort_keys=True))
    return lines


# How far back org-winner merges are still republished as tombstones. Bounds
# tombstones.jsonl growth (otherwise unbounded: every publish would
# republish the FULL org-merge history forever) while staying safe for any
# consumer importing at least this often — see _tombstones' docstring for
# what happens to a consumer that falls further behind than this.
_TOMBSTONE_RETENTION_DAYS = 180


def _tombstones(store) -> list[Tombstone]:
    """Merged-away ids whose WINNER is currently an org-layer row, merged
    within the last _TOMBSTONE_RETENTION_DAYS, become tombstones pointing at
    that winner, so a consumer re-import never resurrects the loser id
    (spec B3.4/B5.4).

    Scoped to org-winner merges only — NOT every row in entity_merge_log. A
    curator install is a normal member machine too: its daily
    resolve_entities(curator=False) local-dedup cadence runs exactly like
    every other install's and merges the curator's own purely-local
    duplicates, logging them to the same entity_merge_log. Entity ids are
    name-derived slugs, so publishing those merges unfiltered would leak the
    curator's private local contacts' name-derived ids into the fleet-wide
    tombstones.jsonl file — a second egress channel that bypasses the entire
    fail-closed contribution edge (org_contrib.collect_from_drain) built
    specifically to prevent exactly that kind of leak.

    Retention bound: without one, _tombstones() re-serialises the org
    layer's ENTIRE merge history into tombstones.jsonl on every single daily
    publish, forever — unbounded growth in publish size and consumer-side
    processing cost. Only the underlying PUBLISHED list is bounded here;
    entity_merge_log itself (used elsewhere, well beyond org tombstones) is
    untouched, as is each consumer's own local org_repoint_log (so
    curator-SPLIT recovery via _restore_from_repoint_log, which reads a
    CONSUMER's own local log populated when THAT consumer processed a
    repoint, is unaffected by this bound). A consumer that hasn't
    successfully imported in longer than the retention window will simply
    miss the clean repoint for an aged-out merge and fall through to
    import_snapshot's normal wholesale-replace demote-if-attached fallback
    instead (the SAME degradation already accepted for a tombstone with no
    valid merge target) — not data loss, just a less clean outcome for a
    consumer already badly behind the fleet's daily cadence.
    """
    with store._connect() as db:
        org_winners = {r["id"] for r in db.execute(
            "SELECT id FROM entities WHERE origin='org'").fetchall()}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=_TOMBSTONE_RETENTION_DAYS)
             ).strftime("%Y-%m-%d %H:%M:%S")
    return [Tombstone(entity_id=m["loser_id"], merged_into=m["winner_id"])
            for m in store.list_entity_merges()
            if m["winner_id"] in org_winners and (m["at"] or "") >= cutoff]


def _publish(store, fleet_storage, home) -> SnapshotManifest:
    """Serialise the current org layer + tombstones into a versioned snapshot
    in the fleet folder. Version is tracked in meta['org_curator_version'] and
    incremented on every publish (never reused, even across empty runs).

    Ordering matters: the snapshot and tombstones are written FIRST, the
    manifest LAST, so a crash mid-publish never leaves a manifest pointing at
    a missing or stale snapshot — the manifest is the "here's what's ready"
    signal for consumers.
    """
    prev = int(store.get_meta("org_curator_version") or 0)
    version = prev + 1
    lines = _snapshot_lines(store, home)
    gz = gzip.compress(("\n".join(lines) + "\n").encode("utf-8"))
    tombs = _tombstones(store)
    n_ent = sum(1 for x in lines if json.loads(x)["kind"] == "entity")
    n_rel = sum(1 for x in lines if json.loads(x)["kind"] == "relation")
    manifest = SnapshotManifest(
        version=version, created_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        entity_count=n_ent, relation_count=n_rel, tombstone_count=len(tombs),
        snapshot_sha256=hashlib.sha256(gz).hexdigest())
    fleet_storage.put_bytes(SNAPSHOT_PATH, gz)
    fleet_storage.put_bytes(
        TOMBSTONES_PATH,
        ("\n".join(json.dumps(t.to_dict(), sort_keys=True) for t in tombs) + "\n").encode()
        if tombs else b"")
    fleet_storage.put_bytes(MANIFEST_PATH,
                            json.dumps(manifest.to_dict(), sort_keys=True).encode())
    store.set_meta("org_curator_version", str(version))
    return manifest


def run(store, fleet_storage, home) -> dict:
    """Full curator pass: ingest -> materialise -> deterministic dedup ->
    adjudicate -> apply -> publish. Safe to run repeatedly: ingest is
    idempotent (UNIQUE-constrained staging) and publish versions
    monotonically regardless of how much actually changed.

    Deterministic dedup here calls resolve.resolve_entities with curator=True
    (Task 11's B4a org<->org merge guard): the curator is the one caller
    allowed to merge org<->org entities during its own pass — local resolve
    callers (curator=False, the default) never do.
    """
    ing = _ingest(store, fleet_storage)
    mat = _materialise(store)
    resolve.resolve_entities(store, home=home, curator=True)
    units = _build_adjudication_units(store)
    cap = config.review_max_apply_per_run(home)
    verdicts = adjudicate(units, home=home)
    adj = _apply_merge_verdicts(store, verdicts, cap=cap)
    manifest = _publish(store, fleet_storage, home)
    return {"published": True, "version": manifest.version,
            "ingested": ing["ingested"], "materialised": mat,
            "adjudicated": adj, "units": len(units)}
