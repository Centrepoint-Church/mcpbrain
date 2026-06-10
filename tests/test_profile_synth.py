"""profile_synthesis: standing 2-4 sentence entity profiles, Cowork-filled."""
from mcpbrain import profile_synth
from mcpbrain.store import Store
import mcpbrain.graph_write as gw


def _store(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    return s


def _person(s, name, email_count=5):
    eid = gw.upsert_entity(s, name=name, entity_type="person", org="Acme")
    with s._connect() as db:
        db.execute("UPDATE entities SET email_count=? WHERE id=?", (email_count, eid))
    return eid


def test_requests_pick_unprofiled_high_signal_people(tmp_path):
    s = _store(tmp_path)
    _person(s, "Taryn Hamilton", email_count=10)
    _person(s, "One Mail", email_count=1)        # below floor
    reqs = profile_synth.build_profile_requests(s, cap=6)
    names = [r["name"] for r in reqs]
    assert "Taryn Hamilton" in names and "One Mail" not in names
    assert all({"entity_id", "name", "org", "role", "relations"} <= set(r) for r in reqs)


def test_requests_cap(tmp_path):
    s = _store(tmp_path)
    for i in range(10):
        _person(s, f"Person Num{i}", email_count=5)
    assert len(profile_synth.build_profile_requests(s, cap=6)) == 6


def test_drain_writes_profile_and_change_log(tmp_path):
    s = _store(tmp_path)
    eid = _person(s, "Taryn Hamilton")
    n = profile_synth.drain_profiles(s, {"profile_synthesis": [
        {"entity_id": eid, "profile": "Executive Pastor at Acme. Leads ops."},
        {"entity_id": "nonexistent", "profile": "x"},        # skipped silently
        {"entity_id": eid, "profile": ""},                   # empty skipped
    ]})
    assert n["profiles_written"] == 1
    ent = s.find_entity("Taryn Hamilton")
    assert "Executive Pastor" in ent["profile"]
    assert s.recent_changes(5)[0]["source"] == "profile_synthesis"


def test_profiled_entity_not_rerequested(tmp_path):
    s = _store(tmp_path)
    eid = _person(s, "Taryn Hamilton")
    profile_synth.drain_profiles(s, {"profile_synthesis": [
        {"entity_id": eid, "profile": "A profile."}]})
    assert profile_synth.build_profile_requests(s, cap=6) == []


def test_drainer_registered_and_callable(tmp_path):
    from mcpbrain import profile_synth as _ps  # noqa: F401 — import registers the drainer
    from mcpbrain.drain import BLOCK_DRAINERS
    s = _store(tmp_path)
    eid = _person(s, "Taryn Hamilton")
    drainer = BLOCK_DRAINERS["profile_synthesis"]
    n = drainer(s, {"profile_synthesis": [
        {"entity_id": eid, "profile": "Executive Pastor."}]})
    assert n["profiles_written"] == 1
