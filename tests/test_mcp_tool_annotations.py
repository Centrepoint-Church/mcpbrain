"""Every tool must carry accurate safety annotations.

Two are load-bearing rather than decorative: brain_gardener_apply overwrites a
records file and git-commits synchronously, and brain_enrich_units/claim acquire
a 15-minute lease despite reading like queries -- a client that retries them as
safe reads leaks leases.
"""
import pytest

from mcpbrain.mcp_server import tool_annotations, tool_schemas

READ_ONLY = {
    "brain_search", "brain_read", "brain_context", "brain_actions", "brain_graph",
    "brain_proactive", "brain_routine", "brain_enrich_pull", "brain_enrich_pending",
    "brain_meetings_today", "brain_meeting_pack_get", "brain_draft_context",
}
DESTRUCTIVE = {"brain_gardener_apply", "brain_enrich_advance"}
LEASE_ACQUIRING = {"brain_enrich_units", "brain_enrich_claim"}
IDEMPOTENT_MUTATORS = {
    "brain_action_update", "brain_meeting_pack_upsert", "brain_finding_resolve",
    "brain_enrich_push",
}


def test_every_tool_is_annotated():
    assert set(tool_annotations()) == set(tool_schemas())


@pytest.mark.parametrize("name", sorted(READ_ONLY))
def test_read_only_tools_are_marked_read_only(name):
    ann = tool_annotations()[name]
    assert ann.read_only_hint is True
    assert ann.destructive_hint is False


@pytest.mark.parametrize("name", sorted(DESTRUCTIVE))
def test_destructive_tools_are_marked_destructive(name):
    ann = tool_annotations()[name]
    assert ann.read_only_hint is False
    assert ann.destructive_hint is True


@pytest.mark.parametrize("name", sorted(LEASE_ACQUIRING))
def test_lease_acquiring_tools_are_not_read_only_and_not_idempotent(name):
    """The subtle case: these read work but their side effect is claiming a lease."""
    ann = tool_annotations()[name]
    assert ann.read_only_hint is False, f"{name} acquires a lease; not a safe read"
    assert ann.idempotent_hint is False, f"{name} returns a different unit each call"
    assert ann.destructive_hint is False


@pytest.mark.parametrize("name", sorted(IDEMPOTENT_MUTATORS))
def test_idempotent_mutators_are_marked_idempotent(name):
    ann = tool_annotations()[name]
    assert ann.read_only_hint is False
    assert ann.idempotent_hint is True


def test_no_tool_touches_the_open_world():
    """Every tool is local store, local files, or loopback HTTP.

    The daemon reaches Gmail/Drive/Calendar, but no MCP tool does directly. If a
    tool ever gains real external reach, this test must be updated deliberately.
    """
    for name, ann in tool_annotations().items():
        assert ann.open_world_hint is False, f"{name} claims external reach"


def test_annotations_are_attached_to_the_advertised_tools(mcp_env):
    """The annotations must reach the wire, not just exist in a dict."""
    import asyncio

    from mcpbrain.mcp_server import build_server
    from tests.conftest import list_tools_via_handler

    server = build_server(**mcp_env)
    for tool in asyncio.run(list_tools_via_handler(server)):
        assert tool.annotations is not None, f"{tool.name} advertised without annotations"
