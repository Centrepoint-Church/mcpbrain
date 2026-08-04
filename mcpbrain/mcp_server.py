"""The MCP protocol adapter: transport, handlers, and tool dispatch.

Deliberately NOT where the tools live. Every tool's handler factory and its
`@tool(...)` registry declaration are in `mcpbrain.tools`, which imports no
`mcp` -- see that module's docstring for why (the daemon reads the same registry
to execute tools, and must not pay the protocol stack's import to do it). This
module is the half that does need `mcp`: it builds the `Server`, advertises the
registry over tools/list (converting the stdlib annotations to the SDK model at
that boundary), serves resources/prompts, validates arguments, and dispatches
on_call_tool to the factories it imports from `mcpbrain.tools`.

Every `mcp` import here is FUNCTION-LOCAL, on purpose, and that is a rule not an
accident: it means importing this module costs ~144 modules / ~11ms rather than
~601 / ~0.26s, and the protocol stack is paid only when build_server()/main()
actually run. A single module-level `from mcp import types` undoes it -- which is
exactly what holding SDK annotation objects in the registry used to force.
"""
import logging
from pathlib import Path

from mcpbrain import config
from mcpbrain.enrich_blocks import PUSH_BLOCKS as _PUSH_BLOCKS
from mcpbrain.tool_registry import registry, spec
from mcpbrain.tools import (
    GRAPH_MAX_HOPS,
    _ROUTINES,
    _routine_instructions,
    make_brain_action_create,
    make_brain_action_update,
    make_brain_actions,
    make_brain_context,
    make_brain_decision,
    make_brain_draft_context,
    make_brain_draft_save,
    make_brain_enrich_advance,
    make_brain_enrich_claim,
    make_brain_enrich_pending,
    make_brain_enrich_pull,
    make_brain_enrich_push,
    make_brain_enrich_units,
    make_brain_finding_resolve,
    make_brain_gardener_apply,
    make_brain_graph,
    make_brain_ingest,
    make_brain_meeting_pack_get,
    make_brain_meeting_pack_upsert,
    make_brain_meetings_today,
    make_brain_memory_write,
    make_brain_note,
    make_brain_proactive,
    make_brain_search,
)

_log = logging.getLogger("mcpbrain.mcp_server")


def _sdk_annotations(ann):
    """Convert a registry `ToolAnnotations` into the SDK's pydantic model.

    The one place the stdlib seam type crosses back into `mcp`. Field names and
    defaults are identical on both sides (see tool_registry.ToolAnnotations), so
    the kwargs splat is total -- a field added on one side and not the other
    raises TypeError here rather than silently dropping a hint from the wire.

    None passes through as None: `annotations` is an optional ToolSpec field
    (tests register probes without it), and `types.Tool(annotations=None)` is
    exactly what such a tool advertised before this conversion existed.
    """
    import dataclasses

    from mcp import types
    if ann is None:
        return None
    return types.ToolAnnotations(**dataclasses.asdict(ann))


def write_heartbeat(home: str, *, now=None) -> None:
    """Record that Claude Desktop launched this MCP server (the verified-connected
    signal the status layer reads). Best-effort: never raise into startup."""
    import json
    from datetime import datetime, timezone
    now = now or datetime.now(timezone.utc)
    try:
        (Path(home) / "mcp_heartbeat.json").write_text(
            json.dumps({"last_seen": now.isoformat()})
        )
    except OSError:
        pass


def _default_owner() -> str:
    """The install owner for MCP-initiated writes, from config (empty if unset)."""
    return config.owner_name(str(config.app_dir()))


def _progress_reporter(ctx):
    """Return an async progress reporter for `ctx`, or a genuine no-op.

    Progress notifications exist only because Claude resets its idle timer on
    receipt (it reportedly doesn't render them) -- the point is keeping a slow
    call from being reaped, not UI. So this is deliberately narrow: no token in
    the request's `_meta` means the client never asked, and sending anything
    unsolicited would be noise, so the no-op path sends nothing at all. And a
    failed send must never fail the tool call -- progress is advisory -- so
    the real path swallows any exception from the send and logs at debug.

    `ctx.meta` is a `RequestParamsMeta` TypedDict (`{"progress_token": ...}`)
    and can itself be None, hence the `(ctx.meta or {})` guard.
    """
    token = (ctx.meta or {}).get("progress_token")
    if token is None:
        async def _noop(progress, total=None, message=None) -> None:
            return None
        return _noop

    async def _report(progress, total=None, message=None) -> None:
        try:
            await ctx.session.send_progress_notification(
                token, progress, total=total, message=message
            )
        except Exception:  # noqa: BLE001 - advisory only; never fail the call
            _log.debug("progress notification failed", exc_info=True)

    return _report


