from mcpbrain.store import Store
from mcpbrain.enrich_eval import graph_metrics, gold_docs_cold_marked


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


def test_graph_metrics_empty_store(tmp_path):
    s = Store(str(tmp_path / "b.sqlite3"), dim=384)
    s.init()
    m = graph_metrics(s)
    assert m["relations_total"] == 0
    assert m["relations_with_doc_id_pct"] == 0.0
    assert m["relations_semantic_pct"] == 0.0
    assert m["entities_total"] == 0
    assert m["person_email_pct"] == 0.0
    assert m["observation_attributes"] == {}
    assert m["relation_type_counts"] == {}


def test_main_writes_baseline(tmp_path, monkeypatch, capsys):
    s = _seed(tmp_path)
    monkeypatch.setattr("mcpbrain.enrich_eval._open_store", lambda: s)
    from mcpbrain.enrich_eval import main
    out = tmp_path / "base.json"
    main(["--baseline", str(out)])
    import json
    saved = json.loads(out.read_text())
    assert saved["relations_total"] == 2


def _seed_chunks_for_gold(tmp_path):
    """Seed a store with chunks for gold_docs_cold_marked testing."""
    s = Store(str(tmp_path / "gold.sqlite3"), dim=384)
    s.init()
    with s._connect() as db:
        # chunk 1: not cold
        db.execute(
            "INSERT INTO chunks(doc_id, text, content_hash, enrich_state) "
            "VALUES('chunk-1', 'text one', 'hash1', '')"
        )
        # chunk 2: cold
        db.execute(
            "INSERT INTO chunks(doc_id, text, content_hash, enrich_state) "
            "VALUES('chunk-2', 'text two', 'hash2', 'cold')"
        )
        # chunk 3: not cold
        db.execute(
            "INSERT INTO chunks(doc_id, text, content_hash, enrich_state) "
            "VALUES('chunk-3', 'text three', 'hash3', '')"
        )
        # chunk 4: cold
        db.execute(
            "INSERT INTO chunks(doc_id, text, content_hash, enrich_state) "
            "VALUES('chunk-4', 'text four', 'hash4', 'cold')"
        )
    return s


def test_gold_docs_cold_marked_basic(tmp_path, monkeypatch):
    """Test basic functionality: present=3, cold=1, pct=33.3."""
    s = _seed_chunks_for_gold(tmp_path)

    # Gold cases reference: chunk-1, chunk-2, chunk-3, and chunk-missing (which doesn't exist)
    gold_fixture = [
        {
            "id": "case-1",
            "query": "test",
            "expected_chunk_ids": ["chunk-1", "chunk-2"]
        },
        {
            "id": "case-2",
            "query": "test",
            "expected_chunk_ids": ["chunk-3", "chunk-missing"]
        }
    ]

    monkeypatch.setattr(
        "mcpbrain.enrich_eval._load_gold_cases",
        lambda: gold_fixture
    )

    m = gold_docs_cold_marked(s)

    # chunk-1 exists but not cold
    # chunk-2 exists and is cold
    # chunk-3 exists but not cold
    # chunk-missing does NOT exist, so excluded
    # present = 3 (chunk-1, chunk-2, chunk-3)
    # cold = 1 (chunk-2)
    # pct = 100.0 * 1 / 3 = 33.3
    assert m["present"] == 3
    assert m["cold"] == 1
    assert m["pct"] == 33.3


def test_gold_docs_cold_marked_no_gold_cases(tmp_path, monkeypatch):
    """Test when _load_gold_cases is None (tests module unavailable)."""
    s = _seed_chunks_for_gold(tmp_path)

    monkeypatch.setattr(
        "mcpbrain.enrich_eval._load_gold_cases",
        None
    )

    m = gold_docs_cold_marked(s)

    assert m == {"present": 0, "cold": 0, "pct": 0.0}


def test_gold_docs_cold_marked_empty_store(tmp_path, monkeypatch):
    """Test with empty store: no chunks exist, so present=0."""
    s = Store(str(tmp_path / "empty.sqlite3"), dim=384)
    s.init()

    gold_fixture = [
        {
            "id": "case-1",
            "query": "test",
            "expected_chunk_ids": ["chunk-1", "chunk-2"]
        }
    ]

    monkeypatch.setattr(
        "mcpbrain.enrich_eval._load_gold_cases",
        lambda: gold_fixture
    )

    m = gold_docs_cold_marked(s)

    # None of the gold chunk ids exist
    assert m["present"] == 0
    assert m["cold"] == 0
    assert m["pct"] == 0.0


def test_gold_docs_cold_marked_all_cold(tmp_path, monkeypatch):
    """Test when all present chunks are marked cold: pct=100.0."""
    s = Store(str(tmp_path / "all_cold.sqlite3"), dim=384)
    s.init()
    with s._connect() as db:
        db.execute("INSERT INTO chunks(doc_id, text, content_hash, enrich_state) "
                   "VALUES('chunk-a', 'text a', 'hasha', 'cold')")
        db.execute("INSERT INTO chunks(doc_id, text, content_hash, enrich_state) "
                   "VALUES('chunk-b', 'text b', 'hashb', 'cold')")

    gold_fixture = [
        {
            "id": "case-1",
            "query": "test",
            "expected_chunk_ids": ["chunk-a", "chunk-b"]
        }
    ]

    monkeypatch.setattr(
        "mcpbrain.enrich_eval._load_gold_cases",
        lambda: gold_fixture
    )

    m = gold_docs_cold_marked(s)

    assert m["present"] == 2
    assert m["cold"] == 2
    assert m["pct"] == 100.0


def test_sender_coverage_metric(tmp_path):
    s = Store(str(tmp_path / "b.sqlite3"), dim=4)
    s.init()
    with s._connect() as db:
        db.execute("INSERT INTO entities(id,name,type,email_addr) VALUES('p1','A','person','a@x.org')")
        db.execute("INSERT INTO entities(id,name,type,email_addr) VALUES('p2','B','person','')")
        db.execute("INSERT INTO email_entities(message_id,entity_id,role) VALUES('m1','p1','authored')")
    m = graph_metrics(s)
    assert m["senders_as_entities_pct"] == 50.0   # 1 of 2 persons is an authored sender
