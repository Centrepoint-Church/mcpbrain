# tests/test_graph_write_provenance.py
import json
from pathlib import Path

from mcpbrain.store import Store
from mcpbrain import graph_write


def _store(tmp_path):
    s = Store(str(tmp_path / "b.sqlite3"), dim=384); s.init(); return s


def _seed_entity(store, entity_id, *, name="Sam", entity_type="person"):
    """Insert a bare entity row directly, bypassing upsert_entity's junk/dedup
    checks — used by write_observation unit tests that don't need apply()'s
    full entity-creation pipeline."""
    with store._connect() as db:
        db.execute(
            "INSERT INTO entities (id, name, type) VALUES (?, ?, ?)",
            (entity_id, name, entity_type),
        )
    return entity_id


def test_relation_gets_real_doc_id(tmp_path):
    s = _store(tmp_path)
    extraction = {
        "thread_id": "t1", "org": "unknown", "content_type": "email",
        "summary": "s", "messages": [{"message_id": "m1", "sender": "a@x.org", "date": "2026-02-01"}],
        "entities": [{"name": "Sam", "type": "person"}, {"name": "Pat", "type": "person"}],
        "relations": [{"source_name": "Sam", "type": "reports_to", "target_name": "Pat"}],
        "actions": [], "topics": [],
    }
    graph_write.apply(s, extraction, doc_ids=["doc-42"])
    with s._connect() as db:
        rows = db.execute(
            "SELECT source_doc_id FROM entity_relations WHERE relation='reports_to'").fetchall()
    assert rows, "relation was not written"
    assert rows[0][0] == "doc-42", f"expected provenance doc-42, got {rows[0][0]!r}"


def test_relation_valid_from_is_event_date(tmp_path):
    s = _store(tmp_path)
    extraction = {
        "thread_id": "t2", "org": "unknown", "content_type": "email", "summary": "s",
        "messages": [{"message_id": "m1", "sender": "a@x.org", "date": "2025-09-15T10:00:00Z"}],
        "entities": [{"name": "Sam", "type": "person"}, {"name": "Pat", "type": "person"}],
        "relations": [{"source_name": "Sam", "type": "manages", "target_name": "Pat"}],
        "actions": [], "topics": [],
    }
    graph_write.apply(s, extraction, doc_ids=["doc-9"])
    with s._connect() as db:
        vf = db.execute("SELECT valid_from FROM entity_relations WHERE relation='manages'").fetchone()[0]
    assert vf.startswith("2025-09-15"), f"valid_from should be the event date, got {vf!r}"


def test_header_person_gets_email(tmp_path):
    """Brief's literal single-message test: the lead sender's own entity mention
    gets email_addr via the existing sender-upsert path. This already passes on
    main with zero code changes (verified before this change) — kept as a
    regression guard, not proof of the multi-message fix below.
    """
    s = _store(tmp_path)
    extraction = {
        "thread_id": "t3", "org": "unknown", "content_type": "email", "summary": "s",
        "messages": [{"message_id": "m1", "sender": "Sam Lee <sam.lee@northgatetrust.org>", "date": "2026-02-01"}],
        "entities": [{"name": "Sam Lee", "type": "person"}],
        "relations": [], "actions": [], "topics": ["x"],
    }
    graph_write.apply(s, extraction, doc_ids=["doc-7"])
    with s._connect() as db:
        email = db.execute("SELECT email_addr FROM entities WHERE name='Sam Lee'").fetchone()[0]
    assert email == "sam.lee@northgatetrust.org"


def test_non_lead_message_sender_gets_email(tmp_path):
    """Real gap: in a multi-message thread, only the lead message's sender ever
    got matched to an email address. A later message's sender (Pat Nguyen, who
    replied second) also appears in entities[] but historically got no email_addr
    even though their header is right there in messages[].
    """
    s = _store(tmp_path)
    extraction = {
        "thread_id": "t4", "org": "unknown", "content_type": "email", "summary": "s",
        "messages": [
            {"message_id": "m1", "sender": "Sam Lee <sam.lee@northgatetrust.org>", "date": "2026-02-01"},
            {"message_id": "m2", "sender": "Pat Nguyen <pat.nguyen@northgatetrust.org>", "date": "2026-02-02"},
        ],
        "entities": [
            {"name": "Sam Lee", "type": "person"},
            {"name": "Pat Nguyen", "type": "person"},
        ],
        "relations": [], "actions": [], "topics": ["x"],
    }
    graph_write.apply(s, extraction, doc_ids=["doc-8"])
    with s._connect() as db:
        sam_email = db.execute("SELECT email_addr FROM entities WHERE name='Sam Lee'").fetchone()[0]
        pat_email = db.execute("SELECT email_addr FROM entities WHERE name='Pat Nguyen'").fetchone()[0]
    assert sam_email == "sam.lee@northgatetrust.org"
    assert pat_email == "pat.nguyen@northgatetrust.org", (
        "non-lead message sender who also appears in entities[] must get email_addr"
    )


def test_dedup_redirect_backfills_email_from_header(tmp_path):
    """write_time_dedup redirect branch (the branch Task 5.3 will make default-on)
    must also backfill email_addr onto the redirect target, or this fix becomes
    dead code once that flag flips. Pre-seed an existing "Pat Nguyen" entity with
    no email, then apply a thread whose second message's sender header carries
    Pat's email; the near-dup redirect must fill it in via
    update_entity_email_if_empty.
    """
    from mcpbrain.graph_write import apply, upsert_entity
    import mcpbrain.orgs as orgs_mod

    db_path = tmp_path / "brain.sqlite3"
    s = Store(db_path, dim=4); s.init()
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "write_time_dedup": True,
        "owner_name": "Josh",
        "owner_email": "josh@example.com",
        "orgs": [{"name": "Northgate Trust"}],
    }))

    taxonomy = orgs_mod.taxonomy_from_config()
    existing_id = upsert_entity(s, name="Pat Nguyen", entity_type="person", taxonomy=taxonomy)
    assert existing_id
    with s._connect() as db:
        pre_email = db.execute("SELECT email_addr FROM entities WHERE id=?", (existing_id,)).fetchone()[0]
    assert pre_email == ""

    extraction = {
        "thread_id": "t5", "org": "Northgate Trust", "content_type": "email", "summary": "s",
        "messages": [
            {"message_id": "m1", "sender": "Sam Lee <sam.lee@northgatetrust.org>", "date": "2026-02-01"},
            {"message_id": "m2", "sender": "Pat Nguyen <pat.nguyen@northgatetrust.org>", "date": "2026-02-02"},
        ],
        "entities": [
            {"name": "Sam Lee", "type": "person"},
            {"name": "Pat Nguyen", "type": "person"},
        ],
        "relations": [], "actions": [], "topics": [],
    }
    apply(s, extraction, doc_ids=["doc-9"], home=str(tmp_path))

    with s._connect() as db:
        count = db.execute(
            "SELECT COUNT(*) FROM entities WHERE type='person' AND id LIKE '%pat%nguyen%'"
        ).fetchone()[0]
        email = db.execute("SELECT email_addr FROM entities WHERE id=?", (existing_id,)).fetchone()[0]
    assert count == 1, f"expected exactly 1 Pat Nguyen entity (redirected, not duplicated), got {count}"
    assert email == "pat.nguyen@northgatetrust.org", (
        "redirected entity must have email backfilled from the header via update_entity_email_if_empty"
    )