def _resource_entries() -> list[tuple[str, Path]]:
    """(name, resolved_path) for every context resource we expose.

    Two roots: the app-dir context (the daemon-maintained note index, e.g.
    memory.md) and the per-user records repo (identity, voice, preferences,
    reference, decisions, MEMORY.md, CLAUDE.md) so the working Cowork project can
    read standing context through the MCP server without any filesystem paths.
    Only existing files are returned; a missing file or repo is simply absent.
    """
    entries: list[tuple[str, Path]] = []
    app_ctx = config.app_dir() / "context"
    if app_ctx.is_dir():
        for md in sorted(app_ctx.glob("*.md")):
            entries.append((md.name, md.resolve()))
    records = Path(config.records_dir(str(config.app_dir())))
    candidates: list[Path] = [records / "CLAUDE.md", records / "MEMORY.md",
                              records / "state" / "decisions.md"]
    for sub in ("context", "reference"):
        sub_dir = records / sub
        if sub_dir.is_dir():  # guard: never raise if the repo isn't scaffolded yet
            candidates.extend(sorted(sub_dir.glob("*.md")))
    for p in candidates:
        if p.is_file():
            entries.append((str(p.relative_to(records)), p.resolve()))
    return entries


async def list_context_resources():
    """Return types.Resource entries for the app-dir context + the records repo."""
    from mcp import types
    return [
        types.Resource(uri=f"file://{path}", name=name, mimeType="text/markdown")
        for name, path in _resource_entries()
    ]


async def read_context_resource(uri) -> str:
    """Return a resource's text, rejecting any uri not in the advertised allowlist.

    Exact membership against _resource_entries() is the containment guard: only a
    path we actually expose can be read, so no traversal or arbitrary-file read is
    possible regardless of the uri given.
    """
    from urllib.parse import unquote, urlparse
    # urlparse handles both file:///abs and file://localhost/abs forms a client
    # might send; unquote decodes %20 etc. (the allowlist is the real guard).
    path = Path(unquote(urlparse(str(uri)).path)).resolve()
    allowed = {p for _, p in _resource_entries()}
    if path not in allowed:
        raise ValueError(f"resource not in allowlist: {uri}")
    return path.read_text(encoding="utf-8")


_RESOURCE_POLL_INTERVAL_S = 5.0


def _resource_fingerprint() -> frozenset[str]:
    """The advertised resource SET, as a comparable value.

    Paths only, deliberately: notifications/resources/list_changed is about the
    LIST changing, not content. Re-reading an existing URI after an in-place edit
    (hot.md, decisions.md) is what resources/subscribe is for, and Claude does not
    support subscribe — so we do not track content or mtimes here. Including them
    would emit list_changed for changes a client cannot act on.
    """
    return frozenset(str(p) for _, p in _resource_entries())


