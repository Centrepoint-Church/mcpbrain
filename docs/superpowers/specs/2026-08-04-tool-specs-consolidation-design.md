# `TOOL_SPECS` — consolidate the four parallel per-tool mappings

**Status:** design approved 2026-08-04, not implemented.

## The problem

`mcpbrain/mcp_server.py` now describes each of its 26 tools across **four separate name-keyed
mappings**, accumulated one per task during the 0.7.113 migration:

| Mapping | Added by | Shape |
|---|---|---|
| `tool_schemas()` | the port + validation | name → `inputSchema` |
| `_TOOL_DESCRIPTIONS` | the port | name → description string |
| `tool_annotations()` | annotations task | name → `types.ToolAnnotations` |
| `tool_output_schemas()` | structured-output task | name → `outputSchema`, **partial** (13 of 26) |

Nothing structurally forces a 27th tool to be added to all four. **Three separate task reviews
independently flagged this as "invites drift"**, and each correctly declined to act because
consolidation was outside its own scope. The final whole-branch review put it in scope, judged it
real maintainability debt, and still recommended **not** doing it inside that merge — because
restructuring all four would invalidate the live verification the release rested on.

It is not a latent production defect today: drift is fenced by set-equality guard tests
(`test_every_advertised_tool_has_a_description`, `test_every_tool_has_a_schema_to_validate_against`,
the 26/26 annotation test, and the partiality assertion on output schemas). A tool added to one
mapping fails tests before it reaches users. So this is friction and future-risk, not a live bug —
which is why it is a deliberate follow-up rather than a hotfix.

## Design — colocated declarations, not a central mapping

**A single `TOOL_SPECS` dict was considered and rejected as the end state.** It fixes "four
mappings → one" but leaves a tool's metadata ~1000 lines from the handler it describes:
`make_brain_graph` sits around line 200, its schema around line 1300. Consolidating the
*separation* is not the same as removing it.

Instead each tool declares itself **next to its implementation**, and the registry is assembled
from those declarations:

```python
@dataclass(frozen=True)
class ToolSpec:
    description: str
    input_schema: dict
    annotations: "types.ToolAnnotations"
    output_schema: dict | None = None   # None => tool declares no outputSchema


@tool(
    "brain_graph",
    description="Traverse the relationship graph …",   # verbatim from today's value
    input_schema={...},
    annotations=_ro("Traverse the graph"),
)
def make_brain_graph(store):
    async def brain_graph(entity, hops=1, *, at_time=None, on_hop=None): ...
    return brain_graph
```

The decorator registers the spec and returns the factory unchanged, so nothing about how factories
are called changes. Reading one screen tells you a tool's description, schema, safety annotations,
output schema **and** behaviour.

Why this is the better end state:
- **Omission becomes structurally impossible.** A new tool cannot exist without its metadata,
  because the decorator supplies it — versus a central dict where a 27th entry must be *remembered*
  in a second location (which is the current defect, merely reduced from four places to one).
- `output_schema` keeps a `None` default because its absence is meaningful and tested
  (`brain_routine` and `brain_enrich_pull` carry markdown; shipping `structured_content` there
  would double the two largest payloads in the surface for a consumer that wants prose).
- It is the seam the **thin-adapter** work needs (see
  `2026-08-04-mcp-server-process-lifecycle.md`): once tool definition and execution are declared
  together in one module, the daemon can execute from the registry while the MCP server reads it
  for `tools/list` — or fetches the list from the daemon and holds no tool knowledge at all.
  **This project should land first for that reason**, independently of its own merits.

Cost, stated plainly: the decorator touches all 20 `make_brain_*` factories. The work is
mechanical, but it is 20 edits rather than one new function, and the diff will look large for a
change that must provably alter no behaviour — hence the gate below.

**Three constraints shape the implementation, and each is easy to get wrong:**

1. **`brain_enrich_push`'s `inputSchema` is generated**, not literal — `push_input_schema()`
   builds it from `_PUSH_BLOCKS` so a new block cannot be forgotten. A decorator argument is
   evaluated **at import**, which is acceptable here only because `push_input_schema()` is pure
   and depends on nothing but a module-level tuple — verify that still holds before relying on it.
   If it ever gains a dependency on config or the filesystem, the decorator must accept a
   **callable** for `input_schema` and resolve it lazily on first read rather than at import.
   Either way the schema is built **once**, which is itself part of the point: the four current
   accessors are rebuilt on *every* `list_tools` and `call_tool`, roughly 30 dict literals per
   tool invocation.
