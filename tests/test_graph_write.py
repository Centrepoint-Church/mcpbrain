"""Tests for mcpbrain/graph_write.py (Phase 1, Task 2).

Offline only — no network, no LLM. Each test builds a fresh Store on a tmp
path and exercises the ported write-path functions against it.
"""

import json
from pathlib import Path


from mcpbrain import graph_write as gw, orgs
from mcpbrain.chunking import slugify
from mcpbrain.store import Store

_ACME_ORGS = [
    {"name": "Acme", "domains": ["example.org", "example.com.au"],
     "aliases": ["Acme Corp", "Acme Corp Incorporated",
                 "Acme Baptist"]},
    {"name": "ACC", "domains": ["acc.org.au", "acci.org.au", "accwa.org.au",
                                 "acc.net.au", "acc.church"]},
    {"name": "Courageous Church", "domains": ["courageouschurch.org.au"]},
    {"name": "Curtin", "domains": ["curtin.edu.au"]},
]


_CP_TAXONOMY = orgs.OrgTaxonomy(
    names=("Acme", "ACC", "Courageous Church", "Curtin"),
    domain_map={
        "example.org": "Acme", "example.com.au": "Acme",
        "acc.org.au": "ACC", "acci.org.au": "ACC", "accwa.org.au": "ACC",
        "acc.net.au": "ACC", "acc.church": "ACC",
        "courageouschurch.org.au": "Courageous Church",
        "curtin.edu.au": "Curtin",
    },
    aliases={
        "acme corp": "Acme",
        "acme corp incorporated": "Acme",
        "acme baptist": "Acme",
    },
)


def _write_cp_config(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"orgs": _ACME_ORGS}))

FIXTURES = Path(__file__).parent / "fixtures" / "extractions"


_OWNER_IDENTITY = gw.OwnerIdentity(
    name="Sam",
    entity_id="sam-chen",
    aliases=frozenset({"sam", "alex", "sam chen"}),
)


def _store(tmp_path):
    s = Store(tmp_path / "g.sqlite3", dim=4)
    s.init()
    return s


def _load(name):
    return json.loads((FIXTURES / name).read_text())


# --- 2.1 org-domain map + slug/junk helpers -------------------------------

def test_org_from_email_known_domains():
    assert gw.org_from_email("joel@example.org", _CP_TAXONOMY) == "Acme"
    assert gw.org_from_email("x@gmail.com", _CP_TAXONOMY) == "external"
    assert gw.org_from_email("") == ""


def test_org_casing_is_display_form():
    assert gw.org_from_email("a@acc.org.au", _CP_TAXONOMY) == "ACC"


def test_domain_org_lines_present_and_shaped():
    lines = _CP_TAXONOMY.domain_lines
    assert isinstance(lines, list) and lines
    assert all(isinstance(line, str) for line in lines)
    assert len(lines) == len(_CP_TAXONOMY.domain_map)
    assert any(
        "example.org" in line and "Acme" in line for line in lines
    )
    for domain, org in _CP_TAXONOMY.domain_map.items():
        assert any(domain in line and org in line for line in lines)


def test_entity_slug():
    assert slugify("Taryn Hamilton") == "taryn-hamilton"
    assert slugify("ACC (National)") == "acc-national"
    assert slugify("") == ""


def test_is_junk_entity():
    # A person name with a 4-digit run is junk.
    assert gw.is_junk_entity("Booking 2026", "person") is True
    # An org name with a year is fine.
    assert gw.is_junk_entity("Acme 2026", "org") is False
    # A normal person name is fine.
    assert gw.is_junk_entity("Joel Chelliah", "person") is False


# --- 2.2 entity upsert with email->alias->suppressed dedup ----------------

def test_upsert_entity_new_returns_slug(tmp_path):
    s = _store(tmp_path)
    eid = gw.upsert_entity(s, name="Joel Chelliah", entity_type="person",
                           org="Acme")
    assert eid == "joel-chelliah"
    ent = s.get_entity(eid)
    assert ent["name"] == "Joel Chelliah"
    assert ent["type"] == "person"
    assert ent["org"] == "Acme"


def test_upsert_entity_email_dedup(tmp_path):
    s = _store(tmp_path)
    first = gw.upsert_entity(s, name="Joel Chelliah", entity_type="person",
                             org="Acme", email_addr="joel@example.org")
    # Same email, different display name → merges into the existing entity.
    second = gw.upsert_entity(s, name="J. Chelliah", entity_type="person",
                              email_addr="joel@example.org")
    assert second == first
    # One person entity (the org is auto-created via works_at, so count persons).
    persons = [e for e in s.list_entities() if e["type"] == "person"]
    assert len(persons) == 1
    ent = s.get_entity(first)
    # email_count is driven by message links in apply(), not by the raw upsert,
    # so a direct double-upsert with no link leaves it at 0.
    assert ent["email_count"] == 0



def test_upsert_entity_alias_merge(tmp_path):
    s = _store(tmp_path)
    # Seed an entity with an alias.
    with s._connect() as db:
        db.execute(
            "INSERT INTO entities(id,name,type,aliases) VALUES(?,?,?,?)",
            ("joel-chelliah", "Joel Chelliah", "person", "Pastor Joel"))
    # A new upsert whose name matches the alias merges, no new row.
    eid = gw.upsert_entity(s, name="Pastor Joel", entity_type="person")
    assert eid == "joel-chelliah"
    assert len(s.list_entities()) == 1


# --- 2.3 role observations with provenance + supersession -----------------

def _role_rows(s, entity_id):
    with s._connect() as db:
        return [dict(r) for r in db.execute(
            "SELECT value, source, valid_from, valid_to FROM entity_observations "
            "WHERE entity_id=? AND attribute='role' ORDER BY id", (entity_id,)).fetchall()]


def _seed_person(s, eid="joel-chelliah", name="Joel Chelliah"):
    with s._connect() as db:
        db.execute("INSERT INTO entities(id,name,type) VALUES(?,?,'person')", (eid, name))
    return eid


def test_write_role_observation_inserts(tmp_path):
    s = _store(tmp_path)
    eid = _seed_person(s)
    gw.write_role_observation(s, eid, "Senior Pastor", "llm_extraction",
                              "2026-04-18", "medium")
    rows = _role_rows(s, eid)
    assert len(rows) == 1
    assert rows[0]["value"] == "Senior Pastor"
    assert rows[0]["valid_to"] is None


def test_role_obs_same_source_supersedes(tmp_path):
    s = _store(tmp_path)
    eid = _seed_person(s)
    gw.write_role_observation(s, eid, "Pastor", "from_header", "2026-04-01", "medium")
    gw.write_role_observation(s, eid, "Senior Pastor", "from_header", "2026-04-18", "medium")
    rows = _role_rows(s, eid)
    assert len(rows) == 2
    current = [r for r in rows if r["valid_to"] is None]
    closed = [r for r in rows if r["valid_to"] is not None]
    assert len(current) == 1 and current[0]["value"] == "Senior Pastor"
    assert len(closed) == 1 and closed[0]["value"] == "Pastor"


