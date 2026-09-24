"""graph_corrections engine: ops, stickiness end to end, undo, pending (Task 4)."""
from mcpbrain import graph_corrections as gc
from mcpbrain import graph_write as gw
from mcpbrain.store import Store

D, N, S = "dana-okafor", "northgate-trust", "southbank-community-trust"


def _store(tmp_path):
    s = Store(tmp_path / "g.sqlite3", dim=4)
    s.init()
    with s._connect(write=True) as db:
        db.execute("INSERT INTO entities(id,name,type,org) VALUES(?, 'Dana Okafor', 'person', 'Northgate Trust')", (D,))
        db.execute("INSERT INTO entities(id,name,type) VALUES(?, 'Northgate Trust', 'org')", (N,))
        db.execute("INSERT INTO entities(id,name,type) VALUES(?, 'Southbank Community Trust', 'org')", (S,))
    gw.upsert_relation(s, D, "works_at", N, valid_from="2026-01-01")
    return s


def _rel(s, a, rel, b):
    with s._connect() as db:
        return db.execute("SELECT * FROM entity_relations WHERE entity_a=? AND relation=? "
                          "AND entity_b=?", (a, rel, b)).fetchone()


def _stated(**kw):
    return {"basis": "user_stated", "reason": "Dana told me", **kw}


def test_reject_relation_is_applied_logged_and_sticky(tmp_path):
    s = _store(tmp_path)
    out = gc.submit(s, _stated(op="reject_relation", entity_a=D, relation="works_at", entity_b=N))
    assert out["status"] == "applied" and out["undo"] == f"brain_graph_correct op=undo correction_id={out['correction_id']}"
    assert _rel(s, D, "works_at", N)["user_verdict"] == "rejected"
    gw.upsert_relation(s, D, "works_at", N, valid_from="2026-09-01")  # re-extraction
    assert _rel(s, D, "works_at", N)["invalidated_at"] is not None
    with s._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM change_log WHERE change_type='graph_corrected'").fetchone()[0] == 1


def test_undo_reject_restores_the_row(tmp_path):
    s = _store(tmp_path)
    before = dict(_rel(s, D, "works_at", N))
    cid = gc.submit(s, _stated(op="reject_relation", entity_a=D, relation="works_at", entity_b=N))["correction_id"]
    assert gc.undo(s, cid)["status"] == "reverted"
    assert dict(_rel(s, D, "works_at", N)) == before
    assert gc.undo(s, cid)["status"] == "refused"  # already reverted


def test_reject_unknown_relation_is_refused_and_writes_nothing(tmp_path):
    s = _store(tmp_path)
    out = gc.submit(s, _stated(op="reject_relation", entity_a=D, relation="manages", entity_b=N))
    assert out["status"] == "refused" and "no relation" in out["error"]
    with s._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM graph_corrections").fetchone()[0] == 0


def test_assert_relation_supersedes_singleton_and_undo_restores(tmp_path):
    s = _store(tmp_path)
    out = gc.submit(s, _stated(op="assert_relation", entity_a=D, relation="works_at", entity_b=S))
    assert out["status"] == "applied"
    assert _rel(s, D, "works_at", S)["user_verdict"] == "asserted"
    assert _rel(s, D, "works_at", N)["invalidated_at"] is not None  # recency rule retired it
    gc.undo(s, out["correction_id"])
    assert _rel(s, D, "works_at", S) is None
    assert _rel(s, D, "works_at", N)["invalidated_at"] is None


def test_assert_relation_overrides_an_earlier_rejection(tmp_path):
    s = _store(tmp_path)
    gc.submit(s, _stated(op="reject_relation", entity_a=D, relation="works_at", entity_b=N))
    gc.submit(s, _stated(op="assert_relation", entity_a=D, relation="works_at", entity_b=N))
    row = _rel(s, D, "works_at", N)
    assert row["invalidated_at"] is None and row["user_verdict"] == "asserted"


def test_set_field_org_locks_and_undo_unlocks(tmp_path):
    s = _store(tmp_path)
    cid = gc.submit(s, _stated(op="set_field", entity_id=D, field="org",
                               value="Southbank Community Trust"))["correction_id"]
    assert s.get_entity(D)["org"] == "Southbank Community Trust"
    assert s.update_entity_org(D, "The Lantern Co") is False  # automated writer blocked
    gc.undo(s, cid)
    assert s.get_entity(D)["org"] == "Northgate Trust" and s.locked_fields(D) == set()


