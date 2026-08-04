"""Registry semantics, and the immutability invariant a module-scope registry needs.

Why immutability matters here: the four mappings this replaces each built a FRESH
dict per call, which is exactly why the 0.7.113 review judged the shared-by-
reference _queued output-schema dict harmless ("the aliasing cannot outlive one
call"). A registry populated at import shares every schema dict for the life of
the process -- and an MCP server process lives as long as its client stays open.
"""
import pytest

from mcpbrain import tool_registry
from mcpbrain.tool_registry import ToolSpec, registry, spec, tool


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch):
    """Register this module's probes into a THROWAWAY copy of the registry.

    The registry is a process-wide singleton populated at import, and
    on_list_tools now advertises everything in it -- so a probe registered by a
    test would become an advertised tool for the rest of the session (that is
    exactly what test_every_advertised_tool_is_registered caught). Swapping the
    dict for a copy keeps probes out of the real one, and restores it after.
    """
    monkeypatch.setattr(tool_registry, "_REGISTRY", dict(tool_registry._REGISTRY))


@pytest.fixture
def probe():
    """The shared `t_probe` registration, per test.

    NOT autouse and NOT module-level state: the suite runs `-n auto` with
    xdist's default `--dist load`, which hands individual tests to different
    worker processes, so a probe registered by one test is simply absent in the
    process that runs the next. Requesting it explicitly makes each test below
    independent of which worker it lands on -- and keeps it out of the two tests
    that assert on the real advertised surface.
    """
    return tool_registry.declare("t_probe", description="d",
                                 input_schema={"type": "object"}, annotations=None)


def test_decorator_returns_the_factory_unchanged():
    marker = object()

    @tool("t_probe", description="d", input_schema={"type": "object"},
          annotations=None)
    def make_probe():
        return marker

    assert make_probe() is marker


def test_decorator_registers_the_spec(probe):
    assert spec("t_probe").description == "d"


def test_duplicate_registration_is_rejected(probe):
    """Two tools cannot claim one name -- silent overwrite would lose a tool."""
    with pytest.raises(ValueError, match="t_probe"):
        @tool("t_probe", description="x", input_schema={}, annotations=None)
        def make_dupe():
            return None


def test_identical_re_registration_is_a_no_op(probe):
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


def test_spec_is_frozen(probe):
    with pytest.raises(Exception):
        spec("t_probe").description = "mutated"


def test_mutating_a_schema_read_from_the_registry_cannot_affect_a_later_read(probe):
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


def test_output_schema_defaults_to_none_and_absence_is_distinguishable(probe):
    assert spec("t_probe").output_schema is None


def test_the_four_legacy_mappings_are_gone():
    """One source of truth means the old accessors must not linger as aliases."""
    from mcpbrain import mcp_server as ms

    for gone in ("tool_schemas", "_TOOL_DESCRIPTIONS",
                 "tool_annotations", "tool_output_schemas"):
        assert not hasattr(ms, gone), f"{gone} still exists — two ways to read one thing"


def test_every_advertised_tool_is_registered(mcp_env):
    """What tools/list advertises and what the registry holds are the same set.

    Exact equality, both directions: nothing may be advertised without a spec
    (that is what _validate_tool_arguments enforces against), and nothing may sit
    in the registry unadvertised. This is the test that caught probe leakage into
    the advertised surface, which _isolated_registry now prevents.
    """
    import asyncio

    from mcpbrain.mcp_server import build_server
    from tests.conftest import list_tools_via_handler

    tools = asyncio.run(list_tools_via_handler(build_server(**mcp_env)))
    assert {t.name for t in tools} == set(registry())


def test_advertised_order_is_registration_order(mcp_env):
    """Registration order IS tools/list order; the model sees this sequence.

    Pinned because the declarations now live next to 24 scattered factories
    instead of in one ordered literal, so a moved function silently reorders the
    advertised surface.
    """
    import asyncio

    from mcpbrain.mcp_server import build_server
    from tests.conftest import list_tools_via_handler

    tools = asyncio.run(list_tools_via_handler(build_server(**mcp_env)))
    assert [t.name for t in tools] == list(registry())


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