def test_role_obs_low_source_skipped_when_authoritative_exists(tmp_path):
    s = _store(tmp_path)
    eid = _seed_person(s)
    # An authoritative email_signature role exists.
    gw.write_role_observation(s, eid, "Operations Manager", "email_signature",
                              "2026-04-01", "high")
    # A later low-ranked llm_extraction role is skipped.
    gw.write_role_observation(s, eid, "Volunteer Coordinator", "llm_extraction",
                              "2026-04-18", "medium")
    rows = _role_rows(s, eid)
    assert len(rows) == 1
    assert rows[0]["value"] == "Operations Manager"


def test_role_obs_rejects_junk(tmp_path):
    s = _store(tmp_path)
    eid = _seed_person(s)
    gw.write_role_observation(s, eid, "volunteer", "from_header", "2026-04-18", "medium")
    assert _role_rows(s, eid) == []


# --- 2.4 bitemporal relation upsert (invalidate-not-delete) ---------------

def _rels(s):
    with s._connect() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM entity_relations ORDER BY id").fetchall()]


def _mk_ent(s, eid, etype="person"):
    with s._connect() as db:
        db.execute("INSERT OR IGNORE INTO entities(id,name,type) VALUES(?,?,?)",
                   (eid, eid, etype))


def test_singleton_older_arriving_late_stays_historical(tmp_path):
    # Recency: backfill applies a 2021 fact AFTER the 2024 one. The newer (2024)
    # must stay current; the older must NOT supersede it.
    s = _store(tmp_path)
    _mk_ent(s, "a"); _mk_ent(s, "orgnew", "org"); _mk_ent(s, "orgold", "org")
    gw.upsert_relation(s, "a", "works_at", "orgnew", valid_from="2024-01-01")
    gw.upsert_relation(s, "a", "works_at", "orgold", valid_from="2021-01-01")  # older, late
    rows = {r["entity_b"]: r for r in _rels(s)}
    assert rows["orgnew"]["invalidated_at"] is None         # newer stays current
    assert rows["orgold"]["invalidated_at"] is not None     # older recorded but historical
    assert rows["orgold"]["superseded_reason"] == "older_than_current"
    assert rows["orgold"]["invalidated_by_relation_id"] == rows["orgnew"]["id"]


def test_role_fetch_prefers_newest_dated_regardless_of_insert_order(tmp_path):
    # fetch_role must return the newest-DATED role even when an older role was
    # inserted later (backfill order).
    s = _store(tmp_path)
    eid = _seed_person(s)
    gw.write_role_observation(s, eid, "Senior Pastor", "llm_extraction", "2024-01-01", "medium")
    gw.write_role_observation(s, eid, "Intern", "llm_extraction", "2019-01-01", "medium")  # older, late
    assert gw.fetch_role(s, eid) == "Senior Pastor"


def test_entity_org_recency_overwrite(tmp_path):
    s = _store(tmp_path)
    # first observation (older) sets org
    gw.upsert_entity(s, name="Dana Quinn", entity_type="person", org="OldCo",
                     valid_from="2020-01-01")
    # newer-dated observation overwrites the stale org
    gw.upsert_entity(s, name="Dana Quinn", entity_type="person", org="NewCo",
                     valid_from="2025-01-01")
    eid = gw.upsert_entity(s, name="Dana Quinn", entity_type="person")
    assert s.get_entity(eid)["org"] == "NewCo"
    # an OLDER observation arriving later must NOT clobber the newer org
    gw.upsert_entity(s, name="Dana Quinn", entity_type="person", org="AncientCo",
                     valid_from="2018-01-01")
    assert s.get_entity(eid)["org"] == "NewCo"


def test_relation_reobservation_bumps(tmp_path):
    s = _store(tmp_path)
    _mk_ent(s, "a"); _mk_ent(s, "b", "org")
    gw.upsert_relation(s, "a", "mentioned_with", "b", valid_from="2026-04-01")
    deg_after_first = s.get_entity("a")["degree"]
    gw.upsert_relation(s, "a", "mentioned_with", "b", valid_from="2026-04-10")
    rows = _rels(s)
    assert len(rows) == 1  # re-observation, no new row
    assert rows[0]["confidence"] > 1.0 - 1e-9 or rows[0]["confidence"] >= 1.0
    # confidence bumped (capped at 1.0); last_seen advanced.
    assert rows[0]["last_seen"]
    # degree unchanged on the second (re-observation) call.
    assert s.get_entity("a")["degree"] == deg_after_first


def test_singleton_relation_supersedes(tmp_path):
    s = _store(tmp_path)
    _mk_ent(s, "a"); _mk_ent(s, "orgx", "org"); _mk_ent(s, "orgy", "org")
    gw.upsert_relation(s, "a", "works_at", "orgx", valid_from="2026-04-01")
    gw.upsert_relation(s, "a", "works_at", "orgy", valid_from="2026-05-01")
    rows = _rels(s)
    assert len(rows) == 2  # both rows present (invalidate-not-delete)
    old = [r for r in rows if r["entity_b"] == "orgx"][0]
    new = [r for r in rows if r["entity_b"] == "orgy"][0]
    assert old["invalidated_at"] is not None
    assert old["valid_to"] == "2026-05-01"
    assert old["superseded_reason"] == "superseded_by_newer"
    assert old["invalidated_by_relation_id"] == new["id"]
    assert new["invalidated_at"] is None


def test_reobserving_superseded_pair_revives_not_unique_error(tmp_path):
    """Re-observing a pair whose row was superseded must revive that row, not
    INSERT into the legacy UNIQUE(entity_a,relation,entity_b) (live drain bug:
    'UNIQUE constraint failed' aborted the whole thread apply, 2026-06-05)."""
    s = _store(tmp_path)
    _mk_ent(s, "a"); _mk_ent(s, "orgx", "org"); _mk_ent(s, "orgy", "org")
    gw.upsert_relation(s, "a", "works_at", "orgx", valid_from="2026-04-01")
    gw.upsert_relation(s, "a", "works_at", "orgy", valid_from="2026-05-01")
    deg_before = s.get_entity("a")["degree"]
    # Third observation goes BACK to orgx: must not raise sqlite3.IntegrityError.
    rid = gw.upsert_relation(s, "a", "works_at", "orgx", valid_from="2026-06-01")
    rows = _rels(s)
    assert len(rows) == 2                     # revived, no third row
    revived = [r for r in rows if r["entity_b"] == "orgx"][0]
    assert rid == revived["id"]
    assert revived["invalidated_at"] is None  # live again
    assert revived["valid_to"] is None
    assert revived["valid_from"] == "2026-06-01"
    # works_at is a singleton: orgy must now be superseded by the revival.
    orgy = [r for r in rows if r["entity_b"] == "orgy"][0]
    assert orgy["invalidated_at"] is not None
    assert orgy["invalidated_by_relation_id"] == revived["id"]
    # Revival is a re-observation, not a new edge: degree unchanged.
    assert s.get_entity("a")["degree"] == deg_before


