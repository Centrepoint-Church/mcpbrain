"""build_server() must produce a fully-registered server without any transport."""
import asyncio

from mcpbrain.mcp_server import build_server, init_options
from tests.conftest import list_tools_via_handler

EXPECTED_TOOLS = {
    "brain_search", "brain_read", "brain_context", "brain_actions", "brain_graph",
    "brain_proactive", "brain_finding_resolve", "brain_ingest", "brain_action_create",
    "brain_action_update", "brain_decision", "brain_note", "brain_memory_write",
    "brain_gardener_apply", "brain_draft_context", "brain_draft_save", "brain_routine",
    "brain_enrich_units", "brain_enrich_pull", "brain_enrich_push",
    "brain_enrich_advance", "brain_enrich_claim", "brain_enrich_pending",
    "brain_meetings_today", "brain_meeting_pack_get", "brain_meeting_pack_upsert",
}


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
    opts = init_options(server)
    assert opts.server_version == __version__


def test_declared_tool_with_no_dispatch_branch_returns_is_error(mcp_env, monkeypatch):
    """A declared-but-undispatched tool must return isError, not raise.

    on_call_tool's trailing `else` is unreachable for names ABSENT from the
    registry (validation already returns isError for those). The one case it does
    cover is the real risk: a 27th REGISTERED tool added without a matching
    dispatch branch. Raising there produces exactly the traceback-in-the-fleet-log
    outcome that was deliberately eliminated for validation failures — the SDK's
    handler_exception_to_error_data ladder logger.exception()s a bare ValueError
    and returns ErrorData(code=0), indistinguishable from a genuine internal
    fault. Both paths must report the same way.

    The orphan is injected into a COPY of the registry (not the real dict), so it
    cannot leak into the advertised surface of any later test in this process.
    """
    from mcp import types

    from mcpbrain import tool_registry

    monkeypatch.setattr(tool_registry, "_REGISTRY", dict(tool_registry._REGISTRY))
    tool_registry.declare("brain_orphan", description="an undispatched tool",
                          input_schema={"type": "object", "properties": {}},
                          annotations=None)

    server = build_server(**mcp_env)
    entry = server.get_request_handler("tools/call")

    # ctx is unused by this path (only the brain_graph / brain_draft_context
    # branches read it, via _progress_reporter), so None is safe HERE.
    result = asyncio.run(entry.handler(
        None, types.CallToolRequestParams(name="brain_orphan", arguments={})))

    assert result.is_error, f"expected isError, got {result}"
    text = " ".join(c.text for c in result.content).lower()
    assert "unknown tool" in text and "brain_orphan" in text, text
