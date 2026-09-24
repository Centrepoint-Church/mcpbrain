"""mcp 2.x's low-level server validates nothing -- mcpbrain must.

mcp 1.x's call_tool(validate_input=True) was the default and mcp_server.py used
the bare decorator, so all 26 tools got free jsonschema validation against their
inputSchema. Porting to 2.x removes it. These tests pin the replacement.
"""
import pytest

from mcpbrain.mcp_server import _validate_tool_arguments


def test_missing_required_field_is_rejected():
    with pytest.raises(ValueError, match="unit_id"):
        _validate_tool_arguments("brain_enrich_push", {})


def test_wrong_type_is_rejected():
    with pytest.raises(ValueError, match="hops"):
        _validate_tool_arguments("brain_graph", {"entity": "X", "hops": "not-an-int"})


def test_bad_enum_value_is_rejected():
    with pytest.raises(ValueError, match="outcome"):
        _validate_tool_arguments(
            "brain_finding_resolve", {"finding_id": 1, "outcome": "banana"}
        )


def test_valid_arguments_pass():
    _validate_tool_arguments("brain_graph", {"entity": "X", "hops": 2})


def test_absent_optional_field_is_not_defaulted():
    """Validation must not inject defaults.

    brain_enrich_push's guards distinguish an absent `extractions` (None) from a
    present-but-empty one ([]). A validator that fills in defaults would silently
    break the block-unit vs thread-unit dispatch.
    """
    args = {"unit_id": "u1"}
    _validate_tool_arguments("brain_enrich_push", args)
    assert "extractions" not in args, "validation mutated the arguments dict"


def test_empty_list_is_preserved_distinctly():
    args = {"unit_id": "u1", "extractions": []}
    _validate_tool_arguments("brain_enrich_push", args)
    assert args["extractions"] == []


def test_declared_defaults_are_never_written_into_arguments():
    """No schema `default` may leak into the dict the dispatch layer reads.

    Broader than the brain_enrich_push case above: every tool's schema is
    validated with only its required fields present, and the argument dict must
    come back byte-identical. `arguments.get(k, <fallback>)` in the dispatch
    chain is what encodes each tool's real default, so a validator that
    pre-filled them would silently change behaviour across the whole surface.
    """
    from mcpbrain.tool_registry import registry

    for name, s in registry().items():
        schema = s.input_schema
        args = {}
        for field in schema.get("required", []):
            spec = schema["properties"][field]
            args[field] = {"string": "x", "integer": 1, "boolean": True,
                           "array": [], "object": {}}[spec["type"]]
            if spec.get("enum"):
                args[field] = spec["enum"][0]
        before = dict(args)
        _validate_tool_arguments(name, args)
        assert args == before, f"{name}: validation mutated arguments {before} -> {args}"


def test_unknown_tool_is_rejected():
    with pytest.raises(ValueError, match="unknown tool"):
        _validate_tool_arguments("brain_nonexistent", {})


def test_every_tool_has_a_schema_to_validate_against():
    """No tool may silently skip validation for lack of a registered schema."""
    from mcpbrain.tool_registry import registry

    from tests.test_mcp_protocol_surface import TOOL_CALLS

    assert set(registry()) == set(TOOL_CALLS)


def test_every_advertised_tool_has_a_description():
    """on_list_tools reads spec.description for every registered tool, so an
    empty one is a silently undocumented tool rather than a missing docstring.
    `description` is a required @tool argument, so this now asserts the value is
    real -- not merely present."""
    from mcpbrain.tool_registry import registry

    blank = [n for n, s in registry().items() if not s.description.strip()]
    assert not blank, f"registered with an empty description: {blank}"


def test_every_protocol_surface_call_validates_clean():
    """The arguments Task 3's protocol round-trip sends must all be valid.

    Guards against the validator being stricter than the advertised schema --
    that would turn working tools into protocol errors for real callers.
    """
    from tests.test_mcp_protocol_surface import TOOL_CALLS

    for name, args in TOOL_CALLS.items():
        _validate_tool_arguments(name, args)