def test_revive_updates_source_doc_id_to_new_evidence(tmp_path):
    """A revived relation must carry the doc id of the evidence that revived it,
    not the original source_doc_id from when it was first observed."""
    s = _store(tmp_path)
    _mk_ent(s, "a"); _mk_ent(s, "orgx", "org"); _mk_ent(s, "orgy", "org")
    gw.upsert_relation(s, "a", "works_at", "orgx", valid_from="2026-04-01",
                       evidence="msg-old")
    gw.upsert_relation(s, "a", "works_at", "orgy", valid_from="2026-05-01",
                       evidence="msg-mid")
    gw.upsert_relation(s, "a", "works_at", "orgx", valid_from="2026-06-01",
                       evidence="msg-new")
    revived = [r for r in _rels(s) if r["entity_b"] == "orgx"][0]
    assert revived["source_doc_id"] == "msg-new"


def test_accumulating_relation_coexists(tmp_path):
    s = _store(tmp_path)
    _mk_ent(s, "a"); _mk_ent(s, "b"); _mk_ent(s, "c")
    gw.upsert_relation(s, "a", "mentioned_with", "b", valid_from="2026-04-01")
    gw.upsert_relation(s, "a", "mentioned_with", "c", valid_from="2026-04-01")
    valid = [r for r in _rels(s) if r["invalidated_at"] is None]
    assert len(valid) == 2


def test_relation_degree_incremented_once_per_new_row(tmp_path):
    s = _store(tmp_path)
    _mk_ent(s, "a"); _mk_ent(s, "b")
    gw.upsert_relation(s, "a", "mentioned_with", "b", valid_from="2026-04-01")
    assert s.get_entity("a")["degree"] == 1
    assert s.get_entity("b")["degree"] == 1
    # Re-observation does not bump degree.
    gw.upsert_relation(s, "a", "mentioned_with", "b", valid_from="2026-04-05")
    assert s.get_entity("a")["degree"] == 1
    assert s.get_entity("b")["degree"] == 1


# --- 2.5 apply() structural pass ------------------------------------------

def test_apply_writes_entities_and_email_context(tmp_path):
    s = _store(tmp_path)
    ext = _load("thread_simple.json")
    gw.apply(s, ext, doc_ids=["t-simple-001"])
    # Joel entity exists (sender, from joel@example.org).
    joel = s.find_entity("Joel Chelliah")
    assert joel is not None
    # email_context row written for the thread lead (m-1).
    with s._connect() as db:
        ec = dict(db.execute(
            "SELECT * FROM email_context WHERE message_id='m-1'").fetchone())
        links = [dict(r) for r in db.execute(
            "SELECT * FROM email_entities WHERE message_id='m-1'").fetchall()]
    assert ec["org"] == "Acme"
    assert ec["content_type"] == "request"
    assert ec["summary"]
    assert ec["contextual_summary"]
    assert ec["thread_id"] == "t-simple-001"
    # INBOX is a Gmail system label, stripped from stored custom labels.
    assert ec["labels"] == ""
    # Sender linked.
    sender_links = [lk for lk in links if lk["entity_id"] == joel["id"]]
    assert sender_links and sender_links[0]["role"] == "sender"


def test_apply_excludes_owner(tmp_path):
    s = _store(tmp_path)
    ext = _load("thread_multi_message.json")  # has a "Sam Chen" entity
    gw.apply(s, ext, doc_ids=["t-multi-002"],
             owner=_OWNER_IDENTITY, identity="sam@example.org")
    names = {e["name"].lower() for e in s.list_entities()}
    assert not any("sam" in n for n in names)


def test_apply_relations_resolved_via_name_map(tmp_path, monkeypatch):
    _write_cp_config(tmp_path)
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    s = _store(tmp_path)
    ext = _load("thread_simple.json")
    gw.apply(s, ext, doc_ids=["t-simple-001"])
    joel = s.find_entity("Joel Chelliah")
    # "Acme Corp" canonicalises to the single 'acme' org node.
    org = s.find_entity("Acme")
    assert joel and org
    rels = s.relations_for(joel["id"])
    works = [r for r in rels if r["relation"] == "works_at"
             and r["entity_b"] == org["id"]]
    assert works, f"works_at edge not written; rels={rels}"


def test_apply_email_count_stable_on_reapply(tmp_path):
    """email_count counts distinct message links, not apply invocations.

    The sender is linked to the lead message once, so its count is 1 after a
    first apply and stays 1 when the same thread is reprocessed (the daemon's
    apply-then-mark ordering allows a thread to be re-applied after a crash).
    """
    s = _store(tmp_path)
    ext = _load("thread_simple.json")
    gw.apply(s, ext, doc_ids=["t-simple-001"])
    joel = s.get_entity("joel-chelliah")
    assert joel["email_count"] == 1
    gw.apply(s, ext, doc_ids=["t-simple-001"])
    joel = s.get_entity("joel-chelliah")
    assert joel["email_count"] == 1  # not 2 — re-apply must not inflate


def test_apply_org_affiliation_single_node(tmp_path, monkeypatch):
    """A known org resolves to one canonical node, not a tag/full-name pair.

    'Acme' (the org tag) and 'Acme Corp' (the relation target)
    must converge on the single 'acme' entity with one valid works_at
    edge — no phantom bare-slug node, no immediately-superseded edge.
    """
    _write_cp_config(tmp_path)
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    s = _store(tmp_path)
    ext = _load("thread_simple.json")
    gw.apply(s, ext, doc_ids=["t-simple-001"])
    org_ids = sorted(e["id"] for e in s.list_entities() if e["type"] == "org")
    assert org_ids == ["acme"]
    valid_works = [
        r for r in s.list_relations()
        if r["relation"] == "works_at" and r["invalidated_at"] is None
    ]
    assert len(valid_works) == 1
    assert valid_works[0]["entity_b"] == "acme"


def test_apply_populates_thread_context(tmp_path):
    """apply() materialises the thread_context index (the synthesis producer).

    Without this, thread_context is never populated by normal enrichment, so
    prior_thread_context degrades to '' forever and the synthesis pass finds no
    candidates. apply() writes subject/org/email_count/summary/participants; it
    leaves contextual_summary for the deeper synthesis pass to fill.
    """
    s = _store(tmp_path)
    ext = _load("thread_simple.json")
    gw.apply(s, ext, doc_ids=["t-simple-001"])
    with s._connect() as db:
        row = db.execute(
            "SELECT * FROM thread_context WHERE thread_id = ?",
            ("t-simple-001",)).fetchone()
    assert row is not None
    assert row["org"] == "Acme"
    assert row["email_count"] == 1
    assert row["summary"]  # the thread headline summary is set
    assert "joel-chelliah" in (row["participant_ids"] or "")
    # The deep narrative is the synthesis pass's job, not apply's.
    assert (row["contextual_summary"] or "") == ""


def test_apply_writes_waiting_on(tmp_path):
    """An action flagged waiting_on is stored with the awaited entity + set time.

    This is the producer the waiting-on reconciler depends on: without apply()
    populating these columns the reconciler can never clear anything.
    """
    s = _store(tmp_path)
    ext = dict(_load("thread_simple.json"))
    ext["entities"] = ext["entities"] + [
        {"name": "Taryn Hamilton", "type": "person", "org": "Acme", "role": ""}]
    ext["actions"] = [{
        "description": "Wait for Taryn to confirm the venue.",
        "owner_name": "Sam Chen", "owner_fallback": "", "due_date": "",
        "project_id": "", "area_id": "", "waiting_on": "Taryn Hamilton"}]
    gw.apply(s, ext, doc_ids=["t-simple-001"])
    with s._connect() as db:
        row = db.execute(
            "SELECT waiting_on, waiting_on_entity_id, waiting_on_set_at "
            "FROM actions WHERE waiting_on IS NOT NULL").fetchone()
    assert row is not None
    assert row["waiting_on"] == "Taryn Hamilton"
    assert row["waiting_on_entity_id"] == "taryn-hamilton"
    assert row["waiting_on_set_at"]


