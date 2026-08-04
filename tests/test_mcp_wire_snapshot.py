"""The advertised surface, pinned to what 0.7.113 verified live.

Source: docs/superpowers/specs/2026-08-04-release-verification-record.md.
This is the gate for any change that touches tool metadata or registration.

No pytest-asyncio in this suite (see test_mcp_server_stdio.py): each test is a
plain sync function driving its own event loop with `asyncio.run(...)`.
"""
import asyncio

from mcpbrain.mcp_server import build_server
from tests.conftest import list_tools_via_handler

EXPECTED_TOOL_COUNT = 26
EXPECTED_OUTPUT_SCHEMA_COUNT = 13
OPEN_WORLD = {"brain_meetings_today"}
DESTRUCTIVE = {"brain_gardener_apply", "brain_enrich_advance"}
NO_OUTPUT_SCHEMA_BY_DESIGN = {"brain_routine", "brain_enrich_pull"}


def _tools(mcp_env):
    return asyncio.run(list_tools_via_handler(build_server(**mcp_env)))


def test_tool_count_and_full_annotation_coverage(mcp_env):
    tools = _tools(mcp_env)
    assert len(tools) == EXPECTED_TOOL_COUNT
    unannotated = [t.name for t in tools if t.annotations is None]
    assert not unannotated, f"unannotated: {unannotated}"


def test_output_schema_count_and_deliberate_exclusions(mcp_env):
    tools = _tools(mcp_env)
    declared = {t.name for t in tools if t.output_schema}
    assert len(declared) == EXPECTED_OUTPUT_SCHEMA_COUNT
    assert not (declared & NO_OUTPUT_SCHEMA_BY_DESIGN), (
        "brain_routine/brain_enrich_pull carry markdown; structured_content there "
        "would double the two largest payloads in the surface"
    )


def test_open_world_and_destructive_sets_are_exact(mcp_env):
    tools = _tools(mcp_env)
    assert {t.name for t in tools if t.annotations.open_world_hint} == OPEN_WORLD
    assert {t.name for t in tools if t.annotations.destructive_hint} == DESTRUCTIVE


def test_descriptions_are_non_empty_and_gardener_keeps_its_constraint(mcp_env):
    by_name = {t.name: t for t in _tools(mcp_env)}
    assert all(t.description.strip() for t in by_name.values())
    assert "reference-gardener" in by_name["brain_gardener_apply"].description


def test_enrich_push_schema_still_covers_every_push_block(mcp_env):
    from mcpbrain.enrich_blocks import PUSH_BLOCKS

    schema = {t.name: t for t in _tools(mcp_env)}["brain_enrich_push"].input_schema
    missing = [b for b in PUSH_BLOCKS if b not in schema["properties"]]
    assert not missing, f"generated schema lost blocks: {missing}"
