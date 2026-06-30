from mcpbrain.store import Store
from mcpbrain.enrich_eval import graph_metrics


def _seed(tmp_path):
    s = Store(str(tmp_path / "b.sqlite3"), dim=384)
    s.init()
    with s._connect() as db:
        db.execute("INSERT INTO entities(id,name,type,email_addr) VALUES('e1','Sam','person','sam@x.org')")
        db.execute("INSERT INTO entities(id,name,type,email_addr) VALUES('e2','Pat','person','')")
        db.execute("INSERT INTO entities(id,name,type) VALUES('o1','XOrg','org')")
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b,valid_from,source_doc_id) "
                   "VALUES('e1','reports_to','e2','2026-01-01','doc-1')")
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b,valid_from,source_doc_id) "
                   "VALUES('e1','involved_in','o1','2026-01-01','')")
        db.execute("INSERT INTO entity_observations(entity_id,attribute,value,valid_from) "
                   "VALUES('e1','role','CEO','2026-01-01')")
    return s


def test_graph_metrics_basic(tmp_path):
    m = graph_metrics(_seed(tmp_path))
    assert m["relations_total"] == 2
    assert m["relations_with_doc_id_pct"] == 50.0          # 1 of 2 has a doc id
    assert m["relations_semantic_pct"] == 50.0             # reports_to is semantic, involved_in is not
    assert m["entities_total"] == 3
    assert m["person_email_pct"] == 50.0                   # 1 of 2 persons has email
    assert m["observation_attributes"] == {"role": 1}
    assert m["relation_type_counts"]["reports_to"] == 1
