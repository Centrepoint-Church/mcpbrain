"""Every mcpbrain tool: its handler factory AND its registry declaration.

The EXECUTABLE half of the tool seam. `tool_registry` holds the mechanism
(declare/tool/ToolSpec, deep-freezing, ordering); this module holds the 26
declarations and the 24 handler factories that back them, and importing it is
what populates the registry.

Split out of `mcp_server` so BOTH sides of the seam can reach the tools. The
registry was already stdlib-only, but every `@tool(...)` declaration below
evaluates at import time -- so as long as the factories lived in the module that
builds the MCP `Server`, reading the registry meant importing `mcp` + pydantic +
starlette + uvicorn (measured: 601 modules / ~0.26s, against 143 / ~12ms without).
The daemon will execute Store-touching tools by reading this registry, and it is
already contended on the GIL and the DB (see the 0.7.105/0.7.110 recall-timeout
work); it must not pay the protocol stack's import to learn a tool's schema.

So: NO `mcp` import here, direct or transitive, and no Store or native (fastembed
/onnxruntime/sqlite_vec) import either -- `tests/test_tool_registry.py` pins both
with an AST guard and a subprocess sys.modules check. Annotations are the stdlib
`tool_registry.ToolAnnotations`; `mcp_server._sdk_annotations` converts them to
the SDK model at the tools/list boundary, which is the only place mcp is needed.
The factories take `store`/`client`/`home` as ARGUMENTS and import mcpbrain's
heavier modules inside their handler bodies, which is what keeps that true.

`mcp_server` is the protocol adapter over this module: it imports the factories,
wires them into `build_server`, and dispatches `on_call_tool` to them.

DECLARATION ORDER IS THE ADVERTISED ORDER. tools/list iterates the registry in
registration order, i.e. top-to-bottom through this file, so moving a factory
reorders the surface the model sees (test_advertised_order_is_registration_order).
"""
import logging

from mcpbrain import config
from mcpbrain.enrich_blocks import PUSH_BLOCKS as _PUSH_BLOCKS
from mcpbrain.retrieval import annotate_action_freshness
from mcpbrain.tool_registry import ToolAnnotations, declare, tool

_log = logging.getLogger("mcpbrain.tools")


# --- Tool annotation shorthands ---------------------------------------------
# The five safety-annotation shapes every tool falls into. Module-level because
# each @tool declaration below evaluates at import.
#
# These build the stdlib tool_registry.ToolAnnotations, NOT mcp.types'. That is
# what keeps the declaration path -- and so this whole block of factories --
# importable without the MCP protocol stack; _sdk_annotations converts at the
# tools/list boundary. Do not "simplify" these back to the SDK model.
#
# open_world_hint is False for 25 of the 26: each of those touches only the local
# store, local files, or the loopback control API. The one exception is
# brain_meetings_today, which calls dashboard.calendar_today(home) ->
# auth.build_google_services(...) + a live Calendar events().list(...)
# synchronously as part of the tool call -- a real trust-boundary crossing. The
# annotation describes the CALL, not which process makes it: Phase 4 routes this
# tool to the daemon (tool_exec_in_daemon, default ON), so the Calendar request
# now leaves from there rather than from the MCP server, but it is still one
# outbound network call made on the caller's behalf while it waits. It spells its
# annotations out at its declaration rather than using a shorthand.


def _ro(title: str) -> ToolAnnotations:
    return ToolAnnotations(
        title=title, read_only_hint=True, destructive_hint=False,
        idempotent_hint=True, open_world_hint=False,
    )


def _append(title: str) -> ToolAnnotations:
    # Queued capture: each call appends a new envelope, so two calls create two.
    return ToolAnnotations(
        title=title, read_only_hint=False, destructive_hint=False,
        idempotent_hint=False, open_world_hint=False,
    )


def _idempotent(title: str) -> ToolAnnotations:
    return ToolAnnotations(
        title=title, read_only_hint=False, destructive_hint=False,
        idempotent_hint=True, open_world_hint=False,
    )


def _lease(title: str) -> ToolAnnotations:
    # Reads work but claims a 15-minute lease: not a safe read, not idempotent
    # (two calls hand out different units, by design).
    return ToolAnnotations(
        title=title, read_only_hint=False, destructive_hint=False,
        idempotent_hint=False, open_world_hint=False,
    )


def _destructive(title: str) -> ToolAnnotations:
    return ToolAnnotations(
        title=title, read_only_hint=False, destructive_hint=True,
        idempotent_hint=False, open_world_hint=False,
    )


# The queued-capture envelope, shared by the six write-through-capture tools.
# Still a factory, for the reason it always was: one object referenced six times
# means an in-place edit to any of them silently rewrites all six. (The registry
# now deep-freezes what it stores, so that edit would raise rather than corrupt --
# belt and braces.)
def _queued_output_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "queued": {"type": "boolean"},
            "path": {"type": "string"},
            "error": {"type": "string"},
        },
        "required": ["queued"],
    }


@tool(
    "brain_search",
    description="Search your Gmail/Calendar/Drive index.",
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer", "default": 10},
        },
        "required": ["query"],
    },
    annotations=_ro("Search the brain"),
)
def make_brain_search(client):
    async def brain_search(query: str, limit: int = 10) -> list[dict]:
        try:
            return client.recall(query, limit)
        except Exception:
            _log.exception("brain_search failed for query %r", query)
            return []
    return brain_search


# brain_read has no factory -- it is dispatched inline in on_call_tool as a bare
# store.get_chunk(...) -- so it declares itself here, in the position it is
# advertised in (registration order == tools/list order).
declare(
    "brain_read",
    description="Fetch the full text + metadata of a chunk by doc_id.",
    input_schema={
        "type": "object",
        "properties": {"doc_id": {"type": "string"}},
        "required": ["doc_id"],
    },
    annotations=_ro("Read a chunk"),
)


