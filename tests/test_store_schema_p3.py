"""Schema catch-up tests for mcpbrain/store.py — Phase 3, Task 0.

Tests cover:
  0.1  entity_communities + community_summaries tables
  0.2  thread_context table
  0.3  proactive_findings table
  0.4  actions.waiting_on* columns
  0.5  reader/writer methods (communities, thread_context, proactive_findings, waiting-on)
"""

import sqlite3
from datetime import datetime, timezone

from mcpbrain.store import Store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _store(tmp_path):
    s = Store(tmp_path / "test.db", dim=64)
    s.init()
    return s


def _cols(s, table):
    with s._connect() as db:
        return {r["name"] for r in db.execute(f"PRAGMA table_info({table})").fetchall()}


def _indexes(s, table):
    with s._connect() as db:
        return {r["name"] for r in db.execute(f"PRAGMA index_list({table})").fetchall()}


def _index_ddl(s, name):
    with s._connect() as db:
        row = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)
        ).fetchone()
    return row["sql"] if row else None


# ---------------------------------------------------------------------------
# 0.1  entity_communities + community_summaries
# ---------------------------------------------------------------------------

def test_community_tables_exist(tmp_path):
    s = _store(tmp_path)

    ec = _cols(s, "entity_communities")
    for col in ("entity_id", "community_id", "level"):
        assert col in ec, f"entity_communities missing {col}"

    cs = _cols(s, "community_summaries")
    for col in ("community_id", "level", "title", "summary",
                "member_count", "key_entities", "updated"):
        assert col in cs, f"community_summaries missing {col}"

    assert "idx_ec_community" in _indexes(s, "entity_communities")


def test_community_tables_pks(tmp_path):
    """entity_communities PK is (entity_id, level); community_summaries PK is (community_id, level)."""
    s = _store(tmp_path)
    with s._connect() as db:
        # Duplicate PK must fail.
        db.execute(
            "INSERT INTO entity_communities(entity_id, community_id, level) "
            "VALUES('e1', 1, 0)"
        )
        db.commit()
        try:
            db.execute(
                "INSERT INTO entity_communities(entity_id, community_id, level) "
                "VALUES('e1', 2, 0)"
            )
            db.commit()
            assert False, "Should have raised IntegrityError for duplicate PK"
        except sqlite3.IntegrityError:
            pass

        db.execute(
            "INSERT INTO community_summaries(community_id, level) VALUES(1, 0)"
        )
        db.commit()
        try:
            db.execute(
                "INSERT INTO community_summaries(community_id, level) VALUES(1, 0)"
            )
            db.commit()
            assert False, "Should have raised IntegrityError for duplicate PK"
        except sqlite3.IntegrityError:
            pass


# ---------------------------------------------------------------------------
# 0.2  thread_context
# ---------------------------------------------------------------------------

def test_thread_context_table_shape(tmp_path):
    s = _store(tmp_path)
    cols = _cols(s, "thread_context")
    for col in ("thread_id", "subject", "org", "email_count", "participant_ids",
                "summary", "last_updated", "contextual_summary"):
        assert col in cols, f"thread_context missing {col}"


def test_thread_context_pk(tmp_path):
    """thread_id is the primary key."""
    s = _store(tmp_path)
    with s._connect() as db:
        db.execute(
            "INSERT INTO thread_context(thread_id, subject) VALUES('t1', 'Hello')"
        )
        db.commit()
        try:
            db.execute(
                "INSERT INTO thread_context(thread_id, subject) VALUES('t1', 'Dup')"
            )
            db.commit()
            assert False, "Should have raised IntegrityError for duplicate PK"
        except sqlite3.IntegrityError:
            pass


# ---------------------------------------------------------------------------
# 0.3  proactive_findings
# ---------------------------------------------------------------------------

