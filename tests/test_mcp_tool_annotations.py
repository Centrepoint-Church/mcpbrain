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
ADDITIVE = {
    "brain_ingest", "brain_action_create", "brain_decision", "brain_note",
    "brain_memory_write", "brain_draft_save",
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


@pytest.mark.parametrize("name", sorted(ADDITIVE))
def test_additive_tools_are_marked_non_idempotent_writes(name):
    """Each call creates a new distinct record (a new note/action/draft/...),
    so these must never read as read-only, destructive, or idempotent. Guards
    against e.g. brain_ingest's read_only_hint silently flipping True even
    though it writes via write_capture."""
    ann = tool_annotations()[name]
    assert ann.read_only_hint is False
    assert ann.destructive_hint is False
    assert ann.idempotent_hint is False


def test_open_world_reach_is_declared_accurately():
    """open_world_hint must match each tool's REAL reach, not default to False.

    brain_meetings_today's handler calls dashboard.calendar_today(home), which
    calls auth.build_google_services(...) + a live Calendar events().list(...)
    in-process, synchronously -- a genuine trust-boundary crossing. Every other
    tool touches only the local store, local files, or the loopback control API
    (brain_enrich_advance only wakes the daemon over loopback; it's the *daemon*
    that then reaches Google, one indirection away from this tool's own reach).
    This is stronger than a blanket "all False" check: it also catches a tool
    that UNDERSTATES its reach, not just one that overstates it.
    """
    reaches_open_world = {"brain_meetings_today"}
    for name, ann in tool_annotations().items():
        assert ann.open_world_hint is (name in reaches_open_world), (
            f"{name}: open_world_hint={ann.open_world_hint} misdeclares its reach"
        )


def test_annotations_are_attached_to_the_advertised_tools(mcp_env):
    """The annotations must reach the wire, not just exist in a dict."""
    import asyncio

    from mcpbrain.mcp_server import build_server
    from tests.conftest import list_tools_via_handler

    server = build_server(**mcp_env)
    for tool in asyncio.run(list_tools_via_handler(server)):
        assert tool.annotations is not None, f"{tool.name} advertised without annotations"