@tool(
    "brain_context",
    description=(
        "Profile an entity or list community clusters. "
        "mode='profile' (default): entity is required — returns record, relations, "
        "actions, projects, and areas. "
        "mode='communities': returns all community summaries, or the member entities "
        "for a specific community when community_id is supplied."
    ),
    input_schema={
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
    annotations=_ro("Profile an entity"),
)
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
            return {"entity": {**ent, "profile": ent.get("profile", "")},
                    "relations": relations, "actions": actions}
        except Exception:
            _log.exception("brain_context failed for entity=%r mode=%r", entity, mode)
            return {}
    return brain_context


@tool(
    "brain_actions",
    description="Action items from the unified actions table, filtered by owner + status, with freshness.",
    input_schema={
        "type": "object",
        "properties": {
            "owner": {"type": "string", "default": "",
                      "description": "Empty defaults to the configured install owner."},
            "status": {"type": "string", "default": "open"},
        },
    },
    annotations=_ro("List open actions"),
)
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


GRAPH_MAX_HOPS = 3  # traversal cap; on_call_tool's brain_graph branch reads
                    # this too, so a reported progress `total` can never drift
                    # from the depth the BFS below actually runs.


@tool(
    "brain_graph",
    description="Traverse the relationship graph from an entity up to `hops` (max 3).",
    input_schema={
        "type": "object",
        "properties": {
            "entity": {"type": "string"},
            "hops": {"type": "integer", "default": 1},
            "at_time": {"type": "string"},
            "include_invalidated": {"type": "boolean", "default": False},
        },
        "required": ["entity"],
    },
    annotations=_ro("Traverse the graph"),
)
def make_brain_graph(store):
    async def brain_graph(entity: str, hops: int = 1, *, at_time: str | None = None,
                          include_invalidated: bool = False, on_hop=None) -> dict:
        """Traverse the relationship graph from an entity up to `hops` (capped at
        GRAPH_MAX_HOPS).
        at_time scopes the traversal to relations valid at that ISO date;
        include_invalidated also follows superseded edges.
        Returns {center, nodes:[entity dicts], edges:[{entity_a,relation,entity_b}]}; {} if unknown.

        on_hop, if given, is awaited once per completed hop with the 1-based hop
        number -- optional and keyword-only so every existing direct caller
        (test_mcp_server.py calls this inner function without it) is unaffected.
        A hub entity's traversal can be hundreds of store queries per hop; this
        lets the MCP layer emit a progress notification between hops so the
        client's idle timer resets instead of staying silent for the whole walk.
        """
        try:
            center = store.find_entity(entity)
            if not center:
                return {}
            depth = max(0, min(hops, GRAPH_MAX_HOPS))  # cap; guard against runaway traversal
            visited = {center["id"]}
            edges = {}  # (entity_a, relation, entity_b) -> dict, dedup
            frontier = {center["id"]}
            for hop in range(1, depth + 1):
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
                if on_hop is not None:
                    await on_hop(hop)
                if not frontier:
                    break
            nodes = [n for n in (store.get_entity(i) for i in visited) if n]
            return {"center": center, "nodes": nodes, "edges": list(edges.values())}
        except Exception:
            _log.exception("brain_graph failed for %r", entity)
            return {}
    return brain_graph


@tool(
    "brain_proactive",
    description="Open proactive findings: projects without next actions, areas overdue, lint issues.",
    input_schema={
        "type": "object",
        "properties": {
            "finding_type": {
                "type": "string",
                "description": "Filter by type (e.g. 'project_no_next_action', 'lint:missing_org')",
            },
            "severity": {"type": "string"},
        },
    },
    annotations=_ro("List findings"),
)
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


# Finding types brain_finding_resolve may close. Deliberately narrow: every
# other type is owned by an automated resolver (the review appliers via
# drain.BLOCK_DRAINERS, or lint's own resolve_findings_not_in). Closing one of
# those by hand is churn — the next lint run re-opens it because the underlying
# entity is still there — and a general tool would let any session quietly clear
# the graph-hygiene queue. The dashboard route
# /api/dashboard/findings/<id>/dismiss remains the human override for any type.
MANUAL_RESOLVE_TYPES = ("memory_promotion",)

_RESOLVE_OUTCOMES = ("promoted", "merged", "dismissed")


@tool(
    "brain_finding_resolve",
    description=(
        "Close one proactive finding you have acted on. Only "
        "memory_promotion findings may be closed this way — every other "
        "type is resolved automatically. outcome: 'promoted' (a memory "
        "file was written), 'merged' (folded into an existing memory "
        "file), or 'dismissed' (not durable)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "finding_id": {"type": "integer",
                           "description": "the finding's id, from brain_proactive"},
            "outcome": {"type": "string",
                        "enum": list(_RESOLVE_OUTCOMES),
                        "description": "what you did about it"},
            "note": {"type": "string",
                     "description": "short free text for the change log"},
        },
        "required": ["finding_id", "outcome"],
    },
    annotations=_idempotent("Resolve a finding"),
    output_schema={
        "type": "object",
        "properties": {
            "resolved": {"type": "boolean"},
            "error": {"type": "string"},
        },
        "required": ["resolved"],
    },
)
def make_brain_finding_resolve(store):
    async def brain_finding_resolve(finding_id: int, outcome: str,
                                    note: str = "") -> dict:
        """Close one proactive finding the caller has acted on.

        Only types in MANUAL_RESOLVE_TYPES may be closed this way. outcome is
        one of promoted | merged | dismissed and is recorded in the change log
        alongside `note`. Returns {"resolved": bool, ...} — never raises.
        """
        try:
            if outcome not in _RESOLVE_OUTCOMES:
                return {"resolved": False,
                        "error": f"outcome must be one of {list(_RESOLVE_OUTCOMES)}, "
                                 f"got {outcome!r}"}
            finding = store.get_finding(finding_id)
            if finding is None:
                return {"resolved": False, "error": f"finding {finding_id} not found"}
            if finding["resolved_at"]:
                return {"resolved": False,
                        "error": f"finding {finding_id} is already resolved"}
            ftype = finding["finding_type"]
            if ftype not in MANUAL_RESOLVE_TYPES:
                return {"resolved": False,
                        "error": f"{ftype} is resolved automatically and cannot be "
                                 f"closed by hand; only {list(MANUAL_RESOLVE_TYPES)} "
                                 f"may be"}
            if not store.resolve_finding(finding_id, verdict=outcome):
                return {"resolved": False,
                        "error": f"finding {finding_id} could not be resolved"}
            store.record_change(
                "finding_resolved", ref_id=str(finding_id),
                summary=f"{ftype} {outcome}: {finding['ref_id']}",
                detail=note, source="mcp")
            return {"resolved": True, "finding_id": finding_id, "outcome": outcome}
        except Exception as exc:  # noqa: BLE001 — a tool must return, not raise
            _log.exception("brain_finding_resolve failed")
            return {"resolved": False, "error": str(exc)}
    return brain_finding_resolve