def test_apply_uses_org_hint_when_model_org_unknown(tmp_path, monkeypatch):
    """When the model returns org='unknown' (its own sentinel for "couldn't tell"),
    apply() falls back to the deterministic org_hint (sender-domain-derived,
    attached by prepare._thread_block) rather than writing 'unknown' to
    email_context. enrich_org_default_enabled defaults True, so no config key
    for it is needed; 'orgs' is configured so canonical_org resolves org_hint
    to the taxonomy's own display-case name instead of passing it through raw.

    MCPBRAIN_HOME is set to tmp_path because apply()'s taxonomy lookup
    (orgs.taxonomy_from_config(), used for canonical_org) reads config.app_dir()
    directly rather than the home= kwarg — matching the pre-existing convention
    the sibling dedup-redirect test in this file already relies on.
    """
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    s = _store(tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({
        "orgs": [{"name": "Northgate Trust", "domains": ["northgatetrust.org"]}],
    }))
    extraction = {
        "thread_id": "t6", "org": "unknown", "org_hint": "Northgate Trust",
        "content_type": "email", "summary": "s",
        "messages": [{"message_id": "m1", "sender": "Sam Lee <sam.lee@northgatetrust.org>",
                      "date": "2026-02-01"}],
        "entities": [], "relations": [], "actions": [], "topics": [],
    }
    graph_write.apply(s, extraction, doc_ids=["doc-10"], home=str(tmp_path))
    with s._connect() as db:
        org = db.execute("SELECT org FROM email_context WHERE message_id='m1'").fetchone()[0]
    assert org == "Northgate Trust", f"expected org_hint fallback 'Northgate Trust', got {org!r}"


def test_structural_works_at_has_provenance(tmp_path):
    """Task 3.4 acceptance test: the brief's literal single-message example
    (sender = the lead) already writes a bare works_at row today via the
    legacy _ensure_works_at path (INSERT OR IGNORE, no source_doc_id/valid_from).
    That's not sufficient — the plan's Interfaces section requires the
    deterministic edge to carry real provenance. Assert both fields are
    non-empty and valid_from matches the message's event date.
    """
    s = _store(tmp_path)
    extraction = {
        "thread_id": "t8", "org": "unknown", "content_type": "email", "summary": "s",
        "messages": [{"message_id": "m1", "sender": "Sam <sam@northgatetrust.org>", "date": "2026-02-01"}],
        "entities": [{"name": "Sam", "type": "person"}],
        "relations": [],          # model returned NO relations
        "actions": [], "topics": ["x"],
    }
    (tmp_path / "config.json").write_text(json.dumps({
        "orgs": [{"name": "Northgate Trust", "domains": ["northgatetrust.org"]}],
    }))
    graph_write.apply(s, extraction, doc_ids=["doc-1"], home=str(tmp_path))
    with s._connect() as db:
        rows = db.execute(
            "SELECT source_doc_id, valid_from FROM entity_relations WHERE relation='works_at'"
        ).fetchall()
    assert rows, "domain-derived works_at should be written deterministically"
    assert any(r[0] for r in rows), "works_at row must carry a non-empty source_doc_id"
    assert any(r[1] for r in rows), "works_at row must carry a non-empty valid_from"
    matching = [r for r in rows if r[0] and r[1]]
    assert matching, "at least one works_at row must have BOTH source_doc_id and valid_from"
    assert matching[0][0] == "doc-1"
    assert matching[0][1].startswith("2026-02-01")


def test_structural_mentioned_with_both_directions(tmp_path):
    """Multi-sender thread: deterministic mentioned_with is written in BOTH
    directions between distinct message senders resolved via name_to_id."""
    s = _store(tmp_path)
    extraction = {
        "thread_id": "t9", "org": "unknown", "content_type": "email", "summary": "s",
        "messages": [
            {"message_id": "m1", "sender": "Sam Lee <sam.lee@northgatetrust.org>", "date": "2026-02-01"},
            {"message_id": "m2", "sender": "Pat Nguyen <pat.nguyen@northgatetrust.org>", "date": "2026-02-02"},
        ],
        "entities": [
            {"name": "Sam Lee", "type": "person"},
            {"name": "Pat Nguyen", "type": "person"},
        ],
        "relations": [], "actions": [], "topics": ["x"],
    }
    (tmp_path / "config.json").write_text(json.dumps({
        "orgs": [{"name": "Northgate Trust", "domains": ["northgatetrust.org"]}],
    }))
    graph_write.apply(s, extraction, doc_ids=["doc-2"], home=str(tmp_path))
    with s._connect() as db:
        sam_id = db.execute("SELECT id FROM entities WHERE name='Sam Lee'").fetchone()[0]
        pat_id = db.execute("SELECT id FROM entities WHERE name='Pat Nguyen'").fetchone()[0]
        rows = db.execute(
            "SELECT entity_a, entity_b, source_doc_id, valid_from FROM entity_relations "
            "WHERE relation='mentioned_with'"
        ).fetchall()
    pairs = {(r[0], r[1]) for r in rows}
    assert (sam_id, pat_id) in pairs, "mentioned_with must be written Sam -> Pat"
    assert (pat_id, sam_id) in pairs, "mentioned_with must be written Pat -> Sam"
    for r in rows:
        assert r[2], "mentioned_with row must carry source_doc_id"
        assert r[3], "mentioned_with row must carry valid_from"


