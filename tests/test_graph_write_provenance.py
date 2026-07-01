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