def _capture_envelope(kind: str, source: str = "mcp", **fields) -> dict:
    from datetime import datetime, timezone
    return {"kind": kind, "source": source,
            "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            **fields}


@tool(
    "brain_ingest",
    description=(
        "Save a note, decision, or memory to your knowledge base. "
        "QUEUED: the item is searchable after the next sync cycle (~5 min), "
        "not immediately."
    ),
    input_schema={
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
    annotations=_append("Capture a note or document"),
    output_schema=_queued_output_schema(),
)
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


@tool(
    "brain_action_create",
    description=(
        "Create a new action item. "
        "QUEUED: appears in brain_actions after the next sync cycle (~5 min). "
        "Empty owner defaults to the configured install owner."
    ),
    input_schema={
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
    annotations=_append("Create an action"),
    output_schema=_queued_output_schema(),
)
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


@tool(
    "brain_action_update",
    description=(
        "Mark an action done or reopen it. "
        "QUEUED: applies on the next sync cycle (~5 min)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "action_id": {"type": "integer"},
            "status": {"type": "string", "enum": ["done", "open"]},
        },
        "required": ["action_id", "status"],
    },
    # Goes through write_capture like the non-idempotent _append tools;
    # idempotence is a drain-side property (drain.py's action_update apply is a
    # set-status-by-key no-op when already in the target state), not a property
    # of the write step itself.
    annotations=_idempotent("Update an action's status"),
    output_schema=_queued_output_schema(),
)
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


@tool(
    "brain_decision",
    description=(
        "Record a decision. "
        "QUEUED: the daemon appends a row to state/decisions.md in your records repo "
        "and commits (one daemon cycle, ~seconds-minutes), not instantly."
    ),
    input_schema={
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
    annotations=_append("Record a decision"),
    output_schema=_queued_output_schema(),
)
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


@tool(
    "brain_note",
    description=(
        "Record a continuity note. "
        "QUEUED: the daemon prepends a dated entry to state/hot.md in your records repo "
        "and commits (one daemon cycle), not instantly."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "text": {"type": "string"},
        },
        "required": ["text"],
    },
    annotations=_append("Record a note"),
    output_schema=_queued_output_schema(),
)
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


@tool(
    "brain_memory_write",
    description=(
        "Write a durable auto-memory file. "
        "QUEUED: the daemon writes memory/<slug>.md + a MEMORY.md pointer "
        "in your records repo and commits (one daemon cycle), not instantly."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "slug": {"type": "string"},
            "description": {"type": "string"},
            "body": {"type": "string"},
            "memory_type": {"type": "string", "default": "project"},
        },
        "required": ["slug", "description", "body"],
    },
    annotations=_append("Write a memory"),
    output_schema=_queued_output_schema(),
)
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


@tool(
    "brain_gardener_apply",
    description=(
        "Apply a reference-gardener change directly to the records repo through "
        "the role-attribution guard and per-run change cap. Synchronous (not "
        "queued): commits immediately and returns the result so the gardener gets "
        "enforcement feedback. Use only from the reference-gardener routine in "
        "auto-apply mode."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "lane": {"type": "string", "enum": ["reference", "context"],
                     "description": "'reference' (drift) or 'context' (constitution)"},
            "filename": {"type": "string", "description": "basename of an existing file in that dir"},
            "content": {"type": "string", "description": "full new file content"},
            "asserts_person_role": {"type": "boolean", "default": False,
                                    "description": "True only if assigning a role/title to a person"},
            "attribution_source": {"type": "string",
                                   "enum": ["owner_statement", "signature", "owner_confirmation"],
                                   "description": "required when asserts_person_role"},
            "attribution_quote": {"type": "string",
                                  "description": "verbatim supporting text; required for a role claim and verified against the cited source"},
            "attribution_doc_id": {"type": "string",
                                   "description": "stored chunk id the quote lives in; required for owner_statement/signature"},
        },
        "required": ["lane", "filename", "content"],
    },
    # Overwrites an existing records file with caller-supplied content and
    # git-commits it, synchronously.
    annotations=_destructive("Apply a gardener edit"),
    output_schema={
        "type": "object",
        "properties": {
            "applied": {"type": "boolean"},
            "committed": {"type": "boolean"},
            "error": {"type": "string"},
        },
        "required": ["applied"],
    },
)
def make_brain_gardener_apply(store):
    async def brain_gardener_apply(lane: str, filename: str, content: str,
                                   asserts_person_role: bool = False,
                                   attribution_source: str = "",
                                   attribution_quote: str = "",
                                   attribution_doc_id: str = "") -> dict:
        """Apply a reference-gardener change directly to the records repo, through the
        deterministic guard. NOT queued — the write + commit happen synchronously so the
        gardener gets immediate enforcement feedback.

        lane: 'reference' (drift) or 'context' (constitution). filename: basename of an
        existing file in that dir. content: full new file content.

        Person-role claims are VERIFIED, not just labelled. When asserts_person_role is
        True: attribution_source must be 'owner_statement' | 'signature' |
        'owner_confirmation', and attribution_quote (verbatim supporting text) is
        required. For owner_statement/signature, attribution_doc_id must point at a stored
        chunk and the quote is checked to actually appear in it — a fabricated or inferred
        attribution is rejected. owner_confirmation needs only the confirmed quote.
        Returns {"applied": bool, "committed": bool} or {"applied": False, "error": ...}.
        """
        import subprocess
        from mcpbrain import records_write, config as _cfg
        home = str(config.app_dir())
        repo = _cfg.records_dir(home)
        cap = _cfg.gardener_max_changed_lines(home)
        try:
            if lane == "reference":
                committed = records_write.write_gardener_reference(
                    repo, filename, content, max_changed_lines=cap)
            elif lane == "context":
                if asserts_person_role:
                    err = _verify_role_attribution(
                        store, attribution_source, attribution_quote, attribution_doc_id)
                    if err:
                        return {"applied": False, "error": err}
                committed = records_write.write_gardener_context(
                    repo, filename, content,
                    asserts_person_role=asserts_person_role,
                    attribution_source=(attribution_source or None),
                    attribution_doc_id=(attribution_doc_id or None),
                    max_changed_lines=cap)
            else:
                return {"applied": False, "error": f"unknown lane {lane!r} "
                        "(expected 'reference' or 'context')"}
            return {"applied": True, "committed": committed}
        except (ValueError, FileNotFoundError, OSError) as exc:
            return {"applied": False, "error": str(exc)}
        except subprocess.CalledProcessError as exc:
            # The daemon is the usual records-repo writer; a concurrent commit can
            # hold .git/index.lock. Surface it cleanly so the gardener retries next
            # run rather than crashing the tool call.
            return {"applied": False, "error": f"git busy (retry next run): {exc}"}
    return brain_gardener_apply


