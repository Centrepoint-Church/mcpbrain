"""build_server() must produce a fully-registered server without any transport."""
import asyncio

from mcpbrain.mcp_server import build_server

EXPECTED_TOOLS = {
    "brain_search", "brain_read", "brain_context", "brain_actions", "brain_graph",
    "brain_proactive", "brain_finding_resolve", "brain_ingest", "brain_action_create",
    "brain_action_update", "brain_decision", "brain_note", "brain_memory_write",
    "brain_gardener_apply", "brain_draft_context", "brain_draft_save", "brain_routine",
    "brain_enrich_units", "brain_enrich_pull", "brain_enrich_push",
    "brain_enrich_advance", "brain_enrich_claim", "brain_enrich_pending",
    "brain_meetings_today", "brain_meeting_pack_get", "brain_meeting_pack_upsert",
}


async def list_tools_via_handler(server):
    """Invoke the registered tools/list handler directly and return the tool
    list, without an event loop-driven transport or a real MCP session.

    On mcp 2.x, handlers are looked up by method string
    (`server.get_request_handler("tools/list")` -> a HandlerEntry carrying
    `.params_type` and `.handler`) and invoked as `handler(ctx, params)`
    returning a full `types.ListToolsResult`. build_server()'s handlers ignore
    `ctx` entirely, so None is a faithful stand-in for the per-request
    ServerRequestContext the runner would build. Task 8 (tool annotations)
    reuses this helper, so it lives at module level rather than inline in a
    single test body.
    """
    from mcp import types

    entry = server.get_request_handler("tools/list")
    result = await entry.handler(None, types.PaginatedRequestParams())
    return result.tools


def test_build_server_registers_every_tool(mcp_env):
    """26 tools, registered, without starting stdio."""
    server = build_server(**mcp_env)
    tools = asyncio.run(list_tools_via_handler(server))
    names = {t.name for t in tools}
    assert names == EXPECTED_TOOLS, (
        f"missing={EXPECTED_TOOLS - names} unexpected={names - EXPECTED_TOOLS}"
    )


def test_build_server_registers_resource_handlers(mcp_env):
    server = build_server(**mcp_env)
    assert server.get_request_handler("resources/list") is not None
    assert server.get_request_handler("resources/read") is not None


def test_build_server_reports_mcpbrain_version(mcp_env):
    """serverInfo.version must be mcpbrain's version, not the SDK's."""
    from mcpbrain import __version__

    server = build_server(**mcp_env)
    opts = server.create_initialization_options()
    assert opts.server_version == __version__