def test_deterministic_pass_coexists_with_model_relations(tmp_path):
    """The model's own relations (e.g. reports_to) must still be written
    alongside the deterministic works_at pass, without contradiction or
    double-write breakage."""
    s = _store(tmp_path)
    extraction = {
        "thread_id": "t10", "org": "unknown", "content_type": "email", "summary": "s",
        "messages": [
            {"message_id": "m1", "sender": "Sam Lee <sam.lee@northgatetrust.org>", "date": "2026-02-01"},
            {"message_id": "m2", "sender": "Pat Nguyen <pat.nguyen@northgatetrust.org>", "date": "2026-02-02"},
        ],
        "entities": [
            {"name": "Sam Lee", "type": "person"},
            {"name": "Pat Nguyen", "type": "person"},
        ],
        "relations": [
            {"source_name": "Sam Lee", "type": "reports_to", "target_name": "Pat Nguyen"},
        ],
        "actions": [], "topics": ["x"],
    }
    (tmp_path / "config.json").write_text(json.dumps({
        "orgs": [{"name": "Northgate Trust", "domains": ["northgatetrust.org"]}],
    }))
    graph_write.apply(s, extraction, doc_ids=["doc-3"], home=str(tmp_path))
    with s._connect() as db:
        works_at_count = db.execute(
            "SELECT COUNT(*) FROM entity_relations WHERE relation='works_at' "
            "AND source_doc_id != '' AND valid_from IS NOT NULL AND valid_from != ''"
        ).fetchone()[0]
        reports_to_count = db.execute(
            "SELECT COUNT(*) FROM entity_relations WHERE relation='reports_to'"
        ).fetchone()[0]
    assert works_at_count >= 1, "deterministic works_at must still be written"
    assert reports_to_count >= 1, "model's reports_to must still be written"


def test_upsert_relation_backfills_missing_provenance_only(tmp_path):
    """Regression test for the shared-code fix: the re-observation branch of
    upsert_relation (same entity_a/relation/entity_b) must backfill
    source_doc_id/valid_from onto the EXISTING row only when currently empty —
    and must never overwrite a real value already present."""
    s = _store(tmp_path)
    # entity_relations.entity_a/entity_b are enforced foreign keys, so the
    # endpoints these relations are written between have to exist.
    for eid in ("person:a", "org:x", "person:b", "org:y"):
        _seed_entity(s, eid, entity_type="org" if eid.startswith("org") else "person")

    # Case 1: first write has no source_doc_id; second write supplies one ->
    # backfilled onto the existing row.
    rid1 = graph_write.upsert_relation(
        s, "person:a", "works_at", "org:x", valid_from="2026-01-01", evidence="",
        source_doc_id="")
    rid2 = graph_write.upsert_relation(
        s, "person:a", "works_at", "org:x", valid_from="2026-01-01", evidence="m1",
        source_doc_id="doc-real")
    assert rid1 == rid2, "re-observation of the same triple must return the same row id"
    with s._connect() as db:
        row = db.execute(
            "SELECT source_doc_id, valid_from FROM entity_relations WHERE id=?", (rid1,)
        ).fetchone()
    assert row[0] == "doc-real", "backfill must fill an empty source_doc_id on the existing row"
    assert row[1] == "2026-01-01"

    # Case 2: first write has a real source_doc_id; a later re-observation with
    # a DIFFERENT source_doc_id must NOT overwrite the original.
    rid3 = graph_write.upsert_relation(
        s, "person:b", "works_at", "org:y", valid_from="2026-01-02", evidence="m2",
        source_doc_id="doc-original")
    rid4 = graph_write.upsert_relation(
        s, "person:b", "works_at", "org:y", valid_from="2026-01-02", evidence="m3",
        source_doc_id="doc-other")
    assert rid3 == rid4
    with s._connect() as db:
        row2 = db.execute(
            "SELECT source_doc_id FROM entity_relations WHERE id=?", (rid3,)
        ).fetchone()
    assert row2[0] == "doc-original", "an existing real source_doc_id must never be overwritten"


def test_structural_relations_kill_switch_suppresses_deterministic_writes(tmp_path):
    """enrich_structural_relations_enabled=False must suppress BOTH the
    deterministic works_at and mentioned_with passes."""
    s = _store(tmp_path)
    extraction = {
        "thread_id": "t11", "org": "unknown", "content_type": "email", "summary": "s",
        "messages": [
            {"message_id": "m1", "sender": "Sam Lee <sam.lee@northgatetrust.org>", "date": "2026-02-01"},
            {"message_id": "m2", "sender": "Pat Nguyen <pat.nguyen@northgatetrust.org>", "date": "2026-02-02"},
        ],
        "entities": [
            {"name": "Sam Lee", "type": "person"},
            {"name": "Pat Nguyen", "type": "person"},
        ],
        "relations": [], "actions": [], "topics": ["x"],
    }
    (tmp_path / "config.json").write_text(json.dumps({
        "orgs": [{"name": "Northgate Trust", "domains": ["northgatetrust.org"]}],
        "enrich_structural_relations_enabled": False,
    }))
    graph_write.apply(s, extraction, doc_ids=["doc-4"], home=str(tmp_path))
    with s._connect() as db:
        works_at_with_prov = db.execute(
            "SELECT COUNT(*) FROM entity_relations WHERE relation='works_at' "
            "AND source_doc_id = 'doc-4'"
        ).fetchone()[0]
        mentioned_with = db.execute(
            "SELECT COUNT(*) FROM entity_relations WHERE relation='mentioned_with'"
        ).fetchone()[0]
    assert works_at_with_prov == 0, "kill-switch must suppress the deterministic works_at pass"
    assert mentioned_with == 0, "kill-switch must suppress the deterministic mentioned_with pass"