def test_apply_idempotent(tmp_path):
    s = _store(tmp_path)
    ext = _load("thread_simple.json")
    gw.apply(s, ext, doc_ids=["t-simple-001"])
    ents_1 = len(s.list_entities())
    rels_1 = len([r for r in s.list_relations() if r["invalidated_at"] is None])
    gw.apply(s, ext, doc_ids=["t-simple-001"])
    ents_2 = len(s.list_entities())
    rels_2 = len([r for r in s.list_relations() if r["invalidated_at"] is None])
    assert ents_2 == ents_1
    assert rels_2 == rels_1
    with s._connect() as db:
        n = db.execute("SELECT COUNT(*) FROM email_context WHERE message_id='m-1'").fetchone()[0]
    assert n == 1


# --- 2.6 topic gate (>=2 distinct orgs) + project/area validation ---------

def _ext(thread_id, org, msg_id, topics, sender="A B <a@example.com>"):
    return {
        "thread_id": thread_id, "org": org, "content_type": "update",
        "summary": "s", "contextual_summary": "", "entities": [], "topics": topics,
        "actions": [], "reply_needed": False, "reply_reason": "",
        "resolved_action_ids": [], "updated_actions": [], "relations": [],
        "messages": [{"message_id": msg_id, "sender": sender,
                      "date": "2026-04-18", "labels": "INBOX", "subject": "x"}],
    }


def test_topic_gate_blocks_single_org(tmp_path):
    s = _store(tmp_path)
    gw.apply(s, _ext("t1", "Acme", "m1", ["budget"]), doc_ids=["d1"])
    # Only one org has carried "budget" → the topic entity is not created.
    assert s.get_entity("topic-budget") is None


def test_topic_gate_allows_two_orgs(tmp_path):
    s = _store(tmp_path)
    # Two prior email_context rows under different orgs carry "budget".
    gw.apply(s, _ext("t1", "Acme", "m1", ["budget"]), doc_ids=["d1"])
    gw.apply(s, _ext("t2", "ACC", "m2", ["budget"]), doc_ids=["d2"])
    assert s.get_entity("topic-budget") is None  # not yet (gate runs before this row counts)
    # The third apply now sees 2 distinct orgs already in email_context.
    gw.apply(s, _ext("t3", "Acme", "m3", ["budget"]), doc_ids=["d3"])
    assert s.get_entity("topic-budget") is not None


def test_topic_gate_escapes_like_metachars(tmp_path):
    s = _store(tmp_path)
    # Two prior rows under different orgs carry "q1_budget" (note the underscore).
    gw.apply(s, _ext("t1", "Acme", "m1", ["q1_budget"]), doc_ids=["d1"])
    gw.apply(s, _ext("t2", "ACC", "m2", ["q1_budget"]), doc_ids=["d2"])
    # A different topic "q1xbudget" would match "q1_budget" if "_" acted as a LIKE
    # wildcard. With ESCAPE it does not, so this topic has zero prior appearances
    # and the gate must keep it from being created.
    gw.apply(s, _ext("t3", "Acme", "m3", ["q1xbudget"]), doc_ids=["d3"])
    assert s.get_entity("topic-q1xbudget") is None
    # Sanity: the literal "q1_budget" topic still opens its own gate on the third
    # appearance, confirming the escape didn't break legitimate matching.
    # (entity id slugifies the underscore to a hyphen: topic-q1-budget)
    gw.apply(s, _ext("t4", "Courageous Church", "m4", ["q1_budget"]), doc_ids=["d4"])
    assert s.get_entity("topic-q1-budget") is not None


# --- Task 3 action lifecycle ----------------------------------------------
# A fixed injected clock keeps the 60-day age boundary deterministic.
from datetime import datetime, timezone  # noqa: E402

CLOCK_NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _clock():
    return CLOCK_NOW


def _thread(*, actions=None, content_type="request", lead_sender="A B <a@example.com>",
            lead_date="2026-05-20", labels="INBOX", is_self=None, subject="x",
            body="", org="Acme", resolved_action_ids=None,
            updated_actions=None, entities=None, relations=None, topics=None,
            extra_messages=None, summary="s"):
    """Build a single-message (plus optional extra) thread extraction for the
    action lifecycle tests. The lead message carries the date/labels/sender."""
    lead = {"message_id": "m1", "sender": lead_sender, "date": lead_date,
            "labels": labels, "subject": subject, "body": body}
    if is_self is not None:
        lead["is_self"] = is_self
    messages = [lead] + list(extra_messages or [])
    return {
        "thread_id": "t-act", "org": org, "content_type": content_type,
        "summary": summary, "contextual_summary": "",
        "entities": entities or [], "topics": topics or [],
        "actions": actions or [],
        "reply_needed": False, "reply_reason": "",
        "resolved_action_ids": resolved_action_ids or [],
        "updated_actions": updated_actions or [],
        "relations": relations or [],
        "messages": messages,
    }


def _action(desc, owner_name="A B", owner_fallback="sender", due_date="2026-06-15",
            project_id="", area_id=""):
    return {"description": desc, "owner_name": owner_name,
            "owner_fallback": owner_fallback, "due_date": due_date,
            "project_id": project_id, "area_id": area_id}


# --- 3.1 age gate + notification gate -------------------------------------

def test_age_gate_drops_old_non_inbox_actions(tmp_path):
    s = _store(tmp_path)
    # Lead date is >60 days before the injected clock, no INBOX label, not self.
    ext = _thread(actions=[_action("Send the WA region report")],
                  lead_date="2026-01-01", labels="SENT")
    gw.apply(s, ext, doc_ids=["d1"], clock=_clock)
    assert s.list_unified_actions() == []


def test_age_gate_exactly_60_days_exempt(tmp_path):
    s = _store(tmp_path)
    # Lead date is exactly 60 days before the injected clock (2026-06-01),
    # non-INBOX, not self. The gate is `> 60`, so 60 days exactly is exempt.
    ext = _thread(actions=[_action("Send the WA region report")],
                  lead_date="2026-04-02", labels="SENT")
    gw.apply(s, ext, doc_ids=["d1"], clock=_clock)
    assert len(s.list_unified_actions()) == 1


def test_age_gate_inbox_exempt(tmp_path):
    s = _store(tmp_path)
    ext = _thread(actions=[_action("Send the WA region report")],
                  lead_date="2026-01-01", labels="INBOX")
    gw.apply(s, ext, doc_ids=["d1"], clock=_clock)
    assert len(s.list_unified_actions()) == 1


def test_age_gate_self_exempt(tmp_path):
    s = _store(tmp_path)
    ext = _thread(actions=[_action("Send the WA region report")],
                  lead_date="2026-01-01", labels="SENT",
                  lead_sender="Sam Chen <sam@example.org>", is_self=True)
    gw.apply(s, ext, doc_ids=["d1"], clock=_clock)
    assert len(s.list_unified_actions()) == 1


