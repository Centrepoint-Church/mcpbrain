import sqlite3
from mcpbrain import graph_view


class _Store:
    def __init__(self, path):
        self._path = str(path)


def test_canvas_without_suppressions_table(tmp_path):
    """Real stores predate the suppress/delete feature, so entity_suppressions
    often doesn't exist. The graph must still render, not degrade to empty."""
    p = tmp_path / "no_supp.sqlite3"
    with sqlite3.connect(str(p)) as db:
        db.execute("CREATE TABLE entities(id TEXT PRIMARY KEY, name TEXT, type TEXT, "
                   "org TEXT DEFAULT '', first_seen TEXT DEFAULT '', last_seen TEXT DEFAULT '', "
                   "email_count INTEGER DEFAULT 0, email_addr TEXT DEFAULT '', degree INTEGER DEFAULT 0)")
        db.execute("CREATE TABLE entity_relations(id INTEGER PRIMARY KEY, entity_a TEXT, "
                   "relation TEXT, entity_b TEXT, strength REAL DEFAULT 1)")
        db.execute("CREATE TABLE entity_communities(entity_id TEXT, community_id INTEGER, level INTEGER)")
        db.execute("CREATE TABLE community_summaries(community_id INTEGER, level INTEGER, title TEXT, "
                   "summary TEXT, member_count INTEGER, key_entities TEXT, updated TEXT)")
        db.execute("INSERT INTO entities(id,name,type,degree) VALUES('e1','Alice','person',9)")
        db.execute("INSERT INTO entities(id,name,type,degree) VALUES('e2','Bob','person',8)")
        db.execute("INSERT INTO entity_relations(id,entity_a,relation,entity_b,strength) "
                   "VALUES(0,'e1','knows','e2',4)")
    out = graph_view.graph_canvas(_Store(p), min_conn=7)
    assert {n["id"] for n in out["nodes"]} == {"e1", "e2"}
    assert len(out["links"]) == 1


def _seed(path, entities, relations, *, suppressed=(), communities=(), summaries=()):
    """entities: list of (id, name, type, org, degree, last_seen).
       relations: list of (a, b, relation, strength).
       communities: list of (entity_id, community_id, level).
       summaries: list of (community_id, level, title)."""
    with sqlite3.connect(str(path)) as db:
        db.execute("CREATE TABLE entities(id TEXT PRIMARY KEY, name TEXT, type TEXT, "
                   "org TEXT DEFAULT '', first_seen TEXT DEFAULT '', last_seen TEXT DEFAULT '', "
                   "email_count INTEGER DEFAULT 0, email_addr TEXT DEFAULT '', degree INTEGER DEFAULT 0)")
        db.execute("CREATE TABLE entity_relations(id INTEGER PRIMARY KEY, entity_a TEXT, "
                   "relation TEXT, entity_b TEXT, strength REAL DEFAULT 1)")
        db.execute("CREATE TABLE entity_suppressions(entity_id TEXT PRIMARY KEY, reason TEXT, suppressed_at TEXT)")
        db.execute("CREATE TABLE entity_communities(entity_id TEXT, community_id INTEGER, level INTEGER)")
        db.execute("CREATE TABLE community_summaries(community_id INTEGER, level INTEGER, title TEXT, "
                   "summary TEXT, member_count INTEGER, key_entities TEXT, updated TEXT)")
        for (eid, name, typ, org, degree, last_seen) in entities:
            db.execute("INSERT INTO entities(id,name,type,org,degree,last_seen) VALUES(?,?,?,?,?,?)",
                       (eid, name, typ, org, degree, last_seen))
        for i, (a, b, rel, st) in enumerate(relations):
            db.execute("INSERT INTO entity_relations(id,entity_a,relation,entity_b,strength) VALUES(?,?,?,?,?)",
                       (i, a, rel, b, st))
        for eid in suppressed:
            db.execute("INSERT INTO entity_suppressions(entity_id,reason,suppressed_at) VALUES(?,?,?)",
                       (eid, "test", "2026-01-01"))
        for row in communities:
            db.execute("INSERT INTO entity_communities(entity_id,community_id,level) VALUES(?,?,?)", row)
        for row in summaries:
            db.execute("INSERT INTO community_summaries(community_id,level,title,summary,member_count,key_entities,updated) "
                       "VALUES(?,?,?,?,?,?,?)", (row[0], row[1], row[2], "", 0, "", ""))


