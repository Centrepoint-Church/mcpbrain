# tests/test_graph_write_provenance.py
import json

from mcpbrain.store import Store
from mcpbrain import graph_write


def _store(tmp_path):
    s = Store(str(tmp_path / "b.sqlite3"), dim=384); s.init(); return s


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
        "messages": [{"message_id": "m1", "sender": "Sam Lee <sam.lee@centrepoint.church>", "date": "2026-02-01"}],
        "entities": [{"name": "Sam Lee", "type": "person"}],
        "relations": [], "actions": [], "topics": ["x"],
    }
    graph_write.apply(s, extraction, doc_ids=["doc-7"])
    with s._connect() as db:
        email = db.execute("SELECT email_addr FROM entities WHERE name='Sam Lee'").fetchone()[0]
    assert email == "sam.lee@centrepoint.church"


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
            {"message_id": "m1", "sender": "Sam Lee <sam.lee@centrepoint.church>", "date": "2026-02-01"},
            {"message_id": "m2", "sender": "Pat Nguyen <pat.nguyen@centrepoint.church>", "date": "2026-02-02"},
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
    assert sam_email == "sam.lee@centrepoint.church"
    assert pat_email == "pat.nguyen@centrepoint.church", (
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
        "orgs": [{"name": "Centrepoint"}],
    }))

    taxonomy = orgs_mod.taxonomy_from_config()
    existing_id = upsert_entity(s, name="Pat Nguyen", entity_type="person", taxonomy=taxonomy)
    assert existing_id
    with s._connect() as db:
        pre_email = db.execute("SELECT email_addr FROM entities WHERE id=?", (existing_id,)).fetchone()[0]
    assert pre_email == ""

    extraction = {
        "thread_id": "t5", "org": "Centrepoint", "content_type": "email", "summary": "s",
        "messages": [
            {"message_id": "m1", "sender": "Sam Lee <sam.lee@centrepoint.church>", "date": "2026-02-01"},
            {"message_id": "m2", "sender": "Pat Nguyen <pat.nguyen@centrepoint.church>", "date": "2026-02-02"},
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
    assert email == "pat.nguyen@centrepoint.church", (
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
        "orgs": [{"name": "Centrepoint", "domains": ["centrepoint.church"]}],
    }))
    extraction = {
        "thread_id": "t6", "org": "unknown", "org_hint": "Centrepoint",
        "content_type": "email", "summary": "s",
        "messages": [{"message_id": "m1", "sender": "Sam Lee <sam.lee@centrepoint.church>",
                      "date": "2026-02-01"}],
        "entities": [], "relations": [], "actions": [], "topics": [],
    }
    graph_write.apply(s, extraction, doc_ids=["doc-10"], home=str(tmp_path))
    with s._connect() as db:
        org = db.execute("SELECT org FROM email_context WHERE message_id='m1'").fetchone()[0]
    assert org == "Centrepoint", f"expected org_hint fallback 'Centrepoint', got {org!r}"


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
        "messages": [{"message_id": "m1", "sender": "Sam <sam@centrepoint.church>", "date": "2026-02-01"}],
        "entities": [{"name": "Sam", "type": "person"}],
        "relations": [],          # model returned NO relations
        "actions": [], "topics": ["x"],
    }
    (tmp_path / "config.json").write_text(json.dumps({
        "orgs": [{"name": "Centrepoint", "domains": ["centrepoint.church"]}],
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
            {"message_id": "m1", "sender": "Sam Lee <sam.lee@centrepoint.church>", "date": "2026-02-01"},
            {"message_id": "m2", "sender": "Pat Nguyen <pat.nguyen@centrepoint.church>", "date": "2026-02-02"},
        ],
        "entities": [
            {"name": "Sam Lee", "type": "person"},
            {"name": "Pat Nguyen", "type": "person"},
        ],
        "relations": [], "actions": [], "topics": ["x"],
    }
    (tmp_path / "config.json").write_text(json.dumps({
        "orgs": [{"name": "Centrepoint", "domains": ["centrepoint.church"]}],
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
            {"message_id": "m1", "sender": "Sam Lee <sam.lee@centrepoint.church>", "date": "2026-02-01"},
            {"message_id": "m2", "sender": "Pat Nguyen <pat.nguyen@centrepoint.church>", "date": "2026-02-02"},
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
        "orgs": [{"name": "Centrepoint", "domains": ["centrepoint.church"]}],
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
            {"message_id": "m1", "sender": "Sam Lee <sam.lee@centrepoint.church>", "date": "2026-02-01"},
            {"message_id": "m2", "sender": "Pat Nguyen <pat.nguyen@centrepoint.church>", "date": "2026-02-02"},
        ],
        "entities": [
            {"name": "Sam Lee", "type": "person"},
            {"name": "Pat Nguyen", "type": "person"},
        ],
        "relations": [], "actions": [], "topics": ["x"],
    }
    (tmp_path / "config.json").write_text(json.dumps({
        "orgs": [{"name": "Centrepoint", "domains": ["centrepoint.church"]}],
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


def test_apply_model_org_wins_over_org_hint(tmp_path, monkeypatch):
    """The model's own real (non-empty, non-'unknown') org signal always wins
    over org_hint, even when they disagree — org_hint is a fallback only.
    """
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    s = _store(tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({
        "orgs": [
            {"name": "Acme", "domains": ["example.org"]},
            {"name": "Centrepoint", "domains": ["centrepoint.church"]},
        ],
    }))
    extraction = {
        "thread_id": "t7", "org": "Acme", "org_hint": "Centrepoint",
        "content_type": "email", "summary": "s",
        "messages": [{"message_id": "m1", "sender": "Sam Lee <sam.lee@centrepoint.church>",
                      "date": "2026-02-01"}],
        "entities": [], "relations": [], "actions": [], "topics": [],
    }
    graph_write.apply(s, extraction, doc_ids=["doc-11"], home=str(tmp_path))
    with s._connect() as db:
        org = db.execute("SELECT org FROM email_context WHERE message_id='m1'").fetchone()[0]
    assert org == "Acme", f"model's own org signal must win over org_hint, got {org!r}"