def test_set_field_role_writes_manual_observation_and_rejects_junk(tmp_path):
    s = _store(tmp_path)
    cid = gc.submit(s, _stated(op="set_field", entity_id=D, field="role", value="Operations Lead"))["correction_id"]
    with s._connect() as db:
        rows = db.execute("SELECT value, source FROM entity_observations WHERE entity_id=? "
                          "AND attribute='role' AND valid_to IS NULL", (D,)).fetchall()
    assert [(r["value"], r["source"]) for r in rows] == [("Operations Lead", "manual")]
    assert gc.submit(s, _stated(op="set_field", entity_id=D, field="role", value="volunteer"))["status"] == "refused"
    gc.undo(s, cid)
    with s._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM entity_observations WHERE entity_id=?", (D,)).fetchone()[0] == 0


def test_not_same_and_hide_with_undo(tmp_path):
    s = _store(tmp_path)
    c1 = gc.submit(s, _stated(op="not_same", entity_id=N, other_id=S))["correction_id"]
    assert s.is_distinct_pair(S, N)
    c2 = gc.submit(s, _stated(op="hide", entity_id=S))["correction_id"]
    with s._connect() as db:
        assert db.execute("SELECT 1 FROM entity_suppressions WHERE entity_id=?", (S,)).fetchone()
    gc.undo(s, c2); gc.undo(s, c1)
    assert not s.is_distinct_pair(S, N)
    with s._connect() as db:
        assert db.execute("SELECT 1 FROM entity_suppressions WHERE entity_id=?", (S,)).fetchone() is None


def test_unknown_entity_is_refused(tmp_path):
    s = _store(tmp_path)
    out = gc.submit(s, _stated(op="hide", entity_id="nobody"))
    assert out["status"] == "refused" and "nobody" in out["error"]


# --- inferred: the pending queue ----------------------------------------------

def _inferred(**kw):
    return {"basis": "inferred", "reason": "newer email signature", **kw}


def test_inferred_without_confirmation_is_staged_not_applied(tmp_path):
    s = _store(tmp_path)
    out = gc.submit(s, _inferred(op="set_field", entity_id=D, field="org", value="Southbank Community Trust"))
    assert out["status"] == "pending" and "dashboard" in out["next"]
    assert s.get_entity(D)["org"] == "Northgate Trust"
    finding = [f for f in s.open_findings(gc.FINDING_TYPE)]
    assert len(finding) == 1 and finding[0]["ref_id"] == str(out["correction_id"])


def test_inferred_with_elicitation_confirmation_applies(tmp_path):
    s = _store(tmp_path)
    out = gc.submit(s, _inferred(op="hide", entity_id=S), confirmed_via="elicitation")
    assert out["status"] == "applied"
    assert s.get_correction(out["correction_id"])["confirmed_via"] == "elicitation"


def test_inferred_dedup_and_decline_memory(tmp_path):
    s = _store(tmp_path)
    args = _inferred(op="hide", entity_id=S)
    first = gc.submit(s, args)
    assert gc.submit(s, args)["status"] == "duplicate"
    gc.decline(s, first["correction_id"])
    assert gc.submit(s, args)["status"] == "duplicate"  # declined is remembered
    assert s.open_findings(gc.FINDING_TYPE) == []


def test_declined_elicitation_is_recorded(tmp_path):
    s = _store(tmp_path)
    out = gc.submit(s, _inferred(op="hide", entity_id=S), declined=True)
    assert out["status"] == "declined"
    assert gc.submit(s, _inferred(op="hide", entity_id=S))["status"] == "duplicate"


def test_pending_cap(tmp_path, monkeypatch):
    s = _store(tmp_path)
    monkeypatch.setattr(gc, "PENDING_CAP", 1)
    gc.submit(s, _inferred(op="hide", entity_id=S))
    out = gc.submit(s, _inferred(op="hide", entity_id=N))
    assert out["status"] == "refused" and "await approval" in out["error"]


def test_approve_applies_and_resolves_finding(tmp_path):
    s = _store(tmp_path)
    cid = gc.submit(s, _inferred(op="hide", entity_id=S))["correction_id"]
    assert gc.approve(s, cid)["status"] == "applied"
    assert s.get_correction(cid)["confirmed_via"] == "dashboard"
    assert s.open_findings(gc.FINDING_TYPE) == []


def test_approve_of_a_now_impossible_correction_fails_cleanly(tmp_path):
    s = _store(tmp_path)
    cid = gc.submit(s, _inferred(op="reject_relation", entity_a=D, relation="works_at", entity_b=N))["correction_id"]
    with s._connect(write=True) as db:
        db.execute("DELETE FROM entity_relations")  # admin-delete-ok
    out = gc.approve(s, cid)
    assert out["status"] == "failed"
    assert s.get_correction(cid)["status"] == "failed"
    assert s.open_findings(gc.FINDING_TYPE) == []


