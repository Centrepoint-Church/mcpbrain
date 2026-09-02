"""brain_graph must stay bounded on a hub-heavy graph.

The live store's owner entity carries ~7,262 relations, so an unbounded BFS
from any entity that touches it reached 18,397 nodes / 55,034 edges -- a 14 MB
payload taking 34s at hops=2 and 177s at hops=3, which times out real clients.
Depth was capped (GRAPH_MAX_HOPS); breadth was not.

These pin the two bounds that fix it: hubs are INCLUDED but not TRAVERSED
THROUGH, and the visited set has a hard budget. Both report themselves in the
result rather than truncating silently.
"""
import asyncio

from mcpbrain.store import Store
from mcpbrain.tools import GRAPH_HUB_DEGREE, GRAPH_MAX_NODES, make_brain_graph


def _hub_store(tmp_path, spokes=GRAPH_HUB_DEGREE + 40):
    """center -> hub -> {spokes}. The hub's degree is over the threshold."""
    s = Store(str(tmp_path / "h.sqlite3"), dim=4)
    s.init()
    s.upsert_entity("center", "Center", "org")
    s.upsert_entity("hub", "Hub", "person")
    s.add_relation("center", "works_at", "hub", "doc-0")
    for i in range(spokes):
        s.upsert_entity(f"spoke-{i:04d}", f"Spoke {i}", "person")
        s.add_relation("hub", "works_at", f"spoke-{i:04d}", f"doc-{i}")
    return s


def test_hub_is_included_but_not_expanded(tmp_path):
    """The hub itself is visible, but its ~200 spokes are not dragged in."""
    tool = make_brain_graph(_hub_store(tmp_path))
    out = asyncio.run(tool("center", 2))
    ids = {n["id"] for n in out["nodes"]}
    assert "hub" in ids, "the hub must still be reported as a connection"
    assert not [i for i in ids if i.startswith("spoke-")], \
        "traversal must not expand THROUGH a hub"
    assert "hub" in out["hubs_not_expanded"]


def test_center_hub_still_expands(tmp_path):
    """Asking ABOUT a hub must still return its neighbourhood."""
    tool = make_brain_graph(_hub_store(tmp_path))
    out = asyncio.run(tool("hub", 1))
    ids = {n["id"] for n in out["nodes"]}
    assert len([i for i in ids if i.startswith("spoke-")]) > 0, \
        "the center always expands, even when it is a hub"
    assert out["hubs_not_expanded"] == []


def test_node_budget_is_enforced(tmp_path):
    """A wide graph of non-hub nodes still cannot exceed the node budget."""
    s = Store(str(tmp_path / "w.sqlite3"), dim=4)
    s.init()
    s.upsert_entity("center", "Center", "org")
    # Many branches, each below the hub threshold, that together blow the budget.
    branches = (GRAPH_MAX_NODES // 10) + 40
    for b in range(branches):
        bid = f"b-{b:04d}"
        s.upsert_entity(bid, f"B {b}", "person")
        s.add_relation("center", "works_at", bid, f"d-{b}")
        for i in range(10):
            lid = f"l-{b:04d}-{i}"
            s.upsert_entity(lid, f"L {b}.{i}", "person")
            s.add_relation(bid, "works_at", lid, f"d-{b}-{i}")
    out = asyncio.run(make_brain_graph(s)("center", 3))
    assert len(out["nodes"]) <= GRAPH_MAX_NODES
    assert out["truncated"] is True


def test_small_graph_is_untouched_and_not_flagged(tmp_path):
    """The ordinary case keeps exactly today's behaviour and reports no bounding."""
    s = Store(str(tmp_path / "s.sqlite3"), dim=4)
    s.init()
    for e in ("taryn-hamilton", "joel-chelliah"):
        s.upsert_entity(e, e.replace("-", " ").title(), "person")
    s.upsert_entity("college-2026", "College 2026", "project")
    s.add_relation("taryn-hamilton", "reports_to", "joel-chelliah", "doc-1")
    s.add_relation("taryn-hamilton", "works_on", "college-2026", "doc-2")
    out = asyncio.run(make_brain_graph(s)("taryn-hamilton", 2))
    assert {n["id"] for n in out["nodes"]} == {
        "taryn-hamilton", "joel-chelliah", "college-2026"}
    assert out["truncated"] is False
    assert out["hubs_not_expanded"] == []


def test_result_is_deterministic(tmp_path):
    """Same store, same query -> byte-identical result (no set-iteration drift)."""
    s = _hub_store(tmp_path)
    tool = make_brain_graph(s)
    first = asyncio.run(tool("center", 3))
    for _ in range(4):
        assert asyncio.run(tool("center", 3)) == first


def test_store_batch_helpers(tmp_path):
    """The batch accessors the bounded traversal needs, instead of N+1 queries."""
    s = _hub_store(tmp_path, spokes=5)
    got = s.get_entities(["hub", "center", "missing"])
    assert set(got) == {"hub", "center"}
    assert got["hub"]["name"] == "Hub"
    deg = s.entity_degrees(["hub", "center", "missing"])
    assert deg["hub"] == 6      # 1 to center + 5 spokes
    assert deg["center"] == 1
    assert deg["missing"] == 0
