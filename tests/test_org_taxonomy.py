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
]}


def _acme_taxonomy():
    return orgs.OrgTaxonomy(
        names=("Acme", "Study"),
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
            {"Acme", "Study", "external", "unknown", "personal"})

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

    def test_canonical_personal_case_insensitive(self):
        t = _acme_taxonomy()
        assert t.canonical("personal") == "personal"
        assert t.canonical("Personal") == "personal"
        assert t.canonical("PERSONAL") == "personal"


class TestSignalsPersonal:
    def test_single_word_marker(self):
        assert orgs.signals_personal("Need to pick up groceries tonight")
        assert orgs.signals_personal("It's Sam's birthday next week")

    def test_phrase_marker(self):
        assert orgs.signals_personal("Sort the christmas present for mum")
        assert orgs.signals_personal("Book the dentist appointment please")

    def test_no_signal_for_ordinary_work_text(self):
        assert not orgs.signals_personal("Please send the campus budget report")

    def test_empty_and_none_are_false(self):
        assert not orgs.signals_personal("")
        assert not orgs.signals_personal(None)

    def test_case_insensitive(self):
        assert orgs.signals_personal("GROCERIES for the week")


class TestDefaultTaxonomy:
    def test_empty_names(self):
        assert orgs.DEFAULT_TAXONOMY.names == ()

    def test_empty_domain_and_alias(self):
        t = orgs.DEFAULT_TAXONOMY
        assert t.from_email("x@accwa.org.au") == "external"
        assert t.canonical("acme corp incorporated") == "acme corp incorporated"

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
        assert t.names == ("Acme", "Study")
        assert t.from_email("a@acme.com") == "Acme"
        assert t.canonical("acme pty ltd") == "Acme"

    def test_reserved_and_malformed_entries_skipped(self, tmp_path):
        home = _write_config(tmp_path, {"orgs": [
            {"name": "external"}, "not-an-object", {"name": ""},
            {"name": "Personal"}, {"name": "Real Org"}]})
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
        for tag in ("external", "unknown", "personal"):
            ext = self._envelope(tag)
            assert contract.normalise_org(ext, _acme_taxonomy()) is None
            assert ext["org"] == tag

    def test_normalise_org_personal_case_insensitive(self):
        # The model may emit different casing; the reserved tag must still
        # resolve rather than silently falling through to "unknown".
        ext = self._envelope("Personal")
        assert contract.normalise_org(ext, _acme_taxonomy()) is None
        assert ext["org"] == "personal"


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
        store.upsert_chunk("d-drift", "body", "hash-d-drift",
                           {"thread_id": "t-drift", "message_id": "m-d1"})  # thread has chunks
        (home / "enrich_inbox" / "b1.json").write_text(json.dumps(
            {"batch_id": "b1", "extractions": [envelope], "merge_answers": []}))

        seen = []

        def fake_apply(store_, extraction, *, doc_ids, entity_index=None):
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
            "Acme", "Study", "external", "unknown", "personal"]
        assert prepare._org_domain_lines() == [
            "acme.com -> Acme", "uni.edu.au -> Study"]


# ---------------------------------------------------------------------------
# apply(): deterministic personal-content backstop
# ---------------------------------------------------------------------------

def _personal_owner():
    return gw.OwnerIdentity(name="Sam", entity_id="sam", aliases=frozenset({"sam"}))


def _org_extraction(thread_id, doc_id, *, org, summary, description):
    return {
        "thread_id": thread_id, "org": org, "content_type": "request",
        "summary": summary, "contextual_summary": "",
        "entities": [], "topics": [],
        "actions": [{"description": description, "owner_name": "",
                     "owner_fallback": "", "due_date": ""}],
        "relations": [], "reply_needed": False, "reply_reason": "",
        "resolved_action_ids": [], "updated_actions": [],
        "messages": [{"message_id": f"m-{doc_id}", "sender": "A B <a@example.com>",
                     "date": "2026-06-01", "labels": "INBOX", "subject": "x"}],
    }


class TestApplyPersonalBackstop:
    def test_unknown_org_with_personal_signal_reclassifies(self, tmp_path):
        store = Store(tmp_path / "b.sqlite3", dim=4)
        store.init()
        ext = _org_extraction(
            "t-personal", "d-personal", org="unknown",
            summary="Pick up groceries and the birthday cake before the party.",
            description="Pick up groceries for the weekend")
        gw.apply(store, ext, doc_ids=["d-personal"], home=str(tmp_path),
                owner=_personal_owner())
        rows = store.list_unified_actions()
        assert len(rows) == 1
        assert rows[0]["org"] == "personal"
        changes = [c for c in store.recent_changes(10)
                  if c["change_type"] == "org_reclassified"]
        assert len(changes) == 1
        assert changes[0]["ref_id"] == "t-personal"

    def test_unknown_org_without_personal_signal_stays_unknown(self, tmp_path):
        store = Store(tmp_path / "b.sqlite3", dim=4)
        store.init()
        ext = _org_extraction(
            "t-generic", "d-generic", org="unknown",
            summary="Please review the attached proposal.",
            description="Review the proposal")
        gw.apply(store, ext, doc_ids=["d-generic"], home=str(tmp_path),
                owner=_personal_owner())
        rows = store.list_unified_actions()
        assert len(rows) == 1
        assert rows[0]["org"] == "unknown"
        assert not [c for c in store.recent_changes(10)
                   if c["change_type"] == "org_reclassified"]

    def test_model_assigned_personal_passes_through_untouched(self, tmp_path):
        # The model's own explicit "personal" choice needs no backstop and no
        # reclassification log entry -- it was never "unknown".
        store = Store(tmp_path / "b.sqlite3", dim=4)
        store.init()
        ext = _org_extraction(
            "t-explicit", "d-explicit", org="personal",
            summary="Book the family holiday flights.",
            description="Book flights for the family holiday")
        gw.apply(store, ext, doc_ids=["d-explicit"], home=str(tmp_path),
                owner=_personal_owner())
        rows = store.list_unified_actions()
        assert len(rows) == 1
        assert rows[0]["org"] == "personal"
        assert not [c for c in store.recent_changes(10)
                   if c["change_type"] == "org_reclassified"]

