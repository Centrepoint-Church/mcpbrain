"""Config-driven install-owner identity (replaces the hardcoded owner identity).

The enrichment pipeline attributes self-owned actions and excludes the install
owner from the graph. Historically the identity was hardcoded ("Sam" /
"sam-chen" / the owner-variant alias set). These tests pin that an
unconfigured install keeps exactly that behaviour, and that a configured
owner_name / owner_full_name flows through every identity-sensitive path:
graph_write (attribution + exclusion), semantic (People line), prompt
(known_people), and the legacy enrich prompt.
"""
import json
from datetime import datetime, timezone

import mcpbrain.graph_write as gw
from mcpbrain.config import owner_aliases, owner_email, owner_full_name
from mcpbrain.semantic import build_semantic_doc
from mcpbrain.store import Store


def _write_config(tmp_path, data: dict) -> str:
    (tmp_path / "config.json").write_text(json.dumps(data))
    return str(tmp_path)


def _clock():
    return datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)


SARAH = gw.OwnerIdentity(
    name="Sarah", entity_id="sarah-chen",
    aliases=frozenset({"sarah", "sarah chen"}))


# ---------------------------------------------------------------------------
# config helpers
# ---------------------------------------------------------------------------

class TestConfigHelpers:
    def test_full_name_defaults_to_empty(self, tmp_path):
        home = _write_config(tmp_path, {})
        assert owner_full_name(home) == ""

    def test_full_name_configured(self, tmp_path):
        home = _write_config(tmp_path, {"owner_full_name": "Sarah Chen"})
        assert owner_full_name(home) == "Sarah Chen"

    def test_aliases_empty_when_unconfigured(self, tmp_path):
        home = _write_config(tmp_path, {})
        assert owner_aliases(home) == frozenset()

    def test_aliases_configured(self, tmp_path):
        home = _write_config(tmp_path, {
            "owner_name": "Sarah", "owner_full_name": "Sarah Chen"})
        assert owner_aliases(home) == frozenset({"sarah", "sarah chen"})

    def test_aliases_extra_list(self, tmp_path):
        home = _write_config(tmp_path, {
            "owner_name": "Sarah", "owner_full_name": "Sarah Chen",
            "owner_aliases": ["Sazza"]})
        assert "sazza" in owner_aliases(home)

    def test_aliases_no_alex_when_configured(self, tmp_path):
        home = _write_config(tmp_path, {"owner_name": "Tom"})
        assert "alex" not in owner_aliases(home)

    def test_email_defaults_to_empty(self, tmp_path):
        home = _write_config(tmp_path, {})
        assert owner_email(home) == ""

    def test_email_configured(self, tmp_path):
        home = _write_config(tmp_path, {"owner_email": "sarah@example.org"})
        assert owner_email(home) == "sarah@example.org"


# ---------------------------------------------------------------------------
# _is_owner matching semantics
# ---------------------------------------------------------------------------

_OWNER = gw.OwnerIdentity(
    name="Sam", entity_id="sam-chen",
    aliases=frozenset({"sam", "alex", "sam chen"}))


class TestIsOwner:
    def test_explicit_identity_matches_owner_variants(self):
        assert gw._is_owner("Sam Chen", _OWNER)
        assert gw._is_owner("Sam", _OWNER)
        assert gw._is_owner("sam.k", _OWNER)

    def test_word_boundary_not_substring(self):
        # Historical substring matching would have excluded Amir; the
        # word-level match must not.
        assert not gw._is_owner("Amir Singh", _OWNER)

    def test_short_alias_does_not_swallow_longer_names(self):
        tom = gw.OwnerIdentity(name="Tom", entity_id="tom-li",
                               aliases=frozenset({"tom", "tom li"}))
        assert gw._is_owner("Tom Li", tom)
        assert gw._is_owner("Tom", tom)
        assert not gw._is_owner("Tomlinson Smith", tom)

    def test_multi_word_alias_matches_substring(self):
        assert gw._is_owner("Pastor Sam Chen Jr", _OWNER)

    def test_empty_identity_never_matches(self):
        empty = gw.OwnerIdentity()
        assert not gw._is_owner("Sam Chen", empty)
        assert not gw._is_owner("anyone", empty)


