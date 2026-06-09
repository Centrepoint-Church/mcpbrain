import logging
from pathlib import Path

from mcpbrain import config

from mcpbrain.retrieval import annotate_action_freshness, hybrid_search

_log = logging.getLogger("mcpbrain.mcp_server")


def _default_owner() -> str:
    """The install owner for MCP-initiated writes, from config (empty if unset)."""
    return config.owner_name(str(config.app_dir()))


async def list_context_resources():
    """Return types.Resource entries for every *.md in ~/.mcpbrain/context/.

    # NOTE: This serves files from app_dir/context/. Draft voice rules are read from
    # records_dir/context/voice.md (see draft._load_voice_rules). These two paths differ
    # when records_dir != app_dir/records. Align in a future pass.
    """
    from mcp import types
    ctx = config.app_dir() / "context"
    if not ctx.is_dir():
        return []
    resources = []
    for md in sorted(ctx.glob("*.md")):
        resources.append(types.Resource(
            uri=f"file://{md.resolve()}",
            name=md.name,
            mimeType="text/markdown",
        ))
    return resources


async def read_context_resource(uri) -> str:
    """Return the text content of a context/*.md resource identified by uri.

    Raises ValueError if the resolved path is outside the context directory.
    """
    ctx = (config.app_dir() / "context").resolve()
    path = Path(str(uri).replace("file://", "")).resolve()
    # Containment, defence in depth: the file must sit directly in context/
    # (parent == ctx) AND ctx must be an ancestor of the resolved path. The
    # second check rejects nested subdirs and any ../ escape that resolves out
    # of the context tree.
    if path.parent != ctx or ctx not in path.parents:
        raise ValueError(f"resource outside context dir: {uri}")
    return path.read_text(encoding="utf-8")


def make_brain_search(store, embedder):
    async def brain_search(query: str, limit: int = 10) -> list[dict]:
        try:
            return hybrid_search(store, embedder, query, limit)
        except Exception:
            _log.exception("brain_search failed for query %r", query)
            return []
    return brain_search


def make_brain_context(store):
    async def brain_context(entity: str = "", mode: str = "profile",
                            community_id: int | None = None) -> dict | list:
        """Profile an entity or list community clusters.

        mode="profile" (default): entity is required. Returns the entity record,
            its relations (in + out), the actions it owns, and the projects/areas
            it owns. Returns {} when the entity is unknown.

        mode="communities": entity is ignored.
            - If community_id is given: returns the list of entity dicts that
              are members of that community.
            - Otherwise: returns all community_summaries rows (list of dicts).
        """
        try:
            if mode == "communities":
                if community_id is not None:
                    return store.community_members(community_id)
                return store.list_communities()

            # mode == "profile" (default)
            if not entity:
                return {}
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
            # Actions now come from the unified actions table, not graph_actions_legacy.
            # annotate_action_freshness is read-time only (no DB writes); keeps the MCP tool read-only.
            actions = annotate_action_freshness(store, store.unified_actions(owner=ent["name"]))
            projects = store.projects_owned_by(ent["id"])
            areas = store.areas_owned_by(ent["id"])
            return {"entity": {**ent, "profile": ent.get("profile", "")},
                    "relations": relations, "actions": actions,
                    "projects": projects, "areas": areas}
        except Exception:
            _log.exception("brain_context failed for entity=%r mode=%r", entity, mode)
            return {}
    return brain_context


def make_brain_actions(store):
    async def brain_actions(owner: str = "", status: str = "open") -> list[dict]:
        """Action items from the unified actions table, filtered by owner and
        status, with read-time freshness annotation. Empty owner defaults to
        the configured install owner. Returns [] on error."""
        try:
            if not owner:  # explicit None/empty must not widen to all owners
                owner = config.owner_name(str(config.app_dir()))
            status = status or "open"
            actions = store.unified_actions(owner=owner, status=status)
            return annotate_action_freshness(store, actions)
        except Exception:
            _log.exception("brain_actions failed for owner=%r status=%r", owner, status)
            return []
    return brain_actions