def test_canvas_nodes_and_links(tmp_path):
    p = tmp_path / "b.sqlite3"
    _seed(p,
          entities=[("e1", "Alice", "person", "Acme", 9, "2026-06-01"),
                    ("e2", "Bob", "person", "Acme", 8, "2026-06-02"),
                    ("e3", "Low", "person", "", 2, "2026-06-03")],
          relations=[("e1", "e2", "works_with", 5), ("e1", "e3", "knows", 3)])
    out = graph_view.graph_canvas(_Store(p), min_conn=7)
    ids = {n["id"] for n in out["nodes"]}
    assert ids == {"e1", "e2"}                       # e3 (degree 2) filtered out
    assert out["links"] == [{"source": "e1", "target": "e2",
                             "relation": "works_with", "strength": 5}]  # e1-e3 dropped (e3 not a node)
    n = next(n for n in out["nodes"] if n["id"] == "e1")
    assert n["name"] == "Alice" and n["type"] == "person" and n["connections"] == 9


def test_canvas_excludes_suppressed(tmp_path):
    p = tmp_path / "b.sqlite3"
    _seed(p,
          entities=[("e1", "Alice", "person", "", 9, ""), ("e2", "Bob", "person", "", 9, "")],
          relations=[], suppressed=["e2"])
    out = graph_view.graph_canvas(_Store(p), min_conn=1)
    assert {n["id"] for n in out["nodes"]} == {"e1"}


def test_canvas_org_and_type_filters(tmp_path):
    p = tmp_path / "b.sqlite3"
    _seed(p, entities=[("e1", "Alice", "person", "Acme", 9, ""),
                       ("e2", "Beta Co", "org", "Acme", 9, ""),
                       ("e3", "Carol", "person", "Other", 9, "")],
          relations=[])
    assert {n["id"] for n in graph_view.graph_canvas(_Store(p), min_conn=1, org="Acme")["nodes"]} == {"e1", "e2"}
    assert {n["id"] for n in graph_view.graph_canvas(_Store(p), min_conn=1, types=["person"])["nodes"]} == {"e1", "e3"}


def test_canvas_communities(tmp_path):
    p = tmp_path / "b.sqlite3"
    _seed(p, entities=[("e1", "Alice", "person", "", 9, "")], relations=[],
          communities=[("e1", 3, 0)], summaries=[(3, 0, "Leadership")])
    out = graph_view.graph_canvas(_Store(p), min_conn=1)
    assert out["communities"] == {"3": "Leadership"}
    assert out["nodes"][0]["community"] == 3


def test_canvas_too_large(tmp_path):
    p = tmp_path / "b.sqlite3"
    ents = [(f"e{i}", f"N{i}", "person", "", 9, "") for i in range(5001)]
    _seed(p, entities=ents, relations=[])
    out = graph_view.graph_canvas(_Store(p), min_conn=1)
    assert out.get("error") == "too_large" and out["cap"] == 5000


def test_canvas_degrades_on_bad_store(tmp_path):
    p = tmp_path / "empty.sqlite3"
    with sqlite3.connect(str(p)):
        pass  # no tables
    out = graph_view.graph_canvas(_Store(p), min_conn=1)
    assert out == {"nodes": [], "links": [], "communities": {}}


def test_canvas_degrades_on_corrupt_file(tmp_path):
    p = tmp_path / "corrupt.sqlite3"
    p.write_bytes(b"not a database")
    out = graph_view.graph_canvas(_Store(p), min_conn=1)
    assert out == {"nodes": [], "links": [], "communities": {}}


