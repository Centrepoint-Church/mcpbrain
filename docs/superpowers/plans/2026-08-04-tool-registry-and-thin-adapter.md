# Tool Registry + Thin Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Colocate each tool's metadata with its implementation, then move Store-touching tool execution into the daemon so the MCP server becomes a thin protocol adapter — making stale MCP server code stop mattering and removing the multiple-writable-`Store`-handle class by construction.

**Architecture:** Two phases with a dependency. **(1)** A `@tool(...)` decorator on each of the 24 `make_brain_*` factories declares description / input schema / annotations / output schema beside the handler, replacing four parallel name-keyed mappings and creating a single registry both processes can read. **(2)** The 10 Store-touching tools execute in the daemon behind a new `POST /api/tool` endpoint, gated by a `fleet_flag` defaulting **ON** with the local kill-switch as the fallback path. The six filesystem-only capture tools stay in the MCP server because they work with the daemon down today. Latency is a hard pre-release gate.

**Tech Stack:** Python 3.12, `mcp` 2.x low-level `Server`, `ThreadingHTTPServer` control API, `fleet_flag` config precedence, pytest (no `pytest-asyncio`).

**Specs:** `docs/superpowers/specs/2026-08-04-tool-specs-consolidation-design.md`, `docs/superpowers/specs/2026-08-04-mcp-server-process-lifecycle.md`

## Global Constraints