def test_notification_gate_clears_actions(tmp_path):
    s = _store(tmp_path)
    ext = _thread(actions=[_action("Renew the SSL certificate")],
                  content_type="notification")
    gw.apply(s, ext, doc_ids=["d1"], clock=_clock)
    assert s.list_unified_actions() == []


def test_notification_gate_self_exempt(tmp_path):
    s = _store(tmp_path)
    ext = _thread(actions=[_action("Renew the SSL certificate")],
                  content_type="notification", labels="SENT",
                  lead_sender="Sam Chen <sam@example.org>", is_self=True)
    gw.apply(s, ext, doc_ids=["d1"], clock=_clock)
    assert len(s.list_unified_actions()) == 1


# --- 3.2 self-email synthetic task + within-batch dedup -------------------

def test_self_email_synthesises_task_from_subject(tmp_path):
    s = _store(tmp_path)
    # Self thread, no actions, subject carries a self-prefix → confidence 1.0.
    ext = _thread(actions=[], content_type="fyi", labels="SENT",
                  lead_sender="Sam Chen <sam@example.org>",
                  is_self=True, subject="TODO: book the Narrogin van")
    gw.apply(s, ext, doc_ids=["d1"], clock=_clock,
             owner=_OWNER_IDENTITY, identity="sam@example.org")
    rows = s.list_unified_actions()
    assert len(rows) == 1
    assert rows[0]["text"] == "book the Narrogin van"  # prefix stripped
    assert rows[0]["owner"] == "Sam"
    assert rows[0]["confidence"] == 1.0
    assert rows[0]["context_tag"] == "self-email"


def test_self_email_synthetic_no_prefix_lower_confidence(tmp_path):
    s = _store(tmp_path)
    ext = _thread(actions=[], content_type="fyi", labels="SENT",
                  lead_sender="Sam Chen <sam@example.org>",
                  is_self=True, subject="Carpark briefing note")
    gw.apply(s, ext, doc_ids=["d1"], clock=_clock,
             owner=_OWNER_IDENTITY, identity="sam@example.org")
    rows = s.list_unified_actions()
    assert len(rows) == 1
    assert rows[0]["text"] == "Carpark briefing note"
    assert rows[0]["confidence"] == 0.85  # no self-prefix


def test_self_email_empty_subject_fallback_no_em_dash(tmp_path):
    s = _store(tmp_path)
    # Self thread, no actions, empty subject → synthetic fallback text. The
    # fallback must use a plain hyphen, not an em-dash (project voice rule;
    # surfaces in ClickUp later).
    ext = _thread(actions=[], content_type="fyi", labels="SENT",
                  lead_sender="Sam Chen <sam@example.org>",
                  is_self=True, subject="")
    gw.apply(s, ext, doc_ids=["d1"], clock=_clock)
    rows = s.list_unified_actions()
    assert len(rows) == 1
    assert rows[0]["text"] == "(self-email task - no subject)"
    assert "—" not in rows[0]["text"]  # no em-dash
    assert "–" not in rows[0]["text"]  # no en-dash either


def test_within_batch_dedup_drops_near_identical(tmp_path):
    s = _store(tmp_path)
    # Two actions with Jaccard >= 0.75 → one survives.
    ext = _thread(actions=[
        _action("Send the WA region credentialing report to Taryn"),
        _action("Send the WA region credentialing report to Taryn please"),
    ])
    gw.apply(s, ext, doc_ids=["d1"], clock=_clock)
    assert len(s.list_unified_actions()) == 1


# --- 3.3 owner inference + deadline inference + confidence -----------------

def test_owner_inferred_when_empty(tmp_path):
    s = _store(tmp_path)
    # Imperative-verb description, empty owner, no sender fallback → inferred owner.
    ext = _thread(actions=[_action("Review the draft policy before Friday",
                                   owner_name="", owner_fallback="")])
    gw.apply(s, ext, doc_ids=["d1"], clock=_clock, owner=_OWNER_IDENTITY)
    rows = s.list_unified_actions()
    assert len(rows) == 1
    assert rows[0]["owner"] == "Sam"
    assert rows[0]["owner_entity_id"] == "sam-chen"
    assert rows[0]["confidence"] == 0.6  # imperative-verb inference


def test_owner_normalised(tmp_path):
    for i, name in enumerate(("sam", "alex", "Sam Chen")):
        s2 = Store(tmp_path / f"g{i}.sqlite3", dim=4)
        s2.init()
        ext = _thread(actions=[_action("Confirm the booking", owner_name=name)])
        gw.apply(s2, ext, doc_ids=["d1"], clock=_clock, owner=_OWNER_IDENTITY)
        rows = s2.list_unified_actions()
        assert len(rows) == 1
        assert rows[0]["owner"] == "Sam"
        assert rows[0]["owner_entity_id"] == "sam-chen"


def test_deadline_inferred_from_body(tmp_path):
    s = _store(tmp_path)
    # No due_date on the action, but the body carries an ISO date. Owner is the
    # configured owner so the deadline-inference confidence (0.6) is the value
    # stamped on the row (the owner/decision branch carries deadline_confidence;
    # the non-owner branch keeps action_confidence — see _write_actions routing).
    ext = _thread(actions=[_action("Submit the grant paperwork",
                                   owner_name="Sam", due_date="")],
                  body="Please get this in by 2026-06-20 at the latest.")
    gw.apply(s, ext, doc_ids=["d1"], clock=_clock, owner=_OWNER_IDENTITY)
    rows = s.list_unified_actions()
    assert len(rows) == 1
    assert rows[0]["deadline"] == "2026-06-20"
    assert rows[0]["confidence"] == 0.6  # inferred deadline confidence


# --- 3.4 routing to the unified actions table + near-dup guard -------------

def test_action_routed_with_owner_and_status(tmp_path):
    s = _store(tmp_path)
    # Non-configured-owner confirmed owner (named, resolvable as sender) → that owner, open.
    ext = _thread(actions=[_action("Prepare the venue checklist",
                                   owner_name="Taryn Hamilton")],
                  lead_sender="Taryn Hamilton <taryn@example.org>")
    gw.apply(s, ext, doc_ids=["d1"], clock=_clock)
    rows = s.list_unified_actions()
    assert len(rows) == 1
    assert rows[0]["owner"] == "Taryn Hamilton"
    assert rows[0]["status"] == "open"
    assert rows[0]["source"] == "email"
    # Baseline confidence for a non-self confirmed-owner email action.
    assert rows[0]["confidence"] == 0.7


def test_unclear_owner_routed(tmp_path):
    s = _store(tmp_path)
    # Non-imperative description, no owner, no sender fallback → 'unclear', 0.5.
    ext = _thread(actions=[_action("The venue situation needs resolution",
                                   owner_name="", owner_fallback="")])
    gw.apply(s, ext, doc_ids=["d1"], clock=_clock)
    rows = s.list_unified_actions()
    assert len(rows) == 1
    assert rows[0]["owner"] == "unclear"
    assert rows[0]["confidence"] == 0.5