def test_observation_written_and_superseded_by_later_date(tmp_path):
    """Task 4.2 Step 1 (brief's literal example, done properly): an extraction
    with entities=[Sam] and observations=[{attribute:'title', value:'COO',
    date:'2026-01-01'}] writes one entity_observations row with
    attribute='title', value='COO'. A second extraction for the same
    thread/entity with a later-dated title observation supersedes the first
    (valid_to set on the old row; the new row is current)."""
    s = _store(tmp_path)
    extraction1 = {
        "thread_id": "t20", "org": "unknown", "content_type": "email", "summary": "s",
        "messages": [{"message_id": "m1", "sender": "a@x.org", "date": "2026-01-01"}],
        "entities": [{"name": "Sam", "type": "person"}],
        "observations": [
            {"entity_name": "Sam", "attribute": "title", "value": "COO", "date": "2026-01-01"},
        ],
        "relations": [], "actions": [], "topics": [],
    }
    graph_write.apply(s, extraction1, doc_ids=["doc-20"], home=str(tmp_path))

    with s._connect() as db:
        sam_id = db.execute("SELECT id FROM entities WHERE name='Sam'").fetchone()[0]
        rows = db.execute(
            "SELECT value, valid_to FROM entity_observations "
            "WHERE entity_id=? AND attribute='title'", (sam_id,)
        ).fetchall()
    assert len(rows) == 1, f"expected exactly one title observation, got {rows}"
    assert rows[0][0] == "COO"
    assert rows[0][1] is None or rows[0][1] == "", "first observation should be current (no valid_to)"

    extraction2 = {
        "thread_id": "t20", "org": "unknown", "content_type": "email", "summary": "s",
        "messages": [{"message_id": "m2", "sender": "a@x.org", "date": "2026-06-01"}],
        "entities": [{"name": "Sam", "type": "person"}],
        "observations": [
            {"entity_name": "Sam", "attribute": "title", "value": "CEO", "date": "2026-06-01"},
        ],
        "relations": [], "actions": [], "topics": [],
    }
    graph_write.apply(s, extraction2, doc_ids=["doc-21"], home=str(tmp_path))

    with s._connect() as db:
        rows2 = db.execute(
            "SELECT value, valid_to FROM entity_observations "
            "WHERE entity_id=? AND attribute='title' ORDER BY id", (sam_id,)
        ).fetchall()
    assert len(rows2) == 2, f"expected two title observation rows after supersession, got {rows2}"
    old_row, new_row = rows2[0], rows2[1]
    assert old_row[0] == "COO"
    assert old_row[1], "the first (COO) row must now be superseded (valid_to set)"
    assert new_row[0] == "CEO"
    assert not new_row[1], "the new (CEO) row must be current (no valid_to)"


def test_identical_observation_from_many_sources_consolidates(tmp_path):
    """The 'role x20' fix: the same (entity, attribute, value) reported by many
    different sources collapses onto ONE active row whose observed_count tracks
    the confirmations — not 20 rows."""
    s = _store(tmp_path)
    _seed_entity(s, "sam")
    for i in range(20):
        graph_write.write_observation(s, "sam", "role", "Operations Manager",
                                      f"source_{i}", f"2026-01-{i+1:02d}", "llm_extraction",
                                      source_key=f"thread-{i}", source_class="primary_mail")
    with s._connect() as db:
        rows = db.execute(
            "SELECT observed_count, last_seen FROM entity_observations "
            "WHERE entity_id='sam' AND attribute='role' AND valid_to IS NULL").fetchall()
    assert len(rows) == 1, f"expected one consolidated row, got {len(rows)}"
    assert rows[0]["observed_count"] == 20
    assert rows[0]["last_seen"] == "2026-01-20"   # most recent sighting tracked


def test_distinct_values_stay_separate(tmp_path):
    """Consolidation must not fuse genuinely different values — a real role change
    still produces distinct rows (history), not a bumped count."""
    s = _store(tmp_path)
    _seed_entity(s, "sam")
    graph_write.write_observation(s, "sam", "skill", "Python", "src1", "2026-01-01", "llm_extraction")
    graph_write.write_observation(s, "sam", "skill", "SQL", "src2", "2026-01-02", "llm_extraction")
    with s._connect() as db:
        vals = {r["value"] for r in db.execute(
            "SELECT value FROM entity_observations WHERE entity_id='sam' AND attribute='skill'")}
    assert vals == {"Python", "SQL"}


def test_consolidate_observations_backfill(tmp_path):
    """The one-shot backfill collapses pre-existing duplicate rows (written before
    write-time consolidation) the same way, and is idempotent."""
    s = _store(tmp_path)
    _seed_entity(s, "sam")
    # Simulate legacy bloat: 5 identical active rows, one distinct value.
    with s._connect() as db:
        for i in range(5):
            db.execute("INSERT INTO entity_observations(entity_id,attribute,value,source,valid_from) "
                       "VALUES('sam','role','Ops Manager',?,?)", (f"s{i}", f"2026-02-0{i+1}"))
        db.execute("INSERT INTO entity_observations(entity_id,attribute,value,source,valid_from) "
                   "VALUES('sam','role','Director','sx','2025-01-01')")
    out = graph_write.consolidate_observations(s)
    assert out["groups_consolidated"] == 1 and out["rows_removed"] == 4
    with s._connect() as db:
        rows = db.execute("SELECT value, observed_count, last_seen FROM entity_observations "
                          "WHERE entity_id='sam' AND value='Ops Manager'").fetchall()
        director = db.execute("SELECT COUNT(*) n FROM entity_observations WHERE value='Director'").fetchone()["n"]
    assert len(rows) == 1 and rows[0]["observed_count"] == 5 and rows[0]["last_seen"] == "2026-02-05"
    assert director == 1     # distinct value untouched
    # idempotent: second run finds nothing to do
    assert graph_write.consolidate_observations(s)["rows_removed"] == 0


def test_relation_strength_unchanged_on_unsourced_reobservation(tmp_path):
    """A re-observation with no source (calendar without an id, curator
    import, legacy callers) no longer bumps strength: strength counts
    distinct primary sources, and an unknown source is none."""
    s = _store(tmp_path)
    _seed_entity(s, "a", name="A"); _seed_entity(s, "b", name="B")
    for d in ("2026-01-01", "2026-02-01", "2026-03-01"):
        graph_write.upsert_relation(s, "a", "works_with", "b", valid_from=d)
    with s._connect() as db:
        row = db.execute("SELECT strength FROM entity_relations "
                         "WHERE entity_a='a' AND relation='works_with' AND entity_b='b'").fetchone()
    assert row["strength"] == 1


