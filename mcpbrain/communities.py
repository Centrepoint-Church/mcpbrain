"""Leiden community detection pass for mcpbrain.

Builds a weighted NetworkX graph from entity_relations, runs Leiden clustering
via igraph/leidenalg, then saves the resulting community membership into the
store using store.replace_communities().

Usage (from the daemon, via maybe_communities):
    from mcpbrain.communities import run
    result = run(store)   # {"communities": N, "entities": M}

Direct CLI:
    python -m mcpbrain.communities
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone

import networkx as nx

log = logging.getLogger("mcpbrain.communities")


def build_graph(store) -> nx.Graph:
    """Load entity_relations into a weighted NetworkX graph.

    Only live (non-invalidated, non-expired) relations with strength > 0 are
    loaded. If two separate relation rows exist between the same pair of entities
    (possible when add_relation is called from multiple source docs) their
    strengths are summed on the single undirected edge.
    """
    G = nx.Graph()
    with store._connect() as db:
        rels = db.execute(
            "SELECT entity_a, entity_b, strength FROM entity_relations "
            "WHERE strength > 0 AND invalidated_at IS NULL AND valid_to IS NULL"
        ).fetchall()
        for r in rels:
            if G.has_edge(r["entity_a"], r["entity_b"]):
                G[r["entity_a"]][r["entity_b"]]["weight"] += r["strength"]
            else:
                G.add_edge(r["entity_a"], r["entity_b"], weight=r["strength"])
    log.debug("graph: %d nodes, %d edges", G.number_of_nodes(), G.number_of_edges())
    return G


def detect_communities(G: nx.Graph) -> dict:
    """Run Leiden community detection on G.

    Returns {entity_id: community_id} for all nodes in the largest connected
    component. Nodes outside the LCC are excluded (log a warning if any).

    Returns {} if G has no edges.
    Returns {"skipped": "leiden unavailable"} if igraph/leidenalg are missing.
    The caller (run / maybe_communities) treats the "skipped" key as a noop.

    IMPORTANT: do NOT substitute connected-components as a fallback — it changes
    membership semantics and produces subtly wrong downstream results.
    """
    if G.number_of_edges() == 0:
        log.warning("no edges in graph — cannot detect communities")
        return {}

    try:
        import igraph as ig
        import leidenalg
    except ImportError:
        log.warning("leiden stack unavailable; skipping community detection")
        # Defensive branch — the shared .venv has igraph/leidenalg.
        # Do NOT substitute connected-components: it changes membership semantics.
        return {"skipped": "leiden unavailable"}

    # Run on largest connected component for stability.
    largest_cc = max(nx.connected_components(G), key=len)
    subgraph = G.subgraph(largest_cc)
    excluded = G.number_of_nodes() - len(largest_cc)
    if excluded > 0:
        log.warning("%d nodes excluded — not in largest connected component", excluded)

    node_list = list(subgraph.nodes())
    node_idx = {n: i for i, n in enumerate(node_list)}
    edges = [(node_idx[u], node_idx[v]) for u, v in subgraph.edges()]
    weights = [subgraph[u][v].get("weight", 1) for u, v in subgraph.edges()]

    ig_graph = ig.Graph(n=len(node_list), edges=edges, directed=False)
    ig_graph.es["weight"] = weights

    partition = leidenalg.find_partition(
        ig_graph,
        leidenalg.RBConfigurationVertexPartition,
        weights="weight",
        resolution_parameter=1.5,
        n_iterations=10,
        seed=42,
    )

    result = {node_list[i]: partition.membership[i] for i in range(len(node_list))}
    n_communities = len(set(partition.membership))
    log.info(
        "detected %d communities across %d entities", n_communities, len(result)
    )
    return result


def _save(store, partition: dict) -> None:
    """Save community assignments using store.replace_communities.

    Builds a per-community summary (member_count + top-5-by-email_count key
    entities) then calls store.replace_communities() which atomically replaces
    both tables in a single transaction.
    """
    if not partition:
        return

    community_members: dict[int, list] = defaultdict(list)
    for eid, cid in partition.items():
        community_members[cid].append(eid)

    summaries: dict[int, dict] = {}
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with store._connect() as db:
        for cid, members in community_members.items():
            placeholders = ",".join("?" * len(members))
            rows = db.execute(
                f"SELECT id, name, email_count FROM entities WHERE id IN ({placeholders})"
                " ORDER BY email_count DESC LIMIT 5",
                members,
            ).fetchall()
            key_names = ", ".join(r["name"] for r in rows)
            summaries[cid] = {
                "member_count": len(members),
                "key_entities": key_names,
                "title": "",
                "summary": "",
                "updated": today,
            }

    store.replace_communities(partition, summaries)
    log.info("saved %d community assignments", len(partition))


def run(store) -> dict:
    """Run the full community detection pass: build graph, detect, save.

    Returns a summary dict:
      - {"communities": 0}                   — no edges
      - {"communities": 0, "skipped": ...}   — leiden unavailable
      - {"communities": N, "entities": M}    — success
    """
    G = build_graph(store)
    if G.number_of_edges() == 0:
        return {"communities": 0}

    partition = detect_communities(G)
    if not partition or "skipped" in partition:
        return {"communities": 0, **partition}

    _save(store, partition)
    n_communities = len(set(partition.values()))
    return {"communities": n_communities, "entities": len(partition)}
