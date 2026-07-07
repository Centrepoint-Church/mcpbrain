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
    # multi-select: several types union together (the type-chip filter's contract)
    assert {n["id"] for n in graph_view.graph_canvas(_Store(p), min_conn=1, types=["person","org"])["nodes"]} == {"e1", "e2", "e3"}


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
    assert all("degree" in r for r in graph_view.search_entities(_Store(p), "a"))  # degree returned

def test_search_ranks_exact_then_prefix_then_degree(tmp_path):
    p = tmp_path / "rank.sqlite3"
    with sqlite3.connect(str(p)) as db:
        db.execute("CREATE TABLE entities(id TEXT PRIMARY KEY, name TEXT, type TEXT, "
                   "org TEXT DEFAULT '', aliases TEXT DEFAULT '', degree INTEGER DEFAULT 0)")
        db.execute("INSERT INTO entities VALUES('c','Big Alexander','person','','',99)")  # contains (not prefix), high degree
        db.execute("INSERT INTO entities VALUES('b','Alex Jones','person','','',1)")      # prefix 'Alex '
        db.execute("INSERT INTO entities VALUES('a','Alex','person','','',1)")            # exact
    order = [r["id"] for r in graph_view.search_entities(_Store(p), "Alex")]
    assert order[0] == "a"        # exact wins
    assert order.index("b") < order.index("c")   # prefix beats a higher-degree contains-only match

def test_search_matches_aliases(tmp_path):
    p = tmp_path / "alias.sqlite3"
    with sqlite3.connect(str(p)) as db:
        db.execute("CREATE TABLE entities(id TEXT PRIMARY KEY, name TEXT, type TEXT, "
                   "org TEXT DEFAULT '', aliases TEXT DEFAULT '', degree INTEGER DEFAULT 0)")
        db.execute("INSERT INTO entities VALUES('jk','Josh Kemp','person','','J. Kemp|JK',5)")
    hits = graph_view.search_entities(_Store(p), "J. Kemp")   # old, merged-away name
    assert hits and hits[0]["id"] == "jk" and hits[0]["via_alias"] is True


def _rw_store(tmp_path):
    from mcpbrain.store import Store
    s = Store(tmp_path / "rw.sqlite3", dim=4); s.init()
    with s._connect() as db:
        db.executemany("INSERT INTO entities(id,name,type,org,email_addr) VALUES(?,?,?,?,?)", [
            ("alice","Alice","person","Acme","alice@acme.com"),
            ("al","Alice A.","person","","alice@acme.com"),
            ("office","Office","person","Acme","office@acme.com"),
            ("front","Front Desk","person","Acme","office@acme.com")])
    return s

def test_update_entity_applies_fields(tmp_path):
    s = _rw_store(tmp_path)
    d = graph_view.update_entity(s, "alice", name="Alice Smith", org="Beta", notes="vip")
    assert d["name"] == "Alice Smith" and d["org"] == "Beta" and d["notes"] == "vip"
    assert "Alice" in s.get_entity("alice")["aliases"].split("|")

def test_update_entity_unknown(tmp_path):
    s = _rw_store(tmp_path)
    assert graph_view.update_entity(s, "nope", name="X") is None

def test_merge_ok(tmp_path):
    s = _rw_store(tmp_path)
    out = graph_view.merge_entities(s, "al", "alice")   # equal connectivity -> caller's winner honoured
    assert out["ok"] is True
    assert s.get_entity("al") is None and s.get_entity("alice") is not None
    assert out["winner_id"] == "alice" and out["loser_id"] == "al"

def test_merge_keeps_more_connected_regardless_of_arg_order(tmp_path):
    """The survivor is the more-connected entity even when it's passed as the
    loser — picking either side in the UI must not throw away the richer node."""
    s = _rw_store(tmp_path)
    with s._connect() as db:
        db.execute("UPDATE entities SET degree=25 WHERE id='al'")     # 'al' is the hub
        db.execute("UPDATE entities SET degree=2 WHERE id='alice'")
    # Caller nominates the small node ('alice') as winner; degree must override.
    out = graph_view.merge_entities(s, "al", "alice")
    assert out["ok"] is True
    assert out["winner_id"] == "al" and out["loser_id"] == "alice"
    assert s.get_entity("al") is not None and s.get_entity("alice") is None
    assert "Alice" in (s.get_entity("al")["aliases"] or "").split("|")  # loser name kept as alias