def test_user_stated_ignores_dedup(tmp_path):
    """A stated correction always applies (the user is the authority)."""
    s = _store(tmp_path)
    gc.submit(s, _inferred(op="hide", entity_id=S))  # pending
    assert gc.submit(s, _stated(op="hide", entity_id=S))["status"] == "applied"


# --- binding decision not in the brief: undo of a merge must refuse while a --
# --- later applied correction changed the winner it would restore -----------

def test_undo_merge_refuses_while_later_correction_changed_the_winner(tmp_path):
    import json as _json

    from mcpbrain.store import _merge_entities_tx

    s = _store(tmp_path)
    with s._connect(write=True) as db:
        db.execute("INSERT INTO entities(id,name,type,org) VALUES(?, 'Dana Okafor', 'person', '')",
                  ("dana-okafor-2",))
    with s._connect(write=True) as db:
        snap = _merge_entities_tx(db, "dana-okafor-2", D, method="user")
    with s._connect(write=True) as db:
        merge_cid = db.execute(
            "INSERT INTO graph_corrections(op,basis,status,payload,snapshot,dedup_key,"
            "applied_at,applied_order) VALUES('merge','user_stated','applied',?,?,'merge:[]',?,?) ",
            (_json.dumps({"entity_id": "dana-okafor-2", "other_id": D}),
             _json.dumps(snap), gc._now(), gc._next_applied_order(db))).lastrowid

    later = gc.submit(s, _stated(op="set_field", entity_id=D, field="org",
                                 value="Southbank Community Trust"))
    assert later["status"] == "applied"

    out = gc.undo(s, merge_cid)
    assert out["status"] == "refused"
    assert f"undo correction {later['correction_id']} first" in out["error"]
    assert str(D) in out["error"]

    assert gc.undo(s, later["correction_id"])["status"] == "reverted"
    assert gc.undo(s, merge_cid)["status"] == "reverted"
    assert s.get_entity("dana-okafor-2") is not None


# --- fix round 1 ---------------------------------------------------------------

# --- (1) submit refuses cleanly on missing/malformed arguments, never raises --

def test_submit_refuses_on_missing_or_empty_required_args(tmp_path):
    s = _store(tmp_path)
    assert gc.submit(s, _stated(op="set_field", entity_id=D, field="org", value=""))["status"] == "refused"
    assert gc.submit(s, _stated(op="reject_relation", entity_a=D, relation="works_at"))["status"] == "refused"
    with s._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM graph_corrections").fetchone()[0] == 0


def test_submit_undo_refuses_on_missing_or_malformed_correction_id(tmp_path):
    s = _store(tmp_path)
    assert gc.submit(s, {"op": "undo"})["status"] == "refused"
    assert gc.submit(s, {"op": "undo", "correction_id": "abc"})["status"] == "refused"


def test_confirmed_via_rejects_unknown_values(tmp_path):
    s = _store(tmp_path)
    out = gc.submit(s, _inferred(op="hide", entity_id=S), confirmed_via="bogus")
    assert out["status"] == "refused"
    with s._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM graph_corrections").fetchone()[0] == 0
    assert gc.submit(s, _inferred(op="hide", entity_id=S), confirmed_via="elicitation")["status"] == "applied"


# --- (2) the generalised "later correction" undo guard ------------------------

def test_undo_assert_refused_while_later_correction_touches_retired_rival(tmp_path):
    """(a): assert D-works_at-S retires N; a later reject of D-works_at-N must
    block undoing the assert, or N would be restored wholesale and the reject
    would silently vanish while still reading 'applied'."""
    s = _store(tmp_path)
    a_out = gc.submit(s, _stated(op="assert_relation", entity_a=D, relation="works_at", entity_b=S))
    r_out = gc.submit(s, _stated(op="reject_relation", entity_a=D, relation="works_at", entity_b=N))
    assert r_out["status"] == "applied"

    out = gc.undo(s, a_out["correction_id"])
    assert out["status"] == "refused"
    assert f"undo correction {r_out['correction_id']} first" in out["error"]

    assert gc.undo(s, r_out["correction_id"])["status"] == "reverted"
    assert gc.undo(s, a_out["correction_id"])["status"] == "reverted"
    assert _rel(s, D, "works_at", S) is None
    assert _rel(s, D, "works_at", N)["invalidated_at"] is None


