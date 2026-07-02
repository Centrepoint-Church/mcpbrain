from mcpbrain.store import Store
from mcpbrain.review import build_review_packet, build_review_units


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
