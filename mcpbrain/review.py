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


def _action_sub(store, action_id):
    with store._connect() as db:
        r = db.execute(
            "SELECT text, deadline, thread_id, source_doc_id, owner, owner_entity_id "
            "FROM actions WHERE id=?", (action_id,)).fetchone()
        if not r:
            return None
        spans = []
        if r[3]:
            row = db.execute("SELECT text FROM chunks WHERE doc_id=? LIMIT 1", (r[3],)).fetchone()
            if row and row[0]:
                spans.append(row[0][:400])
        participants = db.execute(
            "SELECT DISTINCT sender, sender_email FROM email_context "
            "WHERE thread_id=? ORDER BY date_iso ASC LIMIT 10", (r[2],)).fetchall()
        first = db.execute(
            "SELECT sender, sender_email FROM email_context "
            "WHERE thread_id=? ORDER BY date_iso ASC LIMIT 1", (r[2],)).fetchone()
    return {"text": r[0], "deadline": r[1], "thread_id": r[2], "source_doc_id": r[3],
            "owner": r[4] or "", "owner_entity_id": r[5] or "",
            "source_spans": spans,
            "participants": [{"sender": p[0], "sender_email": p[1]} for p in participants],
            "sender": {"sender": first[0], "sender_email": first[1]} if first else {}}


def build_review_packet(store, finding: dict) -> dict:
    ftype = finding.get("finding_type", "")
    ref = finding.get("ref_id", "")
    if ftype == "lint:ownerless_action" or ftype.startswith("ownerless_action"):
        try:
            action_id = int(ref)
        except (TypeError, ValueError):
            action_id = None
        act = _action_sub(store, action_id) if action_id is not None else None
        act = act or {}
        pk = {"finding_type": ftype, "ref_id": ref,
              "summary": finding.get("summary", ""), "detail": finding.get("detail", ""),
              "entity": None,
              "action": {k: act.get(k) for k in ("text", "deadline", "owner", "owner_entity_id")},
              "source_spans": act.get("source_spans", []),
              "thread": {"participants": act.get("participants", []), "sender": act.get("sender", {})},
              "relations": [], "observations": [],
              "taxonomy": list(orgs.taxonomy_from_config().names)}
        return pk

    ent = _entity_sub(store, ref) or {}
    pk = {"finding_type": ftype, "ref_id": ref,
          "summary": finding.get("summary", ""), "detail": finding.get("detail", ""),
          "entity": {k: ent.get(k) for k in ("id", "name", "type", "org", "email_addr", "aliases", "mentions")},
          "source_spans": ent.get("source_spans", []),
          "relations": ent.get("relations", []), "observations": ent.get("observations", []),
          "taxonomy": list(orgs.taxonomy_from_config().names)}
    return pk


def build_review_units(store, *, kinds: list[str], cap: int) -> list[dict]:
    """Pull open findings for each requested kind and build a review packet for each.

    `cap` is a PER-KIND limit, not a total shared across all kinds: each kind
    in `kinds` independently contributes up to `cap` units, matching the
    per-block-type capping pattern used elsewhere (e.g. prepare._merge_review_block's
    _MERGE_REVIEW_CAP). With N kinds and this cap, the worst case total returned
    is N * cap, not cap. This is intentional so that one kind reaching the cap
    (e.g. a backlog of `lint:orphan_entity` findings) never starves the other
    kinds out of a review batch.
    """
    units = []
    for kind in kinds:
        count_for_kind = 0
        for finding in store.open_findings(kind):
            if count_for_kind >= cap:
                break
            units.append({"finding_id": finding["id"], "packet": build_review_packet(store, finding)})
            count_for_kind += 1
    return units
