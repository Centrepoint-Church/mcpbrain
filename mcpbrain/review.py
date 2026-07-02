"""Assemble a self-contained evidence packet per proactive finding so a Haiku
adjudicator can decide without fetching. See docs/superpowers/plans/
2026-07-02-session-4-brain-review-cadence.md."""
from mcpbrain import orgs


def _entity_sub(store, eid):
    with store._connect() as db:
        r = db.execute("SELECT id,name,type,org,email_addr,aliases,mentions FROM entities WHERE id=?", (eid,)).fetchone()
        if not r:
            return None
        rels = db.execute("SELECT relation, entity_b FROM entity_relations WHERE entity_a=? LIMIT 20", (eid,)).fetchall()
        obs = db.execute("SELECT attribute, value FROM entity_observations WHERE entity_id=? LIMIT 20", (eid,)).fetchall()
        mids = [m[0] for m in db.execute("SELECT message_id FROM email_entities WHERE entity_id=? LIMIT 5", (eid,)).fetchall()]
        spans = []
        for mid in mids:
            row = db.execute("SELECT text FROM chunks WHERE doc_id=? LIMIT 1", (mid,)).fetchone()
            if row and row[0]:
                spans.append(row[0][:400])
    return {"id": r[0], "name": r[1], "type": r[2], "org": r[3], "email_addr": r[4] or "",
            "aliases": r[5] or "", "mentions": r[6] or 0,
            "relations": [{"relation": x[0], "other_name": x[1]} for x in rels],
            "observations": [{"attribute": x[0], "value": x[1]} for x in obs],
            "source_spans": spans}


def build_review_packet(store, finding: dict) -> dict:
    ftype = finding.get("finding_type", "")
    ref = finding.get("ref_id", "")
    ent = _entity_sub(store, ref) or {}
    pk = {"finding_type": ftype, "ref_id": ref,
          "summary": finding.get("summary", ""), "detail": finding.get("detail", ""),
          "entity": {k: ent.get(k) for k in ("id", "name", "type", "org", "email_addr", "aliases", "mentions")},
          "source_spans": ent.get("source_spans", []),
          "relations": ent.get("relations", []), "observations": ent.get("observations", []),
          "taxonomy": list(orgs.taxonomy_from_config().names)}
    return pk


def build_review_units(store, *, kinds: list[str], cap: int) -> list[dict]:
    units = []
    for kind in kinds:
        if len(units) >= cap:
            break
        for finding in store.open_findings(kind):
            if len(units) >= cap:
                break
            units.append({"finding_id": finding["id"], "packet": build_review_packet(store, finding)})
    return units
