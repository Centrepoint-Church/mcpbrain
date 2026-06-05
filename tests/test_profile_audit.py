"""profile_audit: corrections applied through supersession, logged with undo."""
from mcpbrain import profile_audit
from mcpbrain.store import Store
import mcpbrain.graph_write as gw


def _store(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    return s


def _person_with_profile(s, name, role="Volunteer"):
    eid = gw.upsert_entity(s, name=name, entity_type="person", org="Centrepoint")
    # Insert the role observation directly so test fixtures with contextual
    # role labels (e.g. "Volunteer") bypass write_role_observation's junk guard.
    with s._connect() as db:
        db.execute(
            "INSERT INTO entity_observations"
            "(entity_id, attribute, value, source, valid_from, confidence_source)"
            " VALUES (?, 'role', ?, 'llm_extraction', '2026-01-01', 'medium')",
            (eid, role),
        )
        db.execute("UPDATE entities SET profile='A profile.', "
                   "profile_updated_at='2026-05-01T00:00:00Z', email_count=5 "
                   "WHERE id=?", (eid,))
    return eid


def test_requests_carry_profile_role_relations(tmp_path):
    s = _store(tmp_path)
    _person_with_profile(s, "Taryn Hamilton")
    reqs = profile_audit.build_audit_requests(s, cap=10)
    assert reqs and {"entity_id", "name", "org", "profile", "role"} <= set(reqs[0])


def test_role_correction_applies_via_observation(tmp_path):
    s = _store(tmp_path)
    eid = _person_with_profile(s, "Taryn Hamilton", role="Volunteer")
    n = profile_audit.drain_audit(s, {"profile_audit": [
        {"entity_id": eid,
         "corrections": [{"field": "role", "new_value": "Executive Pastor",
                          "evidence": "signature in 6 emails"}]}]})
    assert n["corrections_applied"] == 1
    change = s.recent_changes(5)[0]
    assert change["source"] == "profile_audit"
    assert "Volunteer" in change["revert_ref"]     # undo info: superseded value


def test_caps_and_unknown_fields_skipped(tmp_path):
    s = _store(tmp_path)
    eid = _person_with_profile(s, "Taryn Hamilton")
    n = profile_audit.drain_audit(s, {"profile_audit": [
        {"entity_id": eid,
         "corrections": [{"field": "shoe_size", "new_value": "11"}]}]},
        max_corrections=10)
    assert n["corrections_applied"] == 0


def test_max_corrections_cap(tmp_path):
    s = _store(tmp_path)
    # Create 3 people, each with one correction.
    eids = [_person_with_profile(s, f"Person {i}", role="Volunteer") for i in range(3)]
    inbox = {"profile_audit": [
        {"entity_id": eid,
         "corrections": [{"field": "org", "new_value": "Updated Org"}]}
        for eid in eids
    ]}
    # Cap at 2 — only 2 should apply.
    n = profile_audit.drain_audit(s, inbox, max_corrections=2)
    assert n["corrections_applied"] == 2


def test_max_corrections_cap_within_single_entity(tmp_path):
    s = _store(tmp_path)
    # One entity carrying more corrections than the cap: the inner loop must
    # stop at the cap, not apply all of them.
    eid = _person_with_profile(s, "Taryn Hamilton", role="Volunteer")
    cap = 3
    inbox = {"profile_audit": [
        {"entity_id": eid,
         "corrections": [
             {"field": "org", "new_value": f"Org {i}"}
             for i in range(cap + 5)
         ]}
    ]}
    n = profile_audit.drain_audit(s, inbox, max_corrections=cap)
    assert n["corrections_applied"] == cap