def test_merge_tie_honours_caller_pick(tmp_path):
    s = _rw_store(tmp_path)
    with s._connect() as db:
        db.execute("UPDATE entities SET degree=5 WHERE id IN ('al','alice')")  # equal
    out = graph_view.merge_entities(s, "alice", "al")   # caller keeps 'al'
    assert out["winner_id"] == "al" and s.get_entity("alice") is None

def test_merge_field_level_best_of(tmp_path):
    """Survivor id follows degree, but each field is taken best-of: the fuller
    name, a real email over a blank one, and unioned notes — nothing dropped."""
    from mcpbrain.store import Store
    s = Store(tmp_path / "m.sqlite3", dim=4); s.init()
    with s._connect() as db:
        # 'jk' is the hub (more connected) but has the worse name + no email/notes.
        db.execute("INSERT INTO entities(id,name,type,org,email_addr,notes,degree) "
                   "VALUES('jk','JK','person','',' ',NULL,20)")
        db.execute("INSERT INTO entities(id,name,type,org,email_addr,notes,degree) "
                   "VALUES('josh','Josh Kemp','person','Acme','josh@acme.com','vip client',2)")
    out = graph_view.merge_entities(s, "josh", "jk")   # caller nominates hub 'jk' as winner
    assert out["ok"] and out["winner_id"] == "jk"       # hub survives (id stable)
    kept = s.get_entity("jk")
    assert kept["name"] == "Josh Kemp"                   # fuller name won, not 'JK'
    assert kept["email_addr"] == "josh@acme.com"         # real email pulled from loser
    assert "vip client" in (kept["notes"] or "")         # notes carried over
    assert kept["org"] == "Acme"                          # non-empty org filled in
    assert s.get_entity("josh") is None

def test_merge_preview_is_non_mutating(tmp_path):
    s = _rw_store(tmp_path)
    with s._connect() as db:
        db.execute("UPDATE entities SET degree=9 WHERE id='alice'")
        db.execute("UPDATE entities SET degree=1 WHERE id='al'")
    p = graph_view.merge_preview(s, "al", "alice")
    assert p["ok"] and p["winner_id"] == "alice" and p["loser_id"] == "al"
    assert "name" in p["result"] and "email_addr" in p["result"]
    assert s.get_entity("al") is not None and s.get_entity("alice") is not None  # nothing changed

def test_merge_name_override(tmp_path):
    s = _rw_store(tmp_path)
    out = graph_view.merge_entities(s, "al", "alice", name_override="Alice Cooper")
    assert out["ok"] and s.get_entity("alice")["name"] == "Alice Cooper"

def test_merge_self_refused(tmp_path):
    s = _rw_store(tmp_path)
    out = graph_view.merge_entities(s, "alice", "alice")
    assert out["ok"] is False and out["error"] == "self_merge"

def test_merge_role_inbox_refused(tmp_path):
    s = _rw_store(tmp_path)
    out = graph_view.merge_entities(s, "front", "office")   # both office@ -> role address
    assert out["ok"] is False and out["error"] == "role_inbox"
    assert s.get_entity("front") is not None                # not merged

def test_suppress(tmp_path):
    s = _rw_store(tmp_path)
    assert graph_view.suppress_entity(s, "al")["ok"] is True
    assert graph_view.entity_detail(s, "al") is None


def test_search_escapes_like_wildcards(tmp_path):
    p = tmp_path / "sw.sqlite3"
    with sqlite3.connect(str(p)) as db:
        db.execute("CREATE TABLE entities(id TEXT PRIMARY KEY, name TEXT, type TEXT, org TEXT DEFAULT '')")
        db.execute("INSERT INTO entities VALUES('a','50% off','person','')")
        db.execute("INSERT INTO entities VALUES('b','50X off','person','')")
    hits = {r["name"] for r in graph_view.search_entities(_Store(p), "50%")}
    assert hits == {"50% off"}   # % matched literally, not as a wildcard