def test_owner_action_routed(tmp_path):
    s = _store(tmp_path)
    ext = _thread(actions=[_action("Sign off the budget", owner_name="Sam")])
    gw.apply(s, ext, doc_ids=["d1"], clock=_clock)
    rows = s.list_unified_actions()
    assert len(rows) == 1
    assert rows[0]["owner"] == "Sam"


def test_near_dup_guard_skips(tmp_path):
    s = _store(tmp_path)
    ext = _thread(actions=[_action("Prepare the venue checklist",
                                   owner_name="Taryn Hamilton")],
                  lead_sender="Taryn Hamilton <taryn@example.org>")
    gw.apply(s, ext, doc_ids=["d1"], clock=_clock)
    # A second apply with the same open action does not insert a duplicate.
    gw.apply(s, ext, doc_ids=["d1"], clock=_clock)
    assert len(s.list_unified_actions()) == 1


# --- 3.5 resolved + updated action lifecycle -------------------------------

def test_resolved_action_ids_close_actions(tmp_path):
    s = _store(tmp_path)
    aid = s.add_unified_action(text="Run the field audit", owner="Sam",
                               status="open", thread_id="t-act")
    ext = _thread(actions=[], resolved_action_ids=[aid])
    gw.apply(s, ext, doc_ids=["d1"], clock=_clock)
    row = [a for a in s.list_unified_actions() if a["id"] == aid][0]
    assert row["status"] == "done"
    assert row["resolved_by"] == "m1"


def test_updated_actions_rewrite_text(tmp_path):
    s = _store(tmp_path)
    aid = s.add_unified_action(text="Run the field audit", owner="Sam",
                               status="open", thread_id="t-act")
    ext = _thread(actions=[],
                  updated_actions=[{"id": aid, "new_text": "Run the WA field audit (done)"}])
    gw.apply(s, ext, doc_ids=["d1"], clock=_clock)
    row = [a for a in s.list_unified_actions() if a["id"] == aid][0]
    assert row["text"] == "Run the WA field audit (done)"


def test_resolve_ignores_non_int_ids(tmp_path):
    s = _store(tmp_path)
    aid = s.add_unified_action(text="Run the field audit", owner="Sam",
                               status="open", thread_id="t-act")
    ext = _thread(actions=[], resolved_action_ids=["101", None, True, aid])
    gw.apply(s, ext, doc_ids=["d1"], clock=_clock)
    # Only the genuine int id closes the action; the rest are ignored.
    row = [a for a in s.list_unified_actions() if a["id"] == aid][0]
    assert row["status"] == "done"


def test_resolved_action_id_other_thread_not_closed(tmp_path):
    """A resolved id is scoped to the resolving thread: an id belonging to a
    different thread (e.g. a hallucinated or stale id) must not close it."""
    s = _store(tmp_path)
    other = s.add_unified_action(text="Unrelated open work", owner="Sam",
                                 status="open", thread_id="t-other")
    ext = _thread(actions=[], resolved_action_ids=[other])  # _thread is "t-act"
    gw.apply(s, ext, doc_ids=["d1"], clock=_clock)
    row = [a for a in s.list_unified_actions() if a["id"] == other][0]
    assert row["status"] == "open"  # untouched — different thread


# --- 3.6 apply() summary + integration acceptance --------------------------

def test_apply_full_lifecycle_summary(tmp_path):
    s = _store(tmp_path)
    # Pre-seed two of this thread's open actions — one to resolve, one to
    # update — plus two prior email_context rows under different orgs so the
    # "audit" topic gate opens. Both belong to t-rich so the thread-scoped
    # resolve/update reach them.
    pre = s.add_unified_action(text="Run the WA credentialing audit",
                               owner="Sam", status="open", thread_id="t-rich")
    pre_upd = s.add_unified_action(text="Draft the audit cover note",
                                   owner="Sam", status="open", thread_id="t-rich")
    gw.apply(s, _ext("seed1", "Acme", "seed-m1", ["audit"]), doc_ids=["s1"])
    gw.apply(s, _ext("seed2", "ACC", "seed-m2", ["audit"]), doc_ids=["s2"])

    ext = {
        "thread_id": "t-rich", "org": "ACC", "content_type": "update",
        "summary": "CAMS audit thread", "contextual_summary": "",
        "entities": [
            {"name": "Taryn Hamilton", "type": "person", "org": "Acme",
             "role": "Executive Pastor"},
            {"name": "CAMS Review", "type": "project", "org": "ACC", "role": ""},
        ],
        "topics": ["audit"],
        "actions": [
            _action("Compile the regional credentialing summary",
                    owner_name="Taryn Hamilton"),
            _action("Sign off the audit findings", owner_name="Sam"),
        ],
        "reply_needed": False, "reply_reason": "",
        "resolved_action_ids": [pre],
        "updated_actions": [{"id": pre_upd, "new_text": "Draft the audit cover note (revised)"}],
        "relations": [
            {"source_name": "Taryn Hamilton", "type": "works_at",
             "target_name": "Acme Corp"},
        ],
        "messages": [
            {"message_id": "m-rich", "sender": "Taryn Hamilton <taryn@example.org>",
             "date": "2026-05-28", "labels": "INBOX", "subject": "CAMS audit"},
        ],
    }
    summary = gw.apply(s, ext, doc_ids=["t-rich-1"], clock=_clock)

    # Summary counts.
    assert set(summary) >= {"entities", "relations", "actions", "topics",
                            "resolved", "updated"}
    assert summary["actions"] == 2  # two new actions written
    assert summary["resolved"] == 1
    assert summary["updated"] == 1
    assert summary["relations"] == 1
    assert summary["topics"] == 1  # "audit" gate opened by the two seed rows

    # Graph state spot-checks.
    all_actions = s.list_unified_actions()
    taryn_act = [a for a in all_actions if a["owner"] == "Taryn Hamilton"]
    owner_act = [a for a in all_actions if a["owner"] == "Sam" and a["status"] == "open"]
    assert taryn_act and owner_act
    closed = [a for a in all_actions if a["id"] == pre][0]
    assert closed["status"] == "done"
    updated = [a for a in all_actions if a["id"] == pre_upd][0]
    assert updated["text"] == "Draft the audit cover note (revised)"
    assert s.get_entity("topic-audit") is not None


# --- entity normalisation layer (deterministic guards) --------------------

# Part 1 — affiliation-suffix stripping for person names.

def test_strip_affiliation_strips_from_suffix():
    assert gw.strip_affiliation("Franz from The Church Co") == "Franz"


def test_strip_affiliation_strips_at_suffix():
    assert gw.strip_affiliation("Tim at TechCorp") == "Tim"


def test_strip_affiliation_strips_from_known_org():
    assert gw.strip_affiliation("Joel from Acme") == "Joel"


def test_strip_affiliation_leaves_plain_names_untouched():
    assert gw.strip_affiliation("Franz") == "Franz"
    assert gw.strip_affiliation("Joel Chelliah") == "Joel Chelliah"
    assert gw.strip_affiliation("Nathan") == "Nathan"


def test_strip_affiliation_does_not_strip_of():
    # " of " is not an affiliation keyword we strip (org-shaped names use it).
    assert gw.strip_affiliation("Bank of Melbourne") == "Bank of Melbourne"