async def watch_resources(session, interval_s: float = _RESOURCE_POLL_INTERVAL_S) -> None:
    """Emit notifications/resources/list_changed when the resource set changes.

    mcpbrain's own tools add resource files mid-session (brain_memory_write →
    memory/<slug>.md, brain_gardener_apply → reference/*.md); without this a
    long-lived client never learns they exist.

    Polls rather than watching the filesystem: a native watcher (watchfiles) would
    add a binary dependency to a fleet-shipped package with an open Windows QA
    gate, while _resource_entries() is a handful of globs over a few dozen files.
    5s is far below human reaction time for "I just saved a memory".

    Never cancelled, deliberately: this is a stdio server, so the process lifetime
    IS the connection lifetime — a leaked poller dies with the process. Building a
    cancellation mechanism for a lifetime that doesn't exist would be ceremony,
    not safety.
    """
    import asyncio

    # The ENTIRE poll is inside the try, fingerprinting included. It used to sit
    # outside, so any OSError/ValueError escaping _resource_entries() (a glob
    # permission blip, config.records_dir() reading a config.json torn mid-write
    # by apply_config or a restore, a relative_to surprise) killed the watcher
    # for the rest of the session and printed "Task exception was never
    # retrieved" to the server's stderr -- the one channel this server is
    # expected to keep silent. A transient poll failure must cost one poll.
    #
    # previous starts as None ("no baseline yet") rather than being seeded before
    # the loop, so a first fingerprint that raises just retries next tick instead
    # of taking the whole watcher down before it starts. None never notifies.
    previous = None
    while True:
        try:
            current = _resource_fingerprint()
            changed = previous is not None and current != previous
            previous = current  # update first: a failed send must not re-fire forever
            if changed:
                await session.send_resource_list_changed()
        except Exception:  # noqa: BLE001 - client may have gone away / poll blipped
            _log.debug("resource watch poll failed", exc_info=True)
        await asyncio.sleep(interval_s)


def init_options(server):
    """InitializationOptions for `server`, advertising resources/list_changed.

    A module-level helper rather than a second return value from build_server():
    the contract `build_server(store, draft_store, client, home) -> Server` is
    already depended on by main() and four test modules, and bolting an attribute
    onto the SDK's Server object would be worse. One way to build the options.

    resources_changed=True is backed by watch_resources(). Deliberately does NOT
    advertise subscribe: we never send resources/updated, and promising per-URI
    updates we don't implement is worse than not advertising them (the SDK derives
    subscribe from whether a resources/subscribe handler is registered, and we
    register none).

    KNOWN DIVERGENCE (measured, not theoretical). The SDK has a second
    capability-reporting surface that disagrees with this one:

      initialize                    -> {'subscribe': False, 'listChanged': True}
      server/discover @ 2026-07-28  -> {'subscribe': False, 'listChanged': False}

    `Server.get_capabilities` takes a `protocol_version`, and at
    MODERN_PROTOCOL_VERSIONS (currently just "2026-07-28") it IGNORES
    notification_options entirely, deriving every listChanged flag from whether a
    `subscriptions/listen` handler is registered. We register none, so that branch
    reports False. It is reached only from `server/discover` — which
    `Server.__init__` registers UNCONDITIONALLY (lowlevel/server.py:446-462), so
    this server already serves it; it is not gated behind a kwarg we omit.

    Bounded by two facts. create_initialization_options() never passes
    protocol_version, so the handshake capabilities below are computed once at
    startup and do NOT change when a client negotiates a newer era — the
    list_changed advertisement cannot silently vanish on the path clients actually
    use. And no Claude client calls server/discover today.

    Deliberately NOT "fixed": registering on_subscriptions_listen just to flip
    that branch would advertise a capability we do not implement — the same
    mistake we refuse to make with subscribe. REVISIT WHEN: a client actually
    calls server/discover, or an SDK bump makes the handshake path
    protocol-version-dependent.
    """
    from mcp.server.lowlevel.server import NotificationOptions

    return server.create_initialization_options(
        notification_options=NotificationOptions(resources_changed=True)
    )


def _draft_reply_canonical_body() -> str:
    """The canonical draft-reply prompt body, shipped inside the wheel at
    mcpbrain/prompts/draft-reply.md (package-data — see pyproject.toml). This
    is the single source of truth for the draft-reply pipeline instructions;
    plugin/skills/mcpbrain-draft-reply/SKILL.md carries a byte-identical copy
    between markers for the plugin loader, because plugin/ does not ship in
    the wheel and so cannot be the canonical location. bin/sync_agents.py
    regenerates that copy; test_draft_reply_skill_in_sync pins the two
    byte-equal. Returns '' if the bundled file is somehow missing (never
    raises)."""
    from pathlib import Path
    try:
        return (Path(__file__).parent / "prompts" / "draft-reply.md").read_text(
            encoding="utf-8").strip()
    except OSError:
        return ""


def _draft_reply_prompt_body(email_id: str, intent: str) -> str:
    """Render the draft-reply prompt: the canonical pipeline body, followed by
    the caller-supplied arguments interpolated at the end."""
    body = _draft_reply_canonical_body()
    return (
        f"{body}\n\n---\n"
        f"email_id: {email_id}\n"
        f"intent: {intent or '(none given — infer from the thread)'}\n"
    )