- **NO `pytest-asyncio` in this repo.** `@pytest.mark.asyncio` is an unknown mark — the async body is never awaited and the test **passes while asserting nothing**. Use plain sync tests driving `asyncio.run(...)`. Prove it with `-W error::RuntimeWarning`.
- **Shared test machinery lives in `tests/conftest.py`**: `mcp_env`, `protocol_session` (an `@asynccontextmanager` **factory**, optional `message_handler`), `protocol_session_with_progress`, `list_tools_via_handler`. Use them; do not duplicate or relocate.
- **`brain_enrich_push`'s `inputSchema` is GENERATED** from `_PUSH_BLOCKS` via `push_input_schema()`, and its handler absorbs keys via `**blocks`. Never replace with type-hint inference.
- **The `None` vs `[]` distinction must survive.** `extractions` stays `None` when absent (four guards depend on it). `_validate_tool_arguments` must never inject defaults or mutate the arguments dict.
- **`on_call_tool` invariant:** exactly 3 `return`s (validation early-return, `brain_actions` early-return, one final) and exactly 1 `except ValueError`, wrapping **only** the `_validate_tool_arguments` call. A blanket catch around the dispatch would dress a real handler bug as a tidy error result.
- **Validation failures return `isError`, not a raised exception** (a bare `ValueError` gets `code=0` plus a ~20-line traceback into the fleet's MCP log). Ruled 2026-08-04.
- **`mcpbrain/mcp_server.py` must stay free of native/heavy imports.** `tests/test_mcp_server_no_native.py` AST-scans it for `get_embedder`/`hybrid_search` and asserts no `fastembed`/`onnxruntime` loads on import; `embedder_dim` stays function-local in `main()`.
- **All 26 tool descriptions preserved byte-identical.** They are the documentation the model reads; `brain_gardener_apply`'s carries a real usage constraint.
- **Feature flag defaults ON**, via `config.fleet_flag(home, "tool_exec_in_daemon", default=True)`. Rationale: `schema_grounding` and `write_time_dedup` have sat OFF since they shipped because nothing exercised them. Default-ON makes the flag a **kill switch**, not an opt-in — and therefore the latency gate (Task 11) is a **hard pre-release gate**, since there is no opt-in soak period.
- **This working tree is SHARED with concurrent sessions.** Stage only your own named files by explicit path — never `git add -A`/`git add .`/`git commit -a`. Scope review diffs to your own commit's parent.
- **No version bumps. No pushes.** Releasing is a separate explicit decision.
- Commit trailers:
  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_015Fvt8hiXRVRmWvykBqqpyb
  ```

## The wire snapshot — the gate everything is measured against

0.7.113 recorded this from the **installed** wheel against real Claude Desktop. Any task that could change what the server advertises must re-assert it verbatim. Source: `docs/superpowers/specs/2026-08-04-release-verification-record.md`.

```
capabilities: {"prompts":{"listChanged":false},
               "resources":{"listChanged":true,"subscribe":false}, "tools":{"listChanged":false}}
serverInfo:   {"name":"mcpbrain","version":"<current>"}
protocol:     2025-11-25
prompts:      enrich, meeting-packs, gardener, reference-gardener, draft-reply
tools:        26 | annotated 26/26 | outputSchema 13
openWorld:    ['brain_meetings_today']
destructive:  ['brain_gardener_apply','brain_enrich_advance']
stderr:       0 bytes
```

## Tool inventory — which tools move, and which do not

**Stay in the MCP server (filesystem-only; work with the daemon DOWN today** because `capture.write_capture` only writes a JSON envelope into `capture_inbox/`**):**
`brain_ingest`, `brain_action_create`, `brain_action_update`, `brain_decision`, `brain_note`, `brain_memory_write`

**Also stay (no Store handle):** `brain_search` (already delegates via `ControlClient`), `brain_routine`, `brain_enrich_units`, `brain_enrich_pull`, `brain_enrich_push`, `brain_enrich_advance`, `brain_enrich_pending`, `brain_enrich_claim`

**Move behind `/api/tool` (hold a `Store` handle today):**
`brain_read`, `brain_context`, `brain_actions`, `brain_graph`, `brain_proactive`, `brain_finding_resolve`, `brain_draft_context`, `brain_draft_save`, `brain_meetings_today`, `brain_meeting_pack_get`, `brain_meeting_pack_upsert`, `brain_gardener_apply`

> **Verify this list against the code before relying on it** (Task 6 Step 1). It was derived from which `make_brain_*` factories take a `store`/`draft_store` argument at `mcpbrain/mcp_server.py:220-1150`. `brain_gardener_apply` takes `store` only for `_verify_role_attribution`; `brain_meetings_today` takes both `store` and `home`. If a tool's real Store usage differs from this list, **report it and stop** — the whole design rests on this seam.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `mcpbrain/tool_registry.py` | **New.** `ToolSpec`, the `@tool` decorator, the registry, and the immutability guard. No MCP or Store imports — importable by both the MCP server and the daemon. | Create |
| `mcpbrain/mcp_server.py` | Protocol surface. Gains `@tool` decorations; loses the four mappings; loses unconditional `Store` construction. | Modify |
| `mcpbrain/control_api.py` | Gains `POST /api/tool`. | Modify |
| `mcpbrain/control_client.py` | Gains `call_tool(name, arguments)`. | Modify |
| `mcpbrain/daemon.py` | Executes registry handlers for the endpoint. | Modify |
| `mcpbrain/doctor.py` | Gains the version-drift check. | Modify |
| `tests/test_tool_registry.py` | **New.** Registry semantics + immutability. | Create |
| `tests/test_mcp_wire_snapshot.py` | **New.** The snapshot gate above, as assertions. | Create |
| `tests/test_tool_exec_routing.py` | **New.** Flag on/off routing, daemon-down behaviour. | Create |
| `tests/test_doctor_version_drift.py` | **New.** Drift check with faked records. | Create |

---

## Phase 1 — Colocated tool registry

### Task 1: `ToolSpec`, the `@tool` decorator, and the immutability guard

Build the mechanism and prove it on a **vertical slice of three tools** before touching 24. The old accessors keep working alongside, so nothing breaks mid-phase.

**Files:**
- Create: `mcpbrain/tool_registry.py`, `tests/test_tool_registry.py`
- Modify: `mcpbrain/mcp_server.py` (decorate 3 factories only)

**Interfaces:**
- Produces: `ToolSpec` (frozen dataclass: `description: str`, `input_schema: dict`, `annotations`, `output_schema: dict | None = None`); `tool(name, *, description, input_schema, annotations, output_schema=None)` decorator returning the factory unchanged; `registry() -> Mapping[str, ToolSpec]`; `spec(name) -> ToolSpec`.

- [ ] **Step 1: Write the failing test**

```python
"""Registry semantics, and the immutability invariant a module-scope registry needs.

Why immutability matters here: the four mappings this replaces each built a FRESH
dict per call, which is exactly why the 0.7.113 review judged the shared-by-
reference _queued output-schema dict harmless ("the aliasing cannot outlive one
call"). A registry populated at import shares every schema dict for the life of
the process -- and an MCP server process lives as long as its client stays open.
"""
import pytest

from mcpbrain.tool_registry import ToolSpec, registry, spec, tool


def test_decorator_returns_the_factory_unchanged():
    marker = object()

    @tool("t_probe", description="d", input_schema={"type": "object"},
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_tool_registry.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mcpbrain.tool_registry'`

- [ ] **Step 3: Implement the registry**

Pick **one** immutability mechanism and apply it uniformly to `input_schema` *and* `output_schema` — do not protect one and leave the other exposed. `MappingProxyType` is the lightest option that also makes `registry()` non-mutable; a deep-freeze helper or returning copies are both acceptable if applied consistently. Whatever you choose, the tests above must pass unmodified.

```python
"""Single source of truth for tool metadata, declared beside each handler.

Importable by BOTH the MCP server (which advertises tools/list) and the daemon
(which executes them), so it must not import mcp, the Store, or anything native.
"""
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

_REGISTRY: dict[str, "ToolSpec"] = {}


@dataclass(frozen=True)
class ToolSpec:
    description: str
    input_schema: Any          # a read-only mapping; see the module's guard
    annotations: Any = None    # types.ToolAnnotations, kept untyped to avoid importing mcp
    output_schema: Any = None  # None => this tool declares no outputSchema (meaningful)


def tool(name, *, description, input_schema, annotations, output_schema=None):
    """Register a tool's metadata and return the factory unchanged."""
    def _decorate(factory):
        if name in _REGISTRY:
            raise ValueError(f"duplicate tool registration: {name}")
        _REGISTRY[name] = ToolSpec(
            description=description,
            input_schema=...,      # frozen per the chosen mechanism
            annotations=annotations,
            output_schema=...,     # same mechanism, or None
        )
        return factory
    return _decorate


def registry():
    """All registered specs, as a read-only mapping."""
    return MappingProxyType(_REGISTRY)


def spec(name):
    """One tool's spec. Raises KeyError naming the tool if unregistered."""
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(f"no such tool: {name}") from None
```

- [ ] **Step 4: Decorate three tools as a vertical slice**

Pick `brain_read` (trivial), `brain_note` (a capture tool that stays local) and **`brain_enrich_push`** (the generated-schema case — the one most likely to break). Move each one's description, schema, annotations and output schema **verbatim** from the four existing mappings onto the decorator. Leave the four mappings in place and unchanged; this step only proves the pattern.

For `brain_enrich_push`, `input_schema=push_input_schema()` is evaluated **at import**. Verify `push_input_schema()` is pure (depends only on the module-level `_PUSH_BLOCKS` tuple) before relying on that; if it touches config or the filesystem, make the decorator accept a callable and resolve lazily instead, and say so in your report.

- [ ] **Step 5: Prove the slice matches the old mappings exactly**

Add to `tests/test_tool_registry.py`:

```python
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
```

- [ ] **Step 6: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_tool_registry.py tests/test_mcp_build_server.py tests/test_mcp_tool_annotations.py tests/test_mcp_structured_output.py -v`
Expected: all pass. `brain_enrich_push`'s equality proves the generated schema survived.

- [ ] **Step 7: Commit**

```bash
git add mcpbrain/tool_registry.py tests/test_tool_registry.py mcpbrain/mcp_server.py
git commit -m "feat(tools): colocated tool registry, proven on a 3-tool slice

Declares a tool's metadata beside its handler instead of in four parallel
name-keyed mappings ~1000 lines away. Registry is import-populated, so it also
restores by design the immutability the four fresh-dict accessors provided by
accident. Slice covers the trivial case, a stay-local capture tool, and
brain_enrich_push's GENERATED schema; the legacy mappings are untouched and an
equality test pins the three against them."
```

---

### Task 2: Migrate the remaining 21 tools and delete the four mappings

**Files:**
- Modify: `mcpbrain/mcp_server.py`, `tests/test_mcp_tool_annotations.py`, `tests/test_mcp_structured_output.py`, `tests/test_mcp_build_server.py`, `tests/test_mcp_input_validation.py`

**Interfaces:**
- Consumes: `tool`, `registry`, `spec` from Task 1
- Produces: `tool_schemas()`, `_TOOL_DESCRIPTIONS`, `tool_annotations()`, `tool_output_schemas()` **removed**; `on_list_tools` and `_validate_tool_arguments` read `registry()`.

- [ ] **Step 1: Write the failing test**

```python
def test_the_four_legacy_mappings_are_gone():
    """One source of truth means the old accessors must not linger as aliases."""
    from mcpbrain import mcp_server as ms

    for gone in ("tool_schemas", "_TOOL_DESCRIPTIONS",
                 "tool_annotations", "tool_output_schemas"):
        assert not hasattr(ms, gone), f"{gone} still exists — two ways to read one thing"


def test_every_advertised_tool_is_registered(mcp_env):
    from mcpbrain.mcp_server import build_server
    from mcpbrain.tool_registry import registry
    from tests.conftest import list_tools_via_handler
    import asyncio

    tools = asyncio.run(list_tools_via_handler(build_server(**mcp_env)))
    assert {t.name for t in tools} == set(registry())
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_tool_registry.py -k "legacy_mappings or every_advertised" -v`
Expected: FAIL — the four attributes still exist.

- [ ] **Step 3: Migrate the remaining 21, then delete the mappings**

Move each tool's four values onto its `@tool(...)` decorator **verbatim**. Then rewrite the two consumers to read the registry:

```python
    async def on_list_tools(ctx, params) -> types.ListToolsResult:
        # Registry is the single source: what we advertise and what
        # _validate_tool_arguments enforces cannot drift, because they are the
        # same object.
        return types.ListToolsResult(tools=[
            types.Tool(
                name=name,
                description=s.description,
                inputSchema=dict(s.input_schema),
                annotations=s.annotations,
                **({"outputSchema": dict(s.output_schema)} if s.output_schema else {}),
            )
            for name, s in registry().items()
        ])
```

and in `_validate_tool_arguments`, replace the `tool_schemas()` lookup with `spec(name).input_schema`, keeping the `unknown tool: {name}` message and the no-mutation guarantee.

- [ ] **Step 4: Run the full MCP set**

Run:
```bash
.venv/bin/python -m pytest $(ls tests/test_mcp_*.py | tr '\n' ' ') tests/test_tool_registry.py \
  -q -W error::RuntimeWarning
.venv/bin/ruff check mcpbrain/ tests/
```
Expected: all pass, ruff clean on changed files. `tests/test_mcp_protocol_surface.py`'s 26-tool round-trip is the guard that no tool was dropped in the move.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/mcp_server.py tests/test_tool_registry.py tests/test_mcp_tool_annotations.py \
        tests/test_mcp_structured_output.py tests/test_mcp_build_server.py tests/test_mcp_input_validation.py
git commit -m "refactor(tools): all 24 factories declare their own metadata; drop the four mappings

Deletes tool_schemas/_TOOL_DESCRIPTIONS/tool_annotations/tool_output_schemas
rather than leaving adapters — two ways to read one thing is the defect being
removed. A 27th tool can no longer be added without its metadata, because the
decorator supplies it."
```

---

### Task 3: Pin the wire snapshot

The refactor's whole risk is silently changing what the server advertises. Turn the 0.7.113 recorded surface into assertions so that risk is a test failure rather than a review question.

**Files:**
- Create: `tests/test_mcp_wire_snapshot.py`

- [ ] **Step 1: Write the test**

```python
"""The advertised surface, pinned to what 0.7.113 verified live.

Source: docs/superpowers/specs/2026-08-04-release-verification-record.md.
This is the gate for any change that touches tool metadata or registration.
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
```

Confirm `PUSH_BLOCKS`' real import path before writing that last test (`mcp_server.py` imports it at module top).

- [ ] **Step 2: Run**

Run: `.venv/bin/python -m pytest tests/test_mcp_wire_snapshot.py -v`
Expected: all pass against the current tree.

- [ ] **Step 3: Prove the gate bites**

Temporarily flip one tool's `open_world_hint` to `True`, confirm `test_open_world_and_destructive_sets_are_exact` fails, and restore. Capture the real output in your report — a gate nobody has seen fail is not known to work.

- [ ] **Step 4: Commit**

```bash
git add tests/test_mcp_wire_snapshot.py
git commit -m "test(mcp): pin the advertised surface to the 0.7.113 live snapshot

Turns 'did the refactor change what we advertise?' into a test rather than a
review question. Mutation-proved."
```

---

## Phase 2 — `doctor` surfaces version drift

Independent of everything else; useful while the thin-adapter migration is partial, because whatever remains in the shim is still version-sensitive.

### Task 4: Per-process version records

**Files:**
- Modify: `mcpbrain/mcp_server.py`
- Create: `tests/test_mcp_version_records.py`

**Interfaces:**
- Produces: `write_version_record(home)` — writes `mcp_heartbeat/<pid>.json` containing `{"pid", "version", "started"}`; `live_version_records(home) -> list[dict]` — records whose pid is alive, pruning dead ones.

Why not `mcp_heartbeat.json`: it is a **single** file and there are **multiple** live servers (three observed on 2026-08-04), so last-writer-wins describes only the newest and cannot answer "is *any* live server stale?". Keep the existing single file exactly as it is — `probes`/status read it as the "Claude Desktop connected" signal and that contract must not change.

- [ ] **Step 1: Write the failing test**

```python
"""Per-process version records, because there are multiple live MCP servers."""
import json
import os

from mcpbrain.mcp_server import live_version_records, write_version_record


def test_record_carries_pid_and_version(tmp_path):
    write_version_record(str(tmp_path))
    recs = live_version_records(str(tmp_path))
    assert len(recs) == 1
    assert recs[0]["pid"] == os.getpid()
    from mcpbrain import __version__
    assert recs[0]["version"] == __version__


def test_dead_pids_are_pruned(tmp_path):
    d = tmp_path / "mcp_heartbeat"
    d.mkdir(parents=True)
    (d / "999999.json").write_text(json.dumps(
        {"pid": 999999, "version": "0.0.1", "started": 0}), encoding="utf-8")
    write_version_record(str(tmp_path))
    pids = {r["pid"] for r in live_version_records(str(tmp_path))}
    assert 999999 not in pids
    assert not (d / "999999.json").exists(), "dead record should be removed"


def test_multiple_live_servers_are_all_reported(tmp_path):
    d = tmp_path / "mcp_heartbeat"
    d.mkdir(parents=True)
    write_version_record(str(tmp_path))
    # a second live record: reuse this process's pid under a different filename
    # is not valid, so use the parent pid, which is alive.
    (d / f"{os.getppid()}.json").write_text(json.dumps(
        {"pid": os.getppid(), "version": "0.0.1", "started": 0}), encoding="utf-8")
    assert len(live_version_records(str(tmp_path))) == 2


def test_write_never_raises_into_startup(tmp_path, monkeypatch):
    """Same best-effort contract as write_heartbeat: never break a connect."""
    monkeypatch.setattr("mcpbrain.mcp_server.Path", lambda *a, **k: (_ for _ in ()).throw(OSError))
    write_version_record(str(tmp_path))   # must not raise
```

Adjust the last test to whatever injection point actually forces an `OSError` in your implementation — the requirement is that a write failure cannot break server startup, matching `write_heartbeat`'s existing contract.

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_mcp_version_records.py -v`
Expected: FAIL with `ImportError: cannot import name 'write_version_record'`

- [ ] **Step 3: Implement, and call it beside `write_heartbeat(home)` in `main()`**

Liveness check: `os.kill(pid, 0)` raising `ProcessLookupError` means dead; `PermissionError` means alive but not ours. Do not add a dependency for this.

- [ ] **Step 4: Run**

Run: `.venv/bin/python -m pytest tests/test_mcp_version_records.py tests/test_mcp_heartbeat.py tests/test_mcp_server_no_native.py -v`
Expected: all pass — `test_mcp_heartbeat.py` proves the existing single-file contract is untouched.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/mcp_server.py tests/test_mcp_version_records.py
git commit -m "feat(mcp): per-process version records so drift is detectable for N servers

mcp_heartbeat.json is a single file and there are multiple live MCP servers, so
last-writer-wins describes only the newest one. Adds mcp_heartbeat/<pid>.json
with version + start time, pruned by pid liveness, leaving the existing
single-file connected-signal contract untouched."
```

---

### Task 5: The `doctor` drift check

**Files:**
- Modify: `mcpbrain/doctor.py`
- Create: `tests/test_doctor_version_drift.py`

**Interfaces:**
- Consumes: `live_version_records(home)` from Task 4
- Produces: `version_drift_line(home, installed=None) -> str | None` — a formatted `doctor` line, or `None` when there is nothing to say. `run_doctor` appends it when not `None`, following the existing `lines.append(arch_line())` pattern at `mcpbrain/doctor.py:340`.

- [ ] **Step 1: Write the failing test**

```python
"""doctor must say when a live MCP server is running superseded code."""
from mcpbrain.doctor import version_drift_line


def _recs(*versions):
    return [{"pid": 1000 + i, "version": v, "started": 0}
            for i, v in enumerate(versions)]


def test_silent_when_every_server_matches(monkeypatch):
    monkeypatch.setattr("mcpbrain.doctor.live_version_records",
                        lambda home: _recs("0.7.113", "0.7.113"))
    assert version_drift_line("/tmp/h", installed="0.7.113") is None


def test_warns_when_one_server_is_stale(monkeypatch):
    monkeypatch.setattr("mcpbrain.doctor.live_version_records",
                        lambda home: _recs("0.7.112", "0.7.113"))
    line = version_drift_line("/tmp/h", installed="0.7.113")
    assert line is not None
    assert "0.7.112" in line and "0.7.113" in line
    assert "restart" in line.lower(), "must tell the user what to do"


def test_silent_when_no_servers_are_running(monkeypatch):
    """No MCP server is not a drift problem — doctor already covers connectivity."""
    monkeypatch.setattr("mcpbrain.doctor.live_version_records", lambda home: [])
    assert version_drift_line("/tmp/h", installed="0.7.113") is None


def test_counts_stale_servers_rather_than_naming_pids(monkeypatch):
    monkeypatch.setattr("mcpbrain.doctor.live_version_records",
                        lambda home: _recs("0.7.111", "0.7.112", "0.7.113"))
    line = version_drift_line("/tmp/h", installed="0.7.113")
    assert "2" in line, "should say how many are stale"
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_doctor_version_drift.py -v`
Expected: FAIL with `ImportError: cannot import name 'version_drift_line'`

- [ ] **Step 3: Implement and wire into `run_doctor`**

Use `⚠️` and the existing `f"⚠️  {label:<16} …"` formatting so it matches the surrounding output. Default `installed` to `importlib.metadata.version("mcpbrain")`.

- [ ] **Step 4: Run + eyeball the real output**

Run:
```bash
.venv/bin/python -m pytest tests/test_doctor_version_drift.py tests/test_doctor*.py -v
mcpbrain doctor
```
Expected: tests pass; the live run either stays silent (all servers current) or names the drift. Paste the real `doctor` output into your report — this check's whole value is being readable.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/doctor.py tests/test_doctor_version_drift.py
git commit -m "feat(doctor): warn when a live MCP server runs superseded code

A live MCP server executes the code it started with for its whole life, and
nothing signals it on update — so a shipped fix reaches a user on their next
client restart, not when the wheel lands, while doctor previously reported the
installed version and looked green. Reports the actual per-process versions."
```

---

## Phase 3 — Measure before moving anything

### Task 6: Confirm the seam, and take the latency baseline

Without a baseline the Task 11 gate is unfalsifiable. And because the flag defaults **ON**, that gate is a hard pre-release gate — so this measurement is load-bearing, not informational.

**Files:**
- Create: `bin/measure_tool_latency.py`, `tests/test_tool_seam.py`

- [ ] **Step 1: Verify the move-list against the code**

```bash
grep -n "^def make_brain_" mcpbrain/mcp_server.py
```
For each of the 24 factories, record whether it takes `store`/`draft_store`. Compare against the plan's **Tool inventory** section. **If the lists disagree, stop and report** — the whole design rests on this seam. Write the finding into `tests/test_tool_seam.py` as an executable assertion:

```python
def test_the_store_touching_set_is_exactly_what_the_plan_assumes():
    """Pins the seam. If a tool gains or loses Store access, this fails loudly
    rather than silently changing which side of the adapter it belongs on."""
    import inspect
    from mcpbrain import mcp_server as ms

    STORE_TOUCHING = {
        "brain_read", "brain_context", "brain_actions", "brain_graph",
        "brain_proactive", "brain_finding_resolve", "brain_draft_context",
        "brain_draft_save", "brain_meetings_today", "brain_meeting_pack_get",
        "brain_meeting_pack_upsert", "brain_gardener_apply",
    }
    actual = set()
    for name in dir(ms):
        if not name.startswith("make_brain_"):
            continue
        params = inspect.signature(getattr(ms, name)).parameters
        if "store" in params or "draft_store" in params:
            actual.add(name.removeprefix("make_"))
    assert actual == STORE_TOUCHING, f"seam moved: {actual ^ STORE_TOUCHING}"
```

- [ ] **Step 2: Write the latency harness**

`bin/measure_tool_latency.py` calls each of the 12 Store-touching tools through a real `protocol_session`-style stdio client N times (default 5), reports median and p95 per tool as JSON, and writes it to a path given on the command line. It must be runnable before and after the move with identical invocation.

- [ ] **Step 3: Take the baseline against the LIVE store**

The live store is ~11.9 GB; a temp store would measure nothing useful.

```bash
.venv/bin/python bin/measure_tool_latency.py --out /tmp/latency-before.json
python -c "import json;d=json.load(open('/tmp/latency-before.json'));[print(f'{k:26s} {v[\"median_ms\"]:8.1f} {v[\"p95_ms\"]:8.1f}') for k,v in sorted(d.items())]"
```

Record it in your report. Note in the report which tools are on the recall path (`brain_context`, `brain_actions`, `brain_graph`) — those are the ones the 0.7.105 and 0.7.110 incidents were about.

- [ ] **Step 4: Commit**

```bash
git add bin/measure_tool_latency.py tests/test_tool_seam.py
git commit -m "test(tools): pin the Store-access seam and add a latency baseline harness

The thin adapter's whole design rests on which tools hold a Store handle, so
that set is now an executable assertion rather than a comment. The harness runs
identically before and after the move, because a default-ON flag makes the
latency comparison a hard pre-release gate with no opt-in soak period."
```

---

### Task 7: Finding 3 — the WAL hypothesis, under load, before the move

**Files:**
- Create: `bin/probe_wal_contention.py`

The 0.7.113 investigation refuted this **under idle conditions** (`lsof` showed one holder, no `-wal` file). It was never tested under load, and backups were failing for real with `wal_checkpoint(TRUNCATE) busy=1`.

- [ ] **Step 1: Write the probe**

With two MCP servers connected, invoke a **writing** tool (`brain_note` writes only to the spool — use `brain_meeting_pack_upsert` or `brain_draft_save`, which genuinely write to the Store) in each concurrently, then attempt `wal_checkpoint(TRUNCATE)` from a third connection and record whether it returns busy. Report: holder count from `lsof`, presence and size of `-wal`/`-shm`, and the checkpoint result.

- [ ] **Step 2: Run it and record the answer**

This is a measurement, not a test — its output is the deliverable. Put the real captured output in your report. **A refutation is a valid and useful result**; the failure mode is leaving the cause unattributed.

- [ ] **Step 3: Commit**

```bash
git add bin/probe_wal_contention.py
git commit -m "test(backup): probe the WAL-checkpoint hypothesis under concurrent MCP writes

0.7.113 refuted this under IDLE conditions only, while real backups were failing
with wal_checkpoint(TRUNCATE) busy=1. Tests the loaded case that was never
tested, before the thin adapter removes the writable handles and makes the
question unanswerable."
```

---

### Task 8: Finding 2 — the two-server measurement

**Files:** none (measurement only)

- [ ] **Step 1: Measure and record**

Confirm both Desktop-spawned servers complete `initialize` (the 0.7.113 logs suggest they do), measure each one's RSS, and establish whether the count tracks windows/workspaces by opening and closing a second window. **A documented "this is client behaviour, nothing to change" is a legitimate outcome** — the point is to stop guessing. Note this gets cheaper to care about after Phase 4, since a shim holding no `Store` is a much smaller process.

- [ ] **Step 2: Report, no commit**

Append the finding to `docs/superpowers/specs/2026-08-04-mcp-server-process-lifecycle.md` under Finding 2 and commit that doc change only.

---

## Phase 4 — Thin adapter behind a default-ON flag

### Task 9: `POST /api/tool`, `ControlClient.call_tool`, and one tool routed

**Files:**
- Modify: `mcpbrain/control_api.py`, `mcpbrain/control_client.py`, `mcpbrain/daemon.py`, `mcpbrain/mcp_server.py`, `mcpbrain/config.py`
- Create: `tests/test_tool_exec_routing.py`

**Interfaces:**
- Produces: `POST /api/tool` accepting `{"name": str, "arguments": dict}` → `200 {"result": <handler return>}` or an error body; `ControlClient.call_tool(name, arguments) -> Any` raising `DaemonUnavailable`; `config.tool_exec_in_daemon(home) -> bool` delegating to `fleet_flag(home, "tool_exec_in_daemon", default=True)`.

- [ ] **Step 1: Write the failing test**

```python
"""Routing: flag on -> daemon; flag off or daemon down -> local. Never a crash."""
import asyncio

import pytest


def test_flag_defaults_on(tmp_path):
    from mcpbrain import config
    assert config.tool_exec_in_daemon(str(tmp_path)) is True


def test_local_kill_switch_wins(tmp_path):
    """An install must always be able to shut this off for itself."""
    from mcpbrain import config
    config.write_config(str(tmp_path), {"tool_exec_in_daemon": False})
    assert config.tool_exec_in_daemon(str(tmp_path)) is False


def test_routed_tool_reaches_the_daemon(protocol_session):
    """brain_read with the flag on must produce a daemon call, not a local Store read."""
    ...


def test_daemon_down_returns_isError_not_a_crash(protocol_session):
    """A store tool with no daemon must return a readable error result.

    The six capture tools stay local precisely so they keep working here; a
    store tool cannot, but it must degrade to isError with a message naming the
    daemon, never an unhandled exception.
    """
    ...


def test_capture_tools_still_work_with_no_daemon(protocol_session):
    """The reason the seam is 'Store access' and not 'everything'."""
    ...
```

Fill the three protocol-level bodies using the shared `protocol_session` factory and the `_FakeDaemon`/`ControlServer` pattern from `tests/test_mcp_server_stdio.py`. Drive them with `asyncio.run(...)` — **not** `@pytest.mark.asyncio`, which is an unknown mark here and would make them pass while asserting nothing.

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_tool_exec_routing.py -v`
Expected: FAIL — `config.tool_exec_in_daemon` does not exist.

- [ ] **Step 3: Implement the endpoint, the client method, and the flag**

Follow the existing `control_api.py` shape (`if h.path == "/api/tool":` beside the others at `:323-380`) and return via the same `h_json(h, 200, {...})` helper. The daemon executes by looking the handler up in the registry — it must **not** import `mcp_server`'s protocol layer to do so; that is what `tool_registry.py` being MCP-free is for. Decide and document where the handler *functions* live so the daemon can reach them without importing the MCP surface, and report the choice.

Validation stays at the **MCP server** boundary (it is the protocol boundary, and failures must return `isError`). The daemon re-validates defensively; a mismatch between the two is a bug worth failing loudly on.

- [ ] **Step 4: Route exactly one tool — `brain_read`**

Simplest possible case: one argument, pure read, small result. Prove the round trip end to end before moving eleven more.

- [ ] **Step 5: Run**

Run: `.venv/bin/python -m pytest tests/test_tool_exec_routing.py tests/test_mcp_protocol_surface.py tests/test_mcp_wire_snapshot.py -q -W error::RuntimeWarning`
Expected: all pass. The wire snapshot must be **unchanged** — routing is an execution detail, not a surface change.

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/control_api.py mcpbrain/control_client.py mcpbrain/daemon.py \
        mcpbrain/mcp_server.py mcpbrain/config.py tests/test_tool_exec_routing.py
git commit -m "feat(tools): route tool execution through the daemon, behind a default-ON flag

Adds POST /api/tool, ControlClient.call_tool, and the tool_exec_in_daemon flag
(default ON, so it is a kill switch rather than an opt-in — schema_grounding and
write_time_dedup have both sat OFF since shipping because nothing exercised
them). brain_read is routed as the proving slice; the advertised surface is
unchanged."
```

---

### Task 10: Route the remaining 11 Store-touching tools

**Files:**
- Modify: `mcpbrain/mcp_server.py`, `mcpbrain/daemon.py`, `tests/test_tool_exec_routing.py`

- [ ] **Step 1: Route them in groups, testing each group**

Group by risk, easiest first: reads (`brain_context`, `brain_actions`, `brain_graph`, `brain_proactive`, `brain_meetings_today`, `brain_meeting_pack_get`) → writes (`brain_finding_resolve`, `brain_draft_save`, `brain_meeting_pack_upsert`) → the awkward two (`brain_draft_context`, which may invoke a ~30s critique subprocess; `brain_gardener_apply`, which does a synchronous `git commit`).

For `brain_draft_context` and `brain_gardener_apply`, note that moving them into the daemon puts a long blocking call **on a daemon thread** rather than the MCP server's event loop. That is an improvement for the MCP server but adds load to the contended process — flag the latency result for these two specifically in Task 11.

- [ ] **Step 2: Assert the MCP server holds no Store handle when the flag is on**

```python
def test_no_store_handle_when_routing_is_enabled(mcp_env):
    """The observable success criterion for the thin adapter.

    Flag ON: the MCP server must construct no Store at all. Flag OFF (kill
    switch) it still must, because the local fallback path needs one.
    """
    ...
```

Implement by making `main()` construct the `Store`/`draft_store` **lazily, only when the flag is off**, and asserting on that. This is where "no Store handle" stops being a claim and becomes a test.

- [ ] **Step 3: Run the full MCP set**

Run:
```bash
.venv/bin/python -m pytest $(ls tests/test_mcp_*.py | tr '\n' ' ') tests/test_tool_exec_routing.py \
  tests/test_tool_registry.py tests/test_tool_seam.py -q -W error::RuntimeWarning
```
Expected: all pass, including the 26-tool protocol round-trip with the flag both on and off.

- [ ] **Step 4: Commit**

```bash
git add mcpbrain/mcp_server.py mcpbrain/daemon.py tests/test_tool_exec_routing.py
git commit -m "feat(tools): route all Store-touching tools through the daemon

The MCP server now constructs no Store at all when the flag is on (lazily only
for the kill-switch fallback), which removes the multiple-writable-handle class
by construction rather than by measurement. The six filesystem-only capture
tools deliberately stay local so they keep working with the daemon down."
```

---

### Task 11: The latency gate — hard, pre-release

**Files:** none (gate only), plus a report

Because the flag defaults **ON**, there is no opt-in soak period: this must pass before the change ships.

- [ ] **Step 1: Re-measure with identical invocation**

```bash
.venv/bin/python bin/measure_tool_latency.py --out /tmp/latency-after.json
```

- [ ] **Step 2: Compare and decide, per tool**

Report median and p95 before/after for all 12. **The gate:** no tool on the recall path (`brain_context`, `brain_actions`, `brain_graph`) may regress materially, given the 0.7.105 timeouts (drain pinning the process) and 0.7.110's `prompt_recall` raise to 3.0s after measuring 1.3-2.6s cold.

If a tool regresses: **it does not move.** Revert that tool to local execution and record why. Partial adoption is an acceptable outcome and better than shipping a latency regression to a fleet by default. If the recall-path tools regress, escalate — that likely means the flag should default OFF after all, which is a decision for the human, not this task.

- [ ] **Step 3: Report — no commit unless a tool is reverted**

---

### Task 12: Re-probe Finding 3 after the move

**Files:** none (measurement)

- [ ] **Step 1: Re-run Task 7's probe**

With the flag on and the MCP server holding no Store handle, re-run `bin/probe_wal_contention.py`. Expected: the writable-handle contention class is **gone by construction**. Confirm rather than assume, and record it against Task 7's before-numbers.

- [ ] **Step 2: Close Finding 3 in the spec**

Append the before/after to the lifecycle spec and commit that doc change. **The success criterion for this plan is that Finding 3 ends fixed or explicitly closed with evidence** — "still unexplained" is a failure, because live backups were failing and the cause is currently unattributed.

---

## Phase 5 — Live verification

### Task 13: Verify from the installed wheel

**Files:** none

- [ ] **Step 1: Install and restart, mirroring the fleet**

```bash
launchctl bootout gui/$(id -u)/com.mcpbrain
uv tool install --force --refresh-package mcpbrain --reinstall-package mcpbrain ".[daemon]"
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.mcpbrain.plist
```

`--refresh-package`/`--reinstall-package` are required: with the version unchanged, `--force` alone reinstalls a **stale cached wheel**. Verify by grepping the installed file for a string unique to this work (e.g. `tool_exec_in_daemon`), never by trusting the install output.

- [ ] **Step 2: Restart Claude Desktop and confirm the full surface**

```bash
osascript -e 'tell application "Claude" to quit'; sleep 3; open -a Claude; sleep 14
tail -40 ~/Library/Logs/Claude/mcp-server-mcpbrain.log | grep -E '^\d{4}-'
```
Expected: `initialize` → result, `notifications/initialized`, `tools/list` → result, `prompts/list` → result, `resources/list` → result, **0 tracebacks**.

- [ ] **Step 3: Confirm the wire snapshot live, and the drift check**

Probe the installed server and assert the snapshot from this plan's gate section. Then run `mcpbrain doctor` and confirm the new drift line behaves (it should be silent immediately after a restart, since every server is current).

- [ ] **Step 4: Exercise a routed tool and a local tool from the real client**

A routed one (`brain_graph`) and a stay-local one (`brain_note`), so both sides of the seam are proven against the real client rather than a harness.

- [ ] **Step 5: Report — do NOT release**

Releasing is a separate explicit decision. Report the evidence and stop.

---

## Self-Review

**Spec coverage.** Registry/colocation → Tasks 1-3; immutability constraint → Task 1 Steps 1, 3; generated `brain_enrich_push` schema → Task 1 Step 4 and Task 3 Step 1; deleting the four accessors → Task 2. Lifecycle Finding 1 → Tasks 4-5 (surfacing) and 9-11 (the cause); Finding 2 → Task 8; Finding 3 → Tasks 7 and 12, with an explicit "must not end unexplained" criterion. The Store-access seam → Task 6 Step 1 as an executable assertion. Flag default-ON and its consequence (hard pre-release gate) → Global Constraints and Task 11. Deliberately excluded per the specs: splitting `mcp_server.py`, normalising the inconsistent success keys, and `update.py` killing client-owned processes.

**Placeholder scan.** Four steps deliberately require reading real code before finalising and say so rather than hiding it: Task 3 Step 1's `PUSH_BLOCKS` import path, Task 4 Step 1's `OSError` injection point, Task 9 Step 1's three protocol-test bodies, and Task 9 Step 3's decision about where handler functions live so the daemon can reach them without importing the MCP surface. Task 2 Step 3 and Task 10 Step 1 describe bulk metadata moves as "verbatim" because they are literal moves of existing values, not new content. Task 6 Step 2's harness is specified by behaviour (identical invocation before/after, median + p95, JSON out) rather than by code, because its implementation is unconstrained.

**Type consistency.** `ToolSpec` fields (`description`, `input_schema`, `annotations`, `output_schema`) are used identically in Tasks 1, 2, 3. `spec(name)`/`registry()` are consumed in Tasks 2, 3. `live_version_records(home)` is produced in Task 4 and consumed in Task 5. `config.tool_exec_in_daemon(home)` is produced in Task 9 and consumed in Tasks 10, 11. `bin/measure_tool_latency.py --out` is created in Task 6 and re-run identically in Task 11. `bin/probe_wal_contention.py` is created in Task 7 and re-run in Task 12.
