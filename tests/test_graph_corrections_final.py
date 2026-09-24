"""Final-review fixes: corrections survive the ACTUAL automated writers.

Every test here drives the real writer (resolve, graph_write, profile_audit,
org_import, org_curate, graph_cleanup, review_apply) against a real Store --
not only the Store setters, which is the seam earlier tests exercised.
"""
from mcpbrain import graph_corrections as gc
from mcpbrain.store import Store


def _store(tmp_path):
    s = Store(tmp_path / "f.sqlite3", dim=4)
    s.init()
    return s


def _ent(s, eid, name, type_="person", *, org="", email="", mentions=1, origin="local",
         aliases=""):
    with s._connect(write=True) as db:
        db.execute("INSERT INTO entities(id,name,type,org,email_addr,mentions,origin,aliases) "
                   "VALUES(?,?,?,?,?,?,?,?)", (eid, name, type_, org, email, mentions, origin,
                                                aliases))


def _distinct(s, a, b):
    with s._connect(write=True) as db:
        db.execute("INSERT INTO entity_distinct_pairs(a,b) VALUES(?,?)", tuple(sorted((a, b))))


def _lock(s, eid, field):
    with s._connect(write=True) as db:
        db.execute("INSERT INTO entity_field_locks(entity_id,field) VALUES(?,?)", (eid, field))


def _ids(s):
    with s._connect() as db:
        return {r[0] for r in db.execute("SELECT id FROM entities")}


# --- C1: not_same is checked LIVE inside the resolve loops -------------------

def test_c1_deterministic_merge_respects_not_same_transitively(tmp_path):
    from mcpbrain.resolve import _deterministic_merges
    s = _store(tmp_path)
    _ent(s, "dana-a", "Dana Okafor", mentions=1)
    _ent(s, "dana-b", "Dana Okafor", mentions=2)
    _ent(s, "dana-c", "Dana Okafor", mentions=9)   # survivor
    _distinct(s, "dana-a", "dana-b")
    _deterministic_merges(s)
    ids = _ids(s)
    assert "dana-c" in ids
    # A folded into C, so (A,B) became (B,C): B must NOT then merge into C.
    assert "dana-b" in ids
    assert s.is_distinct_pair("dana-b", "dana-c")


def test_c1_email_merge_respects_not_same_transitively(tmp_path):
    from mcpbrain.resolve import _email_equality_merges
    s = _store(tmp_path)
    _ent(s, "dana-a", "Dana Okafor", email="dana@northgate.example", mentions=1)
    _ent(s, "dana-b", "D Okafor", email="dana@northgate.example", mentions=2)
    _ent(s, "dana-c", "Dana O", email="dana@northgate.example", mentions=9)
    _distinct(s, "dana-a", "dana-b")
    _email_equality_merges(s, home=tmp_path)
    ids = _ids(s)
    assert "dana-c" in ids and "dana-b" in ids
    assert s.is_distinct_pair("dana-b", "dana-c")
