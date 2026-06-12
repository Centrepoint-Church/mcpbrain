"""Schema catch-up tests for mcpbrain/store.py (Phase 1, Task 1).

Each test inits a fresh Store on a tmp path and inspects the resulting SQLite
schema via PRAGMA. Migration tests build an OLD-shaped table first, then run
init() and assert the back-fill happened without data loss.
"""

from mcpbrain.store import Store


def _store(tmp_path):
    s = Store(tmp_path / "g.sqlite3", dim=4)
    s.init()
    return s


def _cols(s, table):
    with s._connect() as db:
        return {r["name"] for r in db.execute(f"PRAGMA table_info({table})").fetchall()}


def _indexes(s, table):
    with s._connect() as db:
        return {r["name"] for r in db.execute(f"PRAGMA index_list({table})").fetchall()}


# --- 1.1 projects + areas -------------------------------------------------

def test_projects_and_areas_tables_exist(tmp_path):
    s = _store(tmp_path)
    proj = _cols(s, "projects")
    for col in ("id", "name", "org_tag", "status_line", "status",
                "archived_at", "area_id", "owner_entity_id"):
        assert col in proj, f"projects missing {col}"
    areas = _cols(s, "areas")
    for col in ("id", "org_id", "name", "description", "active", "archived_at"):
        assert col in areas, f"areas missing {col}"
    assert "idx_projects_active" in _indexes(s, "projects")


# --- 1.2 email_context + doc_context --------------------------------------

def test_email_context_table_shape(tmp_path):
    s = _store(tmp_path)
    ec = _cols(s, "email_context")
    for col in ("message_id", "subject", "sender", "sender_email", "sender_id",
                "date_iso", "thread_id", "org", "content_type", "summary",
                "topics", "labels", "contextual_summary", "reply_needed",
                "reply_reason"):
        assert col in ec, f"email_context missing {col}"
    # doc_context table removed in Task A6
    with s._connect() as db:
        dc_exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='doc_context'"
        ).fetchone()
    assert dc_exists is None, "doc_context table should not exist"
    idx = _indexes(s, "email_context")
    assert "idx_ec_org" in idx
    assert "idx_ec_thread" in idx


# --- 2.5 email_entities link table + writers ------------------------------

def test_email_entities_table(tmp_path):
    s = _store(tmp_path)
    cols = _cols(s, "email_entities")
    for col in ("message_id", "entity_id", "role"):
        assert col in cols, f"email_entities missing {col}"
    idx = _indexes(s, "email_entities")
    assert "idx_ee_entity" in idx
    assert "idx_ee_message" in idx


def test_store_email_context_writer(tmp_path):
    s = _store(tmp_path)
    s.upsert_email_context(
        "m-1", subject="Hall B", sender="Joel <joel@x>", sender_email="joel@x",
        date_iso="2026-04-18", thread_id="t1", org="Acme",
        content_type="request", summary="Confirm Hall B", topics="facilities",
        labels="INBOX", contextual_summary="Room booking follow-up",
        reply_needed=True, reply_reason="Direct question")
    with s._connect() as db:
        row = dict(db.execute(
            "SELECT * FROM email_context WHERE message_id='m-1'").fetchone())
    assert row["org"] == "Acme"
    assert row["content_type"] == "request"
    assert row["summary"] == "Confirm Hall B"
    assert row["topics"] == "facilities"
    assert row["labels"] == "INBOX"
    assert row["contextual_summary"] == "Room booking follow-up"
    assert row["reply_needed"] == 1
    assert row["reply_reason"] == "Direct question"
    # Upsert again with new situational fields — single row, fields refresh.
    s.upsert_email_context("m-1", subject="Hall B", org="Acme",
                           summary="Confirmed Hall B", content_type="update")
    with s._connect() as db:
        n = db.execute("SELECT COUNT(*) FROM email_context").fetchone()[0]
        row = dict(db.execute(
            "SELECT summary, content_type FROM email_context WHERE message_id='m-1'").fetchone())
    assert n == 1
    assert row["summary"] == "Confirmed Hall B"
    assert row["content_type"] == "update"


