import asyncio
import logging
from unittest.mock import patch
from mcpbrain.store import Store
from mcpbrain.index import index_pending
from mcpbrain.mcp_server import make_brain_search
from tests.test_retrieval import FakeEmbedder


def test_brain_search_tool_returns_results(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    s.upsert_chunk("d-budget", "the annual budget review", "h1", {})
    index_pending(s, FakeEmbedder())
    tool = make_brain_search(s, FakeEmbedder())
    out = asyncio.run(tool("money planning", 5))
    assert any(r["doc_id"] == "d-budget" for r in out)


def test_brain_search_clean_error_when_empty(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    tool = make_brain_search(s, FakeEmbedder())
    out = asyncio.run(tool("anything", 5))
    assert out == []  # empty, not an exception


def test_brain_search_logs_on_failure(tmp_path, caplog):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    tool = make_brain_search(s, FakeEmbedder())
    with caplog.at_level(logging.ERROR, logger="mcpbrain.mcp_server"):
        with patch("mcpbrain.mcp_server.hybrid_search", side_effect=RuntimeError("boom")):
            out = asyncio.run(tool("anything", 5))
    assert out == []
    assert any("brain_search failed" in r.message for r in caplog.records)


def test_brain_read_returns_full_chunk(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    s.upsert_chunk("d-budget", "the annual budget review", "h1", {"source_type": "gmail"})
    chunk = s.get_chunk("d-budget")
    assert chunk["text"] == "the annual budget review"
    assert chunk["metadata"]["source_type"] == "gmail"
    assert s.get_chunk("missing") is None


# --- brain_context / brain_graph graph tools (Task 4.5) ------------------

from mcpbrain.mcp_server import make_brain_context, make_brain_graph


def _seed_graph_store(tmp_path):
    s = Store(tmp_path / "g.sqlite3", dim=4)
    s.init()
    s.upsert_entity("taryn-hamilton", "Taryn Hamilton", "person", org="Centrepoint")
    s.upsert_entity("joel-chelliah", "Joel Chelliah", "person", org="Centrepoint")
    s.upsert_entity("college-2026", "College 2026", "project")
    s.add_relation("taryn-hamilton", "reports_to", "joel-chelliah", "doc-1")
    s.add_relation("taryn-hamilton", "works_on", "college-2026", "doc-2")
    s.add_unified_action(text="Confirm college timetable", owner="Taryn Hamilton")
    return s


def test_brain_context_by_id(tmp_path):
    s = _seed_graph_store(tmp_path)
    tool = make_brain_context(s)
    out = asyncio.run(tool("taryn-hamilton"))
    assert out["entity"]["id"] == "taryn-hamilton"
    others = {r["other"] for r in out["relations"]}
    assert others == {"joel-chelliah", "college-2026"}
    assert all(r["direction"] == "out" for r in out["relations"])
    assert any(a["text"] == "Confirm college timetable" for a in out["actions"])


def test_brain_context_by_name(tmp_path):
    s = _seed_graph_store(tmp_path)
    tool = make_brain_context(s)
    out = asyncio.run(tool("Taryn Hamilton"))
    assert out["entity"]["id"] == "taryn-hamilton"
    assert len(out["relations"]) == 2
    assert len(out["actions"]) == 1


def test_brain_context_in_edge_labelled_correctly(tmp_path):
    s = _seed_graph_store(tmp_path)
    tool = make_brain_context(s)
    out = asyncio.run(tool("joel-chelliah"))
    assert len(out["relations"]) == 1
    assert out["relations"][0]["direction"] == "in"
    assert out["relations"][0]["other"] == "taryn-hamilton"
    assert out["relations"][0]["relation"] == "reports_to"


def test_brain_context_unknown_returns_empty(tmp_path):
    s = _seed_graph_store(tmp_path)
    tool = make_brain_context(s)
    assert asyncio.run(tool("nobody")) == {}


def test_brain_graph_one_hop(tmp_path):
    s = _seed_graph_store(tmp_path)
    tool = make_brain_graph(s)
    out = asyncio.run(tool("taryn-hamilton", 1))
    node_ids = {n["id"] for n in out["nodes"]}
    assert node_ids == {"taryn-hamilton", "joel-chelliah", "college-2026"}
    edge_rels = {(e["entity_a"], e["relation"], e["entity_b"]) for e in out["edges"]}
    assert ("taryn-hamilton", "reports_to", "joel-chelliah") in edge_rels
    assert ("taryn-hamilton", "works_on", "college-2026") in edge_rels


def test_brain_graph_caps_hops(tmp_path):
    s = _seed_graph_store(tmp_path)
    tool = make_brain_graph(s)
    out = asyncio.run(tool("taryn-hamilton", 99))  # must not error, behaves as <=3
    assert out["center"]["id"] == "taryn-hamilton"
    assert {n["id"] for n in out["nodes"]} == {"taryn-hamilton", "joel-chelliah", "college-2026"}


def test_brain_graph_unknown_returns_empty(tmp_path):
    s = _seed_graph_store(tmp_path)
    tool = make_brain_graph(s)
    assert asyncio.run(tool("nobody")) == {}


# --- action freshness surfaced through brain_context (Phase 4 exit) --------


def test_brain_context_actions_carry_freshness(tmp_path):
    """Every action returned by brain_context carries a freshness field, and an
    action whose thread has a later resolution message reads as 'stale'."""
    s = Store(tmp_path / "f.sqlite3", dim=4)
    s.init()
    s.upsert_entity("taryn-hamilton", "Taryn Hamilton", "person", org="Centrepoint")
    # Source request and a later reply that resolves it, both on the same thread.
    s.upsert_chunk(
        "msg-req", "Can you confirm the college timetable?", "h-req",
        {"source_type": "gmail", "thread_id": "t-1", "date": "Mon, 01 Jun 2026 09:00:00 +0800"},
    )
    s.upsert_chunk(
        "msg-reply", "All sorted, timetable confirmed.", "h-reply",
        {"source_type": "gmail", "thread_id": "t-1", "date": "Mon, 01 Jun 2026 11:00:00 +0800"},
    )
    s.add_unified_action(text="Confirm college timetable", owner="Taryn Hamilton",
                         source_doc_id="msg-req", thread_id="t-1")

    tool = make_brain_context(s)
    out = asyncio.run(tool("taryn-hamilton"))

    assert out["actions"], "expected at least one action"
    for a in out["actions"]:
        assert a["freshness"] in ("fresh", "stale")
    # The resolved thread makes this action stale.
    stale = [a for a in out["actions"] if a["text"] == "Confirm college timetable"]
    assert stale and stale[0]["freshness"] == "stale"


# --- brain_context unified-table + projects/areas (Task 8.3) -------------


def test_brain_context_actions_from_unified_table(tmp_path):
    """brain_context actions come from the unified `actions` table, not the
    legacy graph_actions_legacy table."""
    s = Store(tmp_path / "u.sqlite3", dim=4)
    s.init()
    s.upsert_entity("taryn-hamilton", "Taryn Hamilton", "person", org="Centrepoint")
    # Legacy table row must NOT surface.
    s.add_action("Legacy action", owner="Taryn Hamilton")
    # Unified table row MUST surface.
    s.add_unified_action(text="Unified action", owner="Taryn Hamilton", status="open")

    tool = make_brain_context(s)
    out = asyncio.run(tool("taryn-hamilton"))
    texts = {a["text"] for a in out["actions"]}
    assert "Unified action" in texts
    assert "Legacy action" not in texts


def test_brain_context_includes_projects_areas(tmp_path):
    """An entity that owns a project (and an area carried by that project)
    surfaces them in the brain_context result."""
    s = Store(tmp_path / "pa.sqlite3", dim=4)
    s.init()
    s.upsert_entity("josh", "Josh Kemp", "person", org="Centrepoint")
    with s._connect() as db:
        db.execute("INSERT INTO areas(id, org_id, name, active) "
                   "VALUES('a-ops', 'Centrepoint', 'Operations', 1)")
        db.execute("INSERT INTO projects(id, name, owner_entity_id, area_id, status) "
                   "VALUES('p-college', 'College 2026', 'josh', 'a-ops', 'active')")

    tool = make_brain_context(s)
    out = asyncio.run(tool("josh"))
    assert {p["id"] for p in out["projects"]} == {"p-college"}
    assert {a["id"] for a in out["areas"]} == {"a-ops"}


def test_brain_graph_at_time(tmp_path):
    s = Store(tmp_path / "gt.sqlite3", dim=4)
    s.init()
    s.upsert_entity("taryn", "Taryn", "person")
    s.upsert_entity("joel", "Joel", "person")
    s.add_relation("taryn", "reports_to", "joel", "doc-1")
    with s._connect() as db:
        db.execute("UPDATE entity_relations SET valid_from=?, valid_to=? "
                   "WHERE entity_a='taryn'", ("2024-01-01", "2025-01-01"))

    tool = make_brain_graph(s)
    inside = asyncio.run(tool("taryn", 1, at_time="2024-06-01"))
    assert {n["id"] for n in inside["nodes"]} == {"taryn", "joel"}
    after = asyncio.run(tool("taryn", 1, at_time="2025-06-01"))
    assert {n["id"] for n in after["nodes"]} == {"taryn"}  # edge no longer valid


def test_brain_graph_include_invalidated(tmp_path):
    s = Store(tmp_path / "gi.sqlite3", dim=4)
    s.init()
    s.upsert_entity("taryn", "Taryn", "person")
    s.upsert_entity("joel", "Joel", "person")
    s.add_relation("taryn", "reports_to", "joel", "doc-1")
    with s._connect() as db:
        db.execute("UPDATE entity_relations SET invalidated_at=? "
                   "WHERE entity_a='taryn'", ("2025-02-01",))

    tool = make_brain_graph(s)
    default = asyncio.run(tool("taryn", 1))
    assert {n["id"] for n in default["nodes"]} == {"taryn"}  # invalidated edge hidden
    incl = asyncio.run(tool("taryn", 1, include_invalidated=True))
    assert {n["id"] for n in incl["nodes"]} == {"taryn", "joel"}


# --- brain_actions MCP tool (Task 8.2) -----------------------------------

from mcpbrain.mcp_server import make_brain_actions


def test_make_brain_actions(tmp_path):
    s = Store(tmp_path / "act.sqlite3", dim=4)
    s.init()
    s.add_unified_action(text="Draft policy", owner="Josh", status="open",
                         thread_id="t1")
    s.add_unified_action(text="Send budget", owner="Josh", status="done",
                         thread_id="t1")
    s.add_unified_action(text="Book hall", owner="Taryn", status="open")

    tool = make_brain_actions(s)

    # Defaults: owner=Josh, status=open.
    out = asyncio.run(tool())
    assert [a["text"] for a in out] == ["Draft policy"]
    # Freshness annotation applied to every row.
    assert all(a["freshness"] in ("fresh", "stale") for a in out)

    # Owner + status filter.
    josh_done = asyncio.run(tool(owner="Josh", status="done"))
    assert [a["text"] for a in josh_done] == ["Send budget"]

    # Different owner.
    taryn = asyncio.run(tool(owner="Taryn", status="open"))
    assert [a["text"] for a in taryn] == ["Book hall"]


def test_brain_actions_explicit_null_owner_defaults_to_josh(tmp_path):
    """An MCP client passing an explicit null owner must default to Josh, not
    widen to every owner. unified_actions(owner=None) would return all owners,
    leaking Taryn's actions into a Josh-scoped query."""
    s = Store(tmp_path / "null.sqlite3", dim=4)
    s.init()
    s.add_unified_action(text="Draft policy", owner="Josh", status="open")
    s.add_unified_action(text="Book hall", owner="Taryn", status="open")

    tool = make_brain_actions(s)

    # Explicit None for both owner and status must fall back to the defaults.
    out = asyncio.run(tool(owner=None, status=None))
    texts = {a["text"] for a in out}
    assert texts == {"Draft policy"}  # scoped to Josh, not all owners
    assert "Book hall" not in texts


def test_brain_context_owner_shortform_does_not_match(tmp_path):
    """Pins accepted behaviour: a Gemini-extracted short-form owner ("Taryn")
    does NOT match a full entity name ("Taryn Hamilton"), so brain_context
    surfaces no actions for it. This is understood, not a silent surprise."""
    s = Store(tmp_path / "sf.sqlite3", dim=4)
    s.init()
    s.upsert_entity("taryn-hamilton", "Taryn Hamilton", "person", org="Centrepoint")
    s.add_unified_action(text="Confirm college timetable", owner="Taryn")  # short form, no match

    tool = make_brain_context(s)
    out = asyncio.run(tool("taryn-hamilton"))
    assert out["actions"] == []


# --- brain_context mode=communities (Phase 3 Task 1.4) --------------------


def _seed_communities(store):
    """Insert two community records directly via replace_communities."""
    partition = {
        "alice-id": 0,
        "bob-id": 0,
        "carol-id": 1,
    }
    store.upsert_entity("alice-id", "Alice", "person")
    store.upsert_entity("bob-id", "Bob", "person")
    store.upsert_entity("carol-id", "Carol", "person")
    summaries = {
        0: {"member_count": 2, "key_entities": "Alice, Bob", "title": "", "summary": "",
            "updated": "2026-06-03"},
        1: {"member_count": 1, "key_entities": "Carol", "title": "", "summary": "",
            "updated": "2026-06-03"},
    }
    store.replace_communities(partition, summaries)
    return partition, summaries


def test_brain_context_mode_communities(tmp_path):
    """mode='communities' with no community_id returns all community_summaries rows."""
    s = Store(tmp_path / "comm.sqlite3", dim=4)
    s.init()
    _seed_communities(s)

    tool = make_brain_context(s)
    out = asyncio.run(tool(mode="communities"))

    assert isinstance(out, list)
    assert len(out) == 2

    community_ids = {row["community_id"] for row in out}
    assert community_ids == {0, 1}

    # Each row must have member_count and key_entities.
    for row in out:
        assert "member_count" in row
        assert "key_entities" in row


def test_brain_context_mode_community_detail(tmp_path):
    """mode='communities' with community_id returns the member entities list."""
    s = Store(tmp_path / "comm2.sqlite3", dim=4)
    s.init()
    _seed_communities(s)

    tool = make_brain_context(s)
    out = asyncio.run(tool(mode="communities", community_id=0))

    assert isinstance(out, list)
    # Community 0 has Alice and Bob.
    member_ids = {row["id"] for row in out}
    assert member_ids == {"alice-id", "bob-id"}


def test_brain_context_communities_empty_store(tmp_path):
    """mode='communities' on a store with no communities returns an empty list."""
    s = Store(tmp_path / "empty.sqlite3", dim=4)
    s.init()

    tool = make_brain_context(s)
    out = asyncio.run(tool(mode="communities"))

    assert out == []


def test_brain_context_profile_still_works_after_signature_change(tmp_path):
    """Regression: the existing profile path still works with the new signature."""
    s = _seed_graph_store(tmp_path)
    tool = make_brain_context(s)
    out = asyncio.run(tool("taryn-hamilton", mode="profile"))
    assert out["entity"]["id"] == "taryn-hamilton"


# --- brain_proactive MCP tool (Phase 3 Task 4.4) -------------------------

from mcpbrain.mcp_server import make_brain_proactive


def _seed_proactive_store(tmp_path, name="pf.sqlite3"):
    """Insert proactive findings directly via store.record_finding."""
    s = Store(tmp_path / name, dim=4)
    s.init()
    # Insert a project finding
    s.record_finding(
        "project_no_next_action", "p-gap",
        summary="Project 'College 2026' has no open next action",
        severity="info",
    )
    # Insert an area finding
    s.record_finding(
        "area_overdue", "a-ops",
        org="Centrepoint",
        summary="Area 'Operations' overdue by 3 days (weekly)",
        severity="info",
    )
    return s


def test_brain_proactive_returns_open_findings(tmp_path):
    """brain_proactive() with no filter returns both open findings."""
    s = _seed_proactive_store(tmp_path)
    tool = make_brain_proactive(s)
    out = asyncio.run(tool())
    assert len(out) == 2
    finding_types = {f["finding_type"] for f in out}
    assert finding_types == {"project_no_next_action", "area_overdue"}


def test_brain_proactive_filter_by_type(tmp_path):
    """brain_proactive(finding_type=...) returns only that type."""
    s = _seed_proactive_store(tmp_path)
    tool = make_brain_proactive(s)

    projects_only = asyncio.run(tool(finding_type="project_no_next_action"))
    assert len(projects_only) == 1
    assert projects_only[0]["ref_id"] == "p-gap"

    areas_only = asyncio.run(tool(finding_type="area_overdue"))
    assert len(areas_only) == 1
    assert areas_only[0]["ref_id"] == "a-ops"


def test_brain_proactive_includes_lint(tmp_path):
    """Lint findings (from lint_graph) live in proactive_findings alongside
    proactive findings; brain_proactive returns both when no filter is given."""
    s = _seed_proactive_store(tmp_path, name="lint_pf.sqlite3")
    # Insert a lint finding (same table, different finding_type)
    s.record_finding(
        "lint:missing_org", "taryn-hamilton",
        summary="Entity 'Taryn Hamilton' has no org",
        severity="warning",
    )
    tool = make_brain_proactive(s)

    # No filter -> all three findings returned
    out = asyncio.run(tool())
    assert len(out) == 3
    finding_types = {f["finding_type"] for f in out}
    assert "lint:missing_org" in finding_types

    # Filter by lint type only
    lint_only = asyncio.run(tool(finding_type="lint:missing_org"))
    assert len(lint_only) == 1
    assert lint_only[0]["ref_id"] == "taryn-hamilton"


def test_brain_proactive_filter_by_severity(tmp_path):
    """brain_proactive(severity=...) filters to that severity level."""
    s = _seed_proactive_store(tmp_path, name="sev_pf.sqlite3")
    s.record_finding(
        "lint:missing_org", "some-entity",
        summary="Entity has no org",
        severity="warning",
    )
    tool = make_brain_proactive(s)

    # "info" severity: should return the project and area findings
    info_findings = asyncio.run(tool(severity="info"))
    assert all(f["severity"] == "info" for f in info_findings)
    assert len(info_findings) == 2

    # "warning" severity: should return only the lint finding
    warn_findings = asyncio.run(tool(severity="warning"))
    assert len(warn_findings) == 1
    assert warn_findings[0]["finding_type"] == "lint:missing_org"


def test_brain_proactive_resolved_findings_excluded(tmp_path):
    """Resolved (closed) findings are not returned by brain_proactive."""
    s = _seed_proactive_store(tmp_path, name="res_pf.sqlite3")
    # Resolve the project finding
    with s._connect() as db:
        db.execute(
            "UPDATE proactive_findings SET resolved_at='2026-06-03T10:00:00Z' "
            "WHERE finding_type='project_no_next_action'"
        )
    tool = make_brain_proactive(s)
    out = asyncio.run(tool())
    assert all(f["finding_type"] != "project_no_next_action" for f in out)
    assert len(out) == 1  # only area_overdue remains


def test_brain_proactive_empty_store_returns_empty_list(tmp_path):
    """brain_proactive() on a store with no findings returns []."""
    s = Store(tmp_path / "empty_pf.sqlite3", dim=4)
    s.init()
    tool = make_brain_proactive(s)
    out = asyncio.run(tool())
    assert out == []


def test_brain_proactive_error_returns_empty_list(tmp_path, caplog):
    """An internal failure returns [] rather than raising."""
    s = Store(tmp_path / "err_pf.sqlite3", dim=4)
    s.init()
    tool = make_brain_proactive(s)
    with patch("mcpbrain.mcp_server.make_brain_proactive") as _:
        # Patch open_findings on the store to raise
        original = s.open_findings
        s.open_findings = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db-boom"))
        with caplog.at_level(logging.ERROR, logger="mcpbrain.mcp_server"):
            out = asyncio.run(tool())
        s.open_findings = original
    assert out == []
