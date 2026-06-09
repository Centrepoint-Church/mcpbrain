"""Config-driven org taxonomy (replaces the hardcoded four-org identity).

Pins three things:
  1. DEFAULT_TAXONOMY reproduces the historical hardcoded taxonomy exactly, so
     an unconfigured install behaves as before.
  2. A configured `orgs` list in config.json flows through every consumer:
     canonicalisation, domain mapping, the contract org gate, the extractor
     context, apply(), the legacy enrich prompt, and lint.
  3. The drift loop: an extractor org outside the configured list is coerced
     to "unknown" by drain (never quarantined for that) and recorded as an
     org_unrecognised proactive finding so the user can grow their taxonomy.
"""
import json
from datetime import datetime, timezone


from mcpbrain import contract, drain, orgs
import mcpbrain.graph_write as gw
from mcpbrain.store import Store


def _write_config(tmp_path, data: dict) -> str:
    (tmp_path / "config.json").write_text(json.dumps(data))
    return str(tmp_path)


ACME_CFG = {"orgs": [
    {"name": "Acme", "domains": ["acme.com"], "aliases": ["Acme Pty Ltd"]},
    {"name": "Study", "domains": ["uni.edu.au"]},
    {"name": "Personal"},
]}


def _acme_taxonomy():
    return orgs.OrgTaxonomy(
        names=("Acme", "Study", "Personal"),
        domain_map={"acme.com": "Acme", "uni.edu.au": "Study"},
        aliases={"acme pty ltd": "Acme"},
    )


# ---------------------------------------------------------------------------
# OrgTaxonomy unit behaviour
# ---------------------------------------------------------------------------

class TestOrgTaxonomy:
    def test_valid_orgs_includes_reserved_tags(self):
        t = _acme_taxonomy()
        assert t.valid_orgs == frozenset(
            {"Acme", "Study", "Personal", "external", "unknown"})

    def test_org_tags_lowercase(self):
        assert "acme" in _acme_taxonomy().org_tags
        assert "external" in _acme_taxonomy().org_tags

    def test_canonical_alias_and_case(self):
        t = _acme_taxonomy()
        assert t.canonical("Acme Pty Ltd") == "Acme"
        assert t.canonical("ACME") == "Acme"
        assert t.canonical("Rotary Club") == "Rotary Club"  # passthrough
        assert t.canonical("") == ""

    def test_from_email_exact_subdomain_external(self):
        t = _acme_taxonomy()
        assert t.from_email("a@acme.com") == "Acme"
        assert t.from_email("a@mail.acme.com") == "Acme"
        assert t.from_email("a@gmail.com") == "external"
        assert t.from_email("") == ""

    def test_domain_lines_sorted(self):
        assert _acme_taxonomy().domain_lines == [
            "acme.com -> Acme", "uni.edu.au -> Study"]


class TestDefaultTaxonomy:
    def test_empty_names(self):
        assert orgs.DEFAULT_TAXONOMY.names == ()

    def test_empty_domain_and_alias(self):
        t = orgs.DEFAULT_TAXONOMY
        assert t.from_email("x@accwa.org.au") == "external"
        assert t.canonical("centrepoint church incorporated") == "centrepoint church incorporated"

    def test_graph_write_module_views_match(self):
        # Module constants mirror DEFAULT_TAXONOMY (both now empty).
        assert gw.KNOWN_ORGS == frozenset(orgs.DEFAULT_TAXONOMY.names)
        assert gw._ORG_TAGS == orgs.DEFAULT_TAXONOMY.org_tags
        assert gw._DOMAIN_ORG == orgs.DEFAULT_TAXONOMY.domain_map


# ---------------------------------------------------------------------------
# taxonomy_from_config
# ---------------------------------------------------------------------------