def test_strip_affiliation_word_boundaries_protect_at():
    # "at" inside "Atherton" must NOT trigger the " at " rule.
    assert gw.strip_affiliation("Mary Atherton") == "Mary Atherton"


def test_strip_affiliation_requires_non_empty_head():
    # No head before the keyword → leave unchanged rather than reduce to "".
    assert gw.strip_affiliation("from The Church Co") == "from The Church Co"
    assert gw.strip_affiliation("at TechCorp") == "at TechCorp"


# Part 2 — external sender domain stays external.

def _sender_ext(thread_org, sender):
    return {
        "thread_id": "ts-1", "org": thread_org, "content_type": "update",
        "summary": "s", "contextual_summary": "", "entities": [], "topics": [],
        "actions": [], "reply_needed": False, "reply_reason": "",
        "resolved_action_ids": [], "updated_actions": [], "relations": [],
        "messages": [{"message_id": "ms-1", "sender": sender,
                      "date": "2026-04-18", "labels": "INBOX", "subject": "x"}],
    }


def test_external_sender_in_acme_thread_is_external(tmp_path):
    s = _store(tmp_path)
    gw.apply(s, _sender_ext("Acme", "Franz <franz@thechurchco.com>"),
             doc_ids=["d1"])
    franz = s.find_entity("Franz")
    assert franz is not None
    assert franz["org"] == "external"


def test_known_domain_sender_gets_its_org(tmp_path, monkeypatch):
    _write_cp_config(tmp_path)
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    s = _store(tmp_path)
    gw.apply(s, _sender_ext("ACC", "Joel <joel@example.org>"),
             doc_ids=["d1"])
    joel = s.find_entity("Joel")
    assert joel is not None
    assert joel["org"] == "Acme"


def test_no_email_sender_inherits_thread_org(tmp_path):
    s = _store(tmp_path)
    # Display name only, no resolvable email address.
    gw.apply(s, _sender_ext("Acme", "Bob Builder"), doc_ids=["d1"])
    # No email → no sender entity is upserted (needs name + email); assert
    # that, then check the org logic via the email_context org instead.
    assert s.find_entity("Bob Builder") is None
    with s._connect() as db:
        ec = dict(db.execute(
            "SELECT * FROM email_context WHERE message_id='ms-1'").fetchone())
    assert ec["org"] == "Acme"


# Part 3 — reject relations whose endpoint is an org-classification TAG.

def _rel_ext(target_name):
    return {
        "thread_id": "tr-1", "org": "Acme", "content_type": "update",
        "summary": "s", "contextual_summary": "",
        "entities": [{"name": "Franz", "type": "person", "org": "external",
                      "role": ""}],
        "topics": [], "actions": [], "reply_needed": False, "reply_reason": "",
        "resolved_action_ids": [], "updated_actions": [],
        "relations": [{"source_name": "Franz", "type": "works_at",
                       "target_name": target_name}],
        "messages": [{"message_id": "mr-1",
                      "sender": "Franz <franz@thechurchco.com>",
                      "date": "2026-04-18", "labels": "INBOX", "subject": "x"}],
    }


def test_relation_to_org_tag_is_skipped(tmp_path, monkeypatch):
    _write_cp_config(tmp_path)
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    s = _store(tmp_path)
    gw.apply(s, _rel_ext("Acme"), doc_ids=["d1"])
    # The bare tag "Acme" must not have been created as an entity.
    assert s.get_entity("acme") is None
    franz = s.find_entity("Franz")
    assert franz is not None
    assert not s.relations_for(franz["id"])


def test_relation_to_external_tag_is_skipped(tmp_path):
    s = _store(tmp_path)
    gw.apply(s, _rel_ext("external"), doc_ids=["d1"])
    franz = s.find_entity("Franz")
    assert franz is not None
    assert not s.relations_for(franz["id"])


def test_relation_to_real_org_is_kept(tmp_path, monkeypatch):
    _write_cp_config(tmp_path)
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    s = _store(tmp_path)
    gw.apply(s, _rel_ext("Acme Corp"), doc_ids=["d1"])
    franz = s.find_entity("Franz")
    # "Acme Corp" is a real org name (contains a tag word but is not the
    # bare tag), so it is NOT skipped — it canonicalises to the 'acme' node
    # and the works_at edge is written there.
    org = s.find_entity("Acme")
    assert franz and org
    rels = [r for r in s.relations_for(franz["id"])
            if r["relation"] == "works_at" and r["entity_b"] == org["id"]]
    assert rels, "works_at edge to real org should be kept"


# Combined Franz regression lock.

def test_franz_regression_lock(tmp_path, monkeypatch):
    _write_cp_config(tmp_path)
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    s = _store(tmp_path)
    ext = {
        "thread_id": "tf-1", "org": "Acme", "content_type": "update",
        "summary": "s", "contextual_summary": "",
        "entities": [{"name": "Franz from The Church Co", "type": "person",
                      "org": "Acme", "role": ""}],
        "topics": [], "actions": [], "reply_needed": False, "reply_reason": "",
        "resolved_action_ids": [], "updated_actions": [],
        "relations": [{"source_name": "Franz from The Church Co",
                       "type": "works_at", "target_name": "Acme"}],
        "messages": [{"message_id": "mf-1",
                      "sender": "Franz from The Church Co <franz@thechurchco.com>",
                      "date": "2026-04-18", "labels": "INBOX", "subject": "x"}],
    }
    gw.apply(s, ext, doc_ids=["d1"])

    # Normalised to "Franz", org external, type person.
    franz = s.find_entity("Franz")
    assert franz is not None
    assert franz["name"] == "Franz"
    assert franz["org"] == "external"
    assert franz["type"] == "person"

    # No "Franz from The Church Co" entity.
    names = {e["name"] for e in s.list_entities()}
    assert "Franz from The Church Co" not in names

    # No works_at edge to the bare "Acme" tag.
    assert s.get_entity("acme") is None
    assert not s.relations_for(franz["id"])


# Part 4 — gated stripping preserves org names that contain " at ".

def test_relation_to_org_containing_at_is_preserved(tmp_path):
    """A works_at relation whose target is a real org whose name contains ' at '
    must be written intact.  Prior to Fix 1, strip_affiliation fired unconditionally
    and truncated 'Church at the Bay' to 'Church', which then failed to resolve,
    silently dropping the edge.

    This test seeds a person ("Alice") and expects the relation
    Alice -works_at-> 'Church at the Bay' to be written, with the org entity
    stored under its full name (not truncated)."""
    s = _store(tmp_path)
    ext = {
        "thread_id": "tat-1", "org": "Acme", "content_type": "update",
        "summary": "s", "contextual_summary": "",
        "entities": [
            {"name": "Alice", "type": "person", "org": "external", "role": ""},
        ],
        "topics": [], "actions": [], "reply_needed": False, "reply_reason": "",
        "resolved_action_ids": [], "updated_actions": [],
        "relations": [
            {"source_name": "Alice", "type": "works_at",
             "target_name": "Church at the Bay"},
        ],
        "messages": [{"message_id": "mat-1",
                      "sender": "Alice <alice@churchatthebay.org>",
                      "date": "2026-04-18", "labels": "INBOX", "subject": "x"}],
    }
    gw.apply(s, ext, doc_ids=["d1"])

    alice = s.find_entity("Alice")
    assert alice is not None, "Alice entity should exist"

    # The org must be stored with its full name, not truncated to 'Church'.
    org = s.find_entity("Church at the Bay")
    assert org is not None, (
        "Org 'Church at the Bay' should be created intact; "
        "got entities: " + str([e["name"] for e in s.list_entities()])
    )
    assert org["name"] == "Church at the Bay"

    # The works_at edge must be written.
    rels = [r for r in s.relations_for(alice["id"])
            if r["relation"] == "works_at" and r["entity_b"] == org["id"]]
    assert rels, (
        "works_at edge from Alice to 'Church at the Bay' should be preserved; "
        "rels for Alice: " + str(s.relations_for(alice["id"]))
    )


