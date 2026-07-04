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


def test_curator_bypass_still_forces_org_survivor_over_local(tmp_path, monkeypatch):
    """Rule 2 must hold even during the curator's own dedup pass (curator=True):
    curator only controls whether org<->org groups get merged at all (rule 1);
    it must never also let a local row with more mentions outrank an org row
    in the same group, since a curator install is a normal member machine too
    and can have its own local duplicate collide with an org row."""
    monkeypatch.setattr("mcpbrain.config.write_time_dedup_enabled", lambda h: True)
    s = _store(tmp_path)
    with s._connect() as db:                          # local has FAR more mentions
        db.execute("INSERT INTO entities(id,name,type,email_addr,origin,mentions) "
                   "VALUES('joel-local','Joel','person','joel@x.org','local',99)")
        db.execute("INSERT INTO entities(id,name,type,email_addr,origin,mentions) "
                   "VALUES('joel-org','Joel','person','joel@x.org','org',1)")
    resolve.resolve_entities(s, home=str(tmp_path), curator=True)
    assert s.get_entity("joel-org") is not None        # org survivor, even under curator=True
    assert s.get_entity("joel-local") is None


def test_emit_merge_candidates_uses_passed_home_not_live_app_dir(tmp_path, monkeypatch):
    """_emit_merge_candidates must use the home the caller resolved, not
    silently re-derive a live, possibly-different machine's config directory
    — a caller for a non-default home must never have the emit decision
    depend on some other store's fleet-pin config."""
    from mcpbrain import config
    unpinned_home = tmp_path / "not-the-caller-home"
    unpinned_home.mkdir()
    monkeypatch.setattr(config, "app_dir", lambda: unpinned_home)
    pinned_home = tmp_path / "pinned-home"
    pinned_home.mkdir()
    config.write_config(str(pinned_home), {"owner_email": "alice@pinned.org",
                                           "org_config": {"org_pin": {"fleet_secret": "s3cret"}}})
    s = _store(tmp_path)
    with s._connect() as db:                          # two org rows, same canonical name
        db.execute("INSERT INTO entities(id,name,type,origin,mentions) VALUES('acme','Acme','org','org',5)")
        db.execute("INSERT INTO entities(id,name,type,origin,mentions) VALUES('acme-inc','Acme','org','org',2)")
    resolve.resolve_entities(s, home=str(pinned_home))   # local (curator=False)
    with s._connect() as db:
        rows = db.execute("SELECT record FROM org_contrib_outbox").fetchall()
    assert len(rows) == 1   # emitted using the passed pinned_home, not the unpinned app_dir()
    from mcpbrain.org_contracts import ContributionRecord, source_ref
    import json
    rec = ContributionRecord.from_dict(json.loads(rows[0]["record"]))  # round-trips cleanly
    assert rec.claim == {"kind": "merge_candidate", "a": "acme", "b": "acme-inc"}
    assert rec.contributor_email == "alice@pinned.org"   # from pinned_home's config, not app_dir()
    assert rec.source_kind == "local"
    # HMAC-SHA256 over the pinned_home's own fleet_secret, not the raw id pair —
    # confirms the emitted claim contains no plaintext identifier.
    assert rec.source_ref == source_ref("s3cret", "acme|acme-inc")


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
