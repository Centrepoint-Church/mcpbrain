from mcpbrain.store import Store
from mcpbrain.review import build_review_packet, build_review_units
from mcpbrain.review_eval import review_metrics


def _seed(tmp_path):
    s = Store(str(tmp_path / "b.sqlite3"), dim=4)
    s.init()
    with s._connect() as db:
        db.execute("INSERT INTO entities(id,name,type,org,email_addr,mentions) VALUES('e1','Sam Lee','person','Acme','sam@acme.com',3)")
        db.execute("INSERT INTO chunks(doc_id,text,content_hash,metadata,embedded) "
                   "VALUES('d1','Sam Lee leads the Acme rollout.','h1','{}',1)")
        db.execute("INSERT INTO email_entities(message_id,entity_id,role) VALUES('d1','e1','authored')")
    return s


def test_packet_has_entity_and_source_text(tmp_path):
    s = _seed(tmp_path)
    finding = {"finding_type": "lint:orphan_entity", "ref_id": "e1", "summary": "orphan", "detail": ""}
    pk = build_review_packet(s, finding)
    assert pk["entity"]["name"] == "Sam Lee"
    assert pk["entity"]["email_addr"] == "sam@acme.com"
    assert any("Acme rollout" in span for span in pk["source_spans"]), "must carry the source text the entity came from"


def test_build_review_units_respects_cap(tmp_path):
    s = _seed(tmp_path)
    s.record_finding("lint:orphan_entity", "e1", summary="orphan 1")
    s.record_finding("lint:orphan_entity", "e2", summary="orphan 2")
    s.record_finding("lint:orphan_entity", "e3", summary="orphan 3")

    units = build_review_units(s, kinds=["lint:orphan_entity"], cap=2)

    assert len(units) <= 2
    for unit in units:
        assert set(unit.keys()) == {"finding_id", "packet"}
        assert isinstance(unit["finding_id"], int)


def test_build_review_units_cap_is_per_kind_not_shared(tmp_path):
    """cap bounds each kind independently; it must not be a single shared
    budget across kinds (the starvation bug: a backlog in the first kind
    used to consume the whole cap and starve every other kind)."""
    s = _seed(tmp_path)
    s.record_finding("lint:orphan_entity", "e1", summary="orphan 1")
    s.record_finding("lint:orphan_entity", "e2", summary="orphan 2")
    s.record_finding("lint:orphan_entity", "e3", summary="orphan 3")
    s.record_finding("lint:missing_org", "e1", summary="missing org 1")
    s.record_finding("lint:missing_org", "e2", summary="missing org 2")
    s.record_finding("lint:missing_org", "e3", summary="missing org 3")

    units = build_review_units(s, kinds=["lint:orphan_entity", "lint:missing_org"], cap=2)

    orphan_ids = {f["id"] for f in s.open_findings("lint:orphan_entity")}
    missing_org_ids = {f["id"] for f in s.open_findings("lint:missing_org")}
    unit_ids = {u["finding_id"] for u in units}

    orphan_units = unit_ids & orphan_ids
    missing_org_units = unit_ids & missing_org_ids

    assert len(orphan_units) == 2
    assert len(missing_org_units) == 2
    assert len(units) == 4


def test_build_review_units_kind_with_fewer_than_cap_returns_all(tmp_path):
    s = _seed(tmp_path)
    s.record_finding("lint:orphan_entity", "e1", summary="orphan 1")

    units = build_review_units(s, kinds=["lint:orphan_entity"], cap=50)

    assert len(units) == 1


def test_review_metrics(tmp_path):
    s = Store(str(tmp_path / "metrics.sqlite3"), dim=4)
    s.init()

    # Record findings across 2 different types with distinct ref_ids
    s.record_finding("lint:orphan_entity", "e1", summary="orphan entity 1")
    s.record_finding("lint:orphan_entity", "e2", summary="orphan entity 2")
    s.record_finding("lint:unreferenced_chunk", "c1", summary="unreferenced chunk")

    metrics = review_metrics(s)

    # Assert structure and values
    assert isinstance(metrics, dict)
    assert "open_findings" in metrics
    assert "by_type" in metrics
    assert "resolved_last_run" in metrics

    # Total count should be 3
    assert metrics["open_findings"] == 3

    # by_type should have correct counts per type
    assert metrics["by_type"] == {
        "lint:orphan_entity": 2,
        "lint:unreferenced_chunk": 1,
    }

    # resolved_last_run is currently hardcoded to 0
    assert metrics["resolved_last_run"] == 0


# --- Task 3.1: ownerless_action packets are action-anchored, not entity-anchored ---


def test_packet_for_ownerless_action_carries_action_thread_and_source(tmp_path):
    s = Store(str(tmp_path / "ownerless.sqlite3"), dim=4)
    s.init()
    with s._connect() as db:
        db.execute(
            "INSERT INTO chunks(doc_id,text,content_hash,metadata,embedded) "
            "VALUES('d1','Please send the updated budget by Friday.','h1','{}',1)")
        cur = db.execute(
            "INSERT INTO actions(text, deadline, thread_id, source_doc_id, owner, owner_entity_id) "
            "VALUES('Send the updated budget','2026-07-10','t1','d1','','')")
        action_id = cur.lastrowid
        db.execute(
            "INSERT INTO email_context(message_id, sender, sender_email, thread_id, date_iso) "
            "VALUES('m1','Alice Admin','alice@acme.com','t1','2026-07-01T00:00:00Z')")
        db.execute(
            "INSERT INTO email_context(message_id, sender, sender_email, thread_id, date_iso) "
            "VALUES('m2','Bob Builder','bob@acme.com','t1','2026-07-02T00:00:00Z')")

    finding = {"finding_type": "lint:ownerless_action", "ref_id": str(action_id),
               "summary": "ownerless_action: Send the updated budget", "detail": ""}
    pk = build_review_packet(s, finding)

    assert pk["entity"] is None
    assert pk["action"]["text"] == "Send the updated budget"
    assert pk["action"]["deadline"] == "2026-07-10"
    assert pk["action"]["owner"] == ""
    assert pk["action"]["owner_entity_id"] == ""
    assert any("updated budget" in span for span in pk["source_spans"])
    assert {p["sender"] for p in pk["thread"]["participants"]} == {"Alice Admin", "Bob Builder"}
    assert pk["thread"]["sender"]["sender"] == "Alice Admin"  # earliest by date_iso