def test_decay_relations_culls_stale_weak_only(tmp_path):
    """decay zeroes strength on weak+stale relations (dropping them from the
    graph) but leaves strong or recent ones alone."""
    s = _store(tmp_path)
    for n in ("a", "b", "c", "d"):
        _seed_entity(s, n, name=n.upper())
    with s._connect() as db:
        # weak + stale -> should decay
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b,strength,last_seen) "
                   "VALUES('a','involved_in','b',1,'2023-01-01')")
        # weak but recent -> keep
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b,strength,last_seen) "
                   "VALUES('a','involved_in','c',1,'2026-06-01')")
        # stale but strong -> keep
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b,strength,last_seen) "
                   "VALUES('a','involved_in','d',9,'2023-01-01')")
    out = graph_write.decay_relations(s, stale_days=365, weak_max=1, as_of="2026-07-01")
    assert out["relations_decayed"] == 1
    with s._connect() as db:
        kept = {r["entity_b"] for r in db.execute(
            "SELECT entity_b FROM entity_relations WHERE COALESCE(strength,0) > 0")}
    assert kept == {"c", "d"}   # b (weak+stale) culled; c (recent) + d (strong) survive


def test_write_observation_skips_role_guards_for_non_role_attribute():
    """write_observation, called directly (not through apply()), must NOT apply
    the role-specific junk-value/source-rank guards to a non-role attribute.
    'volunteer' is in _JUNK_ROLE_VALUES and would be rejected as a role — but
    for attribute='org_move' it must be written."""
    import tempfile
    from mcpbrain.graph_write import write_observation

    with tempfile.TemporaryDirectory() as d:
        s = _store(Path(d))
        entity_id = _seed_entity(s, "sam")
        write_observation(s, entity_id, "org_move", "volunteer",
                          "llm_extraction", "2026-01-01", "medium")
        with s._connect() as db:
            row = db.execute(
                "SELECT value FROM entity_observations "
                "WHERE entity_id=? AND attribute='org_move'", (entity_id,)
            ).fetchone()
    assert row is not None, "org_move observation with a role-junk value must still be written"
    assert row[0] == "volunteer"


def test_write_role_observation_still_rejects_junk_after_refactor(tmp_path):
    """Regression guard: write_role_observation's existing behavior (junk role
    rejection) must be completely unchanged after generalizing to
    write_observation. Called via the thin wrapper, 'volunteer' must still be
    rejected for attribute='role'."""
    s = _store(tmp_path)
    entity_id = _seed_entity(s, "sam")
    graph_write.write_role_observation(
        s, entity_id, "volunteer", "llm_extraction", "2026-01-01", "medium")
    with s._connect() as db:
        row = db.execute(
            "SELECT value FROM entity_observations "
            "WHERE entity_id=? AND attribute='role'", (entity_id,)
        ).fetchone()
    assert row is None, "junk role value 'volunteer' must still be rejected post-refactor"


def test_observations_kill_switch_suppresses_wiring(tmp_path):
    """enrich_rich_observations_enabled=False must suppress apply()'s
    observations[] wiring entirely — no entity_observations rows written for
    the new attributes."""
    s = _store(tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({
        "orgs": [{"name": "Northgate Trust", "domains": ["northgatetrust.org"]}],
        "enrich_rich_observations_enabled": False,
    }))
    extraction = {
        "thread_id": "t21", "org": "unknown", "content_type": "email", "summary": "s",
        "messages": [{"message_id": "m1", "sender": "a@x.org", "date": "2026-01-01"}],
        "entities": [{"name": "Sam", "type": "person"}],
        "observations": [
            {"entity_name": "Sam", "attribute": "title", "value": "COO", "date": "2026-01-01"},
        ],
        "relations": [], "actions": [], "topics": [],
    }
    graph_write.apply(s, extraction, doc_ids=["doc-22"], home=str(tmp_path))
    with s._connect() as db:
        count = db.execute(
            "SELECT COUNT(*) FROM entity_observations WHERE attribute='title'"
        ).fetchone()[0]
    assert count == 0, "kill-switch must suppress the observations[] wiring"


def test_observation_unresolved_entity_name_is_skipped(tmp_path):
    """An observations[] item whose entity_name doesn't resolve to anything in
    name_to_id must be skipped without raising."""
    s = _store(tmp_path)
    extraction = {
        "thread_id": "t22", "org": "unknown", "content_type": "email", "summary": "s",
        "messages": [{"message_id": "m1", "sender": "a@x.org", "date": "2026-01-01"}],
        "entities": [{"name": "Sam", "type": "person"}],
        "observations": [
            {"entity_name": "Nobody Real", "attribute": "title", "value": "COO", "date": "2026-01-01"},
        ],
        "relations": [], "actions": [], "topics": [],
    }
    # Must not raise.
    graph_write.apply(s, extraction, doc_ids=["doc-23"], home=str(tmp_path))
    with s._connect() as db:
        count = db.execute(
            "SELECT COUNT(*) FROM entity_observations WHERE attribute='title'"
        ).fetchone()[0]
    assert count == 0, "unresolved entity_name must not write an observation"


def test_apply_model_org_wins_over_org_hint(tmp_path, monkeypatch):
    """The model's own real (non-empty, non-'unknown') org signal always wins
    over org_hint, even when they disagree — org_hint is a fallback only.
    """
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    s = _store(tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({
        "orgs": [
            {"name": "Acme", "domains": ["example.org"]},
            {"name": "Northgate Trust", "domains": ["northgatetrust.org"]},
        ],
    }))
    extraction = {
        "thread_id": "t7", "org": "Acme", "org_hint": "Northgate Trust",
        "content_type": "email", "summary": "s",
        "messages": [{"message_id": "m1", "sender": "Sam Lee <sam.lee@northgatetrust.org>",
                      "date": "2026-02-01"}],
        "entities": [], "relations": [], "actions": [], "topics": [],
    }
    graph_write.apply(s, extraction, doc_ids=["doc-11"], home=str(tmp_path))
    with s._connect() as db:
        org = db.execute("SELECT org FROM email_context WHERE message_id='m1'").fetchone()[0]
    assert org == "Acme", f"model's own org signal must win over org_hint, got {org!r}"