def test_store_link_email_entity(tmp_path):
    s = _store(tmp_path)
    s.link_email_entity("m-1", "joel-chelliah", role="sender")
    s.link_email_entity("m-1", "joel-chelliah", role="mentioned")  # no-op re-link
    with s._connect() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT * FROM email_entities WHERE message_id='m-1'").fetchall()]
    assert len(rows) == 1
    assert rows[0]["role"] == "sender"  # first role wins (INSERT OR IGNORE)


# --- 1.3 entity_observations ----------------------------------------------

def test_entity_observations_bitemporal_shape(tmp_path):
    s = _store(tmp_path)
    cols = _cols(s, "entity_observations")
    for col in ("entity_id", "attribute", "value", "source", "valid_from",
                "valid_to", "confidence", "confidence_source", "invalidated_at",
                "invalidated_by_observation_id", "created_at"):
        assert col in cols, f"entity_observations missing {col}"
    idx = _indexes(s, "entity_observations")
    assert "idx_eo_entity" in idx
    assert "idx_eo_valid_to" in idx
    assert "idx_eo_entity_attr" in idx
    # idx_eo_entity_attr is partial (WHERE valid_to IS NULL).
    with s._connect() as db:
        ddl = db.execute(
            "SELECT sql FROM sqlite_master WHERE name='idx_eo_entity_attr'"
        ).fetchone()["sql"]
    assert "valid_to IS NULL" in ddl


# --- 1.4 suppressed_entities (REMOVED - Task A6) -------------------------


# --- 1.5 entities: degree + email_count -----------------------------------

def test_entities_degree_and_email_count_added(tmp_path):
    s = _store(tmp_path)
    cols = _cols(s, "entities")
    assert "degree" in cols
    assert "email_count" in cols
    assert "idx_ent_degree" in _indexes(s, "entities")
    # Both default to 0 on a fresh insert.
    with s._connect() as db:
        db.execute("INSERT INTO entities(id,name,type) VALUES('x','X','person')")
        row = db.execute("SELECT degree, email_count FROM entities WHERE id='x'").fetchone()
    assert row["degree"] == 0
    assert row["email_count"] == 0


def test_entities_dedup_columns_exist(tmp_path):
    """Task 2.2 dedup-support columns: aliases, email_addr, notes."""
    s = _store(tmp_path)
    cols = _cols(s, "entities")
    for col in ("aliases", "email_addr", "notes"):
        assert col in cols, f"entities missing {col}"
    with s._connect() as db:
        db.execute("INSERT INTO entities(id,name,type) VALUES('y','Y','person')")
        row = db.execute(
            "SELECT aliases, email_addr, notes FROM entities WHERE id='y'"
        ).fetchone()
    assert row["aliases"] == ""
    assert row["email_addr"] == ""
    assert row["notes"] == ""


def test_entities_dedup_columns_backfilled_on_old_store(tmp_path):
    """An OLD store lacking the dedup columns gains them on init()."""
    import sqlite3
    path = tmp_path / "old_dedup.sqlite3"
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE entities(
        id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL,
        org TEXT DEFAULT '', first_seen TEXT DEFAULT '',
        last_seen TEXT DEFAULT '', mentions INTEGER DEFAULT 0)""")
    db.execute("INSERT INTO entities(id,name,type) VALUES('joel','Joel','person')")
    db.commit()
    db.close()

    s = Store(path, dim=4)
    s.init()
    cols = _cols(s, "entities")
    for col in ("aliases", "email_addr", "notes"):
        assert col in cols
    with s._connect() as db:
        row = db.execute("SELECT name FROM entities WHERE id='joel'").fetchone()
    assert row["name"] == "Joel"


def test_entities_degree_email_count_backfilled_on_old_store(tmp_path):
    """An OLD store whose entities table lacks both columns gains them on
    init() without losing existing rows."""
    import sqlite3
    path = tmp_path / "old.sqlite3"
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE entities(
        id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL,
        org TEXT DEFAULT '', first_seen TEXT DEFAULT '',
        last_seen TEXT DEFAULT '', mentions INTEGER DEFAULT 0)""")
    db.execute("INSERT INTO entities(id,name,type,mentions) VALUES('joel','Joel','person',5)")
    db.commit()
    db.close()

    s = Store(path, dim=4)
    s.init()
    cols = _cols(s, "entities")
    assert "degree" in cols
    assert "email_count" in cols
    with s._connect() as db:
        row = db.execute("SELECT name, mentions, degree, email_count "
                         "FROM entities WHERE id='joel'").fetchone()
    assert row["name"] == "Joel"
    assert row["mentions"] == 5
    assert row["degree"] == 0
    assert row["email_count"] == 0