def _verify_role_attribution(store, source: str, quote: str, doc_id: str) -> str | None:
    """Verify a person-role attribution before it is written. Returns an error string
    to reject, or None to allow. Enforces the enum (defence-in-depth — the writer
    checks too) and verifies the quote against the cited stored source."""
    from mcpbrain import records_write
    if source not in records_write._APPROVED_ATTRIBUTION_SOURCES:
        return (f"Role attribution source {source!r} is not permitted. Approved: "
                f"{sorted(records_write._APPROVED_ATTRIBUTION_SOURCES)}.")
    if source in records_write._STORE_BACKED_SOURCES:
        if not doc_id:
            return (f"attribution_doc_id is required for a {source} role claim — cite the "
                    "stored chunk that contains the supporting text")
        chunk = store.get_chunk(doc_id)
        if not chunk:
            return f"attribution_doc_id {doc_id!r} not found in the store"
        return records_write.verify_attribution_quote(quote, chunk.get("text", ""))
    # owner_confirmation: live human-in-the-loop; require the confirmed text, no store doc.
    if not quote.strip():
        return "attribution_quote (the confirmed statement) is required for owner_confirmation"
    return None


@tool(
    "brain_draft_context",
    description="Get email context for drafting a reply (subject, body, sender, voice rules, thread samples). Returns context dict to use in the draft-reply skill.",
    input_schema={"type": "object", "properties": {
        "email_id": {"type": "string", "description": "message_id from email_context"},
        "intent": {"type": "string", "description": "optional intent override"},
    }, "required": ["email_id"]},
    annotations=_ro("Gather drafting context"),
)
def make_brain_draft_context(store, home: str):
    async def brain_draft_context(email_id: str, intent: str = "", *, on_stage=None) -> dict:
        """Return context for drafting a reply (subject, body, sender, voice_rules, samples).

        email_id: message_id from email_context.
        intent: optional — 'reply' | 'acknowledge' | 'decline' | 'decide' | 'inform'.
        Returns context dict or {"error": "email not found"}.

        on_stage, if given, is awaited with a stage name as draft_context moves
        through email lookup -> voice rules -> samples -> (optional) critique.
        Optional and keyword-only so every existing caller is unaffected.
        """
        from mcpbrain import draft as _draft
        return await _draft.draft_context(store, home, email_id, intent=intent, on_stage=on_stage)
    return brain_draft_context


@tool(
    "brain_draft_save",
    description="Persist a completed draft to draft history. Call after the Cowork draft-reply skill has finished. Returns draft_record_id.",
    input_schema={"type": "object", "properties": {
        "email_id": {"type": "string"},
        "thread_id": {"type": "string"},
        "intent": {"type": "string"},
        "final_draft": {"type": "string", "description": "The finished draft text to save"},
        "parent_draft_id": {"type": "integer", "description": "optional: id of prior draft being replaced"},
    }, "required": ["email_id", "thread_id", "intent", "final_draft"]},
    annotations=_append("Save a draft"),
    # The `error` branch omits draft_record_id (see make_brain_draft_save), so
    # unlike the other envelopes this schema marks NO field required -- a
    # `required: ["draft_record_id"]` would reject the tool's own error responses.
    output_schema={
        "type": "object",
        "properties": {
            "draft_record_id": {"type": "integer"},
            "error": {"type": "string"},
        },
    },
)
def make_brain_draft_save(store, home: str):
    async def brain_draft_save(email_id: str, thread_id: str, intent: str,
                                final_draft: str, parent_draft_id: int | None = None) -> dict:
        """Persist a completed draft to draft history.

        Call after the Cowork skill has finished drafting.
        Returns {"draft_record_id": <id>} or {"error": ...}.
        """
        try:
            record_id = store.save_draft(
                email_id=email_id, thread_id=thread_id, intent=intent,
                audience_tier="", draft_text=final_draft, critique="",
                voice_issues=[], samples_used=0, model="cowork",
                parent_draft_id=parent_draft_id,
            )
            return {"draft_record_id": record_id}
        except Exception as exc:
            _log.exception("brain_draft_save failed for email_id=%r", email_id)
            return {"error": str(exc)}
    return brain_draft_save


# --- Autonomous-loop tools (host-native; VM-proof) --------------------------
# The Cowork enrich + meeting-packs scheduled tasks must reach the host's app
# data and store. Per the Cowork desktop architecture, shell commands and curl
# run in an isolated VM, but local plugin MCP servers run natively on the host —
# so these tools are the reliable channel. The enrich tools are pure file I/O on
# the app-data dir (mirroring brain_ingest); the meeting tools wrap the store +
# dashboard the control API also uses.

_ENRICH_RULES_CACHE = None


def _enrich_rules() -> str:
    """The canonical extraction rules — the SHARED-EXTRACTION-RULES block of the
    bundled ``enrich_prompt.md`` (shipped inside the wheel). brain_enrich_pull
    returns this so the response is self-contained: the enrichment caller needs
    no plugin/skill file and no source repo to know the extraction protocol.
    Returns '' if the bundled file is somehow missing (never raises)."""
    global _ENRICH_RULES_CACHE
    if _ENRICH_RULES_CACHE is not None:
        return _ENRICH_RULES_CACHE
    from pathlib import Path
    begin, end = "<!-- SHARED-EXTRACTION-RULES:BEGIN -->", "<!-- SHARED-EXTRACTION-RULES:END -->"
    try:
        text = (Path(__file__).parent / "enrich_prompt.md").read_text()
        _ENRICH_RULES_CACHE = text[text.index(begin) + len(begin):text.index(end)].strip()
    except (OSError, ValueError):
        _ENRICH_RULES_CACHE = ""
    return _ENRICH_RULES_CACHE


# The RESPONSE consumer limit, SEPARATE from the packing budget in prepare.py. A pull result
# above ~50KB is persisted by Claude Code to a file the caller must Read back, which
# defeats the flat-context fan-out. The packing budget (60k) lets the agent path
# (with_rules=False ≈ work+context) sit safely under this, but a with_rules=True pull
# adds the ~11.5KB rules block and can land in the (50KB, 60KB) gap — so the assembled
# response trims context to essentials when it crosses this limit, regardless of cap.
_PULL_SOFT_LIMIT = 50_000


