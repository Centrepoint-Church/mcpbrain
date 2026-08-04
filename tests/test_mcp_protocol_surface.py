"""Round-trips the entire MCP surface over a real stdio session.

Why this exists: before this file, 2 of 26 tools were ever called through the
protocol and the resource handlers never were. The dispatch layer in _call is
pure argument plumbing -- exactly the code an SDK port rewrites wholesale --
and it was effectively untested. It also captures subprocess stderr, so a
startup crash reports its traceback instead of a 15-second timeout.

No pytest-asyncio in this suite (see test_mcp_server_stdio.py): each test is a
plain sync function that drives its own event loop with `asyncio.run(...)`,
using the `protocol_session` async-context-manager factory fixture from
conftest.py.
"""
import asyncio
import json
from pathlib import Path

# Every tool, with arguments valid enough to reach its handler. Tools that write
# are safe here: the temp MCPBRAIN_HOME is thrown away after the test.
TOOL_CALLS: dict[str, dict] = {
    "brain_search": {"query": "anything"},
    "brain_read": {"doc_id": "missing-doc"},
    "brain_context": {"entity": "Someone"},
    "brain_actions": {"owner": "Someone", "status": "open"},
    "brain_graph": {"entity": "Someone", "hops": 1},
    "brain_proactive": {},
    "brain_finding_resolve": {"finding_id": 1, "outcome": "dismissed"},
    "brain_ingest": {"title": "t", "content": "c"},
    "brain_action_create": {"text": "do a thing"},
    "brain_action_update": {"action_id": 1, "status": "done"},
    "brain_decision": {"text": "decided a thing"},
    "brain_note": {"text": "a note"},
    "brain_memory_write": {"slug": "s", "description": "d", "body": "b"},
    "brain_gardener_apply": {"lane": "context", "filename": "f.md", "content": "x"},
    "brain_draft_context": {"email_id": "nope"},
    "brain_draft_save": {
        "email_id": "e", "thread_id": "t", "intent": "i", "final_draft": "d",
    },
    "brain_routine": {"name": "enrich"},
    "brain_enrich_units": {},
    "brain_enrich_pull": {"unit_id": "nope"},
    "brain_enrich_push": {"unit_id": "nope", "extractions": []},
    "brain_enrich_advance": {},
    "brain_enrich_claim": {},
    "brain_enrich_pending": {},
    "brain_meetings_today": {},
    "brain_meeting_pack_get": {"event_id": "nope"},
    "brain_meeting_pack_upsert": {
        "event_id": "e", "event_title": "t", "event_date": "2026-08-04", "pack_text": "p",
    },
}


def test_every_tool_round_trips_over_stdio(protocol_session):
    """Each tool returns parseable JSON, and none raises a protocol error.

    Handlers are allowed to return an error PAYLOAD (e.g. {"error": ...} for a
    missing doc) -- that is a working tool. What must not happen is an unhandled
    exception surfacing as isError, which is what a broken dispatch layer does.
    """
    async def _body():
        async with protocol_session() as (session, stderr_path):
            listed = {t.name for t in (await session.list_tools()).tools}
            assert listed == set(TOOL_CALLS), (
                f"TOOL_CALLS is out of sync with the server: "
                f"missing={listed - set(TOOL_CALLS)} stale={set(TOOL_CALLS) - listed}"
            )

            failures = []
            for name, args in TOOL_CALLS.items():
                result = await session.call_tool(name, args)
                if result.isError:
                    failures.append((name, [c.text for c in result.content]))
                    continue
                payload = result.content[0].text
                try:
                    json.loads(payload)
                except json.JSONDecodeError as exc:
                    failures.append((name, f"non-JSON payload: {exc}: {payload[:200]}"))
            assert not failures, (
                f"tools failed over the protocol: {failures}\n"
                f"server stderr:\n{Path(stderr_path).read_text()}"
            )
    asyncio.run(_body())


def test_unknown_tool_reports_unknown_tool(protocol_session):
    """A misspelled name must not fall through to brain_search.

    Regression guard: brain_search was the unguarded fallthrough in _call, so any
    unknown name hit arguments["query"] and raised KeyError.
    """
    async def _body():
        async with protocol_session() as (session, stderr_path):
            result = await session.call_tool("brain_nonexistent", {})
            assert result.isError, (
                f"expected an error result for an unknown tool; got {result}\n"
                f"server stderr:\n{Path(stderr_path).read_text()}"
            )
            text = " ".join(c.text for c in result.content).lower()
            assert "unknown tool" in text and "brain_nonexistent" in text, (
                f"expected 'unknown tool ... brain_nonexistent', got: {text}\n"
                f"server stderr:\n{Path(stderr_path).read_text()}"
            )
    asyncio.run(_body())


def test_resources_round_trip_over_stdio(protocol_session):
    """list_resources + read_resource over the protocol, not as plain functions."""
    async def _body():
        async with protocol_session() as (session, stderr_path):
            resources = (await session.list_resources()).resources
            assert resources, (
                f"no resources advertised\nserver stderr:\n{Path(stderr_path).read_text()}"
            )
            first = resources[0]
            assert str(first.uri).startswith("file://")
            contents = (await session.read_resource(first.uri)).contents
            assert contents and contents[0].text is not None
    asyncio.run(_body())


def test_read_resource_rejects_unadvertised_path(protocol_session):
    """The allowlist guard must hold over the protocol too."""
    async def _body():
        async with protocol_session() as (session, _stderr_path):
            raised = False
            try:
                await session.read_resource("file:///etc/passwd")
            except Exception:
                raised = True
            assert raised, "expected read_resource to reject an unadvertised path"
    asyncio.run(_body())
