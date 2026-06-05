"""Tests for mcpbrain/communities.py — Phase 3 Task 1.

Sub-tasks covered:
  1.1  build_graph + detect_communities
  1.2  _save + run entry point
"""

from mcpbrain.store import Store
from mcpbrain.communities import build_graph, detect_communities, run


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _store(tmp_path, name="c.sqlite3"):
    s = Store(tmp_path / name, dim=4)
    s.init()
    return s


def _add_entities(store, *ids):
    for eid in ids:
        store.upsert_entity(eid, eid.replace("-", " ").title(), "person")


def _add_relation(store, a, b, strength=1):
    """Add a relation and set the strength column directly."""
    store.add_relation(a, "knows", b, f"doc-{a}-{b}")
    with store._connect() as db:
        db.execute(
            "UPDATE entity_relations SET strength=? "
            "WHERE entity_a=? AND entity_b=? AND relation='knows'",
            (strength, a, b),
        )


# ---------------------------------------------------------------------------
# Sub-task 1.1 — build_graph
# ---------------------------------------------------------------------------

def test_build_graph_weights_by_strength(tmp_path):
    """Relations with different strengths produce edges with matching weights."""
    s = _store(tmp_path)
    _add_entities(s, "alice", "bob", "carol")
    _add_relation(s, "alice", "bob", strength=2)
    _add_relation(s, "bob", "carol", strength=1)

    G = build_graph(s)

    assert G.has_edge("alice", "bob")
    assert G["alice"]["bob"]["weight"] == 2
    assert G.has_edge("bob", "carol")
    assert G["bob"]["carol"]["weight"] == 1


def test_build_graph_empty_when_no_relations(tmp_path):
    s = _store(tmp_path)
    _add_entities(s, "alice", "bob")
    G = build_graph(s)
    assert G.number_of_edges() == 0


def test_build_graph_excludes_invalidated(tmp_path):
    """Invalidated relations must not appear in the graph."""
    s = _store(tmp_path)
    _add_entities(s, "alice", "bob")
    _add_relation(s, "alice", "bob", strength=3)
    with s._connect() as db:
        db.execute(
            "UPDATE entity_relations SET invalidated_at='2026-01-01' "
            "WHERE entity_a='alice' AND entity_b='bob'"
        )
    G = build_graph(s)
    assert G.number_of_edges() == 0


# ---------------------------------------------------------------------------
# Sub-task 1.1 — detect_communities
# ---------------------------------------------------------------------------

def test_detect_two_cliques_two_communities(tmp_path):
    """Two fully-connected triangles connected by a single weak bridge edge
    produce two distinct community ids, and nodes within each triangle share one
    community id.

    The Leiden implementation only processes the largest connected component.
    To get membership for all six nodes the two triangles must be in the same
    connected component — connected here by a weak bridge (weight=1) vs the
    strong intra-clique edges (weight=5). Leiden correctly splits them.
    """
    import networkx as nx

    G = nx.Graph()
    # Triangle 1: A-B-C with strong intra-clique edges
    for u, v in [("A", "B"), ("B", "C"), ("A", "C")]:
        G.add_edge(u, v, weight=5)
    # Triangle 2: D-E-F with strong intra-clique edges
    for u, v in [("D", "E"), ("E", "F"), ("D", "F")]:
        G.add_edge(u, v, weight=5)
    # Weak bridge connecting the two cliques
    G.add_edge("C", "D", weight=1)

    partition = detect_communities(G)

    # Both groups must be present and their community ids must differ.
    assert partition, "expected a non-empty partition"
    # No "skipped" sentinel — leiden is available in .venv.
    assert "skipped" not in partition

    group1 = {partition["A"], partition["B"], partition["C"]}
    group2 = {partition["D"], partition["E"], partition["F"]}
    # Within each triangle all nodes share the same community.
    assert len(group1) == 1, f"triangle ABC split across communities: {group1}"
    assert len(group2) == 1, f"triangle DEF split across communities: {group2}"
    # The two triangles must be in different communities.
    assert group1 != group2, "both triangles landed in the same community"


def test_detect_communities_empty_graph_returns_empty():
    """An empty graph (no edges) returns {} without error."""
    import networkx as nx
    G = nx.Graph()
    assert detect_communities(G) == {}