_ROUTINES = ("enrich", "meeting-packs", "gardener", "reference-gardener")


def _routine_instructions(name: str) -> str | None:
    """The bundled protocol markdown for a recurring routine, served via MCP so a
    scheduled task is self-contained — no plugin command/skill resolution (which the
    Cowork/scheduled-task runtime does not reliably do) and no source repo. Returns
    None for an unknown name. The name is validated against a fixed allowlist, so
    there is no path traversal."""
    if name not in _ROUTINES:
        return None
    from pathlib import Path
    try:
        return (Path(__file__).parent / "routines" / f"{name}.md").read_text()
    except OSError:
        return None


# brain_routine, like brain_read, has no factory -- on_call_tool calls
# _routine_instructions directly -- so it declares itself here, beside that
# function and in its advertised position. It deliberately declares NO
# output_schema: it carries routine markdown, and structured_content ships
# ALONGSIDE content, so declaring one would double one of the two largest
# payloads in the surface for a consumer that reads prose, not JSON.
declare(
    "brain_routine",
    description="Return the full instructions for a recurring mcpbrain routine, to follow verbatim. Use this as the FIRST step of a scheduled task: call it, then do exactly what it returns. name is one of: enrich, meeting-packs, gardener, reference-gardener. Self-contained — do not look for a skill or command or read files.",
    input_schema={"type": "object", "properties": {
        "name": {"type": "string", "enum": list(_ROUTINES),
                 "description": "the routine to run"},
    }, "required": ["name"]},
    annotations=_ro("Get routine instructions"),
)


_LEASE_TTL_S = 15 * 60  # a claimed unit is re-listable after this (covers crashed subagents)
_UNITS_BATCH_DEFAULT = 30  # max units handed out (and claimed) per brain_enrich_units call


def _units_batch() -> int:
    """Max ready units returned — and claimed — per brain_enrich_units call.

    Without a cap, one call globs and leases the WHOLE queue, so any overlapping
    caller (the hourly cycle running alongside the backfill loop, or the next
    wave within a run) gets {"empty": true} until the 15-min lease expires. Cap
    each call to a wave's worth so callers share the queue wave-by-wave instead.
    Override with MCPBRAIN_ENRICH_UNITS_BATCH.
    """
    import os
    try:
        return max(1, int(os.environ.get("MCPBRAIN_ENRICH_UNITS_BATCH", _UNITS_BATCH_DEFAULT)))
    except (TypeError, ValueError):
        return _UNITS_BATCH_DEFAULT


def _units_dir(home):
    from pathlib import Path
    return Path(home) / "enrich_queue" / "units"


def _claims_dir(home):
    from pathlib import Path
    return Path(home) / "enrich_queue" / "claims"


def _lease_is_live(claim_path, now: float) -> bool:
    """True iff claim_path exists and its lease has not expired."""
    try:
        return claim_path.exists() and now - claim_path.stat().st_mtime < _LEASE_TTL_S
    except OSError:
        return False


