from mcpbrain.store import Store


def test_init_creates_tables_and_roundtrips_chunk(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    s.upsert_chunk(doc_id="gmail-1-body-0", text="annual budget review",
                   content_hash="h1", metadata={"source_type": "gmail"})
    rows = s.unembedded_chunks()
    assert len(rows) == 1
    assert rows[0]["doc_id"] == "gmail-1-body-0"
    assert rows[0]["text"] == "annual budget review"


def test_upsert_is_idempotent_on_content_hash(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4); s.init()
    for _ in range(2):
        s.upsert_chunk(doc_id="d1", text="x", content_hash="same", metadata={})
    assert len(s.unembedded_chunks()) == 1


def test_upsert_chunk_returns_changed_bool(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4); s.init()
    assert s.upsert_chunk("d1", "x", "h1", {}) is True       # new insert
    assert s.upsert_chunk("d1", "x", "h1", {}) is False      # unchanged no-op
    assert s.upsert_chunk("d1", "y", "h2", {}) is True       # content changed


def test_wal_enabled_and_readonly_rejects_writes(tmp_path):
    p = tmp_path / "b.sqlite3"
    s = Store(p, dim=4); s.init()
    with s._connect() as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    ro = Store(p, dim=4, read_only=True)
    import pytest
    import sqlite3
    with pytest.raises(sqlite3.OperationalError):
        ro.upsert_chunk("d2", "y", "h2", {})  # read-only connection cannot write


# --- graph tables (Task 4.2) ---------------------------------------------

def _store(tmp_path):
    s = Store(tmp_path / "g.sqlite3", dim=4)
    s.init()
    return s


def test_upsert_entity_idempotent_on_id_and_bumps_mentions(tmp_path):
    s = _store(tmp_path)
    first = s.upsert_entity("taryn-hamilton", "Taryn Hamilton", "person",
                            org="Centrepoint", seen="2026-05-30")
    second = s.upsert_entity("taryn-hamilton", "Taryn Hamilton", "person",
                             org="Centrepoint", seen="2026-05-31")
    assert first is True   # new entity row created
    assert second is False  # existing entity merged
    ents = s.list_entities()
    assert len(ents) == 1
    e = s.get_entity("taryn-hamilton")
    assert e["name"] == "Taryn Hamilton"
    assert e["org"] == "Centrepoint"
    assert e["mentions"] == 2
    assert e["first_seen"] == "2026-05-30"
    assert e["last_seen"] == "2026-05-31"


def test_upsert_entity_fills_empty_org_and_name(tmp_path):
    s = _store(tmp_path)
    s.upsert_entity("joel-chelliah", "", "unknown", org="", seen="2026-05-30")
    s.upsert_entity("joel-chelliah", "Joel Chelliah", "person",
                    org="Centrepoint", seen="2026-05-30")
    e = s.get_entity("joel-chelliah")
    assert e["name"] == "Joel Chelliah"
    assert e["org"] == "Centrepoint"


def test_upsert_entity_backfills_empty_first_seen(tmp_path):
    s = _store(tmp_path)
    s.upsert_entity("stub-ent", "Stub", "unknown", org="", seen="")
    assert s.get_entity("stub-ent")["first_seen"] == ""
    s.upsert_entity("stub-ent", "Stub", "person", org="", seen="2026-05-30")
    assert s.get_entity("stub-ent")["first_seen"] == "2026-05-30"


def test_upsert_entity_preserves_earliest_first_seen(tmp_path):
    s = _store(tmp_path)
    s.upsert_entity("early-ent", "Early", "person", org="", seen="2026-05-01")
    s.upsert_entity("early-ent", "Early", "person", org="", seen="2026-05-30")
    assert s.get_entity("early-ent")["first_seen"] == "2026-05-01"


def test_get_entity_returns_none_when_absent(tmp_path):
    s = _store(tmp_path)
    assert s.get_entity("nobody") is None


def test_add_relation_dedups(tmp_path):
    s = _store(tmp_path)
    first = s.add_relation("taryn-hamilton", "reports_to", "joel-chelliah", source_doc_id="d1")
    second = s.add_relation("taryn-hamilton", "reports_to", "joel-chelliah", source_doc_id="d2")
    assert first is True   # new triple inserted
    assert second is False  # duplicate triple ignored
    rels = s.list_relations()
    assert len(rels) == 1
    assert rels[0]["entity_a"] == "taryn-hamilton"
    assert rels[0]["relation"] == "reports_to"
    assert rels[0]["entity_b"] == "joel-chelliah"


def test_add_action_roundtrip(tmp_path):
    s = _store(tmp_path)
    rid = s.add_action("Send the campus budget", owner="Josh",
                       deadline="2026-06-10", source_doc_id="d1", thread_id="t1")
    assert isinstance(rid, int)
    acts = s.list_actions()
    assert len(acts) == 1
    a = acts[0]
    assert a["text"] == "Send the campus budget"
    assert a["owner"] == "Josh"
    assert a["deadline"] == "2026-06-10"
    assert a["status"] == "open"
    assert a["source_doc_id"] == "d1"
    assert a["thread_id"] == "t1"


def test_add_decision_roundtrip(tmp_path):
    s = _store(tmp_path)
    rid = s.add_decision("Approved AV spend for Byford",
                         decided_on="2026-05-30", source_doc_id="d1")
    assert isinstance(rid, int)
    decs = s.list_decisions()
    assert len(decs) == 1
    assert decs[0]["text"] == "Approved AV spend for Byford"
    assert decs[0]["decided_on"] == "2026-05-30"
    assert decs[0]["source_doc_id"] == "d1"


# --- meta accessors (Task 4.3) -------------------------------------------

def test_set_and_get_meta(tmp_path):
    s = _store(tmp_path)
    s.set_meta("enrich_mode", "deferred")
    assert s.get_meta("enrich_mode") == "deferred"


def test_get_meta_returns_none_when_absent(tmp_path):
    s = _store(tmp_path)
    assert s.get_meta("nonexistent_key") is None


def test_set_meta_overwrites_existing_value(tmp_path):
    s = _store(tmp_path)
    s.set_meta("enrich_mode", "deferred")
    s.set_meta("enrich_mode", "live")
    assert s.get_meta("enrich_mode") == "live"


def test_set_meta_does_not_touch_dim(tmp_path):
    # init() seeds dim; set_meta must not clobber it
    s = _store(tmp_path)
    s.set_meta("enrich_mode", "deferred")
    assert s.get_meta("dim") == "4"


# --- thread_chunks reader (Task 4.4) -------------------------------------

def test_thread_chunks_returns_matching_thread_only(tmp_path):
    s = _store(tmp_path)
    # Two chunks in thread t1, one chunk in thread t2.
    s.upsert_chunk("gmail-t1-a", "Can you send the campus budget?", "h1",
                   {"thread_id": "t1", "message_id": "msg-a", "source_type": "gmail"})
    s.upsert_chunk("gmail-t1-b", "Done, sent it through.", "h2",
                   {"thread_id": "t1", "message_id": "msg-b", "source_type": "gmail"})
    s.upsert_chunk("gmail-t2-a", "Unrelated thread message.", "h3",
                   {"thread_id": "t2", "message_id": "msg-c", "source_type": "gmail"})

    results = s.thread_chunks("t1")
    doc_ids = {r["doc_id"] for r in results}

    assert len(results) == 2
    assert doc_ids == {"gmail-t1-a", "gmail-t1-b"}
    # metadata must be parsed (not raw JSON string)
    for r in results:
        assert isinstance(r["metadata"], dict)
        assert r["metadata"]["thread_id"] == "t1"


def test_thread_chunks_returns_empty_for_unknown_thread(tmp_path):
    s = _store(tmp_path)
    s.upsert_chunk("gmail-t1-a", "Some message.", "h1",
                   {"thread_id": "t1", "source_type": "gmail"})
    assert s.thread_chunks("no-such-thread") == []


# --- graph readers for brain_context / brain_graph (Task 4.5) ------------

def _seed_graph(s):
    s.upsert_entity("taryn-hamilton", "Taryn Hamilton", "person", org="Centrepoint")
    s.upsert_entity("joel-chelliah", "Joel Chelliah", "person", org="Centrepoint")
    s.upsert_entity("college-2026", "College 2026", "project")
    s.add_relation("taryn-hamilton", "reports_to", "joel-chelliah", "doc-1")
    s.add_relation("taryn-hamilton", "works_on", "college-2026", "doc-2")
    s.add_action("Confirm college timetable", owner="Taryn Hamilton")


def test_find_entity_by_id(tmp_path):
    s = _store(tmp_path)
    _seed_graph(s)
    ent = s.find_entity("taryn-hamilton")
    assert ent is not None
    assert ent["id"] == "taryn-hamilton"


def test_find_entity_by_name_case_insensitive(tmp_path):
    s = _store(tmp_path)
    _seed_graph(s)
    ent = s.find_entity("taryn hamilton")
    assert ent is not None
    assert ent["id"] == "taryn-hamilton"


def test_find_entity_by_slug_of_display_name(tmp_path):
    s = _store(tmp_path)
    _seed_graph(s)
    # "Taryn Hamilton" is neither a literal id nor matched by the name branch
    # exactly, but slugify("Taryn Hamilton") == "taryn-hamilton".
    ent = s.find_entity("Taryn Hamilton")
    assert ent is not None
    assert ent["id"] == "taryn-hamilton"


def test_find_entity_miss_returns_none(tmp_path):
    s = _store(tmp_path)
    _seed_graph(s)
    assert s.find_entity("nobody") is None


def test_relations_for_returns_in_and_out_edges(tmp_path):
    s = _store(tmp_path)
    _seed_graph(s)
    # Taryn has two out-edges.
    taryn = s.relations_for("taryn-hamilton")
    assert len(taryn) == 2
    # Joel has one in-edge (taryn reports_to joel).
    joel = s.relations_for("joel-chelliah")
    assert len(joel) == 1
    assert joel[0]["entity_a"] == "taryn-hamilton"
    assert joel[0]["entity_b"] == "joel-chelliah"


def test_actions_for_owner_case_insensitive(tmp_path):
    s = _store(tmp_path)
    _seed_graph(s)
    acts = s.actions_for_owner("taryn hamilton")
    assert len(acts) == 1
    assert acts[0]["text"] == "Confirm college timetable"
    assert s.actions_for_owner("Someone Else") == []


# --- enriched-chunk tracker (Task H1) ------------------------------------

def test_enriched_defaults_zero_on_upsert(tmp_path):
    s = _store(tmp_path)
    s.upsert_chunk("d1", "body", "h1", {"source_type": "gmail"})
    with s._connect() as db:
        row = db.execute("SELECT enriched FROM chunks WHERE doc_id=?", ("d1",)).fetchone()
    assert row["enriched"] == 0


def test_unenriched_chunks_returns_only_enriched_zero(tmp_path):
    s = _store(tmp_path)
    s.upsert_chunk("d1", "body one", "h1", {"k": 1})
    s.upsert_chunk("d2", "body two", "h2", {"k": 2})
    s.mark_enriched(["d1"])

    rows = s.unenriched_chunks()
    assert [r["doc_id"] for r in rows] == ["d2"]
    assert rows[0]["text"] == "body two"
    assert rows[0]["metadata"] == {"k": 2}  # metadata json-loaded


def test_unenriched_chunks_limit_caps_rows(tmp_path):
    """limit caps the rows returned; no-arg still returns the full backlog."""
    s = _store(tmp_path)
    for i in range(5):
        s.upsert_chunk(f"d{i}", f"body {i}", f"h{i}", {"k": i})

    assert len(s.unenriched_chunks(limit=2)) == 2
    assert len(s.unenriched_chunks()) == 5


def test_unenriched_chunks_independent_of_embedding(tmp_path):
    """Gated on enriched=0 only — an embedded-but-unenriched chunk still shows."""
    s = _store(tmp_path)
    s.upsert_chunk("d1", "body", "h1", {})
    rows = s.unembedded_chunks()
    s.write_embedding(rows[0]["rowid"], [1.0, 0, 0, 0])  # now embedded=1
    # still unenriched
    assert [r["doc_id"] for r in s.unenriched_chunks()] == ["d1"]


def test_mark_enriched_flips_rows(tmp_path):
    s = _store(tmp_path)
    s.upsert_chunk("d1", "x", "h1", {})
    s.upsert_chunk("d2", "y", "h2", {})
    assert len(s.unenriched_chunks()) == 2
    s.mark_enriched(["d1", "d2"])
    assert s.unenriched_chunks() == []


def test_mark_enriched_empty_list_is_noop(tmp_path):
    s = _store(tmp_path)
    s.upsert_chunk("d1", "x", "h1", {})
    s.mark_enriched([])  # must not raise, must not flip anything
    assert len(s.unenriched_chunks()) == 1


def test_init_migrates_legacy_chunks_table_missing_enriched(tmp_path):
    """init() must ADD enriched to a pre-existing chunks table that lacks it,
    without losing data, and unenriched_chunks must work afterwards."""
    import json
    p = tmp_path / "legacy.sqlite3"
    s = Store(p, dim=4)
    s.init()

    # Simulate a legacy store: drop the modern chunks table and recreate it
    # WITHOUT the enriched column, then insert a row.
    with s._connect() as db:
        db.execute("DROP TABLE chunks")
        db.execute("""CREATE TABLE chunks(
            rowid INTEGER PRIMARY KEY,
            doc_id TEXT UNIQUE, text TEXT, content_hash TEXT,
            metadata TEXT, embedded INTEGER DEFAULT 0)""")
        db.execute(
            "INSERT INTO chunks(doc_id,text,content_hash,metadata) VALUES(?,?,?,?)",
            ("legacy-1", "old body", "h1", json.dumps({"source_type": "gmail"})),
        )
        cols = {r["name"] for r in db.execute("PRAGMA table_info(chunks)").fetchall()}
        assert "enriched" not in cols  # confirm the legacy shape

    # Re-running init() must add the column without dropping the existing row.
    s.init()

    with s._connect() as db:
        cols = {r["name"] for r in db.execute("PRAGMA table_info(chunks)").fetchall()}
    assert "enriched" in cols

    rows = s.unenriched_chunks()
    assert len(rows) == 1
    assert rows[0]["doc_id"] == "legacy-1"
    assert rows[0]["text"] == "old body"  # no data loss
    assert rows[0]["metadata"] == {"source_type": "gmail"}

    # column is usable
    s.mark_enriched(["legacy-1"])
    assert s.unenriched_chunks() == []


def test_init_migration_is_idempotent(tmp_path):
    """Calling init() repeatedly on a store that already has enriched is safe."""
    s = _store(tmp_path)
    s.init()
    s.init()  # must not raise (no duplicate-column error)
    s.upsert_chunk("d1", "x", "h1", {})
    assert len(s.unenriched_chunks()) == 1


# --- upsert_chunk enriched-reset regression (Task H1 bug fix) ------------

def test_upsert_chunk_content_change_resets_enriched_and_embedded(tmp_path):
    """Changed content must reset both embedded=0 and enriched=0.

    Regression: the original UPDATE only reset embedded=0, leaving a changed
    chunk with enriched=1 — so the new content was re-embedded but never
    re-enriched, and stale graph rows from the old content were never corrected.
    """
    s = _store(tmp_path)

    # 1. Initial upsert; write embedding and mark enriched so both flags are 1.
    s.upsert_chunk("d1", "original text", "hash-v1", {"source_type": "gmail"})
    unembedded = s.unembedded_chunks()
    s.write_embedding(unembedded[0]["rowid"], [1.0, 0.0, 0.0, 0.0])
    s.mark_enriched(["d1"])

    # Confirm the chunk is no longer in either pending queue.
    assert s.unenriched_chunks() == []
    assert s.unembedded_chunks() == []

    # 2. Upsert the SAME doc_id with DIFFERENT text/content_hash.
    s.upsert_chunk("d1", "updated text", "hash-v2", {"source_type": "gmail"})

    # Must reappear in unenriched_chunks (enriched reset to 0).
    unenriched = s.unenriched_chunks()
    assert len(unenriched) == 1
    assert unenriched[0]["doc_id"] == "d1"
    assert unenriched[0]["text"] == "updated text"

    # Must also reappear in unembedded_chunks (embedded reset to 0).
    unembedded = s.unembedded_chunks()
    assert len(unembedded) == 1
    assert unembedded[0]["doc_id"] == "d1"


def test_upsert_chunk_same_content_hash_does_not_reset_enriched(tmp_path):
    """Idempotent path (same content_hash) must NOT touch enriched.

    Upserting unchanged content after marking enriched must leave the chunk
    out of unenriched_chunks.
    """
    s = _store(tmp_path)

    s.upsert_chunk("d1", "stable text", "hash-v1", {"source_type": "gmail"})
    s.mark_enriched(["d1"])
    assert s.unenriched_chunks() == []

    # Re-upsert with the SAME content_hash — must be a no-op.
    s.upsert_chunk("d1", "stable text", "hash-v1", {"source_type": "gmail"})

    # Still not in unenriched_chunks.
    assert s.unenriched_chunks() == []


# --- entity merge + audit (R4) -------------------------------------------

def test_merge_repoints_dedups_drops_self_loops_and_removes_loser(tmp_path):
    """merge_entities repoints relations onto the winner, dedups against the
    UNIQUE triple, drops self-loops, and removes the loser from relations."""
    s = _store(tmp_path)
    s.upsert_entity("joel", "joel", "person", org="", seen="2026-05-30")
    s.upsert_entity("ps-joel", "Ps Joel", "person", org="Centrepoint", seen="2026-05-30")
    s.upsert_entity("acc", "ACC", "org", org="", seen="2026-05-30")

    s.add_relation("ps-joel", "works_at", "acc")        # repoints cleanly
    s.add_relation("joel", "works_at", "acc")            # collides on UNIQUE after repoint
    s.add_relation("ps-joel", "mentioned_with", "joel")  # becomes a self-loop after repoint

    s.merge_entities("ps-joel", "joel", canonical_name="Joel Chelliah", method="llm")

    ids = {e["id"] for e in s.list_entities()}
    assert "ps-joel" not in ids
    assert "joel" in ids

    rels = s.list_relations()
    # No relation references the loser.
    assert all(r["entity_a"] != "ps-joel" and r["entity_b"] != "ps-joel" for r in rels)
    # Exactly one (joel, works_at, acc) — the collision was deduped.
    works = [r for r in rels if r["relation"] == "works_at"]
    assert len(works) == 1
    assert works[0]["entity_a"] == "joel" and works[0]["entity_b"] == "acc"
    # No self-loop.
    assert all(r["entity_a"] != r["entity_b"] for r in rels)


def test_merge_scalar_precedence_and_mention_sum(tmp_path):
    s = _store(tmp_path)
    # Winner is a stub: empty org, unknown type. Loser carries the real values.
    s.upsert_entity("joel", "joel", "unknown", org="", seen="2026-05-30")  # mentions=1
    s.upsert_entity("ps-joel", "Ps Joel", "person", org="Centrepoint", seen="2026-05-30")
    s.upsert_entity("ps-joel", "Ps Joel", "person", org="Centrepoint", seen="2026-05-31")  # mentions=2

    s.merge_entities("ps-joel", "joel", canonical_name="Joel Chelliah", method="llm")

    win = s.get_entity("joel")
    assert win["name"] == "Joel Chelliah"      # canonical_name override
    assert win["org"] == "Centrepoint"          # winner org was "" -> loser's real org
    assert win["type"] == "person"              # winner was "unknown" -> upgraded
    assert win["mentions"] == 3                  # 1 + 2 summed


def test_merge_writes_audit_row(tmp_path):
    s = _store(tmp_path)
    s.upsert_entity("joel", "joel", "person", org="Centrepoint", seen="2026-05-30")
    s.upsert_entity("ps-joel", "Ps Joel", "person", org="Centrepoint", seen="2026-05-30")

    s.merge_entities("ps-joel", "joel", method="llm")

    merges = s.list_entity_merges()
    assert len(merges) == 1
    row = merges[-1]
    assert row["loser_id"] == "ps-joel"
    assert row["winner_id"] == "joel"
    assert row["method"] == "llm"
    assert row["loser_name"] == "Ps Joel"
    assert row.get("at")  # CURRENT_TIMESTAMP populated


def test_merge_is_noop_when_loser_equals_winner(tmp_path):
    s = _store(tmp_path)
    s.upsert_entity("joel", "Joel", "person", org="Centrepoint", seen="2026-05-30")
    s.merge_entities("joel", "joel")
    assert {e["id"] for e in s.list_entities()} == {"joel"}
    assert s.list_entity_merges() == []


def test_merge_is_noop_when_an_id_is_missing(tmp_path):
    s = _store(tmp_path)
    s.upsert_entity("joel", "Joel", "person", org="Centrepoint", seen="2026-05-30")
    # Neither call should crash or mutate anything.
    s.merge_entities("ghost", "joel")
    s.merge_entities("joel", "ghost")
    assert {e["id"] for e in s.list_entities()} == {"joel"}
    assert s.list_entity_merges() == []


def test_entities_for_resolution_returns_five_fields(tmp_path):
    s = _store(tmp_path)
    s.upsert_entity("joel", "Joel", "person", org="Centrepoint", seen="2026-05-30")
    s.upsert_entity("acc", "ACC", "org", org="", seen="2026-05-30")
    rows = s.entities_for_resolution()
    assert len(rows) == 2
    for r in rows:
        assert set(r.keys()) == {"id", "name", "type", "org", "mentions"}
    by_id = {r["id"]: r for r in rows}
    assert by_id["joel"]["name"] == "Joel"
    assert by_id["joel"]["org"] == "Centrepoint"


def test_stale_reextract_roundtrip(tmp_path):
    from mcpbrain.store import Store
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    assert s.get_stale_reextract("thread-A") is None
    s.set_stale_reextract("thread-A", "sig123", "2026-06-09T00:00:00Z")
    row = s.get_stale_reextract("thread-A")
    assert row["thread_id"] == "thread-A"
    assert row["signature"] == "sig123"
    assert row["triggered_at"] == "2026-06-09T00:00:00Z"
    # upsert replaces in place
    s.set_stale_reextract("thread-A", "sig456", "2026-06-09T01:00:00Z")
    assert s.get_stale_reextract("thread-A")["signature"] == "sig456"


def _add_thread_chunk(s, doc_id, thread_id, text, chash, enriched):
    # Insert a chunk with a thread_id in metadata, then set enriched directly.
    s.upsert_chunk(doc_id, text, chash, {"thread_id": thread_id})
    with s._connect() as db:
        db.execute("UPDATE chunks SET enriched=? WHERE doc_id=?", (enriched, doc_id))


def test_thread_helpers(tmp_path):
    from mcpbrain.store import Store
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    _add_thread_chunk(s, "d1", "T1", "hello", "h1", enriched=1)
    _add_thread_chunk(s, "d2", "T1", "world", "h2", enriched=1)
    _add_thread_chunk(s, "d3", "T2", "other", "h3", enriched=0)

    # T1 fully enriched -> no unenriched; T2 has an unenriched chunk
    assert s.thread_has_unenriched("T1") is False
    assert s.thread_has_unenriched("T2") is True

    # signature is stable and order-independent of insertion
    sig_before = s.thread_signature("T1")
    assert isinstance(sig_before, str) and len(sig_before) == 64

    # mark_thread_unenriched flips only T1's chunks, returns the count
    assert s.mark_thread_unenriched("T1") == 2
    assert s.thread_has_unenriched("T1") is True
    assert s.thread_has_unenriched("T2") is True  # untouched

    # resetting enriched does NOT change content -> signature unchanged
    assert s.thread_signature("T1") == sig_before

    # changing content DOES change the signature
    _add_thread_chunk(s, "d1", "T1", "hello edited", "h1b", enriched=1)
    assert s.thread_signature("T1") != sig_before


def test_clickup_closed_setter(tmp_path):
    from mcpbrain.store import Store
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    aid = s.add_unified_action(text="Do thing", owner="Joshua")
    # default is NULL (never observed)
    assert s.get_unified_action(aid)["clickup_closed"] is None
    s.set_action_clickup_closed(aid, True)
    assert s.get_unified_action(aid)["clickup_closed"] == 1
    s.set_action_clickup_closed(aid, False)
    assert s.get_unified_action(aid)["clickup_closed"] == 0