def test_graph_canvas_nodes_include_origin(tmp_path):
    from mcpbrain.store import Store
    s = Store(tmp_path / "g.sqlite3", dim=4); s.init()
    with s._connect() as db:
        db.execute("INSERT INTO entities(id,name,type,degree,origin) VALUES('a','Alice','person',9,'local')")
        db.execute("INSERT INTO entities(id,name,type,degree,origin) VALUES('b','Acme','org',9,'org')")
    canvas = graph_view.graph_canvas(s, min_conn=1)
    by_id = {n["id"]: n for n in canvas["nodes"]}
    assert by_id["a"]["origin"] == "local"
    assert by_id["b"]["origin"] == "org"


def test_search_excludes_suppressed(tmp_path):
    p = tmp_path / "ss.sqlite3"
    with sqlite3.connect(str(p)) as db:
        db.execute("CREATE TABLE entities(id TEXT PRIMARY KEY, name TEXT, type TEXT, org TEXT DEFAULT '')")
        db.execute("CREATE TABLE entity_suppressions(entity_id TEXT PRIMARY KEY, reason TEXT, suppressed_at TEXT)")
        db.execute("INSERT INTO entities VALUES('a','Alice','person','')")
        db.execute("INSERT INTO entity_suppressions VALUES('a','x','t')")
    assert graph_view.search_entities(_Store(p), "Alice") == []


def test_ego_includes_low_degree_entity_and_hop1_neighbour(tmp_path):
    """graph_canvas(min_conn=7) would drop e3 entirely; graph_ego must still
    surface it plus its direct neighbour, ignoring the degree floor."""
    p = tmp_path / "b.sqlite3"
    _seed(p,
          entities=[("e1", "Alice", "person", "", 9, ""),
                    ("e2", "Bob", "person", "", 8, ""),
                    ("e3", "Low", "person", "", 1, "")],
          relations=[("e1", "e2", "works_with", 5), ("e2", "e3", "knows", 3)])
    out = graph_view.graph_ego(_Store(p), "e3", hops=1)
    assert {n["id"] for n in out["nodes"]} == {"e2", "e3"}
    assert out["links"] == [{"source": "e2", "target": "e3", "relation": "knows", "strength": 3}]


def test_ego_hops_two_reaches_further(tmp_path):
    p = tmp_path / "b.sqlite3"
    _seed(p,
          entities=[("e1", "Alice", "person", "", 9, ""),
                    ("e2", "Bob", "person", "", 8, ""),
                    ("e3", "Low", "person", "", 1, "")],
          relations=[("e1", "e2", "works_with", 5), ("e2", "e3", "knows", 3)])
    out = graph_view.graph_ego(_Store(p), "e3", hops=2)
    assert {n["id"] for n in out["nodes"]} == {"e1", "e2", "e3"}


def test_ego_hops_zero_is_just_the_entity(tmp_path):
    p = tmp_path / "b.sqlite3"
    _seed(p, entities=[("e1", "Alice", "person", "", 9, ""), ("e2", "Bob", "person", "", 8, "")],
          relations=[("e1", "e2", "knows", 5)])
    out = graph_view.graph_ego(_Store(p), "e1", hops=0)
    assert {n["id"] for n in out["nodes"]} == {"e1"}
    assert out["links"] == []


def test_ego_unknown_entity_is_none(tmp_path):
    p = tmp_path / "b.sqlite3"
    _seed(p, entities=[("e1", "Alice", "person", "", 9, "")], relations=[])
    assert graph_view.graph_ego(_Store(p), "nope") is None


def test_ego_suppressed_entity_is_none(tmp_path):
    p = tmp_path / "b.sqlite3"
    _seed(p, entities=[("e1", "Alice", "person", "", 9, "")], relations=[], suppressed=["e1"])
    assert graph_view.graph_ego(_Store(p), "e1") is None


def test_ego_without_suppressions_table(tmp_path):
    """Mirrors test_canvas_without_suppressions_table: real stores predating the
    suppress/delete feature must still work, not error out."""
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
        db.execute("INSERT INTO entities(id,name,type,degree) VALUES('e1','Alice','person',1)")
        db.execute("INSERT INTO entities(id,name,type,degree) VALUES('e2','Bob','person',1)")
        db.execute("INSERT INTO entity_relations(id,entity_a,relation,entity_b,strength) "
                   "VALUES(0,'e1','knows','e2',4)")
    out = graph_view.graph_ego(_Store(p), "e1", hops=1)
    assert {n["id"] for n in out["nodes"]} == {"e1", "e2"}
