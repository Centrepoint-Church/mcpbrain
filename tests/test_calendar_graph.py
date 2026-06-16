"""Calendar attendees -> person graph (pure structured-data writes, no LLM)."""

from mcpbrain.graph_write import OwnerIdentity
from mcpbrain.store import Store
from mcpbrain.sync.calendar import _apply_attendees_to_graph


def _store(tmp_path):
    s = Store(tmp_path / "cal.sqlite3", dim=4)
    s.init()
    return s


# Owner is "Josh" at josh@centrepoint.church. Aliases are lowercased per
# OwnerIdentity contract; entity_id is the owner's slug (never upserted).
_OWNER = OwnerIdentity(
    name="Josh",
    entity_id="josh-kemp",
    aliases=frozenset({"josh", "josh kemp", "josh.k@centrepoint.church"}),
)


def _event(eid, attendees, start="2026-06-01T09:00:00Z"):
    return {
        "id": eid,
        "summary": "Project sync",
        "status": "confirmed",
        "start": {"dateTime": start},
        "end": {"dateTime": "2026-06-01T10:00:00Z"},
        "attendees": attendees,
    }


def test_two_external_attendees_create_entities_and_attended_relations(tmp_path):
    s = _store(tmp_path)
    ev = _event("evt1", [
        {"displayName": "Sam Chen", "email": "sam@partner.org"},
        {"displayName": "Dana Lee", "email": "dana@other.org"},
    ])
    n = _apply_attendees_to_graph(s, ev, _OWNER)
    assert n == 2

    sam = s.find_entity("sam@partner.org") or s.find_entity("Sam Chen")
    dana = s.find_entity("dana@other.org") or s.find_entity("Dana Lee")
    assert sam is not None and dana is not None
    assert sam["type"] == "person" and dana["type"] == "person"

    with s._connect() as db:
        rows = db.execute(
            "SELECT entity_a, relation, entity_b FROM entity_relations "
            "WHERE relation = 'attended' AND invalidated_at IS NULL").fetchall()
    pairs = {(r["entity_a"], r["entity_b"]) for r in rows}
    assert (_OWNER.entity_id, sam["id"]) in pairs
    assert (_OWNER.entity_id, dana["id"]) in pairs


def test_owner_self_attendee_is_excluded(tmp_path):
    s = _store(tmp_path)
    ev = _event("evt2", [
        {"displayName": "Josh", "email": "josh.k@centrepoint.church"},
        {"displayName": "Sam Chen", "email": "sam@partner.org"},
    ])
    n = _apply_attendees_to_graph(s, ev, _OWNER)
    assert n == 1  # only Sam
    assert s.find_entity("josh-kemp") is None


def test_junk_role_attendee_is_excluded(tmp_path):
    s = _store(tmp_path)
    # A long room-resource name is junk by is_junk_entity (>60 chars or bracket
    # chars); a bracketed resource name trips the structural junk patterns.
    ev = _event("evt3", [
        {"displayName": "Conference Room A [resource]", "email": "room-a@resource.calendar.google.com"},
        {"displayName": "Sam Chen", "email": "sam@partner.org"},
    ])
    n = _apply_attendees_to_graph(s, ev, _OWNER)
    assert n == 1
    assert s.find_entity("Sam Chen") is not None


def test_attendee_with_no_email_uses_display_name(tmp_path):
    s = _store(tmp_path)
    ev = _event("evt4", [{"displayName": "Pat Morgan"}])
    n = _apply_attendees_to_graph(s, ev, _OWNER)
    assert n == 1
    assert s.find_entity("Pat Morgan") is not None


def test_resync_same_event_is_idempotent(tmp_path):
    s = _store(tmp_path)
    ev = _event("evt5", [{"displayName": "Sam Chen", "email": "sam@partner.org"}])
    _apply_attendees_to_graph(s, ev, _OWNER)
    _apply_attendees_to_graph(s, ev, _OWNER)  # re-sync

    with s._connect() as db:
        ent_count = db.execute(
            "SELECT COUNT(*) c FROM entities WHERE type='person'").fetchone()["c"]
        rel_count = db.execute(
            "SELECT COUNT(*) c FROM entity_relations "
            "WHERE relation='attended' AND invalidated_at IS NULL").fetchone()["c"]
    assert ent_count == 1
    assert rel_count == 1
