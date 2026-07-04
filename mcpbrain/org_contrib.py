"""Subsystem B2 — the contribution edge.

After an enrichment drain, allowlisted entity/relation deltas are filtered
fail-closed, redacted into org_contracts.ContributionRecords, and queued in the
local org_contrib_outbox. A daily cadence (_run_org_contrib_upload) uploads the
pending rows as one JSONL batch to the fleet folder. Nothing content-shaped ever
leaves the machine: no chunk text, no profile/notes/mentions, no raw doc_id
(only its HMAC), no non-allowlisted type, no role-address-keyed person, no
cold-sourced claim, and nothing at all until the fleet is pinned.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

from mcpbrain.org_contracts import ContributionRecord, FleetPin, FleetStorage, source_ref
from mcpbrain.resolve import is_role_address

log = logging.getLogger(__name__)

# Layer-1 entity types (spec B1). Fixed allowlist — a new/unknown type is never
# contributed (fail-safe), mirroring resolve._NAME_MERGEABLE_TYPES.
_LAYER1_ENTITY_TYPES = frozenset({"person", "org", "project"})

# Lowercase particles allowed inside a proper name ("van Gogh", "de la Cruz").
_NAME_PARTICLES = frozenset({"van","von","de","del","della","der","den","di","da","la","le","du","bin","al","mac","mc"})


def _clean_name(name: str) -> str:
    """Strip private free-text annotations from a name/alias before it can leave
    the machine: parentheticals, brackets, and text after a note separator.
    'Bob Smith (my divorce lawyer)' -> 'Bob Smith'; 'Jane - re: lawsuit' -> 'Jane'."""
    n = re.split(r"[(\[]", name, 1)[0]
    n = re.split(r"\s[-–—/;:]\s", n, 1)[0]
    return " ".join(n.split()).strip()


def _is_name_like(token: str) -> bool:
    """A token is a shareable name variant only if it looks like a name — not a
    free-text note. Rejects brackets, @, digits-as-notes, and over-long strings."""
    t = token.strip()
    if not t or len(t) > 40 or any(c in t for c in "()[]{}<>@\n\t"):
        return False
    if not all(c.isalpha() or c in " .,'-" for c in t):
        return False
    words = [w for w in t.replace(".", " ").replace(",", " ").split() if w]
    if not words:
        return False
    return all(w[0].isupper() or w.lower() in _NAME_PARTICLES for w in words)


def _safe_aliases(aliases: str) -> str:
    """Keep only name-like alias tokens (cleaned), dropping free-text. Accepts the
    store's comma- OR pipe-delimited alias strings (merge_entities pipe-joins)."""
    toks = [a for a in re.split(r"[|,]", aliases or "") if a.strip()]
    keep = [_clean_name(t) for t in toks if _is_name_like(t)]
    return ",".join(dict.fromkeys(k for k in keep if k))


def _safe_entity_claim(e: dict) -> dict | None:
    """Build the redacted, identity-anchored entity claim, or None to drop it
    (which also drops any relation touching it — fail closed). Decision: a
    PERSON's name/aliases leave the machine only when anchored to their own
    email identity (the deterministic, non-annotated id); without an email we
    cannot vouch the name isn't a private annotation, so the person is not
    contributed. org/project names are taxonomy/slug-canonical and are cleaned."""
    etype = e.get("type", "")
    if etype not in _LAYER1_ENTITY_TYPES:
        return None
    email = (e.get("email_addr") or "").strip()
    if etype == "person" and (not email or is_role_address(email)):
        return None
    name = _clean_name(e.get("name", "") or "")
    if not name:
        return None
    # A PERSON's name must also read like a name, not free-text embedded without a
    # separator that _clean_name would strip ("Bob divorce lawyer"). Aliases already
    # go through _is_name_like; the primary name did not, leaving that residual leak.
    # org/project names are taxonomy-canonical (may contain digits/lowercase, e.g.
    # "Q3 Budget"), so they keep _clean_name only.
    if etype == "person" and not _is_name_like(name):
        return None
    return {"kind": "entity", "id": e["id"], "name": name, "type": etype,
            "org": e.get("org", "") or "", "email_addr": email,
            "aliases": _safe_aliases(e.get("aliases", "") or "")}


def _is_cold(store, doc_id: str) -> bool:
    """True if the provenance doc is a cold (salience-gated) chunk. Cold chunks
    skip graph-extraction, so this is belt-and-suspenders — but the edge filter
    must fail closed on them regardless of how a row got written."""
    if not doc_id:
        return True
    with store._connect() as db:
        r = db.execute(
            "SELECT 1 FROM chunks WHERE doc_id=? AND enrich_state='cold' LIMIT 1",
            (doc_id,)).fetchone()
    return r is not None


