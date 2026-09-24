"""Corrections must survive the automated writers (Task 2)."""
from mcpbrain import graph_write as gw
from mcpbrain.store import Store


def _store(tmp_path):
    s = Store(tmp_path / "g.sqlite3", dim=4)
    s.init()
    with s._connect(write=True) as db:
        db.execute("INSERT INTO entities(id,name,type,org,email_addr) VALUES"
                   "('dana-okafor','Dana Okafor','person','Northgate Trust','dana@northgate.example')")
        db.execute("INSERT INTO entities(id,name,type) VALUES('northgate-trust','Northgate Trust','org')")
    return s


def _reject(s, a, rel, b):
    with s._connect(write=True) as db:
        db.execute("UPDATE entity_relations SET invalidated_at='2026-09-24T00:00:00Z', "
                   "superseded_reason='user_rejected', user_verdict='rejected' "
                   "WHERE entity_a=? AND relation=? AND entity_b=?", (a, rel, b))


def test_rejected_relation_is_not_revived_by_reextraction(tmp_path):
    s = _store(tmp_path)
    rid = gw.upsert_relation(s, "dana-okafor", "works_at", "northgate-trust",
                             valid_from="2026-01-01")
    _reject(s, "dana-okafor", "works_at", "northgate-trust")
    again = gw.upsert_relation(s, "dana-okafor", "works_at", "northgate-trust",
                               valid_from="2026-09-01")
    assert again == rid
    with s._connect() as db:
        row = db.execute("SELECT invalidated_at, user_verdict FROM entity_relations "
                         "WHERE id=?", (rid,)).fetchone()
    assert row["invalidated_at"] is not None and row["user_verdict"] == "rejected"


def test_unrejected_invalidated_relation_still_revives(tmp_path):
    """The existing revive behaviour is unchanged for non-user invalidations."""
    s = _store(tmp_path)
    rid = gw.upsert_relation(s, "dana-okafor", "works_at", "northgate-trust",
                             valid_from="2026-01-01")
    with s._connect(write=True) as db:
        db.execute("UPDATE entity_relations SET invalidated_at='x' WHERE id=?", (rid,))
    gw.upsert_relation(s, "dana-okafor", "works_at", "northgate-trust", valid_from="2026-09-01")
    with s._connect() as db:
        assert db.execute("SELECT invalidated_at FROM entity_relations WHERE id=?",
                          (rid,)).fetchone()[0] is None


def _lock(s, field):
    with s._connect(write=True) as db:
        db.execute("INSERT INTO entity_field_locks(entity_id,field) VALUES('dana-okafor',?)", (field,))


def test_locked_org_refuses_automated_write_but_accepts_user(tmp_path):
    s = _store(tmp_path)
    _lock(s, "org")
    assert s.update_entity_org("dana-okafor", "Southbank Community Trust") is False
    assert s.get_entity("dana-okafor")["org"] == "Northgate Trust"
    assert s.update_entity_org("dana-okafor", "Southbank Community Trust", user=True) is True
    assert s.get_entity("dana-okafor")["org"] == "Southbank Community Trust"


def test_locked_org_survives_bulk_rewrite_and_if_empty(tmp_path):
    s = _store(tmp_path)
    _lock(s, "org")
    assert s.rewrite_org_field("Northgate Trust", "The Lantern Co") == 0
    with s._connect(write=True) as db:
        db.execute("UPDATE entities SET org='' WHERE id='dana-okafor'")
    assert s.update_entity_org_if_empty("dana-okafor", "The Lantern Co") is False


def test_locked_name_and_email(tmp_path):
    s = _store(tmp_path)
    _lock(s, "name")
    _lock(s, "email")
    assert s.rename_entity("dana-okafor", "D. Okafor") is False
    assert s.set_entity_email("dana-okafor", "other@x.example") is False
    assert s.rename_entity("dana-okafor", "Dana A. Okafor", user=True) is True


def test_org_backfill_counts_only_real_updates(tmp_path, monkeypatch):
    from mcpbrain import orgs as _orgs
    from mcpbrain.org_backfill import run_backfill
    s = _store(tmp_path)
    with s._connect(write=True) as db:
        db.execute("UPDATE entities SET org='' WHERE id='dana-okafor'")
    _lock(s, "org")  # the user deliberately cleared it
    monkeypatch.setattr(_orgs, "taxonomy_from_config", lambda: _orgs.OrgTaxonomy(
        names=("Northgate Trust",), domain_map={"northgate.example": "Northgate Trust"}))
    assert run_backfill(s)["updated"] == 0
    assert s.get_entity("dana-okafor")["org"] == ""


def test_graph_ui_edit_is_a_user_write(tmp_path):
    from mcpbrain import graph_view
    s = _store(tmp_path)
    _lock(s, "org")
    graph_view.update_entity(s, "dana-okafor", org="The Lantern Co")
    assert s.get_entity("dana-okafor")["org"] == "The Lantern Co"
