"""Registry semantics, and the immutability invariant a module-scope registry needs.

Why immutability matters here: the four mappings this replaces each built a FRESH
dict per call, which is exactly why the 0.7.113 review judged the shared-by-
reference _queued output-schema dict harmless ("the aliasing cannot outlive one
call"). A registry populated at import shares every schema dict for the life of
the process -- and an MCP server process lives as long as its client stays open.
"""
import pytest

from mcpbrain.tool_registry import ToolSpec, registry, spec, tool


@pytest.fixture(autouse=True)
def _shared_probe():
    """Guarantee the shared `t_probe` registration in EVERY worker process.

    The suite runs `-n auto` with xdist's default `--dist load`, which hands
    individual tests to different worker processes -- so a probe registered by
    one test is simply absent in the process that runs the next one. Registering
    it here (idempotently, once per process) makes each test below independent of
    which worker it lands on, instead of silently depending on same-process
    top-down ordering.
    """
    if "t_probe" not in registry():
        tool("t_probe", description="d", input_schema={"type": "object"},
             annotations=None)(lambda: None)


def test_decorator_returns_the_factory_unchanged():
    marker = object()

    @tool("t_returns_factory", description="d", input_schema={"type": "object"},
          annotations=None)
    def make_probe():
        return marker

    assert make_probe() is marker


def test_decorator_registers_the_spec():
    assert spec("t_probe").description == "d"


def test_duplicate_registration_is_rejected():
    """Two tools cannot claim one name -- silent overwrite would lose a tool."""
    with pytest.raises(ValueError, match="t_probe"):
        @tool("t_probe", description="x", input_schema={}, annotations=None)
        def make_dupe():
            return None


def test_identical_re_registration_is_a_no_op():
    """A module can be imported twice in one process; that must not be fatal.

    test_mcp_server_no_native.py drops mcpbrain.mcp_server from sys.modules and
    re-imports it, and mock.patch("mcpbrain.mcp_server.…") re-imports it too --
    both re-run every module-level @tool declaration. Only a CONFLICTING
    re-registration is an error (see the test above).
    """
    before = spec("t_probe")
    tool("t_probe", description="d", input_schema={"type": "object"},
         annotations=None)(lambda: None)
    assert spec("t_probe") is before


def test_reimporting_mcp_server_does_not_raise_on_its_declarations():
    """The real case the no-op rule exists for, exercised end to end."""
    import importlib
    import sys

    sys.modules.pop("mcpbrain.mcp_server", None)
    importlib.import_module("mcpbrain.mcp_server")  # must not raise
    assert "brain_read" in registry()


def test_unknown_name_raises_with_the_name_in_the_message():
    with pytest.raises(KeyError, match="t_nonexistent"):
        spec("t_nonexistent")


def test_spec_is_frozen():
    with pytest.raises(Exception):
        spec("t_probe").description = "mutated"


def test_mutating_a_schema_read_from_the_registry_cannot_affect_a_later_read():
    """The invariant the four fresh-dict accessors provided by accident."""
    got = spec("t_probe").input_schema
    try:
        got["injected"] = True
    except Exception:
        pass                      # a read-only mapping is an acceptable mechanism
    assert "injected" not in spec("t_probe").input_schema


def test_registry_is_not_directly_mutable():
    with pytest.raises(Exception):
        registry()["t_smuggled"] = None


def test_output_schema_defaults_to_none_and_absence_is_distinguishable():
    assert spec("t_probe").output_schema is None


@pytest.mark.parametrize("name", ["brain_read", "brain_note", "brain_enrich_push"])
def test_decorated_slice_matches_the_legacy_mappings(name):
    """The decorator must reproduce the four mappings bit-for-bit for these three."""
    from mcpbrain import mcp_server as ms

    s = spec(name)
    assert s.description == ms._TOOL_DESCRIPTIONS[name]
    assert dict(s.input_schema) == ms.tool_schemas()[name]
    assert s.annotations == ms.tool_annotations()[name]
    assert (dict(s.output_schema) if s.output_schema else None) == \
           ms.tool_output_schemas().get(name)


def test_tool_registry_imports_nothing_heavy():
    """The daemon will import this module; it must stay MCP-free and native-free.

    A registry that reaches for `mcp`, the Store, or the embedder could not be
    imported by the executor side of the seam this phase exists to create.
    """
    import ast
    from pathlib import Path

    import mcpbrain.tool_registry as tr

    tree = ast.parse(Path(tr.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    forbidden = imported & {"mcp", "mcpbrain", "fastembed", "onnxruntime",
                            "jsonschema", "sqlite_vec", "numpy"}
    assert not forbidden, f"tool_registry.py imports {forbidden}"


def test_deep_mutation_of_a_read_schema_cannot_affect_a_later_read():
    """Nested, not just top-level: the sharing hazard is a mutated inner dict.

    `_queued()` was a factory precisely so six entries could not alias one
    object; an import-populated registry hands out the same nested objects for
    the process lifetime, so the freeze has to reach all the way down.
    """
    s = ToolSpec(description="d",
                 input_schema={"properties": {"a": {"type": "string"}},
                               "required": ["a"]},
                 annotations=None)
    for mutate in (lambda: s.input_schema["properties"].__setitem__("b", {}),
                   lambda: s.input_schema["properties"]["a"].__setitem__("type", "x"),
                   lambda: s.input_schema["required"].append("b")):
        with pytest.raises(TypeError):
            mutate()
    assert s.input_schema == {"properties": {"a": {"type": "string"}},
                              "required": ["a"]}
