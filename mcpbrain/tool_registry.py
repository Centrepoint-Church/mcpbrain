"""Single source of truth for tool metadata, declared beside each handler.

Importable by BOTH the MCP server (which advertises tools/list) and the daemon
(which executes them), so it deliberately imports NOTHING but the stdlib -- no
`mcp`, no Store, nothing native. `tests/test_tool_registry.py` pins that.

Immutability is the one non-obvious property here, and it is deliberate. The
four name-keyed mappings this replaces (`tool_schemas()`, `_TOOL_DESCRIPTIONS`,
`tool_annotations()`, `tool_output_schemas()`) each built a FRESH dict per call,
so a caller that mutated what it got affected nobody -- which is why the 0.7.113
review could judge the shared-by-reference `_queued` output-schema dict harmless
("the aliasing cannot outlive one call"). A registry populated at import shares
every schema object for the life of the process, and an MCP server process lives
as long as its client stays open. So schemas are DEEP-FROZEN at registration:
`_freeze` rebuilds them out of `dict`/`list` subclasses that reject every
mutator, all the way down.

`dict`/`list` subclasses rather than `MappingProxyType`/`tuple` on purpose:
the frozen values stay `isinstance`-compatible with `dict`/`list`, so
`jsonschema.Draft202012Validator.check_schema`, `jsonschema.validate`, pydantic's
`types.Tool` construction and `json.dumps` all keep working unchanged, and
`==` against a plain dict still holds. A `MappingProxyType` fails the JSON Schema
metaschema's own `type: object` check; a `tuple` fails its `type: array`.
"""
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

_REGISTRY: dict[str, "ToolSpec"] = {}

_FROZEN = "tool metadata is frozen; declare it on the tool's @tool(...) instead"


class _FrozenDict(dict):
    """A dict that refuses every mutator. See the module docstring for why this
    is a dict subclass rather than a MappingProxyType."""

    __slots__ = ()

    def _frozen(self, *_args, **_kwargs):
        raise TypeError(_FROZEN)

    __setitem__ = __delitem__ = _frozen
    __ior__ = _frozen
    clear = pop = popitem = setdefault = update = _frozen


class _FrozenList(list):
    """A list that refuses every mutator (kept a list subclass for the same
    isinstance-compatibility reason as _FrozenDict)."""

    __slots__ = ()

    def _frozen(self, *_args, **_kwargs):
        raise TypeError(_FROZEN)

    __setitem__ = __delitem__ = _frozen
    __iadd__ = __imul__ = _frozen
    append = clear = extend = insert = pop = remove = reverse = sort = _frozen


def _freeze(value):
    """Return `value` deep-frozen: mappings and sequences become read-only.

    Recursive, not shallow: the sharing hazard is a mutated INNER dict (a
    `properties` entry, a `required` list), which a top-level guard alone would
    not catch. Scalars are already immutable and pass through.
    """
    if isinstance(value, dict):
        return _FrozenDict((k, _freeze(v)) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return _FrozenList(_freeze(v) for v in value)
    return value


@dataclass(frozen=True)
class ToolSpec:
    """One tool's advertised metadata.

    `output_schema=None` is meaningful, not merely a default: 13 of the 26 tools
    declare no outputSchema on purpose (a markdown carrier gains nothing from
    `structured_content`, which ships alongside `content` and so doubles the
    payload). `None` and `{}` must stay distinguishable.
    """

    description: str
    input_schema: Any          # deep-frozen by __post_init__; see _freeze
    annotations: Any = None    # types.ToolAnnotations, kept untyped to avoid importing mcp
    output_schema: Any = None  # None => this tool declares no outputSchema (meaningful)

    def __post_init__(self):
        # object.__setattr__: the dataclass is frozen, and freezing the schemas
        # HERE rather than in `tool()` means every construction path is covered
        # (tests build ToolSpec directly, and a later daemon-side one might too).
        object.__setattr__(self, "input_schema", _freeze(self.input_schema))
        if self.output_schema is not None:
            object.__setattr__(self, "output_schema", _freeze(self.output_schema))


def declare(name: str, *, description: str, input_schema, annotations,
            output_schema=None) -> ToolSpec:
    """Register a tool's metadata under `name` and return the stored spec.

    The registration primitive. `tool()` is the decorator form for the 24
    `make_brain_*` factories; two tools (brain_read, brain_routine) have no
    factory to hang a decorator on -- they are dispatched inline in
    `on_call_tool` -- so they call this directly, at the point in the module
    where they belong. Registration ORDER is the order tools are advertised in
    tools/list, so where a declaration sits is load-bearing.

    A CONFLICTING second registration under one name raises -- two tools cannot
    claim one name, and a silent overwrite would lose a tool. An IDENTICAL one is
    a no-op, because the registry is populated by module import and a module can
    legitimately be imported twice in one process: `test_mcp_server_no_native.py`
    drops mcpbrain.mcp_server from sys.modules and re-imports it to prove no
    native dependency leaks in, and `unittest.mock.patch("mcpbrain.mcp_server.…")`
    re-imports it too. Making import non-idempotent would turn that into a hard
    error at collection time. The weakened case -- two genuinely different tools
    sharing a name AND a byte-identical description, schema, annotations and
    output schema -- is not distinguishable from the same declaration running
    twice, and is not a thing that can happen by accident.
    """
    candidate = ToolSpec(
        description=description,
        input_schema=input_schema,
        annotations=annotations,
        output_schema=output_schema,
    )
    existing = _REGISTRY.get(name)
    if existing is not None:
        if existing != candidate:
            raise ValueError(f"duplicate tool registration: {name}")
        return existing
    _REGISTRY[name] = candidate
    return candidate


def tool(name: str, *, description: str, input_schema, annotations,
         output_schema=None):
    """Register a tool's metadata and return the factory unchanged.

    Returning the factory untouched is deliberate: the decorator is a
    declaration, not a wrapper, so `make_brain_note()` behaves exactly as it did
    before it was decorated and every existing direct-call test is unaffected.
    """
    def _decorate(factory):
        declare(name, description=description, input_schema=input_schema,
                annotations=annotations, output_schema=output_schema)
        return factory
    return _decorate


def registry():
    """All registered specs, as a read-only mapping in advertised order."""
    return MappingProxyType(_REGISTRY)


def spec(name: str) -> ToolSpec:
    """One tool's spec. Raises KeyError naming the tool if unregistered."""
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(f"no such tool: {name}") from None
