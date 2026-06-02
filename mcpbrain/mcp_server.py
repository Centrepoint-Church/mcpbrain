import logging

from mcpbrain.retrieval import annotate_action_freshness, hybrid_search

_log = logging.getLogger("mcpbrain.mcp_server")


def make_brain_search(store, embedder):
    async def brain_search(query: str, limit: int = 10) -> list[dict]:
        try:
            return hybrid_search(store, embedder, query, limit)
        except Exception:
            _log.exception("brain_search failed for query %r", query)
            return []
    return brain_search


def make_brain_context(store):
    async def brain_context(entity: str) -> dict:
        """Profile an entity: its record, relations (in + out), and actions it owns.
        Returns {} when the entity is unknown."""
        try:
            ent = store.find_entity(entity)
            if not ent:
                return {}
            rels = store.relations_for(ent["id"])
            relations = []
            for r in rels:
                if r["entity_a"] == ent["id"]:
                    relations.append({"relation": r["relation"], "other": r["entity_b"],
                                      "direction": "out", "source_doc_id": r["source_doc_id"]})
                else:
                    relations.append({"relation": r["relation"], "other": r["entity_a"],
                                      "direction": "in", "source_doc_id": r["source_doc_id"]})
            # owner must match ent["name"] exactly (case-insensitive); Gemini-extracted owners may use short forms and won't match.
            # annotate_action_freshness is read-time only (no DB writes); keeps the MCP tool read-only.
            actions = annotate_action_freshness(store, store.actions_for_owner(ent["name"]))
            return {"entity": ent, "relations": relations, "actions": actions}
        except Exception:
            _log.exception("brain_context failed for %r", entity)
            return {}
    return brain_context


def make_brain_graph(store):
    async def brain_graph(entity: str, hops: int = 1) -> dict:
        """Traverse the relationship graph from an entity up to `hops` (capped at 3).
        Returns {center, nodes:[entity dicts], edges:[{entity_a,relation,entity_b}]}; {} if unknown."""
        try:
            center = store.find_entity(entity)
            if not center:
                return {}
            depth = max(0, min(hops, 3))  # cap; guard against runaway traversal
            visited = {center["id"]}
            edges = {}  # (entity_a, relation, entity_b) -> dict, dedup
            frontier = {center["id"]}
            for _ in range(depth):
                next_frontier = set()
                for ent_id in frontier:
                    for r in store.relations_for(ent_id):
                        key = (r["entity_a"], r["relation"], r["entity_b"])
                        if key not in edges:
                            edges[key] = {"entity_a": r["entity_a"], "relation": r["relation"],
                                          "entity_b": r["entity_b"]}
                        for nbr in (r["entity_a"], r["entity_b"]):
                            if nbr not in visited:
                                visited.add(nbr)
                                next_frontier.add(nbr)
                frontier = next_frontier
                if not frontier:
                    break
            nodes = [n for n in (store.get_entity(i) for i in visited) if n]
            return {"center": center, "nodes": nodes, "edges": list(edges.values())}
        except Exception:
            _log.exception("brain_graph failed for %r", entity)
            return {}
    return brain_graph


def main() -> None:  # stdio entry point, exercised manually + in P3 integration
    import mcp.server.stdio
    from mcp.server import Server
    from mcp import types
    from mcpbrain import config
    from mcpbrain.store import Store
    from mcpbrain.embed import get_embedder
    emb = get_embedder(config.EMBEDDER)
    store = Store(config.store_path(), dim=emb.dim, read_only=True)  # daemon is sole writer
    search = make_brain_search(store, emb)
    context = make_brain_context(store)
    graph = make_brain_graph(store)
    server = Server("mcpbrain")

    @server.list_tools()
    async def _tools():
        return [
            types.Tool(
                name="brain_search",
                description="Search your Gmail/Calendar/Drive index.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "default": 10},
                    },
                    "required": ["query"],
                },
            ),
            types.Tool(
                name="brain_read",
                description="Fetch the full text + metadata of a chunk by doc_id.",
                inputSchema={
                    "type": "object",
                    "properties": {"doc_id": {"type": "string"}},
                    "required": ["doc_id"],
                },
            ),
            types.Tool(
                name="brain_context",
                description="Profile an entity: record, relations, and actions it owns.",
                inputSchema={
                    "type": "object",
                    "properties": {"entity": {"type": "string"}},
                    "required": ["entity"],
                },
            ),
            types.Tool(
                name="brain_graph",
                description="Traverse the relationship graph from an entity up to `hops` (max 3).",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "entity": {"type": "string"},
                        "hops": {"type": "integer", "default": 1},
                    },
                    "required": ["entity"],
                },
            ),
        ]

    @server.call_tool()
    async def _call(name, arguments):
        import json
        if name == "brain_read":
            chunk = store.get_chunk(arguments["doc_id"])
            return [types.TextContent(type="text", text=json.dumps(chunk))]
        if name == "brain_context":
            out = await context(arguments["entity"])
            return [types.TextContent(type="text", text=json.dumps(out))]
        if name == "brain_graph":
            out = await graph(arguments["entity"], arguments.get("hops", 1))
            return [types.TextContent(type="text", text=json.dumps(out))]
        results = await search(arguments["query"], arguments.get("limit", 10))
        return [types.TextContent(type="text", text=json.dumps(results))]

    async def _run():
        async with mcp.server.stdio.stdio_server() as (r, w):
            await server.run(r, w, server.create_initialization_options())

    import asyncio
    asyncio.run(_run())


if __name__ == "__main__":  # spawnable: python -m mcpbrain.mcp_server
    main()
