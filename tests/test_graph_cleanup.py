"""Graph hygiene: the org-tag prefix-fold + the one-off cleanup pass."""
from types import MappingProxyType

from mcpbrain import orgs
from mcpbrain.maintenance.graph_cleanup import cleanup_graph, recompute_singletons
from mcpbrain.store import Store


def _tax():
    return orgs.OrgTaxonomy(names=("Centrepoint Church", "Courageous Church", "ACC", "ACCI"),
                            domain_map=MappingProxyType({}), aliases=MappingProxyType({}))


def test_canonical_unambiguous_prefix_fold():
    t = _tax()
    assert t.canonical("Centrepoint") == "Centrepoint Church"      # bare short form folds
    assert t.canonical("centrepoint") == "Centrepoint Church"      # case-insensitive
    assert t.canonical("Centrepoint Church Inc.") == "Centrepoint Church"  # over-long folds back
    assert t.canonical("Courageous") == "Courageous Church"
    assert t.canonical("ACC") == "ACC"          # NOT folded into ACCI (shared prefix, ambiguous-safe)
    assert t.canonical("ACCI") == "ACCI"
    assert t.canonical("Random Co") == "Random Co"                 # unknown passes through


def _store(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    return s


def _ent(db, eid, name, etype, org=""):
    db.execute("INSERT INTO entities(id,name,type,org) VALUES(?,?,?,?)", (eid, name, etype, org))


def _rel(db, a, rel, b):
    db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b,valid_from) "
               "VALUES(?,?,?,?)", (a, rel, b, "2026-01-01"))


def test_cleanup_removes_self_loops_type_invalid_and_folds_orgs(tmp_path):
    s = _store(tmp_path)
    with s._connect() as db:
        _ent(db, "p1", "Ann", "person", "Centrepoint")     # org to be folded
        _ent(db, "o1", "Acme", "org", "external")
        _ent(db, "t1", "Budget", "topic")
        _rel(db, "p1", "works_at", "o1")                   # valid: person -> org (keep)
        _rel(db, "t1", "works_at", "o1")                   # invalid: topic -> org (drop)
        _rel(db, "p1", "works_at", "p1")                   # self-loop (drop)
        _rel(db, "p1", "mentioned_with", "t1")             # unconstrained relation (keep)

    counts = cleanup_graph(s, taxonomy=_tax())
    assert counts == {"self_loops": 1, "type_invalid": 1, "orgs_folded": 1}

    with s._connect() as db:
        rels = {(r[0], r[1], r[2]) for r in
                db.execute("SELECT entity_a,relation,entity_b FROM entity_relations").fetchall()}
        org = db.execute("SELECT org FROM entities WHERE id='p1'").fetchone()[0]
    assert rels == {("p1", "works_at", "o1"), ("p1", "mentioned_with", "t1")}
    assert org == "Centrepoint Church"

    # idempotent — a second pass changes nothing
    assert cleanup_graph(s, taxonomy=_tax()) == {"self_loops": 0, "type_invalid": 0, "orgs_folded": 0}


def test_recompute_singletons_makes_newest_current(tmp_path):
    # Backfill left an OLDER works_at marked current and the NEWER one superseded.
    # The recompute must flip them so the newest valid_from is current. Idempotent.
    s = _store(tmp_path)
    with s._connect() as db:
        _ent(db, "p1", "Ann", "person")
        _ent(db, "old", "OldCo", "org")
        _ent(db, "new", "NewCo", "org")
        # WRONG state: old (2020) current, new (2025) superseded
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b,valid_from) "
                   "VALUES('p1','works_at','old','2020-01-01')")
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b,valid_from,"
                   "invalidated_at,valid_to) VALUES('p1','works_at','new','2025-01-01','x','2020-01-01')")
    counts = recompute_singletons(s)
    assert counts["singletons_recomputed"] == 2
    with s._connect() as db:
        rows = {r["entity_b"]: r for r in db.execute(
            "SELECT entity_b,invalidated_at FROM entity_relations WHERE entity_a='p1'").fetchall()}
    assert rows["new"]["invalidated_at"] is None        # newest is now current
    assert rows["old"]["invalidated_at"] is not None    # older retired
    # idempotent
    assert recompute_singletons(s) == {"singletons_recomputed": 0}