def test_topic_variants_converge(tmp_path):
    # NOTE: brief specified a `store` pytest fixture with `store.db_path`; this
    # file has no such fixture (only the local `_store(tmp_path)` helper used
    # throughout, whose Store exposes the db path as `.path`, not `.db_path`).
    # Adapted accordingly rather than inventing a second, conflicting fixture.
    store = _store(tmp_path)
    home = str(store.path.parent)
    # Two orgs must mention a topic before the min-2-org gate opens it; three
    # applies seed prior rows then create the entity (see apply() topic gate).
    def ext(tid, mid, org, topics):
        return {"thread_id": tid, "org": org, "content_type": "update", "summary": "s",
                "topics": topics, "actions": [], "relations": [], "entities": [],
                "messages": [{"message_id": mid, "sender": "A <a@acme.org>",
                              "date": "2026-01-05", "subject": "s"}]}
    gw.apply(store, ext("t1", "m1", "Acme", ["budgets"]), doc_ids=["d1"], home=home)
    gw.apply(store, ext("t2", "m2", "Beta", ["the budget"]), doc_ids=["d2"], home=home)
    gw.apply(store, ext("t3", "m3", "Acme", ["budget"]), doc_ids=["d3"], home=home)
    with store._connect() as db:
        ids = [r["id"] for r in db.execute(
            "SELECT id FROM entities WHERE type='topic'").fetchall()]
    assert ids == ["topic-budget"]  # 'budgets'/'the budget'/'budget' -> one node


def test_topic_duplicate_variants_deduped(tmp_path):
    # A single extraction naming two variants of the same topic ("budgets" and
    # "budget") normalizes both to "budget" (M1): the topics loop must not run
    # twice for the same tag, and the stored topics string must not repeat it.
    store = _store(tmp_path)
    home = str(store.path.parent)

    def ext(tid, mid, org, topics):
        return {"thread_id": tid, "org": org, "content_type": "update", "summary": "s",
                "topics": topics, "actions": [], "relations": [], "entities": [],
                "messages": [{"message_id": mid, "sender": "A <a@acme.org>",
                              "date": "2026-01-05", "subject": "s"}]}

    # Seed prior appearances in two distinct orgs so the min-2-org gate is open
    # by the third (duplicate-variant) apply.
    gw.apply(store, ext("t1", "m1", "Acme", ["budget"]), doc_ids=["d1"], home=home)
    gw.apply(store, ext("t2", "m2", "Beta", ["budget"]), doc_ids=["d2"], home=home)
    gw.apply(store, ext("t3", "m3", "Acme", ["budgets", "budget"]), doc_ids=["d3"], home=home)

    with store._connect() as db:
        ids = [r["id"] for r in db.execute(
            "SELECT id FROM entities WHERE type='topic'").fetchall()]
        row = db.execute(
            "SELECT topics FROM email_context WHERE message_id='m3'").fetchone()
    assert ids == ["topic-budget"]
    assert row["topics"] == "budget"  # deduped, not "budget, budget"


# --- Task 5: meeting/event series convergence -------------------------------

def _meeting_extraction(entities, thread_id="t1", org="Acme", msg_id="m1"):
    return {
        "thread_id": thread_id, "org": org, "content_type": "update",
        "summary": "s", "topics": [], "actions": [], "relations": [],
        "entities": entities,
        "messages": [{"message_id": msg_id, "sender": "A <a@acme.org>",
                      "date": "2026-01-05", "subject": "sub"}],
    }


def test_meeting_series_id_is_org_scoped():
    a = gw._meeting_series_id("Board Meeting", "Acme")
    b = gw._meeting_series_id("Board Meeting", "Beta")
    assert a == "meeting-acme-board-meeting"
    assert a != b  # same name, different org -> different series


def test_meeting_mentions_converge_on_one_series(tmp_path):
    # NOTE: brief specified a `store` pytest fixture with `store.db_path`; this
    # file has no such fixture (only the local `_store(tmp_path)` helper used
    # throughout, whose Store exposes the db path as `.path`, not `.db_path`).
    # Adapted accordingly, same as test_topic_variants_converge above.
    store = _store(tmp_path)
    home = str(store.path.parent)
    ext = _meeting_extraction(
        [{"name": "College Meeting 12 May", "type": "meeting",
          "series_name": "College Meeting", "occurrence_date": "2026-05-12"}])
    gw.apply(store, ext, doc_ids=["gmail-m1-body-0"], home=home)
    ext2 = _meeting_extraction(
        [{"name": "College Meeting 19 May", "type": "meeting",
          "series_name": "College Meeting", "occurrence_date": "2026-05-19"}],
        thread_id="t2", msg_id="m2")
    gw.apply(store, ext2, doc_ids=["gmail-m2-body-0"], home=home)
    with store._connect() as db:
        meetings = db.execute("SELECT id FROM entities WHERE type='meeting'").fetchall()
        occ = db.execute("SELECT COUNT(*) FROM entity_observations "
                         "WHERE entity_id='meeting-acme-college-meeting' "
                         "AND attribute='occurrence'").fetchone()[0]
    assert [m["id"] for m in meetings] == ["meeting-acme-college-meeting"]
    assert occ == 2  # two occurrences on the one series


def test_event_folds_into_meeting_series(tmp_path):
    store = _store(tmp_path)
    home = str(store.path.parent)
    ext = _meeting_extraction(
        [{"name": "Youth Camp", "type": "event",
          "series_name": "Youth Camp", "occurrence_date": "2026-05-12"}])
    gw.apply(store, ext, doc_ids=["gmail-m1-body-0"], home=home)
    with store._connect() as db:
        row = db.execute("SELECT id, type FROM entities WHERE id='meeting-acme-youth-camp'").fetchone()
    assert row is not None and row["type"] == "meeting"


def test_meeting_series_disabled_falls_back(tmp_path):
    store = _store(tmp_path)
    home = str(store.path.parent)
    (store.path.parent / "config.json").write_text('{"meeting_series_enabled": false}')
    ext = _meeting_extraction(
        [{"name": "College Meeting 12 May", "type": "meeting",
          "series_name": "College Meeting", "occurrence_date": "2026-05-12"}])
    gw.apply(store, ext, doc_ids=["gmail-m1-body-0"], home=home)
    with store._connect() as db:
        ids = [r["id"] for r in db.execute("SELECT id FROM entities WHERE type='meeting'").fetchall()]
    assert ids == ["college-meeting-12-may"]  # legacy bare slugify(name)
