"""User-directed corrections to the knowledge graph (brain_graph_correct).

Every correction is one write transaction holding BOTH the graph change and its
ledger row (graph_corrections) plus a change_log row, so it commits whole or
not at all. The ledger row's snapshot is what undo restores from.

Two bases:
  user_stated -- the user said it in conversation. Applied immediately.
  inferred    -- the model inferred it. Applied only with a confirmation the
                 model cannot forge: `confirmed_via` is set by the MCP server
                 after an elicitation, or by the dashboard's Apply button. It
                 never arrives in the tool's own arguments (the input schema
                 forbids extra properties). Without one, the correction is
                 staged as 'pending' and surfaced as a proactive finding.

Stickiness lives in the writers, not here: graph_write.upsert_relation_in skips
user-rejected triples, the Store setters refuse user-locked fields, and every
merge path consults entity_distinct_pairs. This module only writes the markers.

No mcp / Store / native imports at module scope: the MCP server imports
describe() and payload_from_args() to build its elicitation message.
"""
import json
from datetime import datetime, timezone

OPS = ("reject_relation", "assert_relation", "merge", "not_same", "set_field", "hide", "undo")
BASES = ("user_stated", "inferred")
FIELDS = ("role", "org", "name", "email")
PENDING_CAP = 25
FINDING_TYPE = "graph_correction"

_FIELD_COLUMN = {"org": "org", "name": "name", "email": "email_addr"}
# Every string-typed argument across every op, checked uniformly in submit()
# regardless of which op is in play (a stray value under the wrong op's key
# is still worth refusing on, not silently ignored).
_STRING_ARGS = ("entity_a", "entity_b", "entity_id", "other_id", "relation",
               "field", "value", "reason", "name", "valid_from")
_PAYLOAD_KEYS = {
    "reject_relation": ("entity_a", "relation", "entity_b"),
    "assert_relation": ("entity_a", "relation", "entity_b", "valid_from"),
    "merge": ("entity_id", "other_id", "name"),
    "not_same": ("entity_id", "other_id"),
    "set_field": ("entity_id", "field", "value"),
    "hide": ("entity_id",),
}