def test_sender_role_address_not_email_keyed(tmp_path, monkeypatch):
    # I1/C1: a sender on a shared/role inbox (office@) must NOT be stamped with that
    # address as their email_addr — otherwise email-equality dedup would later merge
    # every distinct person who sends from that shared inbox.
    monkeypatch.setattr("mcpbrain.config.enrich_sender_entities", lambda home: True)
    s = _store(tmp_path)
    extraction = {
        "thread_id": "t-role", "org": "unknown", "content_type": "update", "summary": "s",
        "messages": [{"message_id": "m1", "sender": "Dana Ng <office@northgatetrust.org>", "date": "2026-02-01"}],
        "entities": [], "relations": [], "actions": [], "topics": ["x"],
    }
    graph_write.apply(s, extraction, doc_ids=["doc-1"])
    with s._connect() as db:
        row = db.execute("SELECT email_addr FROM entities WHERE name='Dana Ng'").fetchone()
    if row is not None:
        assert (row[0] or "") == "", "role-address sender must not be email-keyed"


def test_sender_entity_created_without_model(tmp_path, monkeypatch):
    monkeypatch.setattr("mcpbrain.config.enrich_sender_entities", lambda home: True)
    s = _store(tmp_path)
    extraction = {
        "thread_id": "t9", "org": "unknown", "content_type": "update", "summary": "s",
        "messages": [{"message_id": "m1", "sender": "Dana Lee <dana@northgatetrust.org>", "date": "2026-02-01"}],
        "entities": [],   # model surfaced NO entities
        "relations": [], "actions": [], "topics": ["x"],
    }
    graph_write.apply(s, extraction, doc_ids=["doc-1"])
    with s._connect() as db:
        row = db.execute("SELECT type, email_addr FROM entities WHERE name='Dana Lee'").fetchone()
    assert row is not None, "sender must be created as an entity even when the model omits it"
    assert row[0] == "person" and row[1] == "dana@northgatetrust.org"


def test_sender_entity_not_created_for_owner_by_identity(tmp_path, monkeypatch):
    """The deterministic sender-entity loop (Task 1.1) must mirror the
    lead-sender exclusion at :1008-1009: skip a sender whose header email
    matches `identity` even when _is_owner's alias matching would not catch
    the sender's display name (e.g. a signature variant with no configured
    alias). Owner here is "Sam Chen" with aliases {"sam", "sam chen"} — the
    non-lead message uses "S. Chen", which neither alias matches as a whole
    word or substring, but whose header email is the owner's own identity.
    """
    monkeypatch.setattr("mcpbrain.config.enrich_sender_entities", lambda home: True)
    s = _store(tmp_path)
    owner = graph_write.OwnerIdentity(
        name="Sam Chen", entity_id="sam-chen",
        aliases=frozenset({"sam", "sam chen"}),
    )
    assert not graph_write._is_owner("S. Chen", owner), (
        "test setup: alias matching must NOT catch this display name"
    )
    extraction = {
        "thread_id": "t10", "org": "unknown", "content_type": "update", "summary": "s",
        "messages": [
            {"message_id": "m1", "sender": "Dana Lee <dana@northgatetrust.org>", "date": "2026-02-01"},
            {"message_id": "m2", "sender": "S. Chen <sam.chen@northgatetrust.org>", "date": "2026-02-02"},
        ],
        "entities": [],  # model surfaced NO entities
        "relations": [], "actions": [], "topics": ["x"],
    }
    graph_write.apply(
        s, extraction, doc_ids=["doc-2"],
        owner=owner, identity="sam.chen@northgatetrust.org",
    )
    with s._connect() as db:
        row = db.execute("SELECT id FROM entities WHERE name='S. Chen'").fetchone()
    assert row is None, (
        "owner must be excluded from deterministic sender-entity creation by "
        "identity/email, not just alias matching"
    )


# ---------------------------------------------------------------------------
# Strength = independent primary sources (spec 2026-10-22 §3)
# ---------------------------------------------------------------------------

def _mail_chunk(store, doc_id, thread_id, **extra):
    meta = {"source_type": "gmail", "thread_id": thread_id,
            "message_id": f"msg-{doc_id}", "labels": "INBOX"}
    meta.update(extra)
    store.upsert_chunk(doc_id, f"text of {doc_id}", f"h-{doc_id}", meta)


def _rel_extraction(thread_id, msg_id, *, role="Director"):
    return {
        "thread_id": thread_id, "org": "unknown", "content_type": "email",
        "summary": "s",
        "messages": [{"message_id": msg_id, "sender": "Sam <sam@x.org>",
                      "date": "2026-02-01"}],
        "entities": [{"name": "Sam", "type": "person", "role": role},
                     {"name": "Pat", "type": "person"}],
        "relations": [{"source_name": "Sam", "type": "reports_to", "target_name": "Pat"}],
        "actions": [], "topics": [],
    }


def _strength(store, rel="reports_to"):
    with store._connect() as db:
        r = db.execute("SELECT id, strength, derived FROM entity_relations "
                       "WHERE relation=?", (rel,)).fetchone()
        n = db.execute("SELECT COUNT(*) FROM relation_sources WHERE relation_id=?",
                       (r["id"],)).fetchone()[0]
    return r["strength"], r["derived"], n


def _role_count(store):
    with store._connect() as db:
        return db.execute(
            "SELECT observed_count FROM entity_observations "
            "WHERE attribute='role' AND value='Director' AND valid_to IS NULL").fetchone()[0]


def test_reextraction_does_not_raise_strength(tmp_path):
    """Re-extracting the same thread — or another chunk of it — is the same
    evidence again: strength and observed_count stay at 1."""
    s = _store(tmp_path)
    _mail_chunk(s, "gm-1", "t1")
    _mail_chunk(s, "gm-1b", "t1")
    graph_write.apply(s, _rel_extraction("t1", "m1"), doc_ids=["gm-1"])
    graph_write.apply(s, _rel_extraction("t1", "m1"), doc_ids=["gm-1"])
    graph_write.apply(s, _rel_extraction("t1", "m1"), doc_ids=["gm-1b"])
    assert _strength(s) == (1, 0, 1)
    assert _role_count(s) == 1
    with s._connect() as db:
        src = db.execute("SELECT source_key, source_class FROM relation_sources").fetchall()
    assert [tuple(r) for r in src] == [("t1", "primary_mail")]