def _atomic_claim(claims_dir, uid: str, now: float) -> bool:
    """Acquire uid's lease atomically. Returns True iff acquired.

    Exclusive create (O_CREAT|O_EXCL) is atomic across processes on the local
    store, so two concurrent drainers can never both take the same fresh lease.
    A stale lease (>= _LEASE_TTL_S old — crashed worker) is reclaimed via utime;
    that reclaim is the one non-atomic window (two workers could both reclaim the
    same stale lease → an idempotent double-apply downstream, never corruption).
    """
    import os
    try:
        claims_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    claim = claims_dir / uid
    try:
        fd = os.open(str(claim), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.close(fd)
        return True
    except FileExistsError:
        try:
            if now - claim.stat().st_mtime >= _LEASE_TTL_S:
                os.utime(str(claim), (now, now))
                return True
        except OSError:
            pass
        return False
    except OSError:
        return False


def _unit_payload(home, d: dict, unit_id: str, with_rules: bool) -> dict:
    """Build the pull/claim response body from a parsed unit dict `d`.

    Rules (byte-stable) lead when included, then context, so a general-purpose
    caller's serialized prefix stays cacheable; variable per-unit fields trail.
    """
    import json as _json
    from pathlib import Path
    try:
        ctx = _json.loads((Path(home) / "enrich_queue" / "context.json").read_text())
    except (OSError, ValueError):
        ctx = {}
    out = {}
    if with_rules:
        out["rules"] = _enrich_rules()
    out["context"] = ctx
    out["kind"] = d.get("kind")
    out["unit_id"] = unit_id
    if d.get("kind") == "block":
        out["block"] = d.get("block")
        out["items"] = d.get("items") or []
    else:
        out["threads"] = d.get("threads") or []
    if len(_json.dumps(out)) > _PULL_SOFT_LIMIT:
        out["context"] = {k: ctx[k] for k in ("owner_name", "valid_orgs",
                                              "org_domain_map") if k in ctx}
    return out


def _bump_unit_attempts(home, d: dict) -> None:
    """Bump the extraction-attempt counter for a claimed unit's chunks.

    drain._give_up_or_bump only fires on PUSH, so a unit no drainer can process
    (too large to hold, malformed) never increments and re-queues forever — the
    5,075,515-byte unit's failure mode. Bumping at CLAIM time bounds it: after
    _EMPTY_ATTEMPT_CAP claims the chunks are consumed by the push-side give-up.

    Best-effort and store-optional: this runs on the MCP claim path, which must
    stay cheap and must never fail a claim.

    `embedder_dim` (a dict lookup), NOT `get_embedder(...).dim` — the latter
    constructs a _LocalEmbedder, which loads the ONNX model eagerly, and this is
    the claim hot path. The value is the same 384 either way, and
    bump_enrich_attempts is a plain UPDATE that never touches the vec table.
    """
    try:
        ids = [i for t in (d.get("threads") or [])
               for i in (t.get("part_doc_ids") or [])]
        if not ids:
            return
        from pathlib import Path

        from mcpbrain.embed import embedder_dim
        from mcpbrain.store import Store
        # Store's real signature is (path, dim, read_only=False). `Store(str(home))`
        # raised TypeError on EVERY call and was swallowed by the except below, so
        # this whole backstop was inert in production while its tests — whose fakes
        # matched the wrong one-argument signature — passed.
        Store(Path(home) / "brain.sqlite3",
              dim=embedder_dim("bge-small")).bump_enrich_attempts(ids)
    except Exception:  # noqa: BLE001 — bookkeeping must never break a claim
        _log.debug("claim: attempt bump failed", exc_info=True)


@tool(
    "brain_enrich_units",
    description="List ready enrichment work units (descriptors only — unit_id, kind, block, count; NO payloads, so the caller stays context-flat) and claim each with a short lease. Recipe: call this, then for each unit_id, brain_enrich_pull(unit_id) to fetch its payload, extract, and brain_enrich_push(unit_id, …) to write the result. Returns {\"empty\": true} when the queue is dry. Loop it (with brain_enrich_advance) to drain a backlog.",
    input_schema={"type": "object", "properties": {}},
    annotations=_lease("Lease a batch of work units"),
)
def make_brain_enrich_units(home: str):
    async def brain_enrich_units() -> dict:
        """List up to a wave's worth of ready work units and CLAIM each with a short
        lease. Returns descriptors only — `unit_id`, `kind`, `block`, `count` — NO
        payloads, so the caller stays context-flat. A caller pulls each unit's payload
        with brain_enrich_pull(unit_id), extracts it, and pushes the result with
        brain_enrich_push(unit_id, …). At most `_units_batch()` units are returned per
        call (the rest stay unclaimed for the next call / an overlapping caller), so no
        single call leases the whole queue. Units claimed within the lease are skipped,
        so overlapping runs and the backfill loop never re-hand-out in-flight work; a
        stale claim (crashed subagent) becomes re-listable. Call again for the next
        wave; returns {"empty": true} when no unclaimed units remain."""
        import json as _json
        import time as _time
        try:
            files = sorted(_units_dir(home).glob("*.json"))
        except OSError:
            return {"empty": True}
        claims = _claims_dir(home)
        batch = _units_batch()
        ready, now = [], _time.time()
        for f in files:
            uid = f.stem
            if _lease_is_live(claims / uid, now):
                continue                              # still leased to another worker
            try:
                d = _json.loads(f.read_text())
            except (OSError, ValueError):
                continue                              # skip a half-written/garbage unit
            if not _atomic_claim(claims, uid, now):
                continue                              # lost the race to another caller
            ready.append({"unit_id": uid, "kind": d.get("kind"), "block": d.get("block"),
                          "count": len(d.get("threads") or d.get("items") or [])})
            if len(ready) >= batch:
                break
        return {"units": ready} if ready else {"empty": True}
    return brain_enrich_units


@tool(
    "brain_enrich_pull",
    description="Fetch one work unit's payload by unit_id (from brain_enrich_units), with a `rules` field carrying the FULL extraction protocol to follow (envelope schema, entity/relation/merge rules) and the standing `context`. A `kind` \"thread\" unit returns `threads`; a `kind` \"block\" unit returns `block` + `items`. Returns {\"empty\": true} if the unit is gone. Follow `rules` from this response; do not read skill files or source.",
    input_schema={"type": "object", "properties": {
        "unit_id": {"type": "string",
                    "description": "the unit to fetch (from brain_enrich_units)"},
        "with_rules": {"type": "boolean",
                       "description": "include the full extraction rules in the "
                                      "response (default true). enrich-batch workers "
                                      "pass false — they already carry the rules in "
                                      "their cached system prompt, so re-sending here "
                                      "would pay for them twice."},
    }, "required": ["unit_id"]},
    annotations=_ro("Pull a named unit's payload"),
    # NO output_schema, deliberately: the response carries the ~11.5KB rules blob,
    # and structured_content would ship a second copy alongside `content`.
)
def make_brain_enrich_pull(home: str):
    async def brain_enrich_pull(unit_id: str, with_rules: bool = True) -> dict:
        """Return one work unit's payload (from brain_enrich_units) with the current
        context attached, or {"empty": true} if the unit is gone. A `kind` "thread"
        unit returns `threads`; a `kind` "block" unit returns `block` + `items`.

        `with_rules` (default True) attaches the FULL extraction protocol so a
        general-purpose caller is self-contained (no plugin/skill file or source
        repo). The `enrich-batch` subagent passes ``with_rules=False``: it already
        carries the rules in its SYSTEM PROMPT (kept byte-identical to `_enrich_rules`
        by test_enrich_agent_rules_in_sync), so every sibling subagent shares one
        cacheable prefix — re-sending the rules here, in the uncached tool result,
        would pay for them a second time and defeat the caching."""
        import json as _json
        if not unit_id:
            return {"empty": True}
        try:
            d = _json.loads((_units_dir(home) / f"{unit_id}.json").read_text())
        except (OSError, ValueError):
            return {"empty": True}
        return _unit_payload(home, d, unit_id, with_rules)
    return brain_enrich_pull


# The optional answer blocks brain_enrich_pull may ask for, beyond extractions +
# merge_answers (_PUSH_BLOCKS, imported at module top). Each is drained
# by the daemon from the inbox object under this exact key (see drain.py
# BLOCK_DRAINERS + synthesise_threads). Without forwarding them, the
# synthesis/profile/community/memory/audit/review/curator work the batch
# requested is silently dropped on the MCP path.

def push_input_schema() -> dict:
    """brain_enrich_push's inputSchema, with one property per push block.

    Generated from _PUSH_BLOCKS rather than hand-listed so a block can never be
    added to the registry and forgotten here — an undeclared key is one the MCP
    client is not allowed to send, which makes its drainer unreachable.
    merge_answers stays explicit: it is the only block whose answer key differs
    from its unit key (merge_review).
    """
    return {"type": "object", "properties": {
        "unit_id": {"type": "string",
                    "description": "the unit you pulled (writes enrich_inbox/<unit_id>.json)"},
        "extractions": {"type": "array", "items": {"type": "object"},
                        "description": "one extraction object per thread (thread unit)"},
        "merge_answers": {"type": "array", "items": {"type": "object"},
                          "description": "answers for a merge_review block unit"},
        **{_k: {"type": "array", "items": {"type": "object"},
                "description": f"answers for a {_k} block unit"}
           for _k in _PUSH_BLOCKS},
    }, "required": ["unit_id"]}


@tool(
    "brain_enrich_push",
    description=(
        "Submit a unit's enrichment result by unit_id → enrich_inbox/<unit_id>.json; "
        "the daemon applies it, marks chunks enriched, and deletes the unit. Pass "
        "`extractions` (one per thread, for a thread unit) and/or the block answer "
        "field for a block unit: merge_answers (merge_review), or the block's own "
        "name for " + ", ".join(_PUSH_BLOCKS) + "."
    ),
    # GENERATED, never inlined: a new push block must reach both the advertised
    # schema and the validator from the one registry (see push_input_schema).
    # Evaluated once at import, which is safe because push_input_schema() is pure --
    # it reads only the module-level _PUSH_BLOCKS tuple and touches no config or
    # filesystem, so there is nothing for a later config change to invalidate.
    input_schema=push_input_schema(),
    annotations=_idempotent("Push extractions for a unit"),
    # atomic tmp.replace(target) keyed on unit_id, so a repeat is a no-op
    output_schema={
        "type": "object",
        "properties": {
            "written": {"type": "boolean"},
            "path": {"type": "string"},
            "error": {"type": "string"},
        },
        "required": ["written"],
    },
)
def make_brain_enrich_push(home: str):
    async def brain_enrich_push(unit_id: str = "", extractions: list | None = None,
                                merge_answers: list | None = None,
                                **blocks) -> dict:
        """Write a unit's enrichment result to enrich_inbox/<unit_id>.json for the
        daemon to drain (it applies the result, marks chunks enriched, and deletes the
        unit). Besides `extractions` and `merge_answers`, accepts the optional answer
        blocks — the synthesis/profile/community/memory/audit families and the
        review/curator families (see enrich_blocks.PUSH_BLOCKS) — and forwards
        each. Returns {"written": bool, path|error}.

        Schema rules:
          - `extractions` must be a list when provided; passing a non-list (string,
            dict, number) is rejected with a clear error so a subagent that narrates
            its result instead of producing a proper tool call is caught at the
            boundary rather than silently consuming the unit with zero extractions.
          - `extractions` may be None/omitted ONLY for block units that carry their
            answer in a block field (merge_answers or any enrich_blocks.PUSH_BLOCKS
            key).  A push with no
            extractions AND no block answers is rejected — it indicates a derailed
            subagent that produced prose instead of a real extraction payload.
        """
        import json as _json
        from pathlib import Path
        if not unit_id:
            return {"written": False, "error": "unit_id required"}
        # Type check before the or-coercion so a dict/string/number is never silently
        # treated as an empty list.  extractions=None is handled separately below.
        if extractions is not None and not isinstance(extractions, list):
            return {"written": False, "error": "extractions must be a list"}
        # A push with extractions=None/missing is only valid for block units — when at
        # least one block-answer field carries the real payload.  Without block answers
        # a missing extractions means the subagent derailed (produced prose, timed out,
        # or skipped the tool call entirely) and must not silently drain the unit.
        has_block_answer = (merge_answers is not None and merge_answers != []) or any(
            blocks.get(k) for k in _PUSH_BLOCKS
        )
        if extractions is None and not has_block_answer:
            return {"written": False,
                    "error": "extractions is required for thread units (must be a list of "
                             "extraction objects); omit or pass [] only for block units that "
                             "provide a block-answer field (merge_answers, synthesis, etc.)"}
        # An empty extractions list with no block answers is also invalid for thread units —
        # it would silently drain the unit with zero extractions applied, the same failure
        # mode as the coercion bug.  Only block units legitimately push with no extractions.
        if extractions == [] and not has_block_answer:
            return {"written": False,
                    "error": "extractions must be a non-empty list for thread units; "
                             "pass merge_answers/synthesis for block units"}
        extractions = extractions if extractions is not None else []
        try:
            inbox = Path(home) / "enrich_inbox"
            inbox.mkdir(parents=True, exist_ok=True)
            payload = {"unit_id": unit_id, "extractions": extractions,
                       "merge_answers": merge_answers or []}
            for _k in _PUSH_BLOCKS:
                if blocks.get(_k):
                    payload[_k] = blocks[_k]
            target = inbox / f"{unit_id}.json"
            tmp = inbox / f".{unit_id}.json.tmp"
            tmp.write_text(_json.dumps(payload, ensure_ascii=False))
            tmp.replace(target)  # atomic; the daemon never sees a half-written file
            return {"written": True, "path": str(target)}
        except (OSError, ValueError) as exc:
            return {"written": False, "error": str(exc)}
    return brain_enrich_push


@tool(
    "brain_enrich_advance",
    description="Nudge the daemon to apply pushed unit results and produce the next units immediately (instead of waiting for its normal cycle). Use between backfill rounds, then call brain_enrich_units again.",
    input_schema={"type": "object", "properties": {}},
    # Triggers an unbounded daemon drain+prepare cycle: the widest blast radius
    # of any tool. open_world_hint stays False -- this call itself only hits
    # the loopback control API; it's the *daemon* that then reaches Google,
    # one indirection away from this tool, which is deliberately not counted
    # as this tool's own open-world reach.
    annotations=_destructive("Wake the daemon to drain"),
    output_schema={
        "type": "object",
        "properties": {"woken": {"type": "boolean"}, "error": {"type": "string"}},
    },
)
def make_brain_enrich_advance(home: str):
    async def brain_enrich_advance() -> dict:
        """Nudge the daemon to run an immediate drain + prepare cycle, so newly
        enriched units are applied and the next units are produced in seconds instead
        of after the normal interval. Use between backfill rounds, then call
        brain_enrich_units again. Returns {"woken": true} or {"error": ...} when the
        daemon isn't reachable."""
        from mcpbrain.control_client import ControlClient, DaemonUnavailable
        try:
            return ControlClient(home).sync_now()
        except DaemonUnavailable as exc:
            return {"error": f"daemon not reachable: {exc}"}
    return brain_enrich_advance


# brain_enrich_claim is declared BEFORE brain_enrich_pending because
# registration order is the order tools/list advertises, and the surface
# 0.7.113 verified live has claim ahead of pending. Do not reorder these two
# functions without expecting test_advertised_order_is_registration_order to
# say so.
@tool(
    "brain_enrich_claim",
    description="Atomically lease ONE enrichment unit and return its payload (kind + threads/items + context) in a single call — units+pull folded. For the enrich-batch drain loop: call it, extract per your system-prompt rules, brain_enrich_push, and repeat until it returns {\"empty\": true}. Concurrent drainers never get the same unit. Rules are omitted by default (they're in your prompt); pass with_rules=true only for a self-contained caller.",
    input_schema={"type": "object", "properties": {
        "with_rules": {"type": "boolean",
                       "description": "inline the full extraction rules (default false; "
                                      "enrich-batch workers carry them in their prompt)"},
    }},
    annotations=_lease("Claim one work unit"),
)
def make_brain_enrich_claim(home: str):
    async def brain_enrich_claim(with_rules: bool = False) -> dict:
        """Atomically lease ONE ready unit and return its payload (units + pull,
        folded into a single call), or {"empty": true} when none is claimable.

        For the enrich-batch drain loop: each drainer calls this repeatedly.
        with_rules defaults False — the subagent carries the rules in its cached
        system prompt; a general-purpose caller may pass True to inline them.
        Lease acquisition is atomic (see _atomic_claim), so N concurrent drainers
        never take the same unit.
        """
        import json as _json
        import time as _time
        claims, now = _claims_dir(home), _time.time()
        try:
            files = sorted(_units_dir(home).glob("*.json"))
        except OSError:
            return {"empty": True}
        for f in files:
            uid = f.stem
            if _lease_is_live(claims / uid, now):
                continue
            try:
                d = _json.loads(f.read_text())
            except (OSError, ValueError):
                continue                              # skip garbage without leasing
            if not _atomic_claim(claims, uid, now):
                continue                              # lost the race; try the next
            _bump_unit_attempts(home, d)
            return _unit_payload(home, d, uid, with_rules)
        return {"empty": True}
    return brain_enrich_claim


@tool(
    "brain_enrich_pending",
    description="Count enrichment units still waiting (not under a live lease), WITHOUT claiming any. The coordinator calls this to decide whether to spawn another drainer wave: {\"pending\": N}. pending==0 means the queue is drained.",
    input_schema={"type": "object", "properties": {}},
    annotations=_ro("Count pending units"),
    output_schema={
        "type": "object",
        "properties": {"pending": {"type": "integer"}},
        "required": ["pending"],
    },
)
def make_brain_enrich_pending(home: str):
    async def brain_enrich_pending() -> dict:
        """Count enrichment units not under a live lease, WITHOUT claiming any.

        The coordinator calls this to decide whether to spawn another drainer
        wave — keeping done-ness a function of queue state, never reply text.
        Drainers self-serve work via brain_enrich_claim; this only observes.
        """
        import time as _time
        claims, now = _claims_dir(home), _time.time()
        try:
            files = sorted(_units_dir(home).glob("*.json"))
        except OSError:
            return {"pending": 0}
        n = sum(1 for f in files if not _lease_is_live(claims / f.stem, now))
        return {"pending": n}
    return brain_enrich_pending


@tool(
    "brain_meetings_today",
    description="Today's calendar events, each with has_pack. Use in the meeting-packs task instead of curl /api/dashboard/today.",
    input_schema={"type": "object", "properties": {}},
    # NOT _ro(): dashboard.calendar_today(home) calls
    # auth.build_google_services(...) and a live Calendar events().list(...)
    # in-process, synchronously, as part of this call. Keep open_world_hint
    # True here even though the tool is otherwise a plain, idempotent read —
    # do not "tidy" this back to _ro() / False.
    annotations=ToolAnnotations(
        title="Today's meetings", read_only_hint=True, destructive_hint=False,
        idempotent_hint=True, open_world_hint=True,
    ),
)
def make_brain_meetings_today(store, home: str):
    async def brain_meetings_today() -> list:
        """Today's calendar events, each annotated with has_pack. Same data the
        meeting-packs task used to read via curl /api/dashboard/today."""
        from mcpbrain import dashboard
        try:
            return dashboard.annotate_meeting_packs(store, dashboard.calendar_today(home))
        except Exception as exc:  # noqa: BLE001
            _log.exception("brain_meetings_today failed")
            return [{"error": str(exc)}]
    return brain_meetings_today


@tool(
    "brain_meeting_pack_get",
    description="Get the stored meeting pack for an event (incl. context_hash for change detection), or {\"found\": false}.",
    input_schema={"type": "object", "properties": {
        "event_id": {"type": "string"},
    }, "required": ["event_id"]},
    annotations=_ro("Get a meeting pack"),
)
def make_brain_meeting_pack_get(store):
    async def brain_meeting_pack_get(event_id: str) -> dict:
        """Return the stored pack for event_id (incl. context_hash), or
        {"found": false} when none exists."""
        try:
            return store.get_meeting_pack(event_id) or {"found": False}
        except Exception as exc:  # noqa: BLE001
            _log.exception("brain_meeting_pack_get failed")
            return {"found": False, "error": str(exc)}
    return brain_meeting_pack_get


@tool(
    "brain_meeting_pack_upsert",
    description="Create or update a meeting pack. Always pass context_hash so the next hourly run can skip it when unchanged.",
    input_schema={"type": "object", "properties": {
        "event_id": {"type": "string"},
        "event_title": {"type": "string"},
        "event_date": {"type": "string", "description": "YYYY-MM-DD"},
        "pack_text": {"type": "string", "description": "the markdown pack"},
        "attendees": {"type": "array", "items": {"type": "string"}},
        "context_hash": {"type": "string", "description": "fingerprint of the pack's inputs"},
    }, "required": ["event_id", "event_title", "event_date", "pack_text"]},
    annotations=_idempotent("Upsert a meeting pack"),
    output_schema={
        "type": "object",
        "properties": {"ok": {"type": "boolean"}, "error": {"type": "string"}},
        "required": ["ok"],
    },
)
def make_brain_meeting_pack_upsert(store):
    async def brain_meeting_pack_upsert(event_id: str, event_title: str,
                                        event_date: str, pack_text: str,
                                        attendees: list | None = None,
                                        context_hash: str = "",
                                        cowork_session: str = "meeting-packs") -> dict:
        """Create or update a meeting pack, storing context_hash so the next
        hourly run can skip it when unchanged. Returns {"ok": bool, error?}."""
        if not event_id:
            return {"ok": False, "error": "event_id required"}
        try:
            store.upsert_meeting_pack(
                event_id=event_id, event_title=event_title, event_date=event_date,
                pack_text=pack_text, attendees=attendees or [],
                cowork_session=cowork_session, context_hash=context_hash)
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            _log.exception("brain_meeting_pack_upsert failed")
            return {"ok": False, "error": str(exc)}
    return brain_meeting_pack_upsert