def _source_kind(store, doc_id: str) -> str:
    """Map a doc's chunk metadata source_type to a contribution source_kind
    (email|drive|calendar). Never reveals the doc id itself."""
    with store._connect() as db:
        r = db.execute("SELECT metadata FROM chunks WHERE doc_id=? LIMIT 1",
                       (doc_id,)).fetchone()
    st = ""
    if r and r["metadata"]:
        try:
            st = (json.loads(r["metadata"]) or {}).get("source_type", "") or ""
        except (ValueError, TypeError):
            st = ""
    # Honest labelling: an unrecognised/absent source_type becomes "unknown", not
    # a silent mislabel as "email" (which would misattribute provenance for any
    # new source type). source_kind is a coarse provenance label only, never gated.
    return {"gmail": "email", "drive": "drive", "calendar": "calendar"}.get(st, "unknown")


def collect_from_drain(store, drain_delta, pin: FleetPin, contributor_email: str) -> int:
    """Filter a drain delta to allowlisted, redacted ContributionRecords and
    enqueue them into org_contrib_outbox. Returns the number of records enqueued.

    drain_delta = {"relations": [entity_relations rows], "entities": {id: row}}.
    See module docstring / plan for the exact shape. Fail-closed at every step.
    """
    if not pin.is_pinned:
        return 0                                   # no fleet_secret => nothing leaves
    allow = set(pin.relation_allowlist)
    entities = drain_delta.get("entities") or {}
    records: list[ContributionRecord] = []
    seen: set = set()                              # dedup identical (source_ref, claim)

    def _emit(rec: ContributionRecord) -> None:
        key = (rec.source_ref, json.dumps(rec.claim, sort_keys=True), rec.valid_to)
        if key in seen:
            return
        seen.add(key)
        records.append(rec)

    for rel in drain_delta.get("relations") or []:
        if (rel.get("origin") or "local") != "local":
            continue                               # never re-contribute org rows (echo guard)
        relation = rel.get("relation") or ""
        if relation not in allow:
            continue                               # allowlist — fail closed
        doc_id = rel.get("source_doc_id") or ""
        if not doc_id or _is_cold(store, doc_id):
            continue                               # no/cold provenance — fail closed
        a = entities.get(rel.get("entity_a"))
        b = entities.get(rel.get("entity_b"))
        if not a or not b:
            continue
        if a.get("type") not in _LAYER1_ENTITY_TYPES or b.get("type") not in _LAYER1_ENTITY_TYPES:
            continue                               # non-layer-1 type — fail closed
        if is_role_address(a.get("email_addr", "")) or is_role_address(b.get("email_addr", "")):
            continue                               # role inbox never keys a person (0.7.77)
        if (a.get("origin") or "local") != "local" or (b.get("origin") or "local") != "local":
            continue
        # Redact + identity-anchor both endpoints. If either can't be safely
        # contributed (e.g. a person without an email identity), drop the whole
        # relation — fail closed, never ship a half-anchored edge.
        claim_a = _safe_entity_claim(a)
        claim_b = _safe_entity_claim(b)
        if claim_a is None or claim_b is None:
            continue
        sref = source_ref(pin.fleet_secret, doc_id)
        skind = _source_kind(store, doc_id)
        vfrom = rel.get("valid_from") or ""
        raw_conf = rel.get("confidence")
        confidence = 1.0 if raw_conf is None else float(raw_conf)
        for claim in (claim_a, claim_b):
            _emit(ContributionRecord(
                claim=claim,
                confidence=1.0, valid_from=vfrom,
                contributor_email=contributor_email, source_kind=skind, source_ref=sref))
        _emit(ContributionRecord(
            claim={"kind": "relation", "entity_a": rel["entity_a"],
                   "relation": relation, "entity_b": rel["entity_b"]},
            confidence=confidence,
            valid_from=vfrom, valid_to=rel.get("valid_to") or "",
            contributor_email=contributor_email, source_kind=skind, source_ref=sref))

    if not records:
        return 0
    inserted = 0
    with store._connect() as db:
        # Dedup against still-pending outbox rows: the boundary-second (>=)
        # watermark re-scan can re-derive an identical record on the next cycle;
        # the curator's staging UNIQUE absorbs it, but skipping it here keeps the
        # contributor's outbox and uploaded JSONL free of duplicate Drive traffic.
        pending = {r["record"] for r in db.execute(
            "SELECT record FROM org_contrib_outbox WHERE uploaded_at=''").fetchall()}
        for rec in records:
            blob = json.dumps(rec.to_dict(), sort_keys=True)
            if blob in pending:
                continue
            db.execute("INSERT INTO org_contrib_outbox(record) VALUES(?)", (blob,))
            pending.add(blob)
            inserted += 1
    return inserted