def test_undo_reject_refused_while_later_assert_on_same_triple_stands(tmp_path):
    """(b): reject then assert the same triple; undoing the reject first must
    be blocked while the assert stands, or the pair ends up rejected even
    after both are undone."""
    s = _store(tmp_path)
    before = dict(_rel(s, D, "works_at", N))
    reject_out = gc.submit(s, _stated(op="reject_relation", entity_a=D, relation="works_at", entity_b=N))
    assert_out = gc.submit(s, _stated(op="assert_relation", entity_a=D, relation="works_at", entity_b=N))
    assert assert_out["status"] == "applied"

    out = gc.undo(s, reject_out["correction_id"])
    assert out["status"] == "refused"
    assert f"undo correction {assert_out['correction_id']} first" in out["error"]

    assert gc.undo(s, assert_out["correction_id"])["status"] == "reverted"
    assert gc.undo(s, reject_out["correction_id"])["status"] == "reverted"
    # Only the correction-owned columns are asserted here: the assert's undo
    # deliberately leaves confidence/evidence/last_seen alone (item 3), so a
    # whole-row comparison against `before` would fail on those, not on
    # anything this fix is responsible for.
    after = dict(_rel(s, D, "works_at", N))
    for col in ("user_verdict", "invalidated_at", "valid_to", "superseded_reason",
               "invalidated_by_relation_id", "valid_from"):
        assert after[col] == before[col], col


def test_undo_first_set_field_refused_while_second_stands(tmp_path):
    """(c): two set_field org corrections on the same entity; undoing the
    first while the second still applies must be blocked, or org reverts and
    the lock is removed while the second reads 'applied'."""
    s = _store(tmp_path)
    c1 = gc.submit(s, _stated(op="set_field", entity_id=D, field="org",
                              value="Southbank Community Trust"))["correction_id"]
    c2 = gc.submit(s, _stated(op="set_field", entity_id=D, field="org",
                              value="The Lantern Co"))["correction_id"]

    out = gc.undo(s, c1)
    assert out["status"] == "refused"
    assert f"undo correction {c2} first" in out["error"]

    assert gc.undo(s, c2)["status"] == "reverted"
    assert gc.undo(s, c1)["status"] == "reverted"
    assert s.get_entity(D)["org"] == "Northgate Trust"
    assert s.locked_fields(D) == set()


def test_undo_refused_when_later_merge_touches_the_entity(tmp_path):
    """(ii): a later merge whose winner or loser id is an entity the earlier
    correction touched must block that correction's undo."""
    import json as _json

    from mcpbrain.store import _merge_entities_tx

    s = _store(tmp_path)
    c1 = gc.submit(s, _stated(op="set_field", entity_id=D, field="org",
                              value="Southbank Community Trust"))["correction_id"]
    with s._connect(write=True) as db:
        snap = _merge_entities_tx(db, S, D, method="user")  # S (loser) folded into D (winner)
    with s._connect(write=True) as db:
        merge_cid = db.execute(
            "INSERT INTO graph_corrections(op,basis,status,payload,snapshot,dedup_key,"
            "applied_at,applied_order) VALUES('merge','user_stated','applied',?,?,'merge:[]',?,?) ",
            (_json.dumps({"entity_id": S, "other_id": D}), _json.dumps(snap), gc._now(),
             gc._next_applied_order(db))).lastrowid

    out = gc.undo(s, c1)
    assert out["status"] == "refused"
    assert f"undo correction {merge_cid} first" in out["error"]


# --- (4) an assert superseded at birth reports itself as historical ----------

def test_assert_relation_older_than_current_reports_historical(tmp_path):
    s = _store(tmp_path)
    out = gc.submit(s, _stated(op="assert_relation", entity_a=D, relation="works_at",
                               entity_b=S, valid_from="2020-01-01"))
    assert out["status"] == "applied"
    assert "(recorded as historical: a newer rival is current)" in out["summary"]
    assert _rel(s, D, "works_at", S)["invalidated_at"] is not None
    assert _rel(s, D, "works_at", N)["invalidated_at"] is None  # the rival stays current


# --- (6) further coverage: pre-existing state survives an undo ---------------

def test_undo_hide_restores_a_pre_existing_suppression(tmp_path):
    s = _store(tmp_path)
    with s._connect(write=True) as db:
        db.execute("INSERT INTO entity_suppressions(entity_id, reason, suppressed_at) "
                  "VALUES(?, 'junk', '2020-01-01T00:00:00Z')", (S,))
    cid = gc.submit(s, _stated(op="hide", entity_id=S))["correction_id"]
    with s._connect() as db:
        row = db.execute("SELECT reason FROM entity_suppressions WHERE entity_id=?", (S,)).fetchone()
    assert row["reason"] == "user"
    gc.undo(s, cid)
    with s._connect() as db:
        row = db.execute("SELECT reason, suppressed_at FROM entity_suppressions WHERE entity_id=?",
                         (S,)).fetchone()
    assert row["reason"] == "junk" and row["suppressed_at"] == "2020-01-01T00:00:00Z"