def test_proactive_findings_table_shape(tmp_path):
    s = _store(tmp_path)
    cols = _cols(s, "proactive_findings")
    for col in ("id", "finding_type", "ref_id", "org", "summary",
                "detail", "severity", "detected_at", "resolved_at"):
        assert col in cols, f"proactive_findings missing {col}"

    idx = _indexes(s, "proactive_findings")
    assert "idx_pf_type" in idx, "missing idx_pf_type"
    assert "idx_pf_open" in idx, "missing idx_pf_open"

    # idx_pf_open is partial WHERE resolved_at IS NULL.
    ddl = _index_ddl(s, "idx_pf_open")
    assert ddl is not None
    assert "resolved_at IS NULL" in ddl

    # UNIQUE on (finding_type, ref_id).
    with s._connect() as db:
        db.execute(
            "INSERT INTO proactive_findings(finding_type, ref_id) "
            "VALUES('overdue', 'act-1')"
        )
        db.commit()
        try:
            db.execute(
                "INSERT INTO proactive_findings(finding_type, ref_id) "
                "VALUES('overdue', 'act-1')"
            )
            db.commit()
            assert False, "Should have raised IntegrityError for duplicate (finding_type, ref_id)"
        except sqlite3.IntegrityError:
            pass


# ---------------------------------------------------------------------------
# 0.4  actions.waiting_on* columns
# ---------------------------------------------------------------------------