def test_canvas_dedupes_multiple_level0_communities(tmp_path):
    p = tmp_path / "b.sqlite3"
    _seed(p, entities=[("e1", "Alice", "person", "", 9, "")], relations=[],
          communities=[("e1", 3, 0), ("e1", 7, 0)])
    out = graph_view.graph_canvas(_Store(p), min_conn=1)
    assert len([n for n in out["nodes"] if n["id"] == "e1"]) == 1


def test_canvas_too_large_reports_true_count(tmp_path):
    p = tmp_path / "b.sqlite3"
    ents = [(f"e{i}", f"N{i}", "person", "", 9, "") for i in range(6000)]
    _seed(p, entities=ents, relations=[])
    out = graph_view.graph_canvas(_Store(p), min_conn=1)
    assert out.get("error") == "too_large" and out["cap"] == 5000
    assert out["candidate_count"] == 6000


def _seed_detail(path):
    import sqlite3
    with sqlite3.connect(str(path)) as db:
        db.execute("CREATE TABLE entities(id TEXT PRIMARY KEY, name TEXT, type TEXT, "
                   "org TEXT DEFAULT '', email_addr TEXT DEFAULT '', aliases TEXT DEFAULT '', "
                   "notes TEXT DEFAULT '', degree INTEGER DEFAULT 0)")
        db.execute("CREATE TABLE entity_relations(id INTEGER PRIMARY KEY, entity_a TEXT, "
                   "relation TEXT, entity_b TEXT, strength REAL DEFAULT 1, invalidated_at TEXT)")
        db.execute("CREATE TABLE entity_observations(id INTEGER PRIMARY KEY, entity_id TEXT, "
                   "attribute TEXT, value TEXT, source TEXT, valid_from TEXT, valid_to TEXT)")
        db.execute("CREATE TABLE entity_suppressions(entity_id TEXT PRIMARY KEY, reason TEXT, suppressed_at TEXT)")
        db.executemany("INSERT INTO entities(id,name,type,org,email_addr,degree) VALUES(?,?,?,?,?,?)", [
            ("e1","Alice","person","Acme","alice@acme.com",5),
            ("e2","Bob","person","Acme","",3),
            ("e3","Acme","org","","",9)])
        db.execute("INSERT INTO entity_relations(id,entity_a,relation,entity_b,strength) VALUES(1,'e1','works_at','e3',4)")
        db.execute("INSERT INTO entity_relations(id,entity_a,relation,entity_b,strength) VALUES(2,'e2','manages','e1',2)")
        db.execute("INSERT INTO entity_observations(id,entity_id,attribute,value,valid_from) VALUES(1,'e1','role','Lead','2026-01')")

def test_entity_detail_shape(tmp_path):
    p = tmp_path / "b.sqlite3"; _seed_detail(p)
    d = graph_view.entity_detail(_Store(p), "e1")
    assert d["name"] == "Alice" and d["org"] == "Acme" and d["email_addr"] == "alice@acme.com"
    assert {r["other_id"] for r in d["relations"]} == {"e3"}          # e1 -> e3 (out)
    assert d["relations"][0]["other_name"] == "Acme" and d["relations"][0]["relation"] == "works_at"
    assert {b["other_id"] for b in d["backlinks"]} == {"e2"}          # e2 -> e1 (in)
    assert d["observations"][0]["attribute"] == "role"

def test_entity_detail_unknown_is_none(tmp_path):
    p = tmp_path / "b.sqlite3"; _seed_detail(p)
    assert graph_view.entity_detail(_Store(p), "nope") is None

def test_entity_detail_suppressed_is_none(tmp_path):
    p = tmp_path / "b.sqlite3"; _seed_detail(p)
    import sqlite3
    with sqlite3.connect(str(p)) as db:
        db.execute("INSERT INTO entity_suppressions(entity_id,reason,suppressed_at) VALUES('e1','x','t')")
    assert graph_view.entity_detail(_Store(p), "e1") is None

def test_search_entities(tmp_path):
    p = tmp_path / "b.sqlite3"; _seed_detail(p)
    ids = {r["id"] for r in graph_view.search_entities(_Store(p), "a")}
    assert "e1" in ids and "e3" in ids          # Alice, Acme
    assert graph_view.search_entities(_Store(p), "") == []