def test_tools_module_imports_nothing_heavy():
    """The DECLARATION half of the seam must stay mcp-free too, source-level.

    tool_registry being stdlib-only was necessary but not sufficient: every
    @tool(...) in mcpbrain/tools.py evaluates at import, so while the factories
    lived beside the Server construction, reading the registry meant importing
    the protocol stack. Same AST guard as above, one module further out --
    function-body imports included, since these factories import their heavy
    dependencies lazily and one hoisted to module scope would undo the split.
    """
    import ast
    from pathlib import Path

    import mcpbrain.tools as mt

    tree = ast.parse(Path(mt.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    forbidden = imported & {"mcp", "fastembed", "onnxruntime", "jsonschema",
                            "sqlite_vec", "numpy"}
    assert not forbidden, f"tools.py imports {forbidden}"


def test_importing_tools_does_not_pull_the_mcp_protocol_stack():
    """The measurement that matters, in a clean interpreter.

    The AST guard above only sees DIRECT imports; this catches a transitive one
    (a new `from mcpbrain.<x> import ...` whose target imports mcp). Subprocess,
    not this process: pytest has already imported mcp via other test modules, so
    an in-process sys.modules check would pass vacuously.
    """
    import json
    import subprocess
    import sys

    probe = (
        "import sys, time;"
        "t=time.perf_counter();"
        "import mcpbrain.tools;"
        "dt=time.perf_counter()-t;"
        "import json;"
        "print(json.dumps({'mcp': [m for m in sys.modules if m.split('.')[0] in "
        "('mcp','pydantic','starlette','uvicorn')][:5],"
        "'n': len(sys.modules), 'ms': dt*1000}))"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                         text=True, check=True)
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got["mcp"] == [], f"mcpbrain.tools pulled the protocol stack: {got['mcp']}"
    # Generous ceiling (measured ~142): this asserts "no heavy stack crept in",
    # not a performance budget, so it must not fail on a slower box or a new
    # small stdlib dependency. mcp alone takes it to ~600.
    assert got["n"] < 300, f"import graph ballooned to {got['n']} modules"


def test_registry_stores_stdlib_annotations_not_sdk_models():
    """The VALUE, not just the type hint, must be mcp-free.

    This is the whole seam. A `mcp.types.ToolAnnotations` instance stored here
    would drag mcp + pydantic + starlette + uvicorn into every importer of the
    registry (601 modules vs 142) no matter how the field is annotated -- and
    would pin the 24 factories' @tool declarations to a module that can import
    mcp at load time.
    """
    from mcpbrain import mcp_server  # noqa: F401 - import populates the registry
    from mcpbrain.tool_registry import ToolAnnotations

    wrong = {n: type(s.annotations).__module__ for n, s in registry().items()
             if not isinstance(s.annotations, ToolAnnotations)}
    assert not wrong, f"annotations are not the stdlib type: {wrong}"


def test_sdk_annotation_conversion_is_wire_exact():
    """Every advertised tool's annotations serialise exactly as the SDK's own.

    Field-by-field, then the camelCase payload `model_dump(by_alias=True)`
    actually puts on the wire: same five keys, same values, nothing dropped and
    nothing invented. This is what makes storing a stdlib value instead of the
    SDK model a pure representation change.
    """
    import dataclasses

    from mcpbrain import mcp_server
    from mcpbrain.tool_registry import ToolAnnotations

    assert [f.name for f in dataclasses.fields(ToolAnnotations)] == [
        "title", "read_only_hint", "destructive_hint", "idempotent_hint",
        "open_world_hint",
    ], "field set/order drifted from mcp.types.ToolAnnotations"

    for name, s in registry().items():
        ours = dataclasses.asdict(s.annotations)
        sdk = mcp_server._sdk_annotations(s.annotations)
        assert {f: getattr(sdk, f) for f in ours} == ours, name
        assert sdk.model_dump(by_alias=True) == {
            "title": s.annotations.title,
            "readOnlyHint": s.annotations.read_only_hint,
            "destructiveHint": s.annotations.destructive_hint,
            "idempotentHint": s.annotations.idempotent_hint,
            "openWorldHint": s.annotations.open_world_hint,
        }, name


def test_sdk_annotation_conversion_passes_none_through():
    """An unannotated spec must still advertise as annotations=None, not crash."""
    from mcpbrain import mcp_server

    assert mcp_server._sdk_annotations(None) is None


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
