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
    s.add_action("Confirm college timetable", owner="Taryn Hamilton")
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
    s.add_action("Confirm college timetable", owner="Taryn Hamilton",
                 source_doc_id="msg-req", thread_id="t-1")

    tool = make_brain_context(s)
    out = asyncio.run(tool("taryn-hamilton"))

    assert out["actions"], "expected at least one action"
    for a in out["actions"]:
        assert a["freshness"] in ("fresh", "stale")
    # The resolved thread makes this action stale.
    stale = [a for a in out["actions"] if a["text"] == "Confirm college timetable"]
    assert stale and stale[0]["freshness"] == "stale"


def test_brain_context_owner_shortform_does_not_match(tmp_path):
    """Pins accepted behaviour: a Gemini-extracted short-form owner ("Taryn")
    does NOT match a full entity name ("Taryn Hamilton"), so brain_context
    surfaces no actions for it. This is understood, not a silent surprise."""
    s = Store(tmp_path / "sf.sqlite3", dim=4)
    s.init()
    s.upsert_entity("taryn-hamilton", "Taryn Hamilton", "person", org="Centrepoint")
    s.add_action("Confirm college timetable", owner="Taryn")  # short form, no match

    tool = make_brain_context(s)
    out = asyncio.run(tool("taryn-hamilton"))
    assert out["actions"] == []