def test_second_independent_source_raises_strength(tmp_path):
    s = _store(tmp_path)
    _mail_chunk(s, "gm-1", "t1")
    _mail_chunk(s, "gm-2", "t2")
    graph_write.apply(s, _rel_extraction("t1", "m1"), doc_ids=["gm-1"])
    graph_write.apply(s, _rel_extraction("t2", "m2"), doc_ids=["gm-2"])
    assert _strength(s) == (2, 0, 2)
    assert _role_count(s) == 2
    with s._connect() as db:
        rid = db.execute("SELECT id FROM entity_relations WHERE relation='reports_to'"
                         ).fetchone()[0]
        assert graph_write.relation_strength(db, rid) == 2


def test_duplicate_carries_canonical_key(tmp_path):
    """A content duplicate (chunks.dup_of) is the canonical's evidence: it
    adds no new source."""
    s = _store(tmp_path)
    _mail_chunk(s, "gm-1", "t1")
    _mail_chunk(s, "gm-9", "t9")
    with s._connect() as db:
        db.execute("UPDATE chunks SET dup_of='gm-1' WHERE doc_id='gm-9'")
    graph_write.apply(s, _rel_extraction("t1", "m1"), doc_ids=["gm-1"])
    graph_write.apply(s, _rel_extraction("t9", "m9"), doc_ids=["gm-9"])
    assert _strength(s) == (1, 0, 1)


def test_derived_source_adds_no_support(tmp_path):
    s = _store(tmp_path)
    _mail_chunk(s, "gm-1", "t1")
    s.upsert_chunk("note-consolidated-abc", "consolidated", "h-c",
                   {"source": "note", "note_id": "note-consolidated-abc"})
    graph_write.apply(s, _rel_extraction("t1", "m1"), doc_ids=["gm-1"])
    sam_before = s.get_entity("sam")["email_count"]

    # The same relation and role restated by a consolidated note: nothing moves.
    graph_write.apply(s, _rel_extraction("note-consolidated-abc", "note-consolidated-abc"),
                      doc_ids=["note-consolidated-abc"])
    assert _strength(s) == (1, 0, 1)
    assert _role_count(s) == 1
    assert s.get_entity("sam")["email_count"] == sam_before
    with s._connect() as db:
        link = db.execute("SELECT derived FROM email_entities "
                          "WHERE message_id='note-consolidated-abc' AND entity_id='sam'"
                          ).fetchone()
    assert link is not None and link["derived"] == 1

    # A relation ONLY a derived source states is written derived=1 / strength 0.
    ext = _rel_extraction("note-consolidated-abc", "note-consolidated-abc")
    ext["relations"] = [{"source_name": "Sam", "type": "manages", "target_name": "Pat"}]
    graph_write.apply(s, ext, doc_ids=["note-consolidated-abc"])
    assert _strength(s, "manages") == (0, 1, 0)

    # A later primary source makes it a real edge with one source.
    ext2 = _rel_extraction("t1", "m1")
    ext2["relations"] = [{"source_name": "Sam", "type": "manages", "target_name": "Pat"}]
    graph_write.apply(s, ext2, doc_ids=["gm-1"])
    assert _strength(s, "manages") == (1, 0, 1)


def test_digest_writes_no_relations(tmp_path):
    s = _store(tmp_path)
    graph_write.apply(s, _rel_extraction("t5", "m5"), doc_ids=["enriched-t5"])
    with s._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM entity_relations "
                          "WHERE relation='reports_to'").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM email_entities "
                          "WHERE message_id='m5'").fetchone()[0] == 0


def test_decay_keeps_evidenced_relation(tmp_path):
    """decay never zeroes a relation still supported by a primary source,
    however stale; an unsourced weak+stale one still decays."""
    s = _store(tmp_path)
    for n in ("a", "b", "c"):
        _seed_entity(s, n, name=n.upper())
    with s._connect() as db:
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b,strength,last_seen) "
                   "VALUES('a','involved_in','b',1,'2023-01-01')")
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b,strength,last_seen) "
                   "VALUES('a','involved_in','c',1,'2023-01-01')")
        rid = db.execute("SELECT id FROM entity_relations WHERE entity_b='b'").fetchone()[0]
        graph_write.record_relation_source(db, rid, "t1", "primary_mail")
    out = graph_write.decay_relations(s, stale_days=365, weak_max=1, as_of="2026-07-01")
    assert out["relations_decayed"] == 1
    with s._connect() as db:
        kept = {r["entity_b"] for r in db.execute(
            "SELECT entity_b FROM entity_relations WHERE COALESCE(strength,0) > 0")}
    assert kept == {"b"}


def test_derived_singleton_never_supersedes_evidence(tmp_path):
    s = _store(tmp_path)
    for n in ("p", "acme", "beta"):
        _seed_entity(s, n, name=n)
    graph_write.upsert_relation(s, "p", "works_at", "acme", valid_from="2026-01-01",
                                source_key="t1", source_class="primary_mail")
    graph_write.upsert_relation(s, "p", "works_at", "beta", valid_from="2026-05-01",
                                source_key="note-x", source_class="consolidated")
    with s._connect() as db:
        rows = {r["entity_b"]: r for r in db.execute(
            "SELECT entity_b, invalidated_at, derived, strength FROM entity_relations")}
    assert rows["acme"]["invalidated_at"] is None and rows["acme"]["strength"] == 1
    assert rows["beta"]["derived"] == 1 and rows["beta"]["strength"] == 0


def test_user_asserted_relation_counts_at_least_one(tmp_path):
    s = _store(tmp_path)
    _seed_entity(s, "a", name="A"); _seed_entity(s, "b", name="B")
    with s._connect() as db:
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b,strength,"
                   "user_verdict) VALUES('a','manages','b',0,'asserted')")
        rid = db.execute("SELECT id FROM entity_relations").fetchone()[0]
        assert graph_write.relation_strength(db, rid) == 1