class Refused(Exception):
    """A correction that must not be written. The message goes to the model."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def payload_from_args(args: dict) -> dict:
    op = args["op"]
    p = {k: (args[k].strip() if isinstance(args[k], str) else args[k])
         for k in _PAYLOAD_KEYS[op] if args.get(k) not in (None, "")}
    p["reason"] = (args.get("reason") or "").strip()
    return p


def dedup_key(op: str, p: dict) -> str:
    if op in ("reject_relation", "assert_relation"):
        ident = [p["entity_a"], p["relation"], p["entity_b"]]
    elif op in ("merge", "not_same"):
        ident = sorted([p["entity_id"], p["other_id"]])
    elif op == "set_field":
        ident = [p["entity_id"], p["field"], p["value"]]
    else:
        ident = [p["entity_id"]]
    return op + ":" + json.dumps(ident)


def describe(op: str, p: dict) -> str:
    if op == "reject_relation":
        return f"Mark relation {p['entity_a']} -{p['relation']}-> {p['entity_b']} as wrong"
    if op == "assert_relation":
        return f"Record {p['entity_a']} -{p['relation']}-> {p['entity_b']}"
    if op == "merge":
        return f"Merge {p['entity_id']} and {p['other_id']} into one entity"
    if op == "not_same":
        return f"Record that {p['entity_id']} and {p['other_id']} are different entities"
    if op == "set_field":
        return f"Set {p['field']} of {p['entity_id']} to {p['value']!r}"
    if op == "hide":
        return f"Hide {p['entity_id']} from the graph"
    return op


# --- ledger + side tables, all on the caller's connection ---------------------

def _next_applied_order(db) -> int:
    """The next value of applied_order: monotonically increasing across the
    table regardless of which row's status changes, so it orders BY WHEN A
    ROW BECAME APPLIED, not by id/created_at -- id is assignment order, and a
    pending inferred correction can be approved well after a later
    user_stated one already applied."""
    return db.execute("SELECT COALESCE(MAX(applied_order),0)+1 FROM graph_corrections").fetchone()[0]


def _insert(db, op, basis, status, payload, snapshot, confirmed_via, key, *,
           applied_at="", applied_order=None) -> int:
    return db.execute(
        "INSERT INTO graph_corrections(op,basis,status,payload,snapshot,reason,"
        "confirmed_via,dedup_key,applied_at,applied_order) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (op, basis, status, json.dumps(payload), json.dumps(snapshot),
         payload.get("reason", ""), confirmed_via, key, applied_at, applied_order)).lastrowid


def _log_change(db, cid: int, change: str, summary: str, detail: str) -> None:
    lid = db.execute(
        "INSERT INTO change_log(change_type, ref_id, summary, detail, revert_ref, source) "
        "VALUES(?,?,?,?,?,?)",
        (change, str(cid), summary, detail, f"correction:{cid}", "brain_graph_correct")).lastrowid
    db.execute("UPDATE graph_corrections SET change_log_id=? WHERE id=?", (lid, cid))


def _stage_finding(db, cid: int, summary: str, reason: str) -> None:
    db.execute(
        "INSERT INTO proactive_findings(finding_type, ref_id, org, summary, detail, severity, "
        "detected_at, resolved_at) VALUES(?,?,?,?,?,?,?,NULL) "
        "ON CONFLICT(finding_type, ref_id) DO UPDATE SET summary=excluded.summary, "
        "detail=excluded.detail, resolved_at=NULL",
        (FINDING_TYPE, str(cid), "", f"Proposed graph correction: {summary}",
         reason, "info", _now()))


def _resolve_finding(db, cid: int, verdict: str) -> None:
    db.execute("UPDATE proactive_findings SET resolved_at=?, verdict=? "
               "WHERE finding_type=? AND ref_id=? AND resolved_at IS NULL",
               (_now(), verdict, FINDING_TYPE, str(cid)))


def _entity(db, eid: str):
    row = db.execute("SELECT * FROM entities WHERE id=?", (eid,)).fetchone()
    if row is None:
        raise Refused(f"no entity with id {eid!r} (use the id from brain_context or brain_graph)")
    return row


def _relation(db, a, rel, b):
    return db.execute("SELECT * FROM entity_relations WHERE entity_a=? AND relation=? "
                      "AND entity_b=?", (a, rel, b)).fetchone()


# --- apply, per op. Each raises Refused BEFORE its first write. --------------

def _apply_reject_relation(store, db, p) -> dict:
    row = _relation(db, p["entity_a"], p["relation"], p["entity_b"])
    if row is None:
        raise Refused(f"no relation {p['entity_a']} -{p['relation']}-> {p['entity_b']}")
    if row["user_verdict"] == "rejected":
        raise Refused("that relation is already marked wrong")
    db.execute("UPDATE entity_relations SET invalidated_at=COALESCE(invalidated_at, ?), "
               "valid_to=COALESCE(valid_to, ?), superseded_reason='user_rejected', "
               "user_verdict='rejected' WHERE id=?", (_now(), _today(), row["id"]))
    return {"relation": dict(row)}


def _apply_assert_relation(store, db, p) -> dict:
    from mcpbrain import graph_write as gw
    a, rel, b = p["entity_a"], p["relation"], p["entity_b"]
    _entity(db, a); _entity(db, b)
    if a == b:
        raise Refused("a relation needs two different entities")
    prior = _relation(db, a, rel, b)
    if prior is not None and prior["user_verdict"] == "asserted" and prior["invalidated_at"] is None:
        raise Refused("that relation is already recorded as stated by the user")
    candidates = [dict(r) for r in db.execute(
        "SELECT * FROM entity_relations WHERE entity_a=? AND relation=? AND entity_b != ? "
        "AND invalidated_at IS NULL", (a, rel, b))]
    if prior is not None and prior["user_verdict"] == "rejected":
        # The user now says it IS true: lift the rejection so the revive path runs.
        db.execute("UPDATE entity_relations SET user_verdict=NULL WHERE id=?", (prior["id"],))
    rid = gw.upsert_relation_in(db, a, rel, b, valid_from=p.get("valid_from") or _today(),
                                evidence=f"stated by the user: {p.get('reason', '')}",
                                source_doc_id="")
    db.execute("UPDATE entity_relations SET user_verdict='asserted' WHERE id=?", (rid,))
    # Only candidates the singleton recency rule ACTUALLY retired (their
    # invalidated_by_relation_id now points at this assert's own row) belong
    # in the snapshot as "rivals". A non-singleton relation (e.g.
    # mentioned_with) never retires anything, so its merely-coexisting
    # candidates must not be snapshotted here -- both `undo`'s restore step
    # AND the undo-conflict guard's target keys treat "rivals" as things this
    # assert is responsible for, and a false one makes the guard block an
    # unrelated later correction it never touched.
    rivals = [r for r in candidates if db.execute(
        "SELECT invalidated_by_relation_id FROM entity_relations WHERE id=?",
        (r["id"],)).fetchone()["invalidated_by_relation_id"] == rid]
    row_after = db.execute("SELECT invalidated_at FROM entity_relations WHERE id=?",
                           (rid,)).fetchone()
    return {"relation_id": rid, "prior": dict(prior) if prior else None, "rivals": rivals,
            "historical": row_after["invalidated_at"] is not None}


def _apply_not_same(store, db, p) -> dict:
    a, b = sorted((p["entity_id"], p["other_id"]))
    if a == b:
        raise Refused("an entity is always the same as itself")
    _entity(db, a); _entity(db, b)
    if db.execute("SELECT 1 FROM entity_distinct_pairs WHERE a=? AND b=?", (a, b)).fetchone():
        raise Refused("those two are already marked as different")
    db.execute("INSERT INTO entity_distinct_pairs(a,b) VALUES(?,?)", (a, b))
    return {"pair": [a, b]}


def _apply_set_field(store, db, p) -> dict:
    from mcpbrain.graph_write import _JUNK_ROLE_VALUES
    field, value, eid = p["field"], p["value"], p["entity_id"]
    ent = _entity(db, eid)
    if field not in FIELDS:
        raise Refused(f"field must be one of {list(FIELDS)}")
    if not value:
        raise Refused("value must not be empty")
    if field == "role":
        if value.lower() in _JUNK_ROLE_VALUES or len(value) > 80:
            raise Refused(f"{value!r} is not a job title mcpbrain records as a role")
        retired = [r["id"] for r in db.execute(
            "SELECT id FROM entity_observations WHERE entity_id=? AND attribute='role' "
            "AND source='manual' AND valid_to IS NULL", (eid,))]
        today = _today()
        db.execute("UPDATE entity_observations SET valid_to=? WHERE entity_id=? AND "
                   "attribute='role' AND source='manual' AND valid_to IS NULL", (today, eid))
        new_id = db.execute(
            "INSERT INTO entity_observations(entity_id, attribute, value, source, valid_from, "
            "confidence_source, last_seen) VALUES(?, 'role', ?, 'manual', ?, 'high', ?)",
            (eid, value, today, today)).lastrowid
        return {"retired_ids": retired, "inserted_id": new_id}
    col = _FIELD_COLUMN[field]
    lock = db.execute("SELECT * FROM entity_field_locks WHERE entity_id=? AND field=?",
                      (eid, field)).fetchone()
    snap = {"entity": {c: ent[c] for c in (col, "aliases", "org_valid_from")},
            "lock": dict(lock) if lock else None}
    if field == "name":
        old = (ent["name"] or "").strip()
        parts = [x for x in (ent["aliases"] or "").split("|") if x]
        if old and old != value and old not in parts:
            parts.append(old)
        db.execute("UPDATE entities SET name=?, aliases=? WHERE id=?", (value, "|".join(parts), eid))
    elif field == "org":
        db.execute("UPDATE entities SET org=?, org_valid_from=? WHERE id=?", (value, _today(), eid))
    else:
        db.execute("UPDATE entities SET email_addr=? WHERE id=?", (value, eid))
    db.execute("INSERT OR IGNORE INTO entity_field_locks(entity_id, field) VALUES(?,?)", (eid, field))
    return snap


def _apply_hide(store, db, p) -> dict:
    _entity(db, p["entity_id"])
    prior = db.execute("SELECT * FROM entity_suppressions WHERE entity_id=?",
                       (p["entity_id"],)).fetchone()
    db.execute("INSERT OR REPLACE INTO entity_suppressions(entity_id, reason, suppressed_at) "
               "VALUES(?, 'user', ?)", (p["entity_id"], _now()))
    return {"prior": dict(prior) if prior else None}


def _apply_merge(store, db, p) -> dict:
    """Fold two entities into one, field-reconciled best-of, via the same
    _merge_entities_tx/_unmerge_tx pair the deterministic/review merge paths
    use -- so `undo` gets an exact, general-purpose reversal for free.

    `_orient`/`is_distinct_pair` read through the store's own connections
    (get_entity) rather than `db`, but that is safe here: they run BEFORE
    `_merge_entities_tx` (this function's first write), so a WAL reader still
    sees the last-committed, pre-merge state. Nothing above that call may
    write -- keep it that way."""
    from mcpbrain import graph_view
    from mcpbrain.resolve import _NAME_MERGEABLE_TYPES
    from mcpbrain.store import _merge_entities_tx
    if p["entity_id"] == p["other_id"]:
        raise Refused("merge needs two different entities")
    oriented = graph_view._orient(store, p["entity_id"], p["other_id"])
    if isinstance(oriented, dict):
        raise Refused(oriented["message"])
    winner, loser = oriented
    if winner["type"] not in _NAME_MERGEABLE_TYPES or loser["type"] not in _NAME_MERGEABLE_TYPES:
        raise Refused("only people, organisations and projects can be merged "
                      f"(these are {loser['type']} and {winner['type']})")
    result = graph_view._merge_result(winner, loser, p.get("name"))
    snap = _merge_entities_tx(db, loser["id"], winner["id"],
                              canonical_name=result["name"], method="user")
    if snap is None:
        raise Refused("nothing to merge")
    db.execute("UPDATE entities SET email_addr=?, notes=? WHERE id=?",
               (result["email_addr"], result["notes"], winner["id"]))
    return snap


_APPLY = {
    "reject_relation": _apply_reject_relation,
    "assert_relation": _apply_assert_relation,
    "merge": _apply_merge,
    "not_same": _apply_not_same,
    "set_field": _apply_set_field,
    "hide": _apply_hide,
}


# --- undo, per op --------------------------------------------------------------

def _undo_reject_relation(db, p, snap):
    r = snap["relation"]
    cur = db.execute("UPDATE entity_relations SET invalidated_at=?, valid_to=?, "
                     "superseded_reason=?, user_verdict=? WHERE id=?",
                     (r["invalidated_at"], r["valid_to"], r["superseded_reason"],
                      r["user_verdict"], r["id"]))
    if cur.rowcount == 0:
        raise Refused("the relation no longer exists")


def _undo_assert_relation(db, p, snap):
    """Undo an assert without rolling back automated changes the assert never
    made: only the columns the assert (via upsert_relation_in/_mark_superseded)
    actually touched are put back, not the whole row. Confidence/evidence/
    strength/last_seen/source_doc_id from the assert are left as they are --
    reverting them would be undoing re-observations that happened alongside
    the assert, which this correction never claimed credit for."""
    relation_id = snap["relation_id"]
    # A rival is only touched here if THIS assert is what retired it (singleton
    # recency rule); one whose invalidated_by_relation_id has since moved on
    # (e.g. a later correction) must be left alone -- restoring it would
    # silently clobber that later change. Checked and captured BEFORE the
    # fresh-row-delete branch below: deleting `relation_id` FK-cascades
    # (ON DELETE SET NULL) and clears every rival's pointer to it, which would
    # make this check always read NULL and never fire if done afterwards.
    to_restore = []
    for r in snap["rivals"]:
        cur = db.execute("SELECT invalidated_by_relation_id FROM entity_relations WHERE id=?",
                         (r["id"],)).fetchone()
        if cur is not None and cur["invalidated_by_relation_id"] == relation_id:
            to_restore.append(r)

    if snap["prior"] is None:
        row = db.execute("SELECT entity_a, entity_b FROM entity_relations WHERE id=?",
                         (relation_id,)).fetchone()
        if row is not None:
            db.execute("DELETE FROM entity_relations WHERE id=?", (relation_id,))  # admin-delete-ok
            db.execute("UPDATE entities SET degree=MAX(COALESCE(degree,0)-1, 0) WHERE id IN (?,?)",
                       (row[0], row[1]))
    else:
        prior = snap["prior"]
        db.execute(
            "UPDATE entity_relations SET user_verdict=?, valid_from=?, valid_to=?, "
            "invalidated_at=?, superseded_reason=?, invalidated_by_relation_id=? WHERE id=?",
            (prior["user_verdict"], prior["valid_from"], prior["valid_to"],
             prior["invalidated_at"], prior["superseded_reason"],
             prior["invalidated_by_relation_id"], prior["id"]))
    for r in to_restore:
        db.execute(
            "UPDATE entity_relations SET valid_to=?, invalidated_at=?, "
            "superseded_reason=?, invalidated_by_relation_id=? WHERE id=?",
            (r["valid_to"], r["invalidated_at"], r["superseded_reason"],
             r["invalidated_by_relation_id"], r["id"]))


def _undo_not_same(db, p, snap):
    db.execute("DELETE FROM entity_distinct_pairs WHERE a=? AND b=?", tuple(snap["pair"]))  # admin-delete-ok


def _undo_set_field(db, p, snap):
    eid, field = p["entity_id"], p["field"]
    if field == "role":
        # Deleting the inserted manual row also drops any observed_count that
        # extraction consolidated into it since it was written -- acceptable,
        # because the value being undone is the user's correction, not a
        # source's observation; there is nothing of the source's left to keep.
        db.execute("DELETE FROM entity_observations WHERE id=?", (snap["inserted_id"],))  # admin-delete-ok
        for oid in snap["retired_ids"]:
            db.execute("UPDATE entity_observations SET valid_to=NULL WHERE id=?", (oid,))
        return
    e = snap["entity"]
    col = _FIELD_COLUMN[field]
    cur = db.execute(f"UPDATE entities SET {col}=?, aliases=?, org_valid_from=? WHERE id=?",
                     (e[col], e["aliases"], e["org_valid_from"], eid))
    if cur.rowcount == 0:
        raise Refused(f"{eid} no longer exists")
    if snap["lock"] is None:
        db.execute("DELETE FROM entity_field_locks WHERE entity_id=? AND field=?", (eid, field))  # admin-delete-ok


def _undo_hide(db, p, snap):
    if snap["prior"] is None:
        db.execute("DELETE FROM entity_suppressions WHERE entity_id=?", (p["entity_id"],))  # admin-delete-ok
    else:
        from mcpbrain.store import _restore_row
        _restore_row(db, "entity_suppressions", snap["prior"], key="entity_id")


def _undo_merge(db, p, snap):
    from mcpbrain.store import _unmerge_tx
    try:
        _unmerge_tx(db, snap)
    except ValueError as exc:
        raise Refused(str(exc)) from exc


_UNDO = {
    "reject_relation": _undo_reject_relation,
    "assert_relation": _undo_assert_relation,
    "merge": _undo_merge,
    "not_same": _undo_not_same,
    "set_field": _undo_set_field,
    "hide": _undo_hide,
}


# --- the one generalised "don't clobber a later correction" guard ------------
#
# Undoing correction N must not silently overwrite something a LATER, still-
# applied correction M did. "Later" means applied_order, NOT id and NOT
# applied_at: a pending inferred correction is assigned a lower id (it was
# SUBMITTED first) but can be approved well after a later user_stated
# correction already applied, and applied_at's 1-second resolution can tie
# on exactly that sequence. applied_order is a monotonically increasing
# integer stamped only at the moment a row becomes 'applied' (submit or
# approve), inside that same write transaction.
#
# Each non-merge op has a "target key" identifying what it wrote; two
# corrections that share a target key conflict directly (rule i). Because a
# merge deletes/renames entities rather than writing a target key of its
# own, it is handled by id membership instead: a later merge touching one of
# N's entities can invalidate N's undo (rule ii), and when N is ITSELF the
# merge, a later correction touching its winner id can invalidate undoing it
# (rule iii) -- the winner survives the merge and keeps being written to;
# the loser id is gone and cannot be touched again.

def _payload_entity_ids(p: dict) -> set:
    return {p[k] for k in ("entity_id", "other_id", "entity_a", "entity_b") if p.get(k)}


def _target_keys(op: str, p: dict, snap: dict) -> set:
    """What this (non-merge) correction wrote, as opaque conflict keys."""
    if op in ("reject_relation", "assert_relation"):
        keys = {f"rel:{p['entity_a']}|{p['relation']}|{p['entity_b']}"}
        if op == "assert_relation":
            for r in (snap or {}).get("rivals") or ():
                keys.add(f"rel:{r['entity_a']}|{r['relation']}|{r['entity_b']}")
        return keys
    if op == "set_field":
        return {f"field:{p['entity_id']}:{p['field']}"}
    if op == "hide":
        return {f"hide:{p['entity_id']}"}
    if op == "not_same":
        a, b = sorted((p["entity_id"], p["other_id"]))
        return {f"pair:{a}|{b}"}
    return set()  # merge has no target key of this shape


def _merge_ids(snap: dict) -> set:
    """The winner + loser ids a merge's OWN snapshot names."""
    ids = {snap.get("winner_id")}
    loser = snap.get("loser") or {}
    if loser.get("id"):
        ids.add(loser["id"])
    return {i for i in ids if i}


def _conflict_note(op: str, p: dict, snap: dict) -> str:
    """Fallback description naming N's OWN target -- used for rule ii/iii,
    where the conflict is "this entity was touched by a later merge" rather
    than a specific shared key."""
    if op in ("reject_relation", "assert_relation"):
        return f"{p['entity_a']} -{p['relation']}-> {p['entity_b']}"
    if op == "set_field":
        return f"{p['entity_id']}'s {p['field']}"
    if op == "hide":
        return p["entity_id"]
    if op == "not_same":
        return f"{p['entity_id']} and {p['other_id']}"
    if op == "merge":
        return f"{snap.get('winner_id')} after this merge"
    return op


def _note_from_key(key: str) -> str:
    """Render an opaque _target_keys() key back into English for a refusal
    message. Used for rule i, where the actual shared key -- e.g. a rival
    triple an assert retired -- can differ from the correction's OWN target
    (naming the assert's own triple there would be misleading: the conflict
    is over the RIVAL, not what the assert itself asserted)."""
    kind, _, rest = key.partition(":")
    if kind == "rel":
        a, rel, b = rest.split("|")
        return f"{a} -{rel}-> {b}"
    if kind == "field":
        eid, field = rest.split(":", 1)
        return f"{eid}'s {field}"
    if kind == "hide":
        return rest
    if kind == "pair":
        a, b = rest.split("|")
        return f"{a} and {b}"
    return key


def _find_blocking_correction(db, applied_order, op: str, p: dict, snap: dict):
    """The lowest-applied_order LATER 'applied' correction that undoing
    (op, p, snap) would silently corrupt, as (blocker_id, note), or
    (None, None)."""
    rows = db.execute(
        "SELECT id, op, payload, snapshot FROM graph_corrections "
        "WHERE status='applied' AND applied_order > ? ORDER BY applied_order",
        (applied_order,))
    if op == "merge":
        watch = {snap.get("winner_id")}  # rule iii: only the survivor can be touched again
        for row in rows:
            m_p = json.loads(row["payload"] or "{}")
            if watch & _payload_entity_ids(m_p):
                return row["id"], _conflict_note("merge", p, snap)
        return None, None

    my_keys = _target_keys(op, p, snap)
    my_ids = _payload_entity_ids(p)
    for row in rows:
        m_op, m_p = row["op"], json.loads(row["payload"] or "{}")
        m_snap = json.loads(row["snapshot"] or "{}")
        shared = my_keys & _target_keys(m_op, m_p, m_snap)  # rule i
        if shared:
            return row["id"], _note_from_key(sorted(shared)[0])
        if m_op == "merge" and my_ids & _merge_ids(m_snap):  # rule ii
            return row["id"], _conflict_note(op, p, snap)
    return None, None


# --- public entry points -------------------------------------------------------

def submit(store, args: dict, *, confirmed_via: str = "", declined: bool = False) -> dict:
    """Apply, stage or record one correction. Never raises for a refusal.

    Every argument coming from the model is untrusted: a missing/empty required
    key or a malformed correction_id must return a 'refused' result, never an
    uncaught KeyError/ValueError -- a tool that raises breaks the MCP contract.
    """
    if confirmed_via and confirmed_via not in ("elicitation", "dashboard"):
        return {"status": "refused",
                "error": "confirmed_via must be 'elicitation' or 'dashboard'"}
    op = args.get("op")
    if op == "undo":
        try:
            cid = int(args["correction_id"])
        except (KeyError, TypeError, ValueError):
            return {"status": "refused", "error": "undo needs a correction_id"}
        return undo(store, cid)
    if op not in _APPLY:
        return {"status": "refused", "error": f"op must be one of {list(OPS)}"}
    basis = args.get("basis")
    if basis not in BASES:
        return {"status": "refused", "error": f"basis must be one of {list(BASES)}"}
    # Every value the model could hand us is untyped as far as Python is
    # concerned: a wrong-typed value (e.g. value=5 for a set_field job title)
    # would otherwise reach describe()/dedup_key()/a SQL bind or a .lower()
    # call downstream and fail there instead of being refused cleanly here.
    bad_type = next((k for k in _STRING_ARGS
                     if args.get(k) is not None and not isinstance(args[k], str)), None)
    if bad_type is not None:
        return {"status": "refused", "error": f"{bad_type} must be a string"}
    # Every payload key except valid_from (optional, defaults to today) and
    # merge's name (optional, defaults to the winner's existing name) is
    # required. Checked BEFORE dedup_key/describe touch p[...], which would
    # otherwise raise KeyError on a missing key.
    required = [k for k in _PAYLOAD_KEYS[op] if k not in ("valid_from", "name")]
    missing = [k for k in required if args.get(k) in (None, "")]
    if missing:
        return {"status": "refused", "error": f"{op} needs {missing[0]}"}
    p = payload_from_args(args)
    key = dedup_key(op, p)
    summary = describe(op, p)
    try:
        with store._connect(write=True) as db:
            if basis == "inferred":
                prior = db.execute(
                    "SELECT id, status FROM graph_corrections WHERE dedup_key=? AND "
                    "status IN ('pending','declined','applied') ORDER BY id DESC LIMIT 1",
                    (key,)).fetchone()
                if prior is not None:
                    return {"status": "duplicate", "correction_id": prior["id"],
                            "summary": f"Already {prior['status']} as correction "
                                       f"{prior['id']}; nothing written."}
                if declined:
                    cid = _insert(db, op, basis, "declined", p, {}, "elicitation", key)
                    return {"status": "declined", "correction_id": cid,
                            "summary": f"{summary}: declined by the user. It will not be "
                                       f"proposed again."}
                if not confirmed_via:
                    n = db.execute("SELECT COUNT(*) FROM graph_corrections "
                                   "WHERE status='pending'").fetchone()[0]
                    if n >= PENDING_CAP:
                        raise Refused(f"{n} corrections already await approval; ask the "
                                      f"user to review them on the dashboard first")
                    cid = _insert(db, op, basis, "pending", p, {}, "", key)
                    _stage_finding(db, cid, summary, p.get("reason", ""))
                    return {"status": "pending", "correction_id": cid,
                            "summary": f"{summary}: staged for approval.",
                            "next": "This client cannot ask the user to confirm, so the "
                                    "correction is waiting on the mcpbrain dashboard "
                                    "(Pending corrections). Ask the user to approve it "
                                    "there. Do not say it was applied."}
            snapshot = _APPLY[op](store, db, p)
            if op == "assert_relation" and snapshot.get("historical"):
                # A singleton relation's recency rule can bury the newly-
                # asserted row under a newer rival at birth -- true, but not
                # the current fact. "applied" alone would misreport that.
                summary += " (recorded as historical: a newer rival is current)"
            cid = _insert(db, op, basis, "applied", p, snapshot, confirmed_via, key,
                          applied_at=_now(), applied_order=_next_applied_order(db))
            _log_change(db, cid, "graph_corrected", summary, p.get("reason", ""))
    except Refused as exc:
        return {"status": "refused", "error": str(exc)}
    return {"status": "applied", "correction_id": cid, "summary": summary,
            "undo": f"brain_graph_correct op=undo correction_id={cid}"}


def undo(store, correction_id: int) -> dict:
    try:
        with store._connect(write=True) as db:
            row = db.execute("SELECT * FROM graph_corrections WHERE id=?",
                             (correction_id,)).fetchone()
            if row is None:
                raise Refused(f"no correction {correction_id}")
            if row["status"] != "applied":
                raise Refused(f"correction {correction_id} is {row['status']}, not applied")
            p, snap = json.loads(row["payload"]), json.loads(row["snapshot"])
            blocker, note = _find_blocking_correction(db, row["applied_order"],
                                                       row["op"], p, snap)
            if blocker is not None:
                raise Refused(f"undo correction {blocker} first: it changed {note}")
            _UNDO[row["op"]](db, p, snap)
            db.execute("UPDATE graph_corrections SET status='reverted', reverted_at=? "
                       "WHERE id=?", (_now(), correction_id))
            _log_change(db, correction_id, "graph_correction_reverted",
                        f"Undid: {describe(row['op'], p)}", "")
    except Refused as exc:
        return {"status": "refused", "error": str(exc)}
    return {"status": "reverted", "correction_id": correction_id,
            "summary": f"Undid: {describe(row['op'], p)}"}


def approve(store, correction_id: int, *, via: str = "dashboard") -> dict:
    try:
        with store._connect(write=True) as db:
            row = db.execute("SELECT * FROM graph_corrections WHERE id=?",
                             (correction_id,)).fetchone()
            if row is None or row["status"] != "pending":
                raise Refused(f"correction {correction_id} is not pending")
            p = json.loads(row["payload"])
            snapshot = _APPLY[row["op"]](store, db, p)
            summary = describe(row["op"], p)
            if row["op"] == "assert_relation" and snapshot.get("historical"):
                summary += " (recorded as historical: a newer rival is current)"
            db.execute("UPDATE graph_corrections SET status='applied', snapshot=?, "
                       "confirmed_via=?, applied_at=?, applied_order=? WHERE id=?",
                       (json.dumps(snapshot), via, _now(), _next_applied_order(db),
                        correction_id))
            _resolve_finding(db, correction_id, "applied")
            _log_change(db, correction_id, "graph_corrected", summary, p.get("reason", ""))
    except Refused as exc:
        with store._connect(write=True) as db:
            cur = db.execute("UPDATE graph_corrections SET status='failed', error=? "
                             "WHERE id=? AND status='pending'", (str(exc), correction_id))
            if cur.rowcount:
                _resolve_finding(db, correction_id, "failed")
        return {"status": "failed", "correction_id": correction_id, "error": str(exc)}
    return {"status": "applied", "correction_id": correction_id, "summary": summary,
            "undo": f"brain_graph_correct op=undo correction_id={correction_id}"}


def decline(store, correction_id: int) -> dict:
    with store._connect(write=True) as db:
        cur = db.execute("UPDATE graph_corrections SET status='declined' "
                         "WHERE id=? AND status='pending'", (correction_id,))
        if cur.rowcount == 0:
            return {"status": "refused", "error": f"correction {correction_id} is not pending"}
        _resolve_finding(db, correction_id, "declined")
    return {"status": "declined", "correction_id": correction_id}