class TestTaxonomyFromConfig:
    def test_absent_key_returns_default(self, tmp_path):
        home = _write_config(tmp_path, {})
        assert orgs.taxonomy_from_config(home) is orgs.DEFAULT_TAXONOMY

    def test_configured(self, tmp_path):
        home = _write_config(tmp_path, ACME_CFG)
        t = orgs.taxonomy_from_config(home)
        assert t.names == ("Acme", "Study", "Personal")
        assert t.from_email("a@acme.com") == "Acme"
        assert t.canonical("acme pty ltd") == "Acme"

    def test_reserved_and_malformed_entries_skipped(self, tmp_path):
        home = _write_config(tmp_path, {"orgs": [
            {"name": "external"}, "not-an-object", {"name": ""},
            {"name": "Real Org"}]})
        t = orgs.taxonomy_from_config(home)
        assert t.names == ("Real Org",)

    def test_all_entries_invalid_falls_back_to_default(self, tmp_path):
        home = _write_config(tmp_path, {"orgs": [{"name": "unknown"}]})
        assert orgs.taxonomy_from_config(home) is orgs.DEFAULT_TAXONOMY

    def test_domains_normalised(self, tmp_path):
        home = _write_config(tmp_path, {"orgs": [
            {"name": "Acme", "domains": ["@Acme.COM ", ""]}]})
        t = orgs.taxonomy_from_config(home)
        assert t.domain_map == {"acme.com": "Acme"}


# ---------------------------------------------------------------------------
# contract: structural org check + normalise_org
# ---------------------------------------------------------------------------

class TestContractOrg:
    def _envelope(self, org):
        return {
            "thread_id": "t1", "org": org, "content_type": "update",
            "summary": "s", "entities": [], "topics": [], "actions": [],
            "relations": [],
            "messages": [{"message_id": "m1", "sender": "A <a@b.c>",
                          "date": "2026-05-01", "labels": "", "subject": "x"}],
        }

    def test_unconfigured_org_string_passes_validation(self):
        # Enum membership is no longer a structural failure.
        assert contract.validate_extraction(self._envelope("Rotary Club")) == []

    def test_non_string_org_rejected(self):
        problems = contract.validate_extraction(self._envelope(None))
        assert any("org must be a non-empty string" in p for p in problems)

    def test_normalise_org_canonicalises_in_place(self):
        ext = self._envelope("acme pty ltd")
        assert contract.normalise_org(ext, _acme_taxonomy()) is None
        assert ext["org"] == "Acme"

    def test_normalise_org_coerces_and_returns_raw(self):
        ext = self._envelope("Rotary Club")
        assert contract.normalise_org(ext, _acme_taxonomy()) == "Rotary Club"
        assert ext["org"] == "unknown"

    def test_normalise_org_valid_untouched(self):
        ext = self._envelope("Acme")
        assert contract.normalise_org(ext, _acme_taxonomy()) is None
        assert ext["org"] == "Acme"

    def test_normalise_org_reserved_tags_valid(self):
        for tag in ("external", "unknown"):
            ext = self._envelope(tag)
            assert contract.normalise_org(ext, _acme_taxonomy()) is None
            assert ext["org"] == tag


# ---------------------------------------------------------------------------
# drain: coercion + proactive finding, never quarantined for org drift
# ---------------------------------------------------------------------------