def test_relations_for_excludes_derived(tmp_path):
    s = _store(tmp_path)
    for n in ("a", "b", "c"):
        _seed_entity(s, n, name=n.upper())
    graph_write.upsert_relation(s, "a", "manages", "b", valid_from="2026-01-01",
                                source_key="t1", source_class="primary_mail")
    graph_write.upsert_relation(s, "a", "manages", "c", valid_from="2026-01-01",
                                source_key="note-x", source_class="session")
    assert {r["entity_b"] for r in s.relations_for("a")} == {"b"}
    assert s.entity_degrees(["c"]) == {"c": 0}


def test_extraction_keyed_on_own_chunk_not_duplicate(tmp_path):
    """A duplicate chunk must not credit the extraction to the canonical's
    source while the extraction has evidence of its own — in either order.
    Only an all-duplicate extraction borrows the canonical's key."""
    s = _store(tmp_path)
    _mail_chunk(s, "gm-1", "t1")
    _mail_chunk(s, "gm-2", "t2")
    _mail_chunk(s, "gm-2d", "t2")
    with s._connect() as db:
        db.execute("UPDATE chunks SET dup_of='gm-1' WHERE doc_id='gm-2d'")
    assert graph_write.extraction_sources(s, ["gm-2d", "gm-2"])[0] == "t2"
    assert graph_write.extraction_sources(s, ["gm-2", "gm-2d"])[0] == "t2"
    assert graph_write.extraction_sources(s, ["gm-2d"]) == ("t1", "primary_mail")


def test_reobserving_legacy_row_records_legacy_strength(tmp_path):
    s = _store(tmp_path)
    _seed_entity(s, "a", name="A"); _seed_entity(s, "b", name="B")
    with s._connect() as db:
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b,strength,valid_from) "
                   "VALUES('a','manages','b',30,'2025-01-01')")
    graph_write.upsert_relation(s, "a", "manages", "b", valid_from="2026-01-01",
                                source_key="t1", source_class="primary_mail")
    graph_write.upsert_relation(s, "a", "manages", "b", valid_from="2026-02-01",
                                source_key="t2", source_class="primary_mail")
    with s._connect() as db:
        r = db.execute("SELECT strength, legacy_strength FROM entity_relations").fetchone()
    assert (r["strength"], r["legacy_strength"]) == (2, 30)


def test_legacy_marker_is_no_evidence_source(tmp_path):
    """Ruling I8: legacy = no EVIDENCE relation_sources row. legacy_strength is
    a saved prior value only; a derived-class source row does not count."""
    s = _store(tmp_path)
    for e in ("a", "b", "c", "d"):
        _seed_entity(s, e, name=e.upper())
    with s._connect(write=True) as db:
        for b in ("b", "c", "d"):
            db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b) "
                       "VALUES('a','knows',?)", (b,))
        ids = {r["entity_b"]: r["id"] for r in db.execute(
            "SELECT id, entity_b FROM entity_relations")}
        graph_write.record_relation_source(db, ids["c"], "s1", "session")
        graph_write.record_relation_source(db, ids["d"], "t1", "primary_mail")
        db.execute("UPDATE entity_relations SET legacy_strength=30 WHERE id=?", (ids["d"],))
        legacy = {r["entity_b"]: r["l"] for r in db.execute(
            f"SELECT entity_b, {graph_write.legacy_relation_sql()} AS l "
            "FROM entity_relations")}
    assert legacy == {"b": 1, "c": 1, "d": 0}


def test_derived_observation_never_retires_primary_nor_is_current_role(tmp_path):
    s = _store(tmp_path)
    _seed_entity(s, "sam")
    graph_write.write_observation(s, "sam", "role", "Director", "llm_extraction",
                                  "2026-03-01", "medium",
                                  source_key="t1", source_class="primary_mail")
    # A LATER derived observation of a different role…
    graph_write.write_observation(s, "sam", "role", "Volunteer Lead", "llm_extraction",
                                  "2026-06-01", "medium",
                                  source_key="note-x", source_class="consolidated")
    # …and an EARLIER primary one arriving after it must not be made
    # historical by the derived row.
    graph_write.write_observation(s, "sam", "role", "Coordinator", "llm_extraction",
                                  "2026-04-01", "medium",
                                  source_key="t2", source_class="primary_mail")
    assert graph_write.fetch_role(s, "sam") == "Coordinator"
    with s._connect() as db:
        cur = {r["value"]: r["valid_to"] for r in db.execute(
            "SELECT value, valid_to FROM entity_observations WHERE derived=0")}
    assert cur["Coordinator"] is None
    from mcpbrain import entity_resource
    with s._connect() as db:
        assert entity_resource._ranked_role(db, "sam") == "Coordinator"


def test_consolidate_observations_keeps_sources(tmp_path):
    s = _store(tmp_path)
    _seed_entity(s, "sam")
    with s._connect() as db:
        for i, der in ((1, 1), (2, 0), (3, 0)):
            db.execute("INSERT INTO entity_observations(id,entity_id,attribute,value,source,"
                       "valid_from,derived) VALUES(?,'sam','role','Ops',?,?,?)",
                       (i, f"s{i}", f"2026-02-0{i}", der))
        graph_write.record_observation_source(db, 2, "t2", "primary_mail")
        graph_write.record_observation_source(db, 3, "t3", "primary_mail")
    graph_write.consolidate_observations(s)
    with s._connect() as db:
        rows = db.execute("SELECT id, observed_count, derived FROM entity_observations").fetchall()
        keys = {r[0] for r in db.execute("SELECT source_key FROM observation_sources")}
    assert [tuple(r) for r in rows] == [(2, 2, 0)]
    assert keys == {"t2", "t3"}


def test_decay_keeps_user_asserted_relation(tmp_path):
    s = _store(tmp_path)
    _seed_entity(s, "a", name="A"); _seed_entity(s, "b", name="B")
    with s._connect() as db:
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b,strength,last_seen,"
                   "user_verdict) VALUES('a','manages','b',1,'2023-01-01','asserted')")
    out = graph_write.decay_relations(s, stale_days=365, weak_max=1, as_of="2026-07-01")
    assert out["relations_decayed"] == 0
