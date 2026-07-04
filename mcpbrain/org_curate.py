"""Subsystem B3 — the curator.

A standard install with config.role='org_curator'. It curates claims, it does
not extract. Pipeline (daily cadence): ingest contribution JSONL from the fleet
into staging, deterministically merge (reusing resolve.py, role-address
guarded), count corroboration (distinct source_ref / contributor), adjudicate
what determinism can't settle on STRUCTURAL evidence only (verdict 'pending'
when it can't decide), and publish a versioned snapshot (manifest written LAST).
Reversible + capped, per the 0.7.84 brain-review hardening.

This module currently implements ingest, materialise (deterministic merge +
corroboration + role-address guards), and the adjudication seam (fuzzy
structural-only candidate packets, an injectable adjudicate() defaulting to
all-pending, and a hardened capped merge applier). The publish step (versioned
snapshot, manifest written last) is a later task.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from mcpbrain import graph_write
from mcpbrain.org_contracts import ContributionRecord
from mcpbrain.resolve import (_NAME_MERGEABLE_TYPES, _candidate_pairs, _pick_winner,
                              is_role_address)

log = logging.getLogger(__name__)

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
    contributor's batch in the same cadence pass.

    Returns {"batches": n, "ingested": rows_new}.
    """
    batches = 0
    ingested = 0
    for path in fleet_storage.list_paths("contrib/"):
        if not path.endswith(".jsonl"):
            continue
        blob = fleet_storage.get_bytes(path)
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

    Supersessions: a claim's latest (max) valid_to is applied to the
    materialised relation row after upsert.

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
                                                   "valid_from": "", "valid_to": "", "conf": 0.0})
            agg["srefs"].add(r["source_ref"])
            agg["contribs"].add(r["contributor_email"])
            agg["conf"] = max(agg["conf"], float(r["confidence"] or 1.0))
            vf = r["valid_from"] or ""
            if vf and (not agg["valid_from"] or vf < agg["valid_from"]):
                agg["valid_from"] = vf
            vt = r["valid_to"] or ""
            if vt > agg["valid_to"]:
                agg["valid_to"] = vt

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
        n_ent += 1

    n_rel = 0
    pending = 0
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
        if agg["valid_to"]:
            with store._connect() as db:
                db.execute("UPDATE entity_relations SET valid_to=? WHERE id=?",
                           (agg["valid_to"], rid))
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