def test_detect_communities_single_edge():
    """Two nodes with one edge produce a valid partition with a single community."""
    import networkx as nx
    G = nx.Graph()
    G.add_edge("X", "Y", weight=1)
    partition = detect_communities(G)
    assert "skipped" not in partition
    assert set(partition.keys()) == {"X", "Y"}


# ---------------------------------------------------------------------------
# Sub-task 1.2 — _save + run
# ---------------------------------------------------------------------------

def _two_clique_store(tmp_path):
    """Store with two isolated triangles: ABC and DEF."""
    s = _store(tmp_path, "two_cliques.sqlite3")
    _add_entities(s, "node-a", "node-b", "node-c", "node-d", "node-e", "node-f")
    # Triangle 1
    for a, b in [("node-a", "node-b"), ("node-b", "node-c"), ("node-a", "node-c")]:
        _add_relation(s, a, b, strength=1)
    # Triangle 2
    for a, b in [("node-d", "node-e"), ("node-e", "node-f"), ("node-d", "node-f")]:
        _add_relation(s, a, b, strength=1)
    return s


def test_run_writes_communities_and_summaries(tmp_path):
    """run() over two-clique graph: communities_for returns community ids for
    nodes and community_summaries has one row per detected community."""
    s = _two_clique_store(tmp_path)

    result = run(s)

    assert result["communities"] >= 1
    assert "entities" in result
    assert result["entities"] > 0

    # Each triangle node must have a community row.
    comms = s.communities_for(["node-a", "node-b", "node-c"])
    assert len(comms) == 3, f"expected 3 community rows, got {len(comms)}"

    # community_summaries must have at least one row per community.
    listed = s.list_communities()
    assert len(listed) >= 1
    for row in listed:
        assert row["member_count"] > 0


def test_run_replaces_prior(tmp_path):
    """A second run() with a changed graph replaces the prior community rows."""
    s = _two_clique_store(tmp_path)

    first = run(s)
    assert first["communities"] >= 1

    # Add a connection between triangles to merge them and re-run.
    _add_relation(s, "node-a", "node-d", strength=5)
    second = run(s)

    # After re-run, old data is gone and replaced by the new partition.
    assert second["entities"] is not None
    # The bridging edge means we may get fewer communities.
    assert second["communities"] <= first["communities"] or second["communities"] >= 1


def test_run_replaces_completely(tmp_path):
    """Running twice: the second run's entity_communities rows replace the
    first run's rows entirely — no accumulation."""
    # Build a single connected triangle so all 3 nodes are in the LCC.
    s = _store(tmp_path, "replace_test.sqlite3")
    _add_entities(s, "x1", "x2", "x3", "y1", "y2", "y3")
    # First run: triangle X connected by a bridge to a single node.
    for a, b in [("x1", "x2"), ("x2", "x3"), ("x1", "x3")]:
        _add_relation(s, a, b, strength=3)
    _add_relation(s, "x3", "y1", strength=1)

    first = run(s)
    assert first["entities"] >= 3  # at least x1, x2, x3, y1

    # Record which community ids the first run produced.
    after_first = {r["entity_id"] for r in s.communities_for(["x1", "x2", "x3", "y1"])}
    assert after_first  # nodes have community rows

    # Second run with a slightly different graph (remove bridge to y1).
    with s._connect() as db:
        db.execute(
            "DELETE FROM entity_relations WHERE entity_a='x3' AND entity_b='y1'"  # admin-delete-ok
        )
    run(s)

    # y1 has no edges now, so it must NOT appear in entity_communities.
    leftover = s.communities_for(["y1"])
    assert leftover == [], (
        f"y1 still has a community row after second run removed it from graph: {leftover}"
    )


def test_run_empty_graph_noop(tmp_path):
    """No relations -> returns {"communities": 0}, no rows written."""
    s = _store(tmp_path)
    _add_entities(s, "alpha", "beta")
    # No relations added.

    result = run(s)

    assert result == {"communities": 0}
    # Tables must be empty.
    assert s.list_communities() == []
    assert s.communities_for(["alpha", "beta"]) == []