def test_actions_waiting_on_columns_added(tmp_path):
    """A fresh store has all waiting_on* columns; init() is idempotent."""
    s = _store(tmp_path)
    cols = _cols(s, "actions")
    for col in ("waiting_on", "waiting_on_entity_id", "waiting_on_set_at",
                "waiting_on_cleared_at", "waiting_on_cleared_by_doc_id",
                "reply_received"):
        assert col in cols, f"actions missing {col}"

    # reply_received defaults to 0.
    with s._connect() as db:
        db.execute(
            "INSERT INTO actions(text, owner) VALUES('Test action', 'Josh')"
        )
        row = db.execute(
            "SELECT waiting_on, reply_received FROM actions ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row["waiting_on"] is None
    assert row["reply_received"] == 0

    # idx_actions_waiting exists and is partial.
    idx = _indexes(s, "actions")
    assert "idx_actions_waiting" in idx, "missing idx_actions_waiting"
    ddl = _index_ddl(s, "idx_actions_waiting")
    assert ddl is not None
    assert "waiting_on IS NOT NULL" in ddl


def test_actions_waiting_on_backfilled_on_old_store(tmp_path):
    """An existing actions table without waiting_on columns gains them on init()
    without dropping rows."""
    path = tmp_path / "old_actions.db"
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE actions(
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        text             TEXT NOT NULL,
        owner            TEXT DEFAULT '',
        status           TEXT DEFAULT 'open',
        created_at       TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at       TEXT DEFAULT CURRENT_TIMESTAMP)""")
    db.execute("INSERT INTO actions(text, owner) VALUES('Existing action', 'Josh')")
    db.commit()
    db.close()

    s = Store(path, dim=64)
    s.init()

    cols = _cols(s, "actions")
    for col in ("waiting_on", "waiting_on_entity_id", "waiting_on_set_at",
                "waiting_on_cleared_at", "waiting_on_cleared_by_doc_id",
                "reply_received"):
        assert col in cols, f"backfilled actions missing {col}"

    with s._connect() as db:
        row = db.execute(
            "SELECT text, owner FROM actions WHERE owner='Josh'"
        ).fetchone()
    assert row is not None
    assert row["text"] == "Existing action"


# ---------------------------------------------------------------------------
# 0.5A  Community reader/writer methods
# ---------------------------------------------------------------------------

def test_store_replace_communities(tmp_path):
    s = _store(tmp_path)
    partition = {"alice": 1, "bob": 1, "carol": 2}
    summaries = {
        1: {"member_count": 2, "key_entities": "alice,bob", "title": "Core team",
            "summary": "Leadership cluster", "updated": "2026-06-03"},
        2: {"member_count": 1, "key_entities": "carol", "title": "External",
            "summary": "External contacts", "updated": "2026-06-03"},
    }
    s.replace_communities(partition, summaries)

    with s._connect() as db:
        ec_rows = [dict(r) for r in db.execute("SELECT * FROM entity_communities").fetchall()]
        cs_rows = [dict(r) for r in db.execute("SELECT * FROM community_summaries").fetchall()]

    assert len(ec_rows) == 3
    entity_ids = {r["entity_id"] for r in ec_rows}
    assert entity_ids == {"alice", "bob", "carol"}

    assert len(cs_rows) == 2
    c1 = next(r for r in cs_rows if r["community_id"] == 1)
    assert c1["title"] == "Core team"
    assert c1["member_count"] == 2


def test_store_replace_communities_is_atomic_replace(tmp_path):
    """Second replace_communities completely replaces the first, no stale rows."""
    s = _store(tmp_path)
    partition1 = {"alice": 1, "bob": 1}
    summaries1 = {1: {"member_count": 2, "key_entities": "alice,bob",
                      "title": "T1", "summary": "", "updated": ""}}
    s.replace_communities(partition1, summaries1)

    partition2 = {"carol": 3}
    summaries2 = {3: {"member_count": 1, "key_entities": "carol",
                      "title": "T2", "summary": "", "updated": ""}}
    s.replace_communities(partition2, summaries2)

    with s._connect() as db:
        ec_rows = [dict(r) for r in db.execute("SELECT * FROM entity_communities").fetchall()]
        cs_rows = [dict(r) for r in db.execute("SELECT * FROM community_summaries").fetchall()]

    # Only carol + community 3 remain.
    assert len(ec_rows) == 1
    assert ec_rows[0]["entity_id"] == "carol"
    assert len(cs_rows) == 1
    assert cs_rows[0]["community_id"] == 3


def test_store_communities_for_member(tmp_path):
    s = _store(tmp_path)
    partition = {"alice": 1, "bob": 1, "carol": 2}
    summaries = {
        1: {"member_count": 2, "key_entities": "alice,bob", "title": "T1",
            "summary": "", "updated": ""},
        2: {"member_count": 1, "key_entities": "carol", "title": "T2",
            "summary": "", "updated": ""},
    }
    s.replace_communities(partition, summaries)

    rows = s.communities_for(["alice", "carol"])
    entity_ids = {r["entity_id"] for r in rows}
    assert entity_ids == {"alice", "carol"}


def test_store_community_members(tmp_path):
    s = _store(tmp_path)
    # Insert entities so the join works.
    with s._connect() as db:
        db.execute("INSERT INTO entities(id, name, type) VALUES('alice', 'Alice', 'person')")
        db.execute("INSERT INTO entities(id, name, type) VALUES('bob', 'Bob', 'person')")
    partition = {"alice": 1, "bob": 1}
    summaries = {1: {"member_count": 2, "key_entities": "alice,bob",
                     "title": "T1", "summary": "", "updated": ""}}
    s.replace_communities(partition, summaries)

    members = s.community_members(1)
    assert len(members) == 2
    names = {m["name"] for m in members}
    assert names == {"Alice", "Bob"}


def test_store_list_communities(tmp_path):
    s = _store(tmp_path)
    partition = {"alice": 1, "bob": 2}
    summaries = {
        1: {"member_count": 1, "key_entities": "alice", "title": "C1",
            "summary": "First", "updated": "2026-06-01"},
        2: {"member_count": 1, "key_entities": "bob", "title": "C2",
            "summary": "Second", "updated": "2026-06-01"},
    }
    s.replace_communities(partition, summaries)

    communities = s.list_communities()
    assert len(communities) == 2
    titles = {c["title"] for c in communities}
    assert titles == {"C1", "C2"}


# ---------------------------------------------------------------------------
# 0.5B  thread_context reader/writer methods
# ---------------------------------------------------------------------------

def test_store_upsert_thread_context_insert_and_update(tmp_path):
    s = _store(tmp_path)
    s.upsert_thread_context(
        "t-1",
        subject="Budget meeting",
        org="Centrepoint",
        email_count=3,
        summary="Budget Q3 discussed",
        contextual_summary="Follow-up needed",
        participant_ids="alice,bob",
    )
    with s._connect() as db:
        row = dict(db.execute(
            "SELECT * FROM thread_context WHERE thread_id='t-1'"
        ).fetchone())
    assert row["subject"] == "Budget meeting"
    assert row["org"] == "Centrepoint"
    assert row["email_count"] == 3
    assert row["summary"] == "Budget Q3 discussed"
    assert row["participant_ids"] == "alice,bob"

    # Update: upsert again with different summary.
    s.upsert_thread_context(
        "t-1",
        subject="Budget meeting",
        org="Centrepoint",
        email_count=5,
        summary="Budget finalised",
    )
    with s._connect() as db:
        row = dict(db.execute(
            "SELECT * FROM thread_context WHERE thread_id='t-1'"
        ).fetchone())
        n = db.execute("SELECT COUNT(*) FROM thread_context").fetchone()[0]
    assert n == 1
    assert row["summary"] == "Budget finalised"
    assert row["email_count"] == 5

    # Partial-update: pass only summary= — existing subject/org/email_count must survive.
    s.upsert_thread_context("t-1", summary="Final summary only")
    with s._connect() as db:
        row = dict(db.execute(
            "SELECT * FROM thread_context WHERE thread_id='t-1'"
        ).fetchone())
    assert row["subject"] == "Budget meeting", "subject should be preserved on partial upsert"
    assert row["org"] == "Centrepoint", "org should be preserved on partial upsert"
    assert row["email_count"] == 5, "email_count should be preserved on partial upsert"
    assert row["summary"] == "Final summary only"


def test_store_threads_needing_summary(tmp_path):
    s = _store(tmp_path)
    # Large thread with a headline summary but no deep contextual_summary yet.
    s.upsert_thread_context("t-big", email_count=10, summary="Headline")
    # Large thread already deeply synthesised.
    s.upsert_thread_context("t-done", email_count=10,
                            contextual_summary="Already synthesised in depth")
    # Small thread below min_emails threshold.
    s.upsert_thread_context("t-small", email_count=2, summary="Headline")

    needing = s.threads_needing_summary(min_emails=5)
    thread_ids = {r["thread_id"] for r in needing}
    assert "t-big" in thread_ids       # headline set, contextual_summary empty
    assert "t-done" not in thread_ids  # already has a contextual_summary
    assert "t-small" not in thread_ids  # below min_emails


def test_store_thread_messages(tmp_path):
    s = _store(tmp_path)
    # Insert email_context rows belonging to thread t-99.
    with s._connect() as db:
        db.execute(
            "INSERT INTO email_context(message_id, subject, sender, date_iso, "
            "thread_id, content_type, summary) "
            "VALUES('m1', 'Hello', 'Alice', '2026-05-01', 't-99', 'request', 'Intro')"
        )
        db.execute(
            "INSERT INTO email_context(message_id, subject, sender, date_iso, "
            "thread_id, content_type, summary) "
            "VALUES('m2', 'Re: Hello', 'Bob', '2026-05-02', 't-99', 'update', 'Reply')"
        )
        db.execute(
            "INSERT INTO email_context(message_id, subject, sender, date_iso, "
            "thread_id, content_type, summary) "
            "VALUES('m3', 'Other', 'Carol', '2026-05-03', 't-other', 'fyi', 'Diff thread')"
        )
    msgs = s.thread_messages("t-99")
    assert len(msgs) == 2
    # Ordered by date_iso ascending.
    assert msgs[0]["message_id"] == "m1"
    assert msgs[1]["message_id"] == "m2"
    assert all(m["thread_id"] == "t-99" for m in msgs)


# ---------------------------------------------------------------------------
# 0.5C  proactive_findings reader/writer methods
# ---------------------------------------------------------------------------

def test_store_record_finding_upsert(tmp_path):
    s = _store(tmp_path)
    s.record_finding(
        finding_type="overdue",
        ref_id="act-42",
        org="Centrepoint",
        summary="Action overdue",
        detail="Assigned to Josh; past deadline",
        severity="warn",
        detected_at="2026-06-01",
    )
    with s._connect() as db:
        rows = [dict(r) for r in db.execute("SELECT * FROM proactive_findings").fetchall()]
    assert len(rows) == 1
    assert rows[0]["finding_type"] == "overdue"
    assert rows[0]["ref_id"] == "act-42"
    assert rows[0]["severity"] == "warn"
    assert rows[0]["org"] == "Centrepoint"

    # Upsert: same (finding_type, ref_id) updates in place.
    s.record_finding(
        finding_type="overdue",
        ref_id="act-42",
        summary="Still overdue — escalated",
        severity="error",
    )
    with s._connect() as db:
        rows = [dict(r) for r in db.execute("SELECT * FROM proactive_findings").fetchall()]
    assert len(rows) == 1
    assert rows[0]["summary"] == "Still overdue — escalated"
    assert rows[0]["severity"] == "error"


def test_store_open_findings(tmp_path):
    s = _store(tmp_path)
    s.record_finding("overdue", "act-1", summary="Overdue action")
    s.record_finding("overdue", "act-2", summary="Another overdue")
    s.record_finding("no_reply", "m-10", summary="Missing reply")

    all_open = s.open_findings()
    assert len(all_open) == 3

    overdue = s.open_findings(finding_type="overdue")
    assert len(overdue) == 2
    assert all(r["finding_type"] == "overdue" for r in overdue)

    # Resolve one; it should disappear from open_findings.
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with s._connect() as db:
        db.execute(
            "UPDATE proactive_findings SET resolved_at=? WHERE ref_id='act-1'", (now,)
        )
    open_after = s.open_findings(finding_type="overdue")
    assert len(open_after) == 1
    assert open_after[0]["ref_id"] == "act-2"


def test_store_resolve_findings_not_in(tmp_path):
    s = _store(tmp_path)
    s.record_finding("overdue", "act-1", summary="A")
    s.record_finding("overdue", "act-2", summary="B")
    s.record_finding("overdue", "act-3", summary="C")

    now = "2026-06-03T10:00:00Z"
    # act-2 is still live; act-1 and act-3 should be resolved.
    count = s.resolve_findings_not_in("overdue", ["act-2"], now)
    assert count == 2

    open_rows = s.open_findings(finding_type="overdue")
    assert len(open_rows) == 1
    assert open_rows[0]["ref_id"] == "act-2"


# ---------------------------------------------------------------------------
# 0.5D  waiting-on reader/writer methods
# ---------------------------------------------------------------------------

def test_store_open_waiting_actions_within_window(tmp_path):
    s = _store(tmp_path)
    # Insert an action with waiting_on set 5 days ago.
    aid = s.add_unified_action(text="Chase invoice", owner="Josh")
    five_days_ago = "2026-05-29T10:00:00Z"
    with s._connect() as db:
        db.execute(
            "UPDATE actions SET waiting_on='Finance team', waiting_on_set_at=? "
            "WHERE id=?",
            (five_days_ago, aid),
        )

    now = "2026-06-03T10:00:00Z"
    waiting = s.open_waiting_actions(window_days=30, now=now)
    assert len(waiting) == 1
    assert waiting[0]["id"] == aid
    assert waiting[0]["waiting_on"] == "Finance team"


def test_store_open_waiting_actions_excludes_old(tmp_path):
    s = _store(tmp_path)
    aid = s.add_unified_action(text="Old waiting action", owner="Josh")
    old_date = "2026-04-01T10:00:00Z"
    with s._connect() as db:
        db.execute(
            "UPDATE actions SET waiting_on='Someone', waiting_on_set_at=? WHERE id=?",
            (old_date, aid),
        )

    # window_days=30 from 2026-06-03 — cutoff is 2026-05-04; old_date is before that.
    now = "2026-06-03T10:00:00Z"
    waiting = s.open_waiting_actions(window_days=30, now=now)
    assert len(waiting) == 0


def test_store_clear_waiting(tmp_path):
    s = _store(tmp_path)
    aid = s.add_unified_action(text="Waiting action", owner="Josh")
    with s._connect() as db:
        db.execute(
            "UPDATE actions SET waiting_on='Finance', waiting_on_set_at='2026-05-01' "
            "WHERE id=?",
            (aid,),
        )

    now = "2026-06-03T10:00:00Z"
    s.clear_waiting(aid, cleared_by_doc_id="gmail-m-99", now=now)

    with s._connect() as db:
        row = dict(db.execute("SELECT * FROM actions WHERE id=?", (aid,)).fetchone())

    assert row["waiting_on"] is None
    assert row["waiting_on_cleared_at"] == now
    assert row["waiting_on_cleared_by_doc_id"] == "gmail-m-99"
    assert row["reply_received"] == 1
    assert row["updated_at"] == now
