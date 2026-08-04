"""Structured output for the tools whose result is a machine-read envelope.

Deliberately NOT universal: structured_content ships alongside content, so
declaring it on brain_enrich_pull (~11.5KB of rules) or brain_routine (routine
markdown) would double the two largest payloads for no gain -- their consumer is
an LLM reading prose, not a parser.

No pytest-asyncio in this suite (see test_mcp_server_stdio.py / Task 3's
test_mcp_protocol_surface.py): the wire-level tests below are plain sync
functions that drive their own event loop with `asyncio.run(...)`, opening the
shared `protocol_session` async-context-manager factory fixture from
conftest.py inside the loop.
"""
import asyncio
import json

from mcpbrain.mcp_server import tool_output_schemas, tool_schemas

# Small, stable, machine-consumed envelopes.
STRUCTURED = {
    "brain_ingest", "brain_action_create", "brain_action_update", "brain_decision",
    "brain_note", "brain_memory_write", "brain_gardener_apply", "brain_enrich_push",
    "brain_enrich_advance", "brain_enrich_pending", "brain_finding_resolve",
    "brain_draft_save", "brain_meeting_pack_upsert",
}
# Markdown carriers: JSON-wrapping them is already overhead; don't double it.
DELIBERATELY_UNSTRUCTURED = {"brain_routine", "brain_enrich_pull"}


def test_declared_output_schemas_match_the_intended_set():
    assert set(tool_output_schemas()) == STRUCTURED


def test_markdown_carriers_declare_no_output_schema():
    for name in DELIBERATELY_UNSTRUCTURED:
        assert name not in tool_output_schemas()


def test_output_schemas_are_valid_json_schema():
    import jsonschema

    for name, schema in tool_output_schemas().items():
        jsonschema.Draft202012Validator.check_schema(schema)


def test_every_output_schema_names_a_real_tool():
    assert set(tool_output_schemas()) <= set(tool_schemas())


def test_structured_content_is_returned_and_matches_its_schema(protocol_session):
    """A declared outputSchema must be honoured on the wire, not just advertised."""
    import jsonschema

    async def _body():
        async with protocol_session() as (session, _stderr_path):
            result = await session.call_tool("brain_note", {"text": "structured probe"})
            assert not result.is_error
            assert result.structured_content is not None, "declared outputSchema but sent none"
            jsonschema.validate(result.structured_content, tool_output_schemas()["brain_note"])
            # content must still ship, so clients that ignore structured output are unaffected
            assert json.loads(result.content[0].text) == result.structured_content
    asyncio.run(_body())


def test_unstructured_tool_sends_no_structured_content(protocol_session):
    async def _body():
        async with protocol_session() as (session, _stderr_path):
            result = await session.call_tool("brain_routine", {"name": "enrich"})
            assert not result.is_error
            assert result.structured_content is None
    asyncio.run(_body())