class TestDrainOrgDrift:
    def test_unconfigured_org_applies_coerced_and_records_finding(self, tmp_path):
        home = tmp_path
        (home / "enrich_inbox").mkdir(parents=True)
        _write_config(home, ACME_CFG)
        store = Store(home / "brain.db", dim=4)
        store.init()

        envelope = {
            "thread_id": "t-drift", "org": "Rotary Club",
            "content_type": "update", "summary": "s",
            "entities": [], "topics": [], "actions": [], "relations": [],
            "messages": [{"message_id": "m-d1", "sender": "A <a@b.c>",
                          "date": "2026-05-01", "labels": "", "subject": "x"}],
            "resolved_action_ids": [], "updated_actions": [],
            "reply_needed": False, "reply_reason": "",
        }
        (home / "enrich_inbox" / "b1.json").write_text(json.dumps(
            {"batch_id": "b1", "extractions": [envelope], "merge_answers": []}))

        seen = []

        def fake_apply(store_, extraction, *, doc_ids):
            seen.append(extraction)
            return {"entities": 0, "relations": 0}

        summary = drain.drain(store, home=home, apply=fake_apply)
        assert summary["quarantined"] == 0
        assert summary["applied"] == 1
        assert seen[0]["org"] == "unknown"
        findings = store.open_findings("org_unrecognised")
        assert len(findings) == 1
        assert findings[0]["ref_id"] == "rotary club"
        assert "Rotary Club" in findings[0]["summary"]

    def test_repeat_sightings_upsert_one_finding(self, tmp_path):
        home = tmp_path
        (home / "enrich_inbox").mkdir(parents=True)
        _write_config(home, ACME_CFG)
        store = Store(home / "brain.db", dim=4)
        store.init()

        def env(tid, mid):
            return {
                "thread_id": tid, "org": "Rotary Club",
                "content_type": "update", "summary": "s",
                "entities": [], "topics": [], "actions": [], "relations": [],
                "messages": [{"message_id": mid, "sender": "A <a@b.c>",
                              "date": "2026-05-01", "labels": "", "subject": "x"}],
                "resolved_action_ids": [], "updated_actions": [],
                "reply_needed": False, "reply_reason": "",
            }
        (home / "enrich_inbox" / "b1.json").write_text(json.dumps(
            {"batch_id": "b1", "extractions": [env("t1", "m1"), env("t2", "m2")],
             "merge_answers": []}))

        drain.drain(store, home=home, apply=lambda s, e, *, doc_ids: {})
        assert len(store.open_findings("org_unrecognised")) == 1


# ---------------------------------------------------------------------------
# apply() end-to-end with a configured taxonomy
# ---------------------------------------------------------------------------

def _clock():
    return datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)


class TestApplyWithConfiguredTaxonomy:
    def test_sender_domain_maps_to_configured_org(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
        _write_config(tmp_path, ACME_CFG)
        store = Store(tmp_path / "g.sqlite3", dim=4)
        store.init()
        ext = {
            "thread_id": "t-acme", "org": "Acme", "content_type": "update",
            "summary": "s", "contextual_summary": "",
            "entities": [], "topics": [], "actions": [],
            "reply_needed": False, "reply_reason": "",
            "resolved_action_ids": [], "updated_actions": [], "relations": [],
            "messages": [{"message_id": "m1",
                          "sender": "Pat Lee <pat@acme.com>",
                          "date": "2026-05-20", "labels": "INBOX",
                          "subject": "x", "body": ""}],
        }
        gw.apply(store, ext, doc_ids=["d1"], clock=_clock)
        pat = store.find_entity("Pat Lee")
        assert pat is not None
        assert pat["org"] == "Acme"

    def test_org_alias_entity_name_canonicalises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
        _write_config(tmp_path, ACME_CFG)
        store = Store(tmp_path / "g.sqlite3", dim=4)
        store.init()
        eid = gw.upsert_entity(store, name="Acme Pty Ltd", entity_type="org",
                               taxonomy=orgs.taxonomy_from_config(str(tmp_path)))
        assert eid == "acme"


# ---------------------------------------------------------------------------
# prepare context + legacy enrich prompt
# ---------------------------------------------------------------------------

class TestExtractorSurfaces:
    def test_prepare_context_carries_valid_orgs(self, tmp_path, monkeypatch):
        from mcpbrain import prepare
        monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
        _write_config(tmp_path, ACME_CFG)
        assert prepare._valid_org_tags() == [
            "Acme", "Study", "Personal", "external", "unknown"]
        assert prepare._org_domain_lines() == [
            "acme.com -> Acme", "uni.edu.au -> Study"]

    def test_enrich_prompt_uses_configured_orgs(self, tmp_path, monkeypatch):
        from mcpbrain.enrich import build_prompt
        monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
        _write_config(tmp_path, ACME_CFG)
        p = build_prompt("doc", {})
        assert "Acme|Study|Personal|external|unknown" in p
        assert "Acme, Study, Personal" in p
        assert "Centrepoint" not in p

    def test_prompt_md_has_no_hardcoded_orgs(self):
        from pathlib import Path
        import mcpbrain
        md = (Path(mcpbrain.__file__).parent / "enrich_prompt.md").read_text()
        assert "Centrepoint" not in md
        assert "valid_orgs" in md
