from mcpbrain import graph_write, resolve
from mcpbrain.store import Store


def _store(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    return s


def test_local_never_merges_org_org(tmp_path, monkeypatch):
    monkeypatch.setattr("mcpbrain.config.write_time_dedup_enabled", lambda h: True)
    s = _store(tmp_path)
    with s._connect() as db:                         # two org rows, same canonical name
        db.execute("INSERT INTO entities(id,name,type,origin,mentions) VALUES('acme','Acme','org','org',5)")
        db.execute("INSERT INTO entities(id,name,type,origin,mentions) VALUES('acme-inc','Acme','org','org',2)")
    resolve.resolve_entities(s, home=str(tmp_path))   # local (curator=False)
    assert s.get_entity("acme") is not None and s.get_entity("acme-inc") is not None


def test_curator_bypass_merges_org_org(tmp_path):
    s = _store(tmp_path)
    with s._connect() as db:
        db.execute("INSERT INTO entities(id,name,type,origin,mentions) VALUES('acme','Acme','org','org',5)")
        db.execute("INSERT INTO entities(id,name,type,origin,mentions) VALUES('acme-inc','Acme','org','org',2)")
    resolve.resolve_entities(s, home=str(tmp_path), curator=True)
    survivors = [e for e in (s.get_entity("acme"), s.get_entity("acme-inc")) if e]
    assert len(survivors) == 1                        # curator dedups org layer


def test_local_org_merge_org_survives(tmp_path, monkeypatch):
    monkeypatch.setattr("mcpbrain.config.write_time_dedup_enabled", lambda h: True)
    s = _store(tmp_path)
    with s._connect() as db:                          # local has MORE mentions, but org must win
        db.execute("INSERT INTO entities(id,name,type,email_addr,origin,mentions) "
                   "VALUES('joel-local','Joel','person','joel@x.org','local',9)")
        db.execute("INSERT INTO entities(id,name,type,email_addr,origin,mentions) "
                   "VALUES('joel-org','Joel','person','joel@x.org','org',1)")
    resolve.resolve_entities(s, home=str(tmp_path))
    assert s.get_entity("joel-org") is not None        # org survivor
    assert s.get_entity("joel-local") is None


def test_upsert_never_overwrites_org_skeleton(tmp_path):
    s = _store(tmp_path)
    with s._connect() as db:
        db.execute("INSERT INTO entities(id,name,type,org,email_addr,origin) "
                   "VALUES('joel','Joel Chelliah','person','Acme','joel@acme.org','org')")
    graph_write.upsert_entity(s, name="Joel Chelliah", entity_type="person",
                              org="Beta", email_addr="joel@beta.org", notes="local note")
    e = s.get_entity("joel")
    assert e["org"] == "Acme" and e["email_addr"] == "joel@acme.org"   # skeleton unchanged
    assert "local note" in (e["notes"] or "")                          # flesh added