def test_set_field_keeps_a_pre_existing_lock_on_undo(tmp_path):
    s = _store(tmp_path)
    with s._connect(write=True) as db:
        db.execute("INSERT INTO entity_field_locks(entity_id, field) VALUES(?, 'org')", (D,))
    cid = gc.submit(s, _stated(op="set_field", entity_id=D, field="org",
                               value="Southbank Community Trust"))["correction_id"]
    gc.undo(s, cid)
    assert s.get_entity(D)["org"] == "Northgate Trust"
    assert s.locked_fields(D) == {"org"}  # the pre-existing lock is not this correction's to remove


def test_assert_relation_undo_restores_degree(tmp_path):
    s = _store(tmp_path)
    before_a = s.get_entity(D)["degree"]
    before_b = s.get_entity(S)["degree"]
    out = gc.submit(s, _stated(op="assert_relation", entity_a=D, relation="mentioned_with", entity_b=S))
    assert out["status"] == "applied"
    assert s.get_entity(D)["degree"] == before_a + 1
    assert s.get_entity(S)["degree"] == before_b + 1
    gc.undo(s, out["correction_id"])
    assert s.get_entity(D)["degree"] == before_a
    assert s.get_entity(S)["degree"] == before_b


def test_refused_set_field_and_hide_write_nothing(tmp_path):
    s = _store(tmp_path)
    assert gc.submit(s, _stated(op="set_field", entity_id=D, field="role", value="volunteer"))["status"] == "refused"
    assert gc.submit(s, _stated(op="hide", entity_id="nobody"))["status"] == "refused"
    with s._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM graph_corrections").fetchone()[0] == 0


# --- fix round 2 ---------------------------------------------------------------

# --- (1) the guard orders by WHEN a correction became applied, not by id -----

def test_undo_guard_uses_applied_order_not_id_for_a_pending_then_approved_pair(tmp_path):
    """A pending inferred correction is assigned a LOWER id than one that
    applies immediately afterwards, but approving it later makes it the
    truly-later change. The guard must use applied_order, not id, or
    undoing #2 here would silently overwrite what approving #1 just did."""
    s = _store(tmp_path)
    pending = gc.submit(s, _inferred(op="set_field", entity_id=D, field="org",
                                     value="The Lantern Co"))
    assert pending["status"] == "pending"
    cid1 = pending["correction_id"]

    applied = gc.submit(s, _stated(op="set_field", entity_id=D, field="org",
                                   value="Southbank Community Trust"))
    assert applied["status"] == "applied"
    cid2 = applied["correction_id"]
    assert cid1 < cid2  # submitted first, but not yet applied

    assert gc.approve(s, cid1)["status"] == "applied"  # applied AFTER cid2

    out = gc.undo(s, cid2)
    assert out["status"] == "refused"
    assert f"undo correction {cid1} first" in out["error"]

    assert gc.undo(s, cid1)["status"] == "reverted"
    assert gc.undo(s, cid2)["status"] == "reverted"
    assert s.get_entity(D)["org"] == "Northgate Trust"


# --- (2) assert's rule-i keys only rivals it actually retired ----------------

def test_undo_assert_not_blocked_by_an_unrelated_non_singleton_rival(tmp_path):
    """mentioned_with is not a singleton relation, so asserting D-S never
    retires the pre-existing D-N relation; a later correction touching D-N
    must not block undoing the assert."""
    s = _store(tmp_path)
    gw.upsert_relation(s, D, "mentioned_with", N, valid_from="2026-01-01")
    a_out = gc.submit(s, _stated(op="assert_relation", entity_a=D, relation="mentioned_with", entity_b=S))
    assert a_out["status"] == "applied"
    r_out = gc.submit(s, _stated(op="reject_relation", entity_a=D, relation="mentioned_with", entity_b=N))
    assert r_out["status"] == "applied"

    assert gc.undo(s, a_out["correction_id"])["status"] == "reverted"


# --- (3) every string argument is type-checked -------------------------------

def test_submit_refuses_a_non_string_value(tmp_path):
    s = _store(tmp_path)
    out = gc.submit(s, _stated(op="set_field", entity_id=D, field="org", value=5))
    assert out["status"] == "refused" and "value" in out["error"]
    with s._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM graph_corrections").fetchone()[0] == 0