2. **`mcp_server.py` must stay free of native/heavy imports** — `tests/test_mcp_server_no_native.py`
   AST-scans it and asserts no `fastembed`/`onnxruntime` loads on import. A `dataclass` and
   `functools` are fine; do not let the record's type annotations pull anything new in at module
   scope.

3. **A module-scope registry removes an accidental safety property — restore it deliberately.**
   The four current accessors each build and return a **fresh dict per call**, and that is exactly
   why the 0.7.113 review judged the shared-by-reference `_queued` output-schema dict harmless:
   *"the aliasing cannot outlive one call."* A registry populated at import shares every schema
   dict globally and permanently, so an in-place mutation anywhere would silently corrupt what the
   server advertises for the rest of the process's life — and this process is long-lived (see the
   sibling lifecycle spec: an MCP server runs for as long as its client stays open).

   Resolve it explicitly rather than relying on nobody mutating:
   - `ToolSpec` is `frozen=True`, so the *record* cannot be rebound.
   - `frozen` does **not** freeze the nested `dict`s, so also assert the invariant with a test:
     obtain a schema from `TOOL_SPECS`, mutate the returned object, and prove a second read is
     unaffected — whichever way you achieve that (returning copies at the boundary, or
     `MappingProxyType`, or a deep-freeze helper). Pick one mechanism and apply it uniformly;
     don't protect `input_schema` and leave `output_schema` exposed.
   - Verified non-issue today: `jsonschema.validate` does not mutate the schema it is given, and
     `_validate_tool_arguments` is required never to mutate the *arguments* dict either. The risk
     is a future edit, which is what the test is for.

**Delete the four accessors rather than keeping adapters.** Leaving `tool_schemas()` as a shim
over `TOOL_SPECS` would preserve two ways to read the same data, which is precisely the defect
being removed. Update the ~6 call sites (`on_list_tools`, `_validate_tool_arguments`,
`on_call_tool`'s structured-content gate) and the tests that import them.

## The gate

This refactor's risk is that it silently changes what the server advertises. The 0.7.113 release
recorded an exact wire snapshot, so the gate is a **diff against known-good numbers** rather than
a vague "still works":

- 26 tools, **26/26** carrying annotations
- **13** declaring `outputSchema`; `brain_routine` and `brain_enrich_pull` among those that do not
- `open_world_hint` true for **exactly** `brain_meetings_today`
- `destructive_hint` true for **exactly** `brain_gardener_apply` and `brain_enrich_advance`
- all 26 tool descriptions **byte-identical** to their current values
- `brain_enrich_push`'s advertised schema still contains one property per `_PUSH_BLOCKS` entry
- mutating a schema read out of `TOOL_SPECS` does not affect a subsequent read (constraint 3)

Plus the existing protocol round-trip over real stdio (all 26 tools + both resource handlers),
and a re-run of the live handshake against the installed wheel before any release that carries
this. Source: `docs/superpowers/specs/2026-08-04-release-verification-record.md`.

## Explicitly out of scope

- **Splitting `mcp_server.py`** (~2050 lines). If it is ever split, the only seam worth trusting
  is `prompts` + `resources` → `mcp_resources.py` (`_resource_entries`, `read_context_resource`,
  `_resource_fingerprint`, `watch_resources`, `prompt_definitions`, `get_prompt_body`,
  `_draft_reply_*` — ~400 lines with no coupling to tool dispatch beyond `_ROUTINES`). Splitting
  along the tool boundary would fight this consolidation and would divide the one function whose
  single-return discipline is a stated safety property. Note `test_mcp_server_no_native.py` scans
  one filename and would need updating.
- **Normalising the inconsistent success keys** (`queued`/`written`/`ok`/`applied`/`resolved`
  across the envelope tools). Real API smell, surfaced while declaring output schemas, but
  renaming them is a breaking change to every caller and belongs in its own change.
- **Changing any tool's behaviour, schema, description, or annotation.** This is a pure data-layout
  refactor; the gate above exists to prove exactly that.