def prompt_definitions() -> dict[str, dict]:
    """name -> {title, description, arguments} for every MCP prompt.

    The 4 routines mirror what brain_routine serves (same _routine_instructions
    source, so they cannot drift); draft-reply is the parameterized reply
    pipeline, exposed here so it also reaches installs without the plugin.
    Deliberately does NOT add draft-reply to _ROUTINES -- that allowlist
    governs brain_routine (a scheduled-task tool) and draft-reply is not a
    scheduled routine.
    """
    return {
        **{
            name: {
                "title": f"Run the {name} routine",
                "description": (
                    f"Full instructions for the {name} routine, to follow verbatim. "
                    "Self-contained: no skill or command resolution needed."
                ),
                "arguments": [],
            }
            for name in _ROUTINES
        },
        "draft-reply": {
            "title": "Draft a reply in the owner's voice",
            "description": (
                "Draft a reply to an email using the 4-stage plan/draft/critique/"
                "voice pipeline, then save it to the brain."
            ),
            "arguments": [
                {
                    "name": "email_id",
                    "description": "The message id to reply to.",
                    "required": True,
                },
                {
                    "name": "intent",
                    "description": "Optional steer, e.g. 'decline politely'.",
                    "required": False,
                },
            ],
        },
    }


async def get_prompt_body(name: str, arguments: dict) -> str:
    """Return a prompt's rendered text, rejecting unknown names and missing
    required arguments.

    A required argument is rejected both when the key is absent AND when it
    is present but empty/whitespace: `arguments.get(name)` is falsy for both
    None and "", so an empty string is treated the same as an omitted key
    rather than being passed through to fail later (e.g. inside
    brain_draft_context) with a less useful error.
    """
    defs = prompt_definitions()
    if name not in defs:
        raise ValueError(f"unknown prompt: {name}")
    for arg in defs[name]["arguments"]:
        if arg["required"] and not arguments.get(arg["name"]):
            raise ValueError(f"missing required argument for {name}: {arg['name']}")
    if name in _ROUTINES:
        return _routine_instructions(name)
    return _draft_reply_prompt_body(
        arguments.get("email_id", ""), arguments.get("intent", "")
    )


def _validate_tool_arguments(name: str, arguments: dict) -> None:
    """Validate arguments against the tool's declared inputSchema.

    The schema comes from the registry -- the SAME object on_list_tools
    advertises, so validation cannot drift from the advertised surface. mcp 2.x's
    low-level server does no validation of its own (mcp 1.x's
    call_tool(validate_input=True) default did), so this is the only thing
    standing between a malformed call and a handler KeyError.

    Raises ValueError with a readable, field-naming message. Deliberately does
    NOT fill in defaults or otherwise mutate `arguments`: brain_enrich_push's
    guards depend on distinguishing an absent field (None) from a present-but-
    empty one ([]), which default-injection would destroy. The registry's frozen
    schemas make the other direction structurally impossible too -- validation
    cannot mutate the schema it validates against.
    """
    import jsonschema

    try:
        schema = spec(name).input_schema
    except KeyError:
        raise ValueError(f"unknown tool: {name}") from None
    try:
        jsonschema.validate(arguments, schema)
    except jsonschema.ValidationError as exc:
        field = ".".join(str(p) for p in exc.absolute_path) or (
            # a `required` violation reports the field in the message, not the path
            exc.message.split("'")[1] if "'" in exc.message else "arguments"
        )
        raise ValueError(f"invalid arguments for {name}: {field}: {exc.message}") from exc