# ---------------------------------------------------------------------------
# owner_identity_from_config
# ---------------------------------------------------------------------------

class TestOwnerIdentityFromConfig:
    def test_unconfigured_returns_empty_identity(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
        o = gw.owner_identity_from_config()
        assert (o.name, o.entity_id) == ("", "")
        assert o.aliases == frozenset()

    def test_configured_identity(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
        _write_config(tmp_path, {
            "owner_name": "Sarah", "owner_full_name": "Sarah Chen"})
        o = gw.owner_identity_from_config()
        assert (o.name, o.entity_id) == ("Sarah", "sarah-chen")
        assert o.aliases == frozenset({"sarah", "sarah chen"})


# ---------------------------------------------------------------------------
# _infer_owner with a configured identity
# ---------------------------------------------------------------------------

class TestInferOwner:
    def test_sender_is_owner(self):
        assert gw._infer_owner(True, "anything", SARAH) == ("Sarah", "sarah-chen", 0.8)

    def test_imperative_verb(self):
        assert gw._infer_owner(False, "Send the report", SARAH) == ("Sarah", "sarah-chen", 0.6)

    def test_no_signal(self):
        assert gw._infer_owner(False, "The report was sent", SARAH) == ("", "", 0.0)


# ---------------------------------------------------------------------------
# apply() end-to-end with a configured owner
# ---------------------------------------------------------------------------

def _store(tmp_path):
    s = Store(tmp_path / "g.sqlite3", dim=4)
    s.init()
    return s


def _self_thread(sender="Sarah Chen <sarah@example.org>", subject="TODO: file BAS"):
    lead = {"message_id": "m1", "sender": sender, "date": "2026-05-20",
            "labels": "SENT", "subject": subject, "body": "", "is_self": True}
    return {
        "thread_id": "t-own", "org": "Acme", "content_type": "request",
        "summary": "s", "contextual_summary": "", "entities": [], "topics": [],
        "actions": [], "reply_needed": False, "reply_reason": "",
        "resolved_action_ids": [], "updated_actions": [], "relations": [],
        "messages": [lead],
    }


class TestApplyIdentityResolution:
    def test_identity_none_resolves_from_config(self, tmp_path, monkeypatch):
        """A sender matching the configured owner_email is detected as self
        even without an explicit is_self flag or identity argument."""
        monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
        _write_config(tmp_path, {
            "owner_name": "Sarah", "owner_full_name": "Sarah Chen",
            "owner_email": "sarah@example.org"})
        s = _store(tmp_path)
        ext = _self_thread()
        del ext["messages"][0]["is_self"]  # force the address-fallback path
        gw.apply(s, ext, doc_ids=["d1"], clock=_clock)
        acts = s.list_unified_actions()
        assert len(acts) == 1
        assert acts[0]["owner"] == "Sarah"
        assert acts[0]["context_tag"] == "self-email"


class TestApplyWithConfiguredOwner:
    def test_self_email_action_owned_by_configured_owner(self, tmp_path):
        s = _store(tmp_path)
        gw.apply(s, _self_thread(), doc_ids=["d1"], clock=_clock,
                 identity="sarah@example.org", owner=SARAH)
        acts = s.list_unified_actions()
        assert len(acts) == 1
        assert acts[0]["owner"] == "Sarah"
        assert acts[0]["owner_entity_id"] == "sarah-chen"

    def test_owner_excluded_other_included_as_entity(self, tmp_path):
        # On a Sarah install, Sarah is the excluded self; Sam Chen is just a
        # person and must land in the graph.
        s = _store(tmp_path)
        ext = _self_thread()
        ext["entities"] = [
            {"name": "Sarah Chen", "type": "person", "org": "Acme"},
            {"name": "Sam Chen", "type": "person", "org": "Acme"},
        ]
        gw.apply(s, ext, doc_ids=["d1"], clock=_clock,
                 identity="sarah@example.org", owner=SARAH)
        assert s.find_entity("Sarah Chen") is None
        assert s.find_entity("Sam Chen") is not None

    def test_llm_owner_alias_resolves_to_configured_owner(self, tmp_path):
        s = _store(tmp_path)
        ext = _self_thread()
        ext["messages"][0]["is_self"] = False
        ext["messages"][0]["labels"] = "INBOX"
        ext["actions"] = [{"description": "File the BAS return",
                           "owner_name": "Sarah Chen", "owner_fallback": "",
                           "due_date": "2026-06-15"}]
        gw.apply(s, ext, doc_ids=["d1"], clock=_clock,
                 identity="sarah@example.org", owner=SARAH)
        acts = s.list_unified_actions()
        assert len(acts) == 1
        assert acts[0]["owner"] == "Sarah"
        assert acts[0]["owner_entity_id"] == "sarah-chen"


# ---------------------------------------------------------------------------
# semantic People line
# ---------------------------------------------------------------------------

class TestSemanticPeopleLine:
    def test_configured_owner_excluded_other_included(self):
        extraction = {
            "org": "Acme", "summary": "s", "content_type": "update",
            "entities": [
                {"name": "Sarah Chen", "type": "person"},
                {"name": "Sam Chen", "type": "person"},
            ],
            "actions": [], "topics": [],
        }
        thread = {"subject": "x", "sender": "a@b.c", "date": "2026-05-20",
                  "labels": ""}
        text, _meta = build_semantic_doc(extraction, thread, owner=SARAH)
        assert "Sarah Chen" not in text
        assert "Sam Chen" in text


# ---------------------------------------------------------------------------
# prompt.build_known_people
# ---------------------------------------------------------------------------

class TestKnownPeopleExclusion:
    def test_configured_owner_excluded_other_included(self, tmp_path):
        from mcpbrain.prompt import build_known_people
        s = _store(tmp_path)
        for name in ("Sarah Chen", "Sam Chen"):
            eid = gw.upsert_entity(s, name=name, entity_type="person",
                                   org="Acme")
            with s._connect() as conn:
                conn.execute(
                    "UPDATE entities SET email_count = 10 WHERE id = ?", (eid,))
            gw.write_role_observation(s, eid, "Operations Manager",
                                      "email_signature", "2026-01-01", "high")
        people = build_known_people(s, batch_thread_ids=[], owner=SARAH)
        names = {p["name"] for p in people}
        assert "Sarah Chen" not in names
        assert "Sam Chen" in names


# ---------------------------------------------------------------------------
# legacy enrich prompt
# ---------------------------------------------------------------------------

class TestEnrichPromptOwner:
    def test_configured_owner_named_in_prompt(self, tmp_path, monkeypatch):
        from mcpbrain.enrich import build_prompt
        monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
        _write_config(tmp_path, {
            "owner_name": "Sarah", "owner_full_name": "Sarah Chen",
            "orgs": [{"name": "Acme", "domains": ["acme.org"]}]})
        p = build_prompt("doc text", {})
        assert "knowledge graph for Sarah Chen" in p
        assert "EXCLUDE Sarah Chen" in p
        assert "tasks Sarah must act on" in p
        assert "Sam Chen" not in p


# ---------------------------------------------------------------------------
# owner_role
# ---------------------------------------------------------------------------

class TestOwnerRole:
    def test_defaults_to_empty(self, tmp_path):
        from mcpbrain.config import owner_role
        home = _write_config(tmp_path, {})
        assert owner_role(home) == ""

    def test_configured(self, tmp_path):
        from mcpbrain.config import owner_role
        home = _write_config(tmp_path, {"owner_role": "Research Lead"})
        assert owner_role(home) == "Research Lead"

    def test_role_named_in_enrich_prompt(self, tmp_path, monkeypatch):
        from mcpbrain.enrich import build_prompt
        monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
        _write_config(tmp_path, {
            "owner_full_name": "Sarah Chen", "owner_role": "Research Lead",
            "orgs": [{"name": "Acme", "domains": ["acme.org"]}]})
        p = build_prompt("doc", {})
        assert "Sarah Chen (Research Lead)" in p
        assert "operations manager" not in p
