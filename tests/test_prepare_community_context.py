from mcpbrain import prepare
from mcpbrain.store import Store


def _store_with_community(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4); s.init()
    s.upsert_entity("alice", "Alice Smith", "person")
    with s._connect() as db:
        db.execute("INSERT INTO community_summaries(community_id, level, title, summary, member_count, key_entities, updated)"
                   " VALUES (1,0,'Leadership','Senior leaders.',3,'alice','2026-06-01')")
        db.execute("INSERT INTO entity_communities(entity_id, community_id, level) VALUES ('alice',1,0)")
    return s


def test_build_context_has_community_summaries_key(tmp_path, monkeypatch):
    s = _store_with_community(tmp_path)
    monkeypatch.setattr(prepare, "_build_known_people", lambda store, **kw: [])
    monkeypatch.setattr(prepare, "_org_domain_lines", lambda: [])
    monkeypatch.setattr(prepare, "_valid_org_tags", lambda: [])
    assert "community_summaries" in prepare._build_context(s, ["t1"])


def test_build_context_includes_entity_community(tmp_path, monkeypatch):
    s = _store_with_community(tmp_path)
    monkeypatch.setattr(prepare, "_build_known_people", lambda store, **kw: [{"id": "alice", "name": "Alice Smith"}])
    monkeypatch.setattr(prepare, "_org_domain_lines", lambda: [])
    monkeypatch.setattr(prepare, "_valid_org_tags", lambda: [])
    ctx = prepare._build_context(s, ["t1"])
    assert any(c.get("title") == "Leadership" for c in ctx["community_summaries"])


def test_build_context_empty_when_no_communities(tmp_path, monkeypatch):
    s = Store(tmp_path / "b.sqlite3", dim=4); s.init()
    monkeypatch.setattr(prepare, "_build_known_people", lambda store, **kw: [])
    monkeypatch.setattr(prepare, "_org_domain_lines", lambda: [])
    monkeypatch.setattr(prepare, "_valid_org_tags", lambda: [])
    assert prepare._build_context(s, [])["community_summaries"] == []