def upload_pending(store, fleet_storage: FleetStorage, contributor_email: str) -> dict:
    """Drain all pending (uploaded_at=='') org_contrib_outbox rows into ONE
    append-only JSONL batch at contrib/<email>/<utc-timestamp>.jsonl, then stamp
    them uploaded. Idempotent-safe: only rows still pending are taken, so a
    re-run after a successful upload is a no-op. Returns
    {"uploaded": n, "batch": path} ({"uploaded": 0, "batch": ""} when nothing
    is pending)."""
    with store._connect() as db:
        rows = db.execute(
            "SELECT id, record FROM org_contrib_outbox WHERE uploaded_at='' ORDER BY id"
        ).fetchall()
    if not rows:
        return {"uploaded": 0, "batch": ""}
    payload = ("\n".join(r["record"] for r in rows) + "\n").encode("utf-8")
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = f"contrib/{contributor_email}/{ts}.jsonl"
    fleet_storage.put_bytes(path, payload)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with store._connect() as db:
        db.executemany("UPDATE org_contrib_outbox SET uploaded_at=? WHERE id=?",
                        [(now, r["id"]) for r in rows])
    return {"uploaded": len(rows), "batch": path}


def _delta_since_watermark(store) -> tuple[dict, dict]:
    """Build a drain_delta of entity_relations changed since the stored
    watermark (org_contrib_hwm = max row id last seen, org_contrib_ts = last
    scan timestamp), plus their endpoint entities. Because Phase B has no
    per-drain hook, the daily upload cadence calls this to do collection AND
    upload in one pass: any row with an id beyond the high-water mark (new
    edge) or an invalidated_at/last_seen at-or-after the last scan (supersession
    or re-observation) is picked up, so collect_from_drain sees the same shape a
    future drain-path hook would hand it. The timestamp comparisons are >=, not
    >, because org_contrib_ts and invalidated_at/last_seen share the same
    1-second string resolution (graph_write._now_iso) — a row changed in the
    same wall-clock second as the last watermark checkpoint would otherwise
    fail every clause and be silently, permanently lost (the watermark only
    moves forward). The harmless cost is a bounded duplicate re-scan of rows
    already seen in that boundary second, absorbed by org_contrib_staging's
    UNIQUE(contributor_email, source_ref, claim). Returns
    (drain_delta, new_watermark) where new_watermark = {"hwm": int, "ts": iso}."""
    hwm = int(store.get_meta("org_contrib_hwm") or 0)
    last_ts = store.get_meta("org_contrib_ts") or ""
    with store._connect() as db:
        rel_rows = db.execute(
            "SELECT id, entity_a, relation, entity_b, valid_from, valid_to, "
            "       confidence, origin, source_doc_id "
            "FROM entity_relations "
            "WHERE id > ? "
            "   OR (invalidated_at IS NOT NULL AND invalidated_at >= ?) "
            "   OR (last_seen IS NOT NULL AND last_seen >= ?) "
            "ORDER BY id",
            (hwm, last_ts, last_ts)).fetchall()
    relations = [dict(r) for r in rel_rows]
    ent_ids = {r["entity_a"] for r in relations} | {r["entity_b"] for r in relations}
    entities: dict = {}
    if ent_ids:
        placeholders = ",".join("?" * len(ent_ids))
        with store._connect() as db:
            for e in db.execute(
                    f"SELECT id, name, type, org, email_addr, aliases, origin "
                    f"FROM entities WHERE id IN ({placeholders})",
                    tuple(ent_ids)).fetchall():
                entities[e["id"]] = dict(e)
    # hwm is derived from the FETCHED rows (not a separate MAX(id) query run after
    # the fact) so a relation inserted between the SELECT above and now can never
    # be silently skipped forever: it wasn't in `relations`, so hwm is never
    # advanced past it, and it will have id > hwm on the very next scan.
    new_hwm = max([hwm] + [r["id"] for r in relations])
    new_wm = {"hwm": int(new_hwm),
              "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    return {"relations": relations, "entities": entities}, new_wm