def make_brain_graph(store):
    async def brain_graph(entity: str, hops: int = 1, *, at_time: str | None = None,
                          include_invalidated: bool = False) -> dict:
        """Traverse the relationship graph from an entity up to `hops` (capped at 3).
        at_time scopes the traversal to relations valid at that ISO date;
        include_invalidated also follows superseded edges.
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
                    for r in store.relations_for(ent_id, at_time=at_time,
                                                 include_invalidated=include_invalidated):
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


def make_brain_proactive(store):
    async def brain_proactive(finding_type: str = "", severity: str = "") -> list:
        """Return open proactive findings, optionally filtered by type and/or severity."""
        try:
            findings = store.open_findings(finding_type or None)
            if severity:
                findings = [f for f in findings if f.get("severity") == severity]
            return findings
        except Exception:
            _log.exception("brain_proactive failed")
            return []
    return brain_proactive


def _capture_envelope(kind: str, source: str = "mcp", **fields) -> dict:
    from datetime import datetime, timezone
    return {"kind": kind, "source": source,
            "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            **fields}


def make_brain_ingest():
    async def brain_ingest(title: str, content: str, tags: str = "",
                           observation_type: str = "note", org: str = "") -> dict:
        """Save a note/decision/memory. QUEUED: searchable after the next
        sync cycle (~5 min), not immediately. Returns {queued, path|error}."""
        from mcpbrain.capture import write_capture
        try:
            p = write_capture(str(config.app_dir()), _capture_envelope(
                "ingest", title=title, content=content, tags=tags,
                observation_type=observation_type or "note", org=org))
            return {"queued": True, "path": str(p)}
        except (ValueError, OSError) as exc:
            return {"queued": False, "error": str(exc)}
    return brain_ingest


def make_brain_action_create():
    async def brain_action_create(text: str, owner: str = "", deadline: str = "",
                                  org: str = "", project_id: str = "",
                                  area_id: str = "") -> dict:
        """Create an action item. QUEUED: appears after the next sync cycle
        (~5 min). Empty owner defaults to the configured install owner."""
        from mcpbrain.capture import write_capture
        try:
            p = write_capture(str(config.app_dir()), _capture_envelope(
                "action_create", text=text, owner=owner, deadline=deadline,
                org=org, project_id=project_id, area_id=area_id))
            return {"queued": True, "path": str(p)}
        except (ValueError, OSError) as exc:
            return {"queued": False, "error": str(exc)}
    return brain_action_create


def make_brain_action_update():
    async def brain_action_update(action_id: int, status: str) -> dict:
        """Mark an action done or reopen it ('done'|'open'). QUEUED: applies
        on the next sync cycle (~5 min)."""
        from mcpbrain.capture import write_capture
        try:
            p = write_capture(str(config.app_dir()), _capture_envelope(
                "action_update", action_id=action_id, status=status))
            return {"queued": True, "path": str(p)}
        except (ValueError, OSError) as exc:
            return {"queued": False, "error": str(exc)}
    return brain_action_update


def make_brain_decision():
    async def brain_decision(text: str, rationale: str = "", owner: str = "",
                             supersedes: str = "", org: str = "") -> dict:
        """Record a decision. QUEUED: the daemon appends a row to state/decisions.md
        in your records repo and commits (one daemon cycle, ~seconds-minutes), not instantly."""
        from mcpbrain.capture import write_capture
        try:
            p = write_capture(str(config.app_dir()), _capture_envelope(
                "decision", text=text, rationale=rationale, owner=owner,
                supersedes=supersedes, org=org))
            return {"queued": True, "path": str(p)}
        except (ValueError, OSError) as exc:
            return {"queued": False, "error": str(exc)}
    return brain_decision


def make_brain_note():
    async def brain_note(text: str) -> dict:
        """Record a continuity note. QUEUED: the daemon prepends a dated entry to
        state/hot.md in your records repo and commits (one daemon cycle), not instantly."""
        from mcpbrain.capture import write_capture
        try:
            p = write_capture(str(config.app_dir()), _capture_envelope(
                "continuity", text=text))
            return {"queued": True, "path": str(p)}
        except (ValueError, OSError) as exc:
            return {"queued": False, "error": str(exc)}
    return brain_note


def make_brain_memory_write():
    async def brain_memory_write(slug: str, description: str, body: str,
                                 memory_type: str = "project") -> dict:
        """Write a durable auto-memory file. QUEUED: the daemon writes memory/<slug>.md
        + a MEMORY.md pointer in your records repo and commits (one daemon cycle), not instantly."""
        from mcpbrain.capture import write_capture
        try:
            p = write_capture(str(config.app_dir()), _capture_envelope(
                "memory", slug=slug, description=description, body=body,
                memory_type=memory_type))
            return {"queued": True, "path": str(p)}
        except (ValueError, OSError) as exc:
            return {"queued": False, "error": str(exc)}
    return brain_memory_write


def make_brain_draft_reply(store, home: str):
    async def brain_draft_reply(email_id: str, intent: str = "") -> dict:
        """Draft an email reply using the 4-stage pipeline (pretrial → generate → critique → voice).

        email_id: message_id from email_context.
        intent: optional override — 'reply' | 'acknowledge' | 'decline' | 'decide' | 'inform'.
        Returns {draft_record_id, final_draft, critique, voice_issues, audience_tier} or {error}.
        """
        try:
            from mcpbrain import draft as _draft
            return _draft.draft_email(store, home, email_id, intent=intent)
        except ValueError as exc:
            return {"error": str(exc)}
        except Exception:
            _log.exception("brain_draft_reply failed for email_id=%r", email_id)
            return {"error": "draft pipeline failed — check daemon log"}
    return brain_draft_reply


def make_brain_draft_refine(store, home: str):
    async def brain_draft_refine(draft_record_id: int, refinement: str) -> dict:
        """Refine an existing draft.

        draft_record_id: id from a prior brain_draft_reply call.
        refinement: 'warmer' | 'shorter' | 'firmer' | 'direct_about:<topic>'
        Returns {draft_record_id, final_draft, critique, voice_issues, audience_tier} or {error}.
        """
        try:
            from mcpbrain import draft as _draft
            return _draft.refine_draft(store, home, draft_record_id, refinement)
        except ValueError as exc:
            return {"error": str(exc)}
        except Exception:
            _log.exception("brain_draft_refine failed for record_id=%r", draft_record_id)
            return {"error": "refine pipeline failed — check daemon log"}
    return brain_draft_refine


def main() -> None:  # stdio entry point, exercised manually + in P3 integration
    import mcp.server.stdio
    from mcp.server import Server
    from mcp import types
    from mcpbrain import config
    from mcpbrain.store import Store
    from mcpbrain.embed import get_embedder
    emb = get_embedder(config.EMBEDDER)
    _store_path, _store_dim = config.store_path(), emb.dim
    store = Store(_store_path, dim=_store_dim, read_only=True)   # read path: index/graph/email
    search = make_brain_search(store, emb)
    context = make_brain_context(store)
    actions = make_brain_actions(store)
    graph = make_brain_graph(store)
    proactive = make_brain_proactive(store)
    ingest = make_brain_ingest()
    action_create = make_brain_action_create()
    action_update = make_brain_action_update()
    decision = make_brain_decision()
    note = make_brain_note()
    memory_write = make_brain_memory_write()
    # Draft tools write to draft_records, so they need a writable store handle.
    # the read-only store cannot INSERT; this writable handle is scoped to draft_records
    # writes by the MCP server (serialised via WAL + busy_timeout).
    draft_store = Store(_store_path, dim=_store_dim, read_only=False)  # draft_records writes
    home = str(config.app_dir())
    draft_reply = make_brain_draft_reply(draft_store, home)
    draft_refine = make_brain_draft_refine(draft_store, home)
    server = Server("mcpbrain")

    @server.list_resources()
    async def _list_resources():
        return await list_context_resources()

    @server.read_resource()
    async def _read_resource(uri):
        from mcp.server.lowlevel.helper_types import ReadResourceContents
        text = await read_context_resource(uri)
        return [ReadResourceContents(content=text, mime_type="text/markdown")]

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
                description=(
                    "Profile an entity or list community clusters. "
                    "mode='profile' (default): entity is required — returns record, relations, "
                    "actions, projects, and areas. "
                    "mode='communities': returns all community summaries, or the member entities "
                    "for a specific community when community_id is supplied."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "entity": {"type": "string"},
                        "mode": {
                            "type": "string",
                            "default": "profile",
                            "enum": ["profile", "communities"],
                        },
                        "community_id": {"type": "integer"},
                    },
                },
            ),
            types.Tool(
                name="brain_actions",
                description="Action items from the unified actions table, filtered by owner + status, with freshness.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string", "default": "",
                                  "description": "Empty defaults to the configured install owner."},
                        "status": {"type": "string", "default": "open"},
                    },
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
                        "at_time": {"type": "string"},
                        "include_invalidated": {"type": "boolean", "default": False},
                    },
                    "required": ["entity"],
                },
            ),
            types.Tool(
                name="brain_proactive",
                description="Open proactive findings: projects without next actions, areas overdue, lint issues.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "finding_type": {
                            "type": "string",
                            "description": "Filter by type (e.g. 'project_no_next_action', 'lint:missing_org')",
                        },
                        "severity": {"type": "string"},
                    },
                },
            ),
            types.Tool(
                name="brain_ingest",
                description=(
                    "Save a note, decision, or memory to your knowledge base. "
                    "QUEUED: the item is searchable after the next sync cycle (~5 min), "
                    "not immediately."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "content": {"type": "string"},
                        "tags": {"type": "string", "default": ""},
                        "observation_type": {
                            "type": "string",
                            "default": "note",
                            "enum": ["note", "decision", "memory", "reference"],
                        },
                        "org": {"type": "string", "default": ""},
                    },
                    "required": ["title", "content"],
                },
            ),
            types.Tool(
                name="brain_action_create",
                description=(
                    "Create a new action item. "
                    "QUEUED: appears in brain_actions after the next sync cycle (~5 min). "
                    "Empty owner defaults to the configured install owner."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "owner": {"type": "string", "default": ""},
                        "deadline": {"type": "string", "default": ""},
                        "org": {"type": "string", "default": ""},
                        "project_id": {"type": "string", "default": ""},
                        "area_id": {"type": "string", "default": ""},
                    },
                    "required": ["text"],
                },
            ),
            types.Tool(
                name="brain_action_update",
                description=(
                    "Mark an action done or reopen it. "
                    "QUEUED: applies on the next sync cycle (~5 min)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "action_id": {"type": "integer"},
                        "status": {"type": "string", "enum": ["done", "open"]},
                    },
                    "required": ["action_id", "status"],
                },
            ),
            types.Tool(
                name="brain_decision",
                description=(
                    "Record a decision. "
                    "QUEUED: the daemon appends a row to state/decisions.md in your records repo "
                    "and commits (one daemon cycle, ~seconds-minutes), not instantly."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "rationale": {"type": "string", "default": ""},
                        "owner": {"type": "string", "default": ""},
                        "supersedes": {"type": "string", "default": ""},
                        "org": {"type": "string", "default": ""},
                    },
                    "required": ["text"],
                },
            ),
            types.Tool(
                name="brain_note",
                description=(
                    "Record a continuity note. "
                    "QUEUED: the daemon prepends a dated entry to state/hot.md in your records repo "
                    "and commits (one daemon cycle), not instantly."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                    },
                    "required": ["text"],
                },
            ),
            types.Tool(
                name="brain_memory_write",
                description=(
                    "Write a durable auto-memory file. "
                    "QUEUED: the daemon writes memory/<slug>.md + a MEMORY.md pointer "
                    "in your records repo and commits (one daemon cycle), not instantly."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "slug": {"type": "string"},
                        "description": {"type": "string"},
                        "body": {"type": "string"},
                        "memory_type": {"type": "string", "default": "project"},
                    },
                    "required": ["slug", "description", "body"],
                },
            ),
            types.Tool(
                name="brain_draft_reply",
                description=(
                    "Draft an email reply using a 4-stage pipeline. "
                    "Stages: pretrial+plan (intent, audience tier), generate, critique+revise, voice check. "
                    "Returns draft_record_id for use with brain_draft_refine."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "email_id": {"type": "string",
                                     "description": "message_id from email_context"},
                        "intent": {"type": "string",
                                   "description": "override intent: reply|acknowledge|decline|decide|inform",
                                   "default": ""},
                    },
                    "required": ["email_id"],
                },
            ),
            types.Tool(
                name="brain_draft_refine",
                description=(
                    "Refine a draft from brain_draft_reply. "
                    "Runs critique+revise+voice stages with a refinement instruction. "
                    "Returns a new draft_record_id."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "draft_record_id": {"type": "integer"},
                        "refinement": {"type": "string",
                                       "description": "warmer | shorter | firmer | direct_about:<topic>"},
                    },
                    "required": ["draft_record_id", "refinement"],
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
            out = await context(
                entity=arguments.get("entity", ""),
                mode=arguments.get("mode", "profile"),
                community_id=arguments.get("community_id"),
            )
            return [types.TextContent(type="text", text=json.dumps(out))]
        if name == "brain_actions":
            # null-coalesce: explicit None/empty defaults to the configured owner
            owner = arguments.get("owner") or _default_owner()
            if not owner:
                return [types.TextContent(type="text", text='[{"error": "Install not configured: set owner_name in config.json"}]')]
            status = arguments.get("status") or "open"
            out = await actions(owner, status)
            return [types.TextContent(type="text", text=json.dumps(out))]
        if name == "brain_graph":
            out = await graph(arguments["entity"], arguments.get("hops", 1),
                              at_time=arguments.get("at_time"),
                              include_invalidated=arguments.get("include_invalidated", False))
            return [types.TextContent(type="text", text=json.dumps(out))]
        if name == "brain_proactive":
            out = await proactive(arguments.get("finding_type", ""), arguments.get("severity", ""))
            return [types.TextContent(type="text", text=json.dumps(out))]
        if name == "brain_ingest":
            out = await ingest(
                title=arguments.get("title", ""),
                content=arguments.get("content", ""),
                tags=arguments.get("tags", ""),
                observation_type=arguments.get("observation_type", "note"),
                org=arguments.get("org", ""),
            )
            return [types.TextContent(type="text", text=json.dumps(out))]
        if name == "brain_action_create":
            out = await action_create(
                text=arguments.get("text", ""),
                owner=arguments.get("owner") or _default_owner(),
                deadline=arguments.get("deadline", ""),
                org=arguments.get("org", ""),
                project_id=arguments.get("project_id", ""),
                area_id=arguments.get("area_id", ""),
            )
            return [types.TextContent(type="text", text=json.dumps(out))]
        if name == "brain_action_update":
            out = await action_update(
                action_id=arguments.get("action_id", 0),
                status=arguments.get("status", ""),
            )
            return [types.TextContent(type="text", text=json.dumps(out))]
        if name == "brain_decision":
            out = await decision(
                text=arguments.get("text", ""),
                rationale=arguments.get("rationale", ""),
                owner=arguments.get("owner") or _default_owner(),
                supersedes=arguments.get("supersedes", ""),
                org=arguments.get("org", ""),
            )
            return [types.TextContent(type="text", text=json.dumps(out))]
        if name == "brain_note":
            out = await note(
                text=arguments.get("text", ""),
            )
            return [types.TextContent(type="text", text=json.dumps(out))]
        if name == "brain_memory_write":
            out = await memory_write(
                slug=arguments.get("slug", ""),
                description=arguments.get("description", ""),
                body=arguments.get("body", ""),
                memory_type=arguments.get("memory_type", "project"),
            )
            return [types.TextContent(type="text", text=json.dumps(out))]
        if name == "brain_draft_reply":
            out = await draft_reply(
                email_id=arguments.get("email_id", ""),
                intent=arguments.get("intent", ""),
            )
            return [types.TextContent(type="text", text=json.dumps(out))]
        if name == "brain_draft_refine":
            out = await draft_refine(
                draft_record_id=arguments.get("draft_record_id", 0),
                refinement=arguments.get("refinement", ""),
            )
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