def build_server(store, draft_store, client, home: str):
    """Construct the MCP Server with every handler registered, no transport started.

    Split out of main() so the registration layer is reachable from tests without
    spawning a subprocess or starting an event loop: an import-only smoke test
    never evaluates the handler registrations, which is how the mcp 2.0 API break
    reached production unseen (see tests/test_mcp_sdk_contract.py).
    """
    from mcp.server import Server
    from mcp import types

    from mcpbrain import __version__, config

    search = make_brain_search(client)
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
    gardener_apply = make_brain_gardener_apply(store)
    # Draft tools write to draft_records, so they need a writable store handle.
    # the read-only store cannot INSERT; this writable handle is scoped to draft_records
    # writes by the MCP server (serialised via WAL + busy_timeout).
    draft_context_fn = make_brain_draft_context(draft_store, home)
    draft_save_fn = make_brain_draft_save(draft_store, home)
    # Autonomous-loop tools (host-native). Reads use the RO store; pack upsert
    # needs the writable handle (same one the draft tools use).
    enrich_units = make_brain_enrich_units(home)
    enrich_pull = make_brain_enrich_pull(home)
    enrich_push = make_brain_enrich_push(home)
    enrich_advance = make_brain_enrich_advance(home)
    enrich_claim = make_brain_enrich_claim(home)
    enrich_pending = make_brain_enrich_pending(home)
    meetings_today = make_brain_meetings_today(store, home)
    meeting_pack_get = make_brain_meeting_pack_get(store)
    meeting_pack_upsert = make_brain_meeting_pack_upsert(draft_store)
    # Writable handle: resolving a finding UPDATEs proactive_findings.
    finding_resolve = make_brain_finding_resolve(draft_store)

    # watch_resources() needs a live ServerSession, which only exists once a client
    # has connected — so it cannot start in main(). It starts from the first
    # resources/list instead: a client that never lists resources has no use for
    # list_changed. Guarded by a closure variable (one Server per process on stdio,
    # so this is per-connection). There is no await between the guard check and the
    # assignment, so a second concurrent resources/list cannot slip past it.
    _watcher_task = None

    async def _ensure_watcher(ctx) -> None:
        nonlocal _watcher_task
        session = getattr(ctx, "session", None)
        if _watcher_task is not None or session is None:
            return
        import asyncio

        # Hold a strong reference: asyncio only tracks tasks weakly, so a bare
        # create_task() result can be garbage-collected mid-poll.
        _watcher_task = asyncio.create_task(watch_resources(session))

    async def on_list_resources(ctx, params) -> types.ListResourcesResult:
        await _ensure_watcher(ctx)
        return types.ListResourcesResult(resources=await list_context_resources())

    async def on_read_resource(ctx, params) -> types.ReadResourceResult:
        # 2.x requires a full result model with the uri echoed back; the 1.x
        # ReadResourceContents helper is no longer accepted at the low level.
        text = await read_context_resource(params.uri)
        return types.ReadResourceResult(contents=[
            types.TextResourceContents(
                uri=params.uri, mimeType="text/markdown", text=text)
        ])

    async def on_list_prompts(ctx, params) -> types.ListPromptsResult:
        # prompt_definitions() is the single source both this list and
        # on_get_prompt read, so the advertised prompt set and its argument
        # schema can never drift from what get_prompt_body actually enforces.
        return types.ListPromptsResult(prompts=[
            types.Prompt(
                name=name,
                title=spec["title"],
                description=spec["description"],
                arguments=[
                    types.PromptArgument(
                        name=a["name"], description=a["description"], required=a["required"]
                    )
                    for a in spec["arguments"]
                ],
            )
            for name, spec in prompt_definitions().items()
        ])

    async def on_get_prompt(ctx, params) -> types.GetPromptResult:
        # get_prompt_body raises ValueError for an unknown name or a missing
        # required argument; the SDK's dispatcher turns that into a JSON-RPC
        # error the client sees as an exception from session.get_prompt(...).
        body = await get_prompt_body(params.name, dict(params.arguments or {}))
        return types.GetPromptResult(
            description=prompt_definitions()[params.name]["description"],
            messages=[
                types.PromptMessage(
                    role="user", content=types.TextContent(type="text", text=body)
                )
            ],
        )

    async def on_list_tools(ctx, params) -> types.ListToolsResult:
        # The registry is the single source: what we advertise and what
        # _validate_tool_arguments enforces cannot drift, because they are the
        # same object. Iteration order is registration order, which is the order
        # each tool's declaration appears in this module.
        #
        # dict(...) on the schemas hands pydantic a plain mapping rather than the
        # registry's frozen one; the stored schema itself is never handed out
        # mutable.
        #
        # _sdk_annotations converts the registry's stdlib ToolAnnotations into
        # the SDK model here, at the protocol boundary -- the registry itself
        # stays mcp-free so the daemon can read it (see tool_registry).
        #
        # `is not None`, not a truthiness test: ToolSpec's contract is that None
        # and {} are DIFFERENT (None = declares no outputSchema), and the
        # structured_content check in on_call_tool already reads `is not None`.
        # A tool declaring `output_schema={}` under a truthy test advertised no
        # outputSchema yet still emitted structured_content -- the two halves of
        # one decision disagreeing.
        return types.ListToolsResult(tools=[
            types.Tool(
                name=name,
                description=s.description,
                inputSchema=dict(s.input_schema),
                annotations=_sdk_annotations(s.annotations),
                **({"outputSchema": dict(s.output_schema)}
                   if s.output_schema is not None else {}),
            )
            for name, s in registry().items()
        ])

    async def on_call_tool(ctx, params) -> types.CallToolResult:
        import json
        name, arguments = params.name, (params.arguments or {})
        # mcp 2.x's low-level server validates NOTHING (1.x's
        # call_tool(validate_input=True) default did), so this call is the only
        # inputSchema enforcement between a client and the handlers below.
        #
        # Returned as isError rather than raised, restoring mcp 1.x behaviour
        # exactly (1.29.0: _make_error_result(f"Input validation error: ...")).
        # A bare ValueError falls through the SDK's
        # handler_exception_to_error_data ladder to logger.exception() +
        # ErrorData(code=0), so every malformed model call would write a ~20-line
        # traceback into the MCP log and be indistinguishable from a genuine
        # internal fault — bad on a fleet-shipped server. An isError result also
        # goes back into the conversation, so the model can retry with corrected
        # arguments.
        #
        # Scoped to THIS call only, deliberately: a blanket `except ValueError`
        # around the dispatch below would swallow a genuine ValueError raised
        # deep inside a handler (store code, date parsing, …) and dress a real
        # bug up as a tidy error result — trading one silent-failure class for
        # another.
        try:
            _validate_tool_arguments(name, arguments)
        except ValueError as exc:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=str(exc))],
                isError=True,
            )
        # Single return point below: every branch here assigns `out` and falls
        # through, rather than each constructing its own CallToolResult. This
        # collapses what was 26 separate `return types.CallToolResult(...)`
        # statements (one per branch) into 26 assignments — removing the class
        # of bug where a missed/miscopied branch returns a bare, unwrapped
        # value instead of a proper result. brain_actions' early
        # "not configured" response is the one deliberate exception: its
        # payload is a hardcoded JSON *string* literal, not a dict produced by
        # a handler, so it stays its own explicit return rather than flowing
        # through the `json.dumps(out)` at the bottom.
        if name == "brain_read":
            out = store.get_chunk(arguments["doc_id"])
        elif name == "brain_context":
            out = await context(
                entity=arguments.get("entity", ""),
                mode=arguments.get("mode", "profile"),
                community_id=arguments.get("community_id"),
            )
        elif name == "brain_actions":
            # null-coalesce: explicit None/empty defaults to the configured owner
            owner = arguments.get("owner") or _default_owner()
            if not owner:
                return types.CallToolResult(
                    content=[types.TextContent(type="text", text='[{"error": "Install not configured: set owner_name in config.json"}]')]
                )
            status = arguments.get("status") or "open"
            out = await actions(owner, status)
        elif name == "brain_graph":
            report = _progress_reporter(ctx)
            hops = arguments.get("hops", 1)
            # The BFS below caps at GRAPH_MAX_HOPS regardless of a larger
            # `hops` argument, so the reported total must be the capped value
            # too -- otherwise a hops=10 call would report "3 of 10" forever
            # and a client rendering progress would never see it complete.
            total_hops = max(0, min(hops, GRAPH_MAX_HOPS))

            async def _on_hop(completed: int) -> None:
                await report(completed, total_hops, f"hop {completed} of {total_hops}")

            out = await graph(arguments["entity"], hops,
                              at_time=arguments.get("at_time"),
                              include_invalidated=arguments.get("include_invalidated", False),
                              on_hop=_on_hop)
        elif name == "brain_proactive":
            out = await proactive(arguments.get("finding_type", ""), arguments.get("severity", ""))
        elif name == "brain_finding_resolve":
            out = await finding_resolve(
                finding_id=arguments.get("finding_id", 0),
                outcome=arguments.get("outcome", ""),
                note=arguments.get("note", ""),
            )
        elif name == "brain_ingest":
            out = await ingest(
                title=arguments.get("title", ""),
                content=arguments.get("content", ""),
                tags=arguments.get("tags", ""),
                observation_type=arguments.get("observation_type", "note"),
                org=arguments.get("org", ""),
            )
        elif name == "brain_action_create":
            out = await action_create(
                text=arguments.get("text", ""),
                owner=arguments.get("owner") or _default_owner(),
                deadline=arguments.get("deadline", ""),
                org=arguments.get("org", ""),
                project_id=arguments.get("project_id", ""),
                area_id=arguments.get("area_id", ""),
            )
        elif name == "brain_action_update":
            out = await action_update(
                action_id=arguments.get("action_id", 0),
                status=arguments.get("status", ""),
            )
        elif name == "brain_decision":
            out = await decision(
                text=arguments.get("text", ""),
                rationale=arguments.get("rationale", ""),
                owner=arguments.get("owner") or _default_owner(),
                supersedes=arguments.get("supersedes", ""),
                org=arguments.get("org", ""),
            )
        elif name == "brain_note":
            out = await note(
                text=arguments.get("text", ""),
            )
        elif name == "brain_memory_write":
            out = await memory_write(
                slug=arguments.get("slug", ""),
                description=arguments.get("description", ""),
                body=arguments.get("body", ""),
                memory_type=arguments.get("memory_type", "project"),
            )
        elif name == "brain_gardener_apply":
            out = await gardener_apply(
                lane=arguments.get("lane", ""),
                filename=arguments.get("filename", ""),
                content=arguments.get("content", ""),
                asserts_person_role=bool(arguments.get("asserts_person_role", False)),
                attribution_source=arguments.get("attribution_source", ""),
                attribution_quote=arguments.get("attribution_quote", ""),
                attribution_doc_id=arguments.get("attribution_doc_id", ""),
            )
        elif name == "brain_draft_context":
            report = _progress_reporter(ctx)
            # 1-based step matching each stage draft_context actually moves
            # through; "critique" only fires when draft_critic is enabled, so
            # total is fixed at the 4 possible stages rather than recomputed.
            _DRAFT_STAGES = {"email_lookup": (1, "looking up email"),
                             "voice_rules": (2, "loading voice rules"),
                             "samples": (3, "gathering thread samples"),
                             "critique": (4, "running voice/coverage critique")}

            async def _on_stage(stage: str) -> None:
                # Direct index, no silent fallback: draft.draft_context's _emit
                # (the only caller) only ever passes one of these 4 literal
                # names, so a `.get(..., (0, stage))` default here would be
                # dead code that could only ever silently misreport step 0 --
                # indistinguishable from a genuine first stage -- if draft.py
                # ever drifted out of sync with this table.
                step, message = _DRAFT_STAGES[stage]
                await report(step, len(_DRAFT_STAGES), message)

            out = await draft_context_fn(
                email_id=arguments.get("email_id", ""),
                intent=arguments.get("intent", ""),
                on_stage=_on_stage,
            )
        elif name == "brain_draft_save":
            out = await draft_save_fn(
                email_id=arguments.get("email_id", ""),
                thread_id=arguments.get("thread_id", ""),
                intent=arguments.get("intent", ""),
                final_draft=arguments.get("final_draft", ""),
                parent_draft_id=arguments.get("parent_draft_id"),
            )
        elif name == "brain_routine":
            rname = (arguments or {}).get("name", "")
            instructions = _routine_instructions(rname)
            out = ({"name": rname, "instructions": instructions} if instructions
                   else {"error": f"unknown routine {rname!r}", "available": list(_ROUTINES)})
        elif name == "brain_enrich_units":
            out = await enrich_units()
        elif name == "brain_enrich_pull":
            out = await enrich_pull(unit_id=arguments.get("unit_id", ""),
                                    with_rules=arguments.get("with_rules", True))
        elif name == "brain_enrich_push":
            # Do NOT coerce extractions=None to [] here — the handler must see None
            # when the field is absent so the block-unit vs thread-unit guard works.
            out = await enrich_push(
                unit_id=arguments.get("unit_id", ""),
                extractions=arguments.get("extractions"),  # None if absent; validated in handler
                merge_answers=arguments.get("merge_answers") or [],
                **{k: arguments[k] for k in _PUSH_BLOCKS if arguments.get(k)},
            )
        elif name == "brain_enrich_advance":
            out = await enrich_advance()
        elif name == "brain_enrich_claim":
            out = await enrich_claim(with_rules=arguments.get("with_rules", False))
        elif name == "brain_enrich_pending":
            out = await enrich_pending()
        elif name == "brain_meetings_today":
            out = await meetings_today()
        elif name == "brain_meeting_pack_get":
            out = await meeting_pack_get(arguments.get("event_id", ""))
        elif name == "brain_meeting_pack_upsert":
            out = await meeting_pack_upsert(
                event_id=arguments.get("event_id", ""),
                event_title=arguments.get("event_title", ""),
                event_date=arguments.get("event_date", ""),
                pack_text=arguments.get("pack_text", ""),
                attendees=arguments.get("attendees") or [],
                context_hash=arguments.get("context_hash", ""),
            )
        elif name == "brain_search":
            out = await search(arguments["query"], arguments.get("limit", 10))
        else:
            # Unreachable for a name ABSENT from the registry — validation
            # above already returned isError for those. What lands here is a
            # schema entry added without a matching dispatch branch, and it
            # reports the SAME way as a validation failure on purpose: raising
            # would fall through the SDK's handler_exception_to_error_data
            # ladder to logger.exception() + ErrorData(code=0), i.e. the
            # traceback-in-the-fleet-log outcome deliberately eliminated above.
            return types.CallToolResult(
                content=[types.TextContent(
                    type="text", text=f"unknown tool: {name}")],
                isError=True,
            )

        # structured_content ships *alongside* content (never instead of it), so
        # clients that ignore structured output see the exact same response as
        # before this task. Only set for the tools with a declared outputSchema,
        # and only when `out` is actually the dict-shaped envelope that schema
        # describes — a declared schema the tool then violates (e.g. by
        # returning a list) would be worse than no schema at all.
        result_kwargs = {}
        if spec(name).output_schema is not None and isinstance(out, dict):
            result_kwargs["structured_content"] = out
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(out))],
            **result_kwargs,
        )

    # Standing instructions read by every session that connects this server —
    # the owner's identity/role/orgs + the brain tools + the capture loop. Rendered
    # from saved config at connect time (so it's never a stale paste), then captured
    # for the life of the connection; a config change is picked up on reconnect.
    #
    # 2.x registration: handlers are constructor kwargs, not decorators (the 1.x
    # @server.list_resources()/read_resource()/list_tools()/call_tool() API was
    # deleted in 2.0 with no shim).
    server = Server(
        "mcpbrain",
        version=__version__,
        instructions=config.render_project_instructions(config.read_config(home)),
        on_list_resources=on_list_resources,
        on_read_resource=on_read_resource,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
        on_list_prompts=on_list_prompts,
        on_get_prompt=on_get_prompt,
    )
    return server


def main() -> None:  # stdio entry point, exercised manually + in P3 integration
    import mcp.server.stdio

    from mcpbrain import config
    from mcpbrain.control_client import ControlClient
    from mcpbrain.embed import embedder_dim
    from mcpbrain.store import Store

    _store_path, _store_dim = config.store_path(), embedder_dim("bge-small")
    store = Store(_store_path, dim=_store_dim, read_only=True)   # read path: index/graph/email
    draft_store = Store(_store_path, dim=_store_dim, read_only=False)  # draft_records writes
    home = str(config.app_dir())
    write_heartbeat(home)
    server = build_server(store, draft_store, ControlClient(), home)

    async def _run():
        async with mcp.server.stdio.stdio_server() as (r, w):
            # init_options(), not create_initialization_options() bare: the bare
            # call leaves NotificationOptions.resources_changed False, so
            # resources/list_changed would never be advertised. The watcher itself
            # needs a live session and so starts from the first resources/list.
            await server.run(r, w, init_options(server))

    import asyncio
    asyncio.run(_run())


if __name__ == "__main__":  # spawnable: python -m mcpbrain.mcp_server
    main()
