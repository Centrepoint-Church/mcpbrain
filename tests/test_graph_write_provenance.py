# tests/test_graph_write_provenance.py
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