# --- 1.6 entity_relations bitemporal --------------------------------------

def test_entity_relations_bitemporal_columns(tmp_path):
    s = _store(tmp_path)
    cols = _cols(s, "entity_relations")
    for col in ("valid_from", "valid_to", "invalidated_at",
                "invalidated_by_relation_id", "superseded_reason", "confidence",
                "evidence", "strength", "last_seen"):
        assert col in cols, f"entity_relations missing {col}"
    # The existing UNIQUE constraint columns + source_doc_id are preserved.
    for col in ("entity_a", "relation", "entity_b", "source_doc_id"):
        assert col in cols
    # Dead columns removed in Task A6: normalised_strength, since
    assert "normalised_strength" not in cols
    assert "since" not in cols


def test_entity_relations_temporal_indexes(tmp_path):
    s = _store(tmp_path)
    idx = _indexes(s, "entity_relations")
    for name in ("idx_er_valid_now", "idx_er_a_rel", "idx_er_b_rel",
                 "idx_er_invalidated", "idx_er_valid_range"):
        assert name in idx, f"missing index {name}"
    with s._connect() as db:
        ddl = db.execute(
            "SELECT sql FROM sqlite_master WHERE name='idx_er_valid_now'"
        ).fetchone()["sql"]
    assert "invalidated_at IS NULL" in ddl
    assert "valid_to IS NULL" in ddl


