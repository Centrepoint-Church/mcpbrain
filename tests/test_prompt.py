"""Tests for mcpbrain/prompt.py (Phase 1, Task 4+6 context-block builders).

Offline only — no network, no LLM. Each test builds a fresh Store on a tmp
path, seeds the relevant tables directly, and exercises the read/build
functions. These functions feed pending.json's `context` block; the consuming
contract is mcpbrain/enrich_prompt.md.
"""


from mcpbrain import prompt
from mcpbrain.store import Store


def _store(tmp_path):
    s = Store(tmp_path / "p.sqlite3", dim=4)
    s.init()
    return s


# --- A: read_projects / read_areas ----------------------------------------

def test_active_projects_block_uses_store(tmp_path):
    s = _store(tmp_path)
    with s._connect() as db:
        db.execute(
            "INSERT INTO projects(id,name,org_tag,status_line,archived_at) "
            "VALUES(?,?,?,?,?)",
            ("byford-grant", "Byford Grant", "Centrepoint", "Awaiting council sign-off", None),
        )
        db.execute(
            "INSERT INTO projects(id,name,org_tag,status_line,archived_at) "
            "VALUES(?,?,?,?,?)",
            ("old-fete", "Old Fete", "Centrepoint", "done", "2026-01-01"),
        )

    rows = prompt.read_projects(s)

    assert [r["id"] for r in rows] == ["byford-grant"]
    r = rows[0]
    assert r["name"] == "Byford Grant"
    assert r["org"] == "Centrepoint"
    assert r["status_line"] == "Awaiting council sign-off"


def test_active_areas_block_uses_store(tmp_path):
    s = _store(tmp_path)
    with s._connect() as db:
        db.execute(
            "INSERT INTO areas(id,org_id,name,description,active) VALUES(?,?,?,?,?)",
            ("centrepoint-it", "Centrepoint", "IT", "Systems and devices", 1),
        )
        db.execute(
            "INSERT INTO areas(id,org_id,name,description,active) VALUES(?,?,?,?,?)",
            ("retired-av", "Centrepoint", "AV", "Old AV area", 0),
        )

    rows = prompt.read_areas(s)

    assert [r["id"] for r in rows] == ["centrepoint-it"]
    r = rows[0]
    assert r["name"] == "IT"
    assert r["org"] == "Centrepoint"
    assert r["description"] == "Systems and devices"


# --- B: build_known_people ------------------------------------------------

def _add_person(db, eid, name, org, email_count):
    db.execute(
        "INSERT INTO entities(id,name,type,org,email_count) VALUES(?,?,?,?,?)",
        (eid, name, "person", org, email_count),
    )


def _add_role(db, eid, value, source="email_signature", valid_to=None,
              invalidated_at=None):
    db.execute(
        "INSERT INTO entity_observations"
        "(entity_id,attribute,value,source,valid_to,invalidated_at) "
        "VALUES(?,?,?,?,?,?)",
        (eid, "role", value, source, valid_to, invalidated_at),
    )


def _link_thread(db, thread_id, message_id, entity_id):
    db.execute(
        "INSERT INTO email_context(message_id,thread_id) VALUES(?,?)",
        (message_id, thread_id),
    )
    db.execute(
        "INSERT INTO email_entities(message_id,entity_id,role) VALUES(?,?,?)",
        (message_id, entity_id, "sender"),
    )


def test_known_people_global_core_capped(tmp_path):
    s = _store(tmp_path)
    with s._connect() as db:
        # Seed more than the cap of confirmed-org + current-role people.
        for i in range(45):
            eid = f"person-{i:02d}"
            _add_person(db, eid, f"Person {i:02d}", "Centrepoint", email_count=100 - i)
            _add_role(db, eid, "Coordinator")

    rows = prompt.build_known_people(s, batch_thread_ids=[], core_cap=40)

    assert len(rows) == 40
    # Ordered by email_count desc → the top 40 (person-00..person-39) are kept.
    names = {r["name"] for r in rows}
    assert "Person 00" in names           # highest email_count
    assert "Person 44" not in names       # lowest, cut by cap
    assert all(r["role"] == "Coordinator" for r in rows)


def test_known_people_includes_batch_senders(tmp_path):
    s = _store(tmp_path)
    with s._connect() as db:
        # A low-traffic person with no confirmed org/role: not in the global core.
        _add_person(db, "batch-only", "Batch Only", "unknown", email_count=1)
        _link_thread(db, "t-1", "m-1", "batch-only")

    rows = prompt.build_known_people(s, batch_thread_ids=["t-1"], core_cap=40)

    assert any(r["name"] == "Batch Only" for r in rows)


def test_known_people_excludes_josh(tmp_path):
    from mcpbrain.graph_write import OwnerIdentity
    s = _store(tmp_path)
    with s._connect() as db:
        _add_person(db, "josh-kemp", "Josh Kemp", "Centrepoint", email_count=999)
        _add_role(db, "josh-kemp", "Operations Manager")
        # Also reach Josh via a batch thread to confirm the overlay excludes him too.
        _link_thread(db, "t-1", "m-1", "josh-kemp")

    # Pass the owner explicitly so the test is self-contained.
    rows = prompt.build_known_people(s, batch_thread_ids=["t-1"], core_cap=40,
                                     owner=OwnerIdentity())

    assert all("josh" not in r["name"].lower() for r in rows)
    assert all(r.get("id") != "josh-kemp" for r in rows)


def test_known_people_dedups_overlap(tmp_path):
    s = _store(tmp_path)
    with s._connect() as db:
        _add_person(db, "joel", "Joel Chelliah", "Centrepoint", email_count=50)
        _add_role(db, "joel", "Senior Pastor")
        # Same person also appears in a batch thread.
        _link_thread(db, "t-1", "m-1", "joel")

    rows = prompt.build_known_people(s, batch_thread_ids=["t-1"], core_cap=40)

    matches = [r for r in rows if r.get("id") == "joel" or r["name"] == "Joel Chelliah"]
    assert len(matches) == 1
    assert matches[0]["role"] == "Senior Pastor"


def test_known_people_picks_current_role_not_superseded(tmp_path):
    s = _store(tmp_path)
    with s._connect() as db:
        _add_person(db, "taryn", "Taryn Hamilton", "Centrepoint", email_count=50)
        # Superseded role: invalidated_at set.
        _add_role(db, "taryn", "Worship Pastor",
                  invalidated_at="2025-01-01T00:00:00Z")
        # Current role: valid_to and invalidated_at both NULL.
        _add_role(db, "taryn", "Executive Pastor")

    rows = prompt.build_known_people(s, batch_thread_ids=[], core_cap=40)

    matches = [r for r in rows if r.get("id") == "taryn"]
    assert len(matches) == 1
    assert matches[0]["role"] == "Executive Pastor"


def test_known_people_empty_batch_returns_core_only(tmp_path):
    s = _store(tmp_path)
    with s._connect() as db:
        # Core person: confirmed org + current role + sufficient email_count.
        _add_person(db, "joel", "Joel Chelliah", "Centrepoint", email_count=50)
        _add_role(db, "joel", "Senior Pastor")
        # Thread-only person: would surface only via a batch overlay.
        _add_person(db, "batch-only", "Batch Only", "unknown", email_count=1)
        _link_thread(db, "t-1", "m-1", "batch-only")

    rows = prompt.build_known_people(s, batch_thread_ids=[], core_cap=40)

    names = {r["name"] for r in rows}
    assert "Joel Chelliah" in names
    assert "Batch Only" not in names
