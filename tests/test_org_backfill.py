"""Tests for Q4 deterministic org backfill."""
import pytest


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Store seeded with entities at various org states."""
    from mcpbrain.store import Store
    s = Store(tmp_path / "t.sqlite3", dim=4)
    s.init()
    return s


def _insert_entity(store, id_, name, email_addr, org=""):
    with store._connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO entities(id,name,type,email_addr,org) VALUES(?,?,?,?,?)",
            (id_, name, "person", email_addr, org))


class TestOrgBackfill:
    def test_backfill_assigns_known_org(self, store, monkeypatch):
        """Entities with a known domain get an org assigned."""
        from mcpbrain import orgs as _orgs
        from mcpbrain.org_backfill import run_backfill

        tax = _orgs.OrgTaxonomy(
            names=("Centrepoint",),
            domain_map={"centrepoint.church": "Centrepoint"},
        )
        monkeypatch.setattr(_orgs, "taxonomy_from_config", lambda: tax)

        _insert_entity(store, "e1", "Alice", "alice@centrepoint.church")
        _insert_entity(store, "e2", "Bob", "bob@external.org")
        _insert_entity(store, "e3", "Charlie", "charlie@centrepoint.church", org="Centrepoint")

        result = run_backfill(store)

        assert result["updated"] == 1   # only e1 (e2=external, e3=already has org)
        assert result["skipped_external"] == 1
        # e3 is excluded by entities_without_org() since org is non-empty.

        # Verify the DB.
        with store._connect() as db:
            row = db.execute("SELECT org FROM entities WHERE id='e1'").fetchone()
            assert row["org"] == "Centrepoint"
            row2 = db.execute("SELECT org FROM entities WHERE id='e2'").fetchone()
            assert row2["org"] == ""  # not updated

    def test_backfill_reports_unknown_domains(self, store, monkeypatch):
        """External/unknown domains are surfaced in the unknown_domains list."""
        from mcpbrain import orgs as _orgs
        from mcpbrain.org_backfill import run_backfill

        tax = _orgs.OrgTaxonomy(names=("Centrepoint",), domain_map={})
        monkeypatch.setattr(_orgs, "taxonomy_from_config", lambda: tax)

        _insert_entity(store, "e1", "Alice", "alice@unknown-church.org")
        _insert_entity(store, "e2", "Bob", "bob@another.org")

        result = run_backfill(store)

        assert result["updated"] == 0
        assert "unknown-church.org" in result["unknown_domains"]
        assert "another.org" in result["unknown_domains"]

    def test_backfill_skips_empty_email(self, store, monkeypatch):
        """Entities with no email_addr are never queried (not returned by entities_without_org)."""
        from mcpbrain import orgs as _orgs
        from mcpbrain.org_backfill import run_backfill

        tax = _orgs.OrgTaxonomy(names=(), domain_map={})
        monkeypatch.setattr(_orgs, "taxonomy_from_config", lambda: tax)

        # Insert entity with blank email_addr.
        with store._connect() as db:
            db.execute(
                "INSERT INTO entities(id,name,type,email_addr,org) VALUES('e_noemail','X','person','','')")

        result = run_backfill(store)
        assert result["updated"] == 0
        assert result["skipped_external"] == 0

    def test_entities_without_org_method(self, store):
        """entities_without_org() only returns entities with empty org + non-empty email."""
        _insert_entity(store, "has_org", "A", "a@x.com", org="Acme")
        _insert_entity(store, "no_org", "B", "b@x.com", org="")
        _insert_entity(store, "no_email", "C", "", org="")

        result = store.entities_without_org()
        ids = {r["id"] for r in result}
        assert "no_org" in ids
        assert "has_org" not in ids
        assert "no_email" not in ids