def test_entity_relations_bitemporal_migration_preserves_data(tmp_path):
    """A store with the CURRENT mcpbrain entity_relations (UNIQUE + source_doc_id,
    no bitemporal columns) gains the bitemporal columns on init() without losing
    rows or the UNIQUE constraint."""
    import sqlite3
    path = tmp_path / "old.sqlite3"
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE entity_relations(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_a TEXT NOT NULL, relation TEXT NOT NULL, entity_b TEXT NOT NULL,
        source_doc_id TEXT DEFAULT '',
        UNIQUE(entity_a, relation, entity_b))""")
    db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b,source_doc_id) "
               "VALUES('joel','leads','acme','doc1')")
    db.commit()
    db.close()

    s = Store(path, dim=4)
    s.init()
    cols = _cols(s, "entity_relations")
    assert "valid_to" in cols
    assert "strength" in cols
    with s._connect() as db:
        row = db.execute("SELECT entity_a, relation, entity_b, source_doc_id "
                         "FROM entity_relations WHERE entity_a='joel'").fetchone()
    assert row["relation"] == "leads"
    assert row["source_doc_id"] == "doc1"


# --- 1.7 unified actions table --------------------------------------------

def test_actions_table_shape(tmp_path):
    s = _store(tmp_path)
    cols = _cols(s, "actions")
    for col in ("id", "text", "owner", "owner_entity_id", "status", "deadline",
                "org", "project_id", "area_id", "confidence", "source",
                "context_tag", "cluster_id", "source_doc_id", "thread_id",
                "resolved_by", "resolved_at", "text_fingerprint", "created_at",
                "updated_at"):
        assert col in cols, f"actions missing {col}"
    idx = _indexes(s, "actions")
    assert "idx_actions_owner" in idx
    assert "idx_actions_status" in idx
    assert "idx_actions_thread" in idx


def test_graph_tables_migrated_into_actions(tmp_path):
    """OLD graph_actions + graph_decisions rows are copied into actions on init(),
    the old tables are renamed to *_legacy, and re-running init() is a no-op."""
    import sqlite3
    path = tmp_path / "old.sqlite3"
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE graph_actions(
        id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL,
        owner TEXT DEFAULT '', deadline TEXT DEFAULT '', status TEXT DEFAULT 'open',
        source_doc_id TEXT DEFAULT '', thread_id TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    db.execute("""CREATE TABLE graph_decisions(
        id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL,
        decided_on TEXT DEFAULT '', source_doc_id TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    db.execute("INSERT INTO graph_actions(text,owner,deadline,status,thread_id) "
               "VALUES('Send budget','Sam','2026-06-10','open','t1')")
    db.execute("INSERT INTO graph_decisions(text,decided_on) "
               "VALUES('Approved AV spend','2026-05-01')")
    db.commit()
    db.close()

    s = Store(path, dim=4)
    s.init()

    with s._connect() as db:
        acts = [dict(r) for r in db.execute("SELECT * FROM actions ORDER BY id").fetchall()]
        # old tables renamed to *_legacy
        tables = {r["name"] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "graph_actions_legacy" in tables
    assert "graph_decisions_legacy" in tables
    assert "graph_actions" not in tables
    assert "graph_decisions" not in tables

    email_rows = [a for a in acts if a["source"] == "email"]
    decision_rows = [a for a in acts if a["source"] == "decision"]
    assert len(email_rows) == 1
    assert len(decision_rows) == 1
    assert email_rows[0]["text"] == "Send budget"
    assert email_rows[0]["owner"] == "Sam"
    assert email_rows[0]["deadline"] == "2026-06-10"
    assert email_rows[0]["status"] == "open"
    assert email_rows[0]["thread_id"] == "t1"
    assert decision_rows[0]["text"] == "Approved AV spend"
    assert decision_rows[0]["status"] == "recorded"
    assert decision_rows[0]["owner"] == ""

    # Idempotent: second init() does not duplicate.
    s.init()
    with s._connect() as db:
        n = db.execute("SELECT COUNT(*) FROM actions").fetchone()[0]
    assert n == 2


def test_unified_action_helpers(tmp_path):
    s = _store(tmp_path)
    aid = s.add_unified_action(text="Draft policy", owner="Sam", status="open",
                               thread_id="t9", source="meeting")
    assert isinstance(aid, int)
    rows = s.list_unified_actions()
    assert len(rows) == 1
    assert rows[0]["text"] == "Draft policy"
    owned = s.actions_for_owner_unified("sam")
    assert len(owned) == 1
    assert s.actions_for_owner_unified("Nobody") == []


def test_set_action_status_closes_and_reopens(tmp_path):
    s = _store(tmp_path)
    aid = s.add_unified_action(text="Run the audit", owner="Sam", status="open")
    s.set_action_status(aid, "done", resolved_by="m-99")
    row = [a for a in s.list_unified_actions() if a["id"] == aid][0]
    assert row["status"] == "done"
    assert row["resolved_by"] == "m-99"
    assert row["resolved_at"]  # set when closing
    # Reopening clears resolved_at.
    s.set_action_status(aid, "open")
    row = [a for a in s.list_unified_actions() if a["id"] == aid][0]
    assert row["status"] == "open"
    assert row["resolved_at"] == ""


def test_set_action_text_rewrites_and_refingerprints(tmp_path):
    s = _store(tmp_path)
    aid = s.add_unified_action(text="Old text", owner="Sam",
                               text_fingerprint="stale")
    s.set_action_text(aid, "New clarified text")
    row = [a for a in s.list_unified_actions() if a["id"] == aid][0]
    assert row["text"] == "New clarified text"
    # Fingerprint refreshed to match the new text (not the stale seed).
    assert row["text_fingerprint"] != "stale"
    assert row["text_fingerprint"]


# --- 1.8 topic-as-entity helper -------------------------------------------

def test_upsert_topic_entity(tmp_path):
    s = _store(tmp_path)
    eid = s.upsert_topic_entity("facilities")
    assert eid == "topic-facilities"
    ent = s.get_entity(eid)
    assert ent["type"] == "topic"
    assert ent["name"] == "facilities"
    assert ent["email_count"] == 1
    # Second call bumps email_count and returns the same id.
    eid2 = s.upsert_topic_entity("facilities")
    assert eid2 == eid
    ent2 = s.get_entity(eid)
    assert ent2["email_count"] == 2

    # Too-short tags return None and create nothing.
    assert s.upsert_topic_entity("x") is None


# --- 8.1 store readers for the new tables ---------------------------------

def test_store_unified_actions_by_owner_status(tmp_path):
    s = _store(tmp_path)
    s.add_unified_action(text="Draft policy", owner="Sam", status="open",
                         thread_id="t1")
    s.add_unified_action(text="Send budget", owner="Sam", status="done",
                         thread_id="t1")
    s.add_unified_action(text="Book hall", owner="Taryn", status="open",
                         thread_id="t2")

    # owner + status filter (owner is case-insensitive).
    sam_open = s.unified_actions(owner="sam", status="open")
    assert [a["text"] for a in sam_open] == ["Draft policy"]

    # status-only filter.
    open_all = s.unified_actions(status="open")
    assert {a["text"] for a in open_all} == {"Draft policy", "Book hall"}

    # owner-only filter.
    sam_all = s.unified_actions(owner="Sam")
    assert {a["text"] for a in sam_all} == {"Draft policy", "Send budget"}

    # thread_id filter.
    t1 = s.unified_actions(thread_id="t1")
    assert {a["text"] for a in t1} == {"Draft policy", "Send budget"}

    # no filters returns everything.
    assert len(s.unified_actions()) == 3


def test_store_relations_at_time(tmp_path):
    s = _store(tmp_path)
    s.add_relation("taryn", "reports_to", "joel", "doc-1")
    with s._connect() as db:
        db.execute(
            "UPDATE entity_relations SET valid_from=?, valid_to=? "
            "WHERE entity_a='taryn'",
            ("2024-01-01", "2025-01-01"))

    # Inside the valid window — returned.
    rels = s.relations_for("taryn", at_time="2024-06-01")
    assert any(r["relation"] == "reports_to" for r in rels)

    # After valid_to — excluded.
    rels = s.relations_for("taryn", at_time="2025-06-01")
    assert not any(r["relation"] == "reports_to" for r in rels)

    # Exactly at valid_to — excluded. The interval is half-open (valid_to > at),
    # so the boundary instant is not part of the valid window. Pins the
    # inequality against a future >= regression.
    rels = s.relations_for("taryn", at_time="2025-01-01")
    assert not any(r["relation"] == "reports_to" for r in rels)


def test_store_relations_include_invalidated(tmp_path):
    s = _store(tmp_path)
    s.add_relation("taryn", "reports_to", "joel", "doc-1")
    with s._connect() as db:
        db.execute(
            "UPDATE entity_relations SET invalidated_at=? WHERE entity_a='taryn'",
            ("2025-02-01",))

    # Default excludes invalidated rows.
    assert s.relations_for("taryn") == []

    # Explicitly include them.
    rels = s.relations_for("taryn", include_invalidated=True)
    assert any(r["relation"] == "reports_to" for r in rels)


def test_store_relations_legacy_rows_still_returned(tmp_path):
    """add_relation leaves invalidated_at NULL, so legacy-written relations
    still surface by default (keeps existing brain_context/graph behaviour)."""
    s = _store(tmp_path)
    s.add_relation("taryn", "reports_to", "joel", "doc-1")
    rels = s.relations_for("taryn")
    assert any(r["relation"] == "reports_to" for r in rels)


def test_store_get_project(tmp_path):
    s = _store(tmp_path)
    with s._connect() as db:
        db.execute(
            "INSERT INTO projects(id, name, org_tag, status, owner_entity_id) "
            "VALUES('p-1', 'College 2026', 'Acme', 'active', 'sam')")
    proj = s.get_project("p-1")
    assert proj["name"] == "College 2026"
    assert proj["org_tag"] == "Acme"
    assert s.get_project("missing") is None


def test_store_get_area(tmp_path):
    s = _store(tmp_path)
    with s._connect() as db:
        db.execute(
            "INSERT INTO areas(id, org_id, name, active) "
            "VALUES('a-1', 'Acme', 'Facilities', 1)")
    area = s.get_area("a-1")
    assert area["name"] == "Facilities"
    assert area["org_id"] == "Acme"
    assert s.get_area("missing") is None


def test_store_projects_and_areas_for_org(tmp_path):
    s = _store(tmp_path)
    with s._connect() as db:
        db.execute("INSERT INTO projects(id, name, org_tag, status) "
                   "VALUES('p-1', 'Live', 'Acme', 'active')")
        db.execute("INSERT INTO projects(id, name, org_tag, archived_at) "
                   "VALUES('p-2', 'Archived', 'Acme', '2025-01-01')")
        db.execute("INSERT INTO projects(id, name, org_tag, status) "
                   "VALUES('p-3', 'Other org', 'ACC', 'active')")
        db.execute("INSERT INTO areas(id, org_id, name, active) "
                   "VALUES('a-1', 'Acme', 'Facilities', 1)")
        db.execute("INSERT INTO areas(id, org_id, name, active) "
                   "VALUES('a-2', 'Acme', 'Retired', 0)")
        db.execute("INSERT INTO areas(id, org_id, name, active) "
                   "VALUES('a-3', 'ACC', 'CAMS', 1)")
    projs = s.projects_for_org("Acme")
    assert {p["id"] for p in projs} == {"p-1"}  # active, not archived, matching org
    areas = s.areas_for_org("Acme")
    assert {a["id"] for a in areas} == {"a-1"}  # active=1, matching org_id


def test_store_projects_owned_by(tmp_path):
    s = _store(tmp_path)
    with s._connect() as db:
        db.execute("INSERT INTO projects(id, name, owner_entity_id, status) "
                   "VALUES('p-1', 'Owned', 'sam', 'active')")
        db.execute("INSERT INTO projects(id, name, owner_entity_id, status) "
                   "VALUES('p-2', 'Other', 'taryn', 'active')")
        db.execute("INSERT INTO projects(id, name, owner_entity_id, archived_at) "
                   "VALUES('p-3', 'Archived', 'sam', '2025-01-01')")
    # Active, not archived, matching owner.
    assert {p["id"] for p in s.projects_owned_by("sam")} == {"p-1"}


def test_store_areas_owned_by(tmp_path):
    """areas has no owner_entity_id column, so an owned area is one carried by a
    project the entity owns (project.area_id)."""
    s = _store(tmp_path)
    with s._connect() as db:
        db.execute("INSERT INTO areas(id, org_id, name, active) "
                   "VALUES('a-1', 'Acme', 'Ops', 1)")
        db.execute("INSERT INTO areas(id, org_id, name, active) "
                   "VALUES('a-2', 'Acme', 'Unrelated', 1)")
        db.execute("INSERT INTO projects(id, name, owner_entity_id, area_id, status) "
                   "VALUES('p-1', 'Owned', 'sam', 'a-1', 'active')")
    assert {a["id"] for a in s.areas_owned_by("sam")} == {"a-1"}


# --- A6: dead schema removed -----------------------------------------------

def test_doc_context_table_not_created(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4); s.init()
    with s._connect() as db:
        row = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='doc_context'").fetchone()
    assert row is None


def test_suppressed_entities_table_not_created(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4); s.init()
    with s._connect() as db:
        row = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='suppressed_entities'").fetchone()
    assert row is None


def test_entity_relations_has_no_normalised_strength_or_since(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4); s.init()
    with s._connect() as db:
        cols = {r["name"] for r in db.execute("PRAGMA table_info(entity_relations)").fetchall()}
    assert "normalised_strength" not in cols and "since" not in cols
