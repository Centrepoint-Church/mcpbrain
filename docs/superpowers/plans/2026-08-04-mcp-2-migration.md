# MCP SDK 2.x Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate mcpbrain's MCP server to the `mcp` 2.x low-level `Server` API without losing input validation or wire compatibility with today's Claude clients, and bring the tool surface up to the current protocol: safety annotations, structured output, prompts, resource-change notifications, and progress — all under protocol-level test coverage that would have caught the 2026-08-04 outage.

**Architecture:** Stay on the **low-level `Server`**. Three phases, in dependency order: **(1) safety net** — extract handler registration out of `main()` into a testable `build_server()` factory and land protocol-level coverage of all 26 tools plus both resource handlers over real stdio (today: 2 of 26, and resources never); **(2) the port** — move the ~80-line registration layer to 2.x's `on_*` constructor kwargs and re-implement the input validation 2.x drops, with the 26 schema literals and 26 handler bodies moving unchanged; **(3) surface upgrades** — annotations, `outputSchema`, prompts, `resources/list_changed`, progress. Phase 3 comes last because on 2.x we construct `CallToolResult` ourselves, so structured content and notifications are natural there and would otherwise be built twice. The high-level `MCPServer` and third-party `fastmcp` are both rejected (see Decision Record).

**Tech Stack:** Python 3.12, `mcp` (official Model Context Protocol SDK) 1.29.0 → 2.x, `mcp-types` 2.x, `jsonschema` (already a transitive dep, becomes direct), pytest.

## Global Constraints

- **Target SDK: official `mcp` 2.x low-level `Server`.** Not `fastmcp` (2.x/3.x pin `mcp<2.0` and would block this migration; 4.0.0b1 is beta + 29 extra packages). Not the high-level `MCPServer` (cannot express `brain_enrich_push`'s dynamic schema — verified failure).
- **Wire compatibility is non-negotiable.** `mcp` 2.x is dual-era (`serve_dual_era_loop`). Claude Desktop negotiates `2025-11-25` today (148 log occurrences, zero `server/discover`). The migrated server MUST still serve that handshake.
- **Input validation MUST be preserved.** `mcp` 1.x `call_tool(validate_input=True)` is the **default** and we rely on it implicitly. `mcp` 2.x low-level validates nothing. Re-add explicit validation (Task 8) or the port is a silent regression across all 26 tools.
- **`brain_enrich_push`'s generated schema must keep working.** `push_input_schema()` builds `inputSchema` from `_PUSH_BLOCKS`; the handler absorbs keys via `**blocks`. Do not replace with type-hint inference.
- **The `None` vs `[]` distinction must survive.** `mcp_server.py:1443-1449` — `extractions=None` (absent) vs `[]` (present-but-empty) drives the block-unit vs thread-unit guard. Do not coerce.
- **The MCP bridge module stays free of native/heavy imports.** `tests/test_mcp_server_no_native.py` asserts `get_embedder`/`hybrid_search` never appear in `mcp_server.py` source and no `fastembed`/`onnxruntime` module loads.
- **Version lives in FIVE files** (`pyproject.toml`, `mcpbrain/__init__.py`, `plugin/.claude-plugin/plugin.json`, `plugin/.claude-plugin/marketplace.json`, `plugin/mcpb/manifest.json`) plus `uv.lock`. Do not bump as part of this plan; release is a separate explicit step.
- **Do NOT release.** Per `CLAUDE.md`, pushing/releasing is an all-users action requiring explicit instruction. This plan ends at "verified locally"; Task 12 is the gate, not a release.
- **Test scope:** run edited + directly impacted test files only. Josh runs full-repo `pytest tests/` himself.
- **NO `pytest-asyncio` IN THIS REPO — this constraint overrides every `@pytest.mark.asyncio` snippet below.** Discovered during Task 3: `pytest_asyncio` is not installed and `pyproject.toml` has no asyncio config, so `@pytest.mark.asyncio` is an **unknown mark** — the async body is never awaited and the test **passes while asserting nothing**. Every async snippet in Tasks 9–12 is written with that decorator and must NOT be implemented literally. Use the convention Task 3 established in `tests/conftest.py` (matching the pre-existing `tests/test_mcp_server_stdio.py`): a plain sync test driving its own `asyncio.run(...)`, with `protocol_session` as an `@asynccontextmanager` **factory** rather than a live-session fixture:
  ```python
  def test_something(protocol_session):
      async def _body():
          async with protocol_session() as (session, stderr_path):
              result = await session.call_tool("brain_note", {"text": "x"})
              assert not result.isError
      asyncio.run(_body())
  ```
  Keep each snippet's assertions exactly as written; change only the async plumbing. Do **not** add `pytest-asyncio` as a dependency to make the snippets work as-published — adding a test-runner plugin to a fleet-shipped package is not this plan's call.
- **Shared fixtures live in `tests/conftest.py`.** `mcp_env` (Task 2) and `protocol_session` (Task 3) are already there and are consumed by Tasks 8–12. Use them; do not duplicate or relocate them. (Ruled on explicitly during Task 2.)
- **This working tree is SHARED with concurrent Claude sessions.** Stage only your own named files — never `git add -A`/`git add .`/`git commit -a`. Review diffs must be scoped to your own commit's parent, because other sessions' commits interleave on `main`.

---

## Decision Record

| Option | Verdict | Reason |
|---|---|---|
| `mcp` 2.x **low-level `Server`** | **CHOSEN** | +5 packages only; keeps raw `inputSchema` literals and `**blocks` handler working; officially-backed line (default install since 2026-08-02); reaches 2026-07-28 era when clients flip |
| `mcp` 1.x (stay) | Rejected | Security-fixes-only with no stated EOL; deferral, not a solution |
| `mcp` 2.x high-level `MCPServer` | Rejected — hard blocker | `add_tool` accepts only callables; forcing `.parameters` leaves advertised schema and pydantic validation model divergent → `brain_enrich_push` calls fail with `Field required [type=missing]` |
| `fastmcp` 3.x | Rejected | Pins `mcp<2.0` — actively blocks this migration (`fastmcp==3.4.5` + `mcp==2.0.0` is unsatisfiable); +29 packages; stuck at protocol `2025-11-25` |
| `fastmcp` 4.0.0b1 | Rejected | Beta, 8 days old; +29 packages; minor releases may break — bad fit for daily unattended fleet auto-update |

**Why now:** `mcp` 1.x is security-fixes-only. The `mcp>=1.2,<2` pin landed 2026-08-04 stops the bleeding but freezes us on a dead branch. Migrating deliberately and attended now beats migrating under pressure when Claude flips to the stateless era.

**What we do NOT get yet:** MRTR, `server/discover`, `subscriptions/listen`, `resultType` all require a `2026-07-28` connection no Claude client negotiates today. The SDK's dual-era `run()` handles that transparently — no era-specific code needed in mcpbrain.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `mcpbrain/mcp_server.py` | The whole MCP surface: 20 `make_*` factories (SDK-agnostic), `_resource_entries`/`list_context_resources`/`read_context_resource`, `push_input_schema`, lease helpers, and `main()` | Modify — extract `build_server()`, port registration layer |
| `tests/test_mcp_sdk_contract.py` | **New.** Dependency-ceiling guard: resolved `mcp` version satisfies the declared range, and the API surface the server targets actually exists | Create |
| `tests/test_mcp_protocol_surface.py` | **New.** Protocol-level round-trip of all 26 tools + both resource handlers over real stdio, with subprocess stderr captured | Create |
| `tests/test_mcp_server_stdio.py` | Existing thin protocol test (`initialize` → `list_tools` → `brain_search`) | Modify — `tool.inputSchema` → `.input_schema` reads if any; keep as-is otherwise |
| `tests/test_mcp_enrich_with_rules.py` | Existing protocol test for `brain_enrich_pull(with_rules=...)` | Modify — same read-name fixes only |
| `pyproject.toml` | Dependency declarations | Modify — raise `mcp` ceiling, add `jsonschema` as a direct dep |
| `uv.lock` | Lockfile | Regenerate |

`main()` currently spans `mcp_server.py:907-1485` (579 lines): setup `:908-958`, resources `:960-968`, `_tools` `:970-1318` (349 lines, 26 `types.Tool`), `_call` `:1320-1478` (159 lines, 26-branch chain), `_run` `:1480-1485`. Everything outside `main()` is SDK-agnostic and untouched.

---

## Task 1: Dependency-ceiling guard

The cheapest, highest-value task: this is the test that would have caught the 2026-08-04 outage before any user saw it. Tests run against `uv.lock`'s pinned `mcp`, but installed daemons auto-update daily via an **unpinned** `uv tool install` re-resolve — so the fleet reaches SDK versions no test ever exercises.

**Files:**
- Create: `tests/test_mcp_sdk_contract.py`

**Interfaces:**
- Consumes: nothing
- Produces: nothing (leaf test file)

- [ ] **Step 1: Write the failing test**

```python
"""Guards the mcp SDK contract mcp_server.py depends on.

Context: on 2026-08-04 every Claude Desktop connection crashed with
`AttributeError: 'Server' object has no attribute 'list_resources'` because an
unpinned `uv tool install` re-resolve picked up mcp 2.0.0, which deleted the 1.x
low-level decorator API. Tests never saw it: uv.lock pinned 1.27.2 while the
fleet auto-updates unpinned. These tests fail loudly on the next ceiling break
instead of surfacing as a 15-second subprocess timeout in production.
"""
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _declared_mcp_requirement() -> Requirement:
    data = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    for raw in data["project"]["dependencies"]:
        req = Requirement(raw)
        if req.name == "mcp":
            return req
    pytest.fail("pyproject.toml declares no `mcp` dependency")


def test_installed_mcp_satisfies_declared_range():
    """The mcp we actually import must satisfy the range pyproject declares."""
    from importlib.metadata import version

    installed = Version(version("mcp"))
    req = _declared_mcp_requirement()
    assert installed in req.specifier, (
        f"installed mcp {installed} violates declared {req}; "
        "the SDK API mcp_server.py targets may not exist"
    )


def test_lowlevel_server_exposes_the_api_mcp_server_targets():
    """mcp_server.py's registration layer must actually be callable.

    Asserts the API surface, not the version number, so this stays meaningful
    across the 1.x -> 2.x port (only the expected-attribute list changes).
    """
    from mcp.server import Server

    server = Server("contract-probe")
    missing = [
        attr for attr in ("list_resources", "read_resource", "list_tools", "call_tool")
        if not hasattr(server, attr)
    ]
    assert not missing, (
        f"mcp.server.Server is missing {missing} — mcp_server.py's registration "
        "layer will raise AttributeError at startup"
    )


def test_call_tool_validates_input_by_default():
    """We rely on the SDK validating arguments against inputSchema.

    mcp 1.x `call_tool(validate_input=True)` is the default and mcp_server.py uses
    the bare decorator, so all 26 tools get free jsonschema validation. mcp 2.x's
    low-level server validates NOTHING. If this assertion ever fails, validation
    must be re-implemented in mcpbrain before the tool surface is trusted.
    """
    import inspect

    from mcp.server import Server

    sig = inspect.signature(Server.call_tool)
    param = sig.parameters.get("validate_input")
    assert param is not None, "Server.call_tool no longer takes validate_input"
    assert param.default is True, (
        "Server.call_tool no longer validates input by default — mcpbrain must "
        "validate arguments against inputSchema itself"
    )
```

- [ ] **Step 2: Run the tests and confirm they pass on the current pin**

Run: `.venv/bin/python -m pytest tests/test_mcp_sdk_contract.py -v`
Expected: 3 passed. (These are guards, not red-first TDD — they must pass now and fail on the next break.)

- [ ] **Step 3: Prove the guard actually catches the real break**

Run against the scratch venv that has the breaking SDK:

```bash
/private/tmp/claude-501/-Users-joshkemp-GitHub-mcpbrain/97b32fd7-b6ae-4873-86e4-f4a2c7a84d52/scratchpad/mcp2/bin/python -m pip install pytest packaging -q
cd /Users/joshkemp/GitHub/mcpbrain && /private/tmp/claude-501/-Users-joshkemp-GitHub-mcpbrain/97b32fd7-b6ae-4873-86e4-f4a2c7a84d52/scratchpad/mcp2/bin/python -m pytest tests/test_mcp_sdk_contract.py -v
```

Expected: `test_installed_mcp_satisfies_declared_range`, `test_lowlevel_server_exposes_the_api_mcp_server_targets`, and `test_call_tool_validates_input_by_default` all **FAIL** with readable messages naming the missing attributes. This is the proof the guard works — record the output in the commit message.

- [ ] **Step 4: Commit**

```bash
git add tests/test_mcp_sdk_contract.py
git commit -m "test(mcp): guard the SDK contract mcp_server.py depends on

Fails loudly when the resolved mcp version drops the low-level API the
registration layer targets, instead of surfacing as a connect-time
AttributeError in production. Verified to fail against mcp 2.0.0."
```

---

## Task 2: Extract `build_server()` so the registration layer is testable

`tests/test_mcp_server_no_native.py` imports `mcp_server` but **never calls `main()`**, so no decorator is ever evaluated — which is precisely why the 2.0 break slipped through an "import smoke test". Extracting the registration layer makes it reachable without an event loop or a subprocess, and is the prerequisite for Tasks 3 and 9.

**Files:**
- Modify: `mcpbrain/mcp_server.py:907-1485`
- Create: `tests/test_mcp_build_server.py`

**Interfaces:**
- Produces: `build_server(store, draft_store, client, home) -> Server` — constructs the `Server`, registers all handlers, returns it without running any transport. `main()` becomes wiring + `build_server()` + `_run()`.

- [ ] **Step 1: Write the failing test**

```python
"""build_server() must produce a fully-registered server without any transport."""
import asyncio
import json

import pytest

from mcpbrain.mcp_server import build_server

EXPECTED_TOOLS = {
    "brain_search", "brain_read", "brain_context", "brain_actions", "brain_graph",
    "brain_proactive", "brain_finding_resolve", "brain_ingest", "brain_action_create",
    "brain_action_update", "brain_decision", "brain_note", "brain_memory_write",
    "brain_gardener_apply", "brain_draft_context", "brain_draft_save", "brain_routine",
    "brain_enrich_units", "brain_enrich_pull", "brain_enrich_push",
    "brain_enrich_advance", "brain_enrich_claim", "brain_enrich_pending",
    "brain_meetings_today", "brain_meeting_pack_get", "brain_meeting_pack_upsert",
}


def test_build_server_registers_every_tool(mcp_env):
    """26 tools, registered, without starting stdio."""
    server = build_server(**mcp_env)
    tools = asyncio.run(server.request_handlers_probe_list_tools())
    names = {t.name for t in tools}
    assert names == EXPECTED_TOOLS, (
        f"missing={EXPECTED_TOOLS - names} unexpected={names - EXPECTED_TOOLS}"
    )


def test_build_server_registers_resource_handlers(mcp_env):
    server = build_server(**mcp_env)
    assert "resources/list" in server.request_handlers
    assert "resources/read" in server.request_handlers


def test_build_server_reports_mcpbrain_version(mcp_env):
    """serverInfo.version must be mcpbrain's version, not the SDK's."""
    from mcpbrain import __version__

    server = build_server(**mcp_env)
    opts = server.create_initialization_options()
    assert opts.server_version == __version__
```

`server.request_handlers_probe_list_tools()` is not a real SDK method — replace it in Step 3 with whatever the registered handler is reachable as. On mcp 1.x the registered coroutine is stored in `server.request_handlers[types.ListToolsRequest]`; call it with a constructed request and read `.root.tools`. Write the test against the real accessor once you have it in front of you, and keep the assertion (exact set equality on 26 names) unchanged.

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_mcp_build_server.py -v`
Expected: FAIL with `ImportError: cannot import name 'build_server'`

- [ ] **Step 3: Add the `mcp_env` fixture**

The existing protocol tests already build a temp home + fake daemon. Reuse that pattern rather than inventing a new one: read `tests/test_mcp_server_stdio.py:42-108` for how it seeds `MCPBRAIN_HOME`, a `Store`, and a `_FakeDaemon` behind a real `ControlServer`, and lift it into a fixture in `tests/test_mcp_build_server.py` returning the kwargs `build_server` needs:

```python
@pytest.fixture
def mcp_env(tmp_path, monkeypatch):
    """A temp app-dir + stores + control client, matching build_server's signature."""
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config
    from mcpbrain.control_client import ControlClient
    from mcpbrain.store import Store

    dim = 384
    path = config.store_path()
    store = Store(path, dim=dim, read_only=False)  # created here so RO open succeeds
    return {
        "store": store,
        "draft_store": Store(path, dim=dim, read_only=False),
        "client": ControlClient(),
        "home": str(tmp_path),
    }
```

- [ ] **Step 4: Extract `build_server()`**

Replace `mcp_server.py:955-1478` — move the `Server(...)` construction and all four decorated handlers into a module-level function, leaving the 26 `types.Tool` literals and 26 handler branches **byte-for-byte unchanged** apart from indentation. Signature:

```python
def build_server(store, draft_store, client, home: str):
    """Construct the MCP Server with every handler registered, no transport started.

    Split out of main() so the registration layer is reachable from tests without
    spawning a subprocess or starting an event loop: an import-only smoke test
    never evaluates the handler registrations, which is how the mcp 2.0 API break
    reached production unseen (see tests/test_mcp_sdk_contract.py).
    """
    from mcp.server import Server
    from mcp import types

    from mcpbrain import __version__, config

    search = make_brain_search(client)
    context = make_brain_context(store)
    # ... all 20 make_* calls, moved verbatim from main() lines 918-950 ...

    server = Server(
        "mcpbrain",
        version=__version__,
        instructions=config.render_project_instructions(config.read_config(home)),
    )

    @server.list_resources()
    async def _list_resources():
        return await list_context_resources()

    # ... the other three handlers, bodies unchanged ...

    return server
```

Note `version=__version__` — that is the Task 4 fix, folded in here because this task already rewrites the construction call and splitting it would create a pointless second edit of the same three lines.

Then reduce `main()` to wiring plus transport:

```python
def main() -> None:  # stdio entry point, exercised manually + in P3 integration
    import mcp.server.stdio

    from mcpbrain import config
    from mcpbrain.control_client import ControlClient
    from mcpbrain.embed import embedder_dim
    from mcpbrain.store import Store

    _store_path, _store_dim = config.store_path(), embedder_dim("bge-small")
    store = Store(_store_path, dim=_store_dim, read_only=True)
    draft_store = Store(_store_path, dim=_store_dim, read_only=False)
    home = str(config.app_dir())
    write_heartbeat(home)
    server = build_server(store, draft_store, ControlClient(), home)

    async def _run():
        async with mcp.server.stdio.stdio_server() as (r, w):
            await server.run(r, w, server.create_initialization_options())

    import asyncio
    asyncio.run(_run())
```

Keep the explanatory comments from `:930-933`, `:938-939`, `:949`, `:951-954` attached to the code they describe — they document non-obvious store-handle scoping and connect-time instruction rendering.

- [ ] **Step 5: Run the new + impacted tests**

Run:
```bash
.venv/bin/python -m pytest tests/test_mcp_build_server.py tests/test_mcp_server_no_native.py \
  tests/test_mcp_server_stdio.py tests/test_mcp_enrich_with_rules.py tests/test_mcp_server.py -v
```
Expected: all pass. `test_mcp_server_no_native.py` matters most — it AST-scans `mcp_server.py` source for `get_embedder`/`hybrid_search`, and moving code between functions must not introduce either.

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/mcp_server.py tests/test_mcp_build_server.py
git commit -m "refactor(mcp): extract build_server() from main()

Makes the handler-registration layer reachable from tests without a
subprocess or event loop — the blind spot that let the mcp 2.0 API break
reach production through an import-only smoke test. Also passes
version=__version__ so serverInfo stops reporting the SDK's version."
```

---

## Task 3: Protocol-level coverage for all 26 tools and both resource handlers

Today only **2 of 26** tools (`brain_search`, `brain_enrich_pull`) are ever invoked through `call_tool`, and `list_resources`/`read_resource` are never exercised over the protocol at all. The `arguments.get(...)` → keyword plumbing in `_call` is 159 lines of untested glue — and Task 9 rewrites every line of it. Without this task the port's correctness rests on review alone.

**Files:**
- Create: `tests/test_mcp_protocol_surface.py`

**Interfaces:**
- Consumes: `build_server` from Task 2 (for the in-process variant); the stdio harness pattern from `tests/test_mcp_server_stdio.py`
- Produces: nothing

- [ ] **Step 1: Write the failing test**

Drive a real `ClientSession` over stdio against a real subprocess, capture the child's **stderr** (the existing harness discards it, which is why the 2.0 crash presented as a bare timeout instead of a traceback), and round-trip every tool.

```python
"""Round-trips the entire MCP surface over a real stdio session.

Why this exists: before this file, 2 of 26 tools were ever called through the
protocol and the resource handlers never were. The dispatch layer in _call is
pure argument plumbing -- exactly the code an SDK port rewrites wholesale -- and
it was effectively untested. It also captures subprocess stderr, so a startup
crash reports its traceback instead of a 15-second timeout.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

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


@pytest.mark.asyncio
async def test_every_tool_round_trips_over_stdio(protocol_session):
    """Each tool returns parseable JSON, and none raises a protocol error.

    Handlers are allowed to return an error PAYLOAD (e.g. {"error": ...} for a
    missing doc) -- that is a working tool. What must not happen is an unhandled
    exception surfacing as isError, which is what a broken dispatch layer does.
    """
    session, stderr_path = protocol_session
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
    assert not failures, f"tools failed over the protocol: {failures}\n" \
                         f"server stderr:\n{Path(stderr_path).read_text()}"


@pytest.mark.asyncio
async def test_unknown_tool_reports_unknown_tool(protocol_session):
    """A misspelled name must not fall through to brain_search.

    Regression guard: brain_search was the unguarded fallthrough in _call, so any
    unknown name hit arguments["query"] and raised KeyError.
    """
    session, _ = protocol_session
    result = await session.call_tool("brain_nonexistent", {})
    assert result.isError
    text = " ".join(c.text for c in result.content).lower()
    assert "unknown tool" in text and "brain_nonexistent" in text


@pytest.mark.asyncio
async def test_resources_round_trip_over_stdio(protocol_session):
    """list_resources + read_resource over the protocol, not as plain functions."""
    session, _ = protocol_session
    resources = (await session.list_resources()).resources
    assert resources, "no resources advertised"
    first = resources[0]
    assert str(first.uri).startswith("file://")
    contents = (await session.read_resource(first.uri)).contents
    assert contents and contents[0].text is not None


@pytest.mark.asyncio
async def test_read_resource_rejects_unadvertised_path(protocol_session):
    """The allowlist guard must hold over the protocol too."""
    session, _ = protocol_session
    with pytest.raises(Exception):
        await session.read_resource("file:///etc/passwd")
```

- [ ] **Step 2: Add the `protocol_session` fixture**

Model it on `tests/test_mcp_server_stdio.py:42-108`, with two changes: seed the records repo so resource tests have files to find, and redirect the child's stderr to a file the assertions can print.

```python
@pytest.fixture
async def protocol_session(tmp_path):
    """A real ClientSession over a real subprocess, with stderr captured."""
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    home = tmp_path / "home"
    (home / "context").mkdir(parents=True)
    (home / "context" / "memory.md").write_text("# memory\n", encoding="utf-8")
    stderr_path = tmp_path / "server-stderr.log"

    env = {
        "HOME": str(tmp_path),
        "PATH": os.environ["PATH"],
        "MCPBRAIN_HOME": str(home),
        "PYTHONPATH": str(Path(__file__).resolve().parent.parent),
    }
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "mcpbrain.mcp_server"], env=env,
    )
    with open(stderr_path, "wb") as errlog:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session, str(stderr_path)
```

If `stdio_client` does not accept an `errlog` parameter on the installed SDK, read its signature (`mcp/client/stdio.py`) and use whatever stderr sink it does provide. Capturing stderr is a requirement of this task, not optional polish — without it a startup crash is indistinguishable from a hang.

- [ ] **Step 3: Run and watch it fail on the two real bugs**

Run: `.venv/bin/python -m pytest tests/test_mcp_protocol_surface.py -v`
Expected: `test_unknown_tool_reports_unknown_tool` **FAILS** (unknown names currently fall through to `brain_search` and raise `KeyError`, not "unknown tool"). The other tests should pass. If any tool in `test_every_tool_round_trips_over_stdio` fails, that is a genuine pre-existing dispatch bug — record it, do not paper over it.

- [ ] **Step 4: Fix the fallthrough**

In `build_server`'s `_call`, replace the bare tail (`mcp_server.py:1477-1478` pre-refactor) with an explicit branch plus a real error:

```python
        if name == "brain_search":
            results = await search(arguments["query"], arguments.get("limit", 10))
            return [types.TextContent(type="text", text=json.dumps(results))]
        raise ValueError(f"unknown tool: {name}")
```

Also harden the entry against a `None` arguments payload — only `brain_routine` defends today, and the other 25 branches assume a dict:

```python
    @server.call_tool()
    async def _call(name, arguments):
        import json
        arguments = arguments or {}
```

- [ ] **Step 5: Run again**

Run: `.venv/bin/python -m pytest tests/test_mcp_protocol_surface.py tests/test_mcp_server_stdio.py tests/test_mcp_enrich_with_rules.py -v`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add tests/test_mcp_protocol_surface.py mcpbrain/mcp_server.py
git commit -m "test(mcp): round-trip all 26 tools + resources over real stdio

Was 2 of 26 tools and zero resource calls exercised through the protocol.
Captures subprocess stderr so a startup crash shows its traceback instead
of a bare timeout. Fixes the brain_search fallthrough found by the new
unknown-tool test, and hardens _call against a None arguments payload."
```

---

## Task 4: Port the registration layer to the mcp 2.x low-level API

The mechanical heart of the migration. All four decorators become constructor kwargs, handlers take `(ctx, params)`, and every handler must return a complete `types.*Result` model — returning a bare list is a hard `TypeError` in 2.x, not a convenience-normalised success.

**Files:**
- Modify: `mcpbrain/mcp_server.py` (`build_server` from Task 2, plus `list_context_resources`)
- Modify: `tests/test_mcp_sdk_contract.py` (retarget the expected API surface)
- Modify: `tests/test_mcp_build_server.py` (handler accessor changes)

**Interfaces:**
- Consumes: `build_server(store, draft_store, client, home) -> Server` from Task 2
- Produces: same signature, unchanged — callers (`main()`, tests) are untouched

- [ ] **Step 1: Install the 2.x SDK into a scratch venv and run the existing suite against it**

Before editing anything, establish the red baseline:

```bash
cd /Users/joshkemp/GitHub/mcpbrain
uv venv /tmp/mcp2-dev --python 3.12
VIRTUAL_ENV=/tmp/mcp2-dev uv pip install -e ".[dev]" "mcp==2.0.0"
/tmp/mcp2-dev/bin/python -m pytest tests/test_mcp_build_server.py tests/test_mcp_protocol_surface.py -v
```
Expected: FAIL — `AttributeError: 'Server' object has no attribute 'list_resources'`. Confirm the failure is the API break and not an install problem.

- [ ] **Step 2: Port the four handlers**

Rewrite `build_server`'s registration block. Handler bodies keep their logic; only the signature, the argument source, and the return wrapper change.

```python
    async def on_list_resources(ctx, params) -> types.ListResourcesResult:
        return types.ListResourcesResult(resources=await list_context_resources())

    async def on_read_resource(
        ctx, params: types.ReadResourceRequestParams
    ) -> types.ReadResourceResult:
        # 2.x requires a full result model with the uri echoed back; the 1.x
        # ReadResourceContents helper is no longer accepted at the low level.
        text = await read_context_resource(params.uri)
        return types.ReadResourceResult(
            contents=[
                types.TextResourceContents(
                    uri=params.uri, mimeType="text/markdown", text=text
                )
            ]
        )

    async def on_list_tools(ctx, params) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[
            # ... the 26 types.Tool literals, moved verbatim ...
        ])

    async def on_call_tool(
        ctx, params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        import json
        name, arguments = params.name, (params.arguments or {})
        _validate_tool_arguments(name, arguments)   # Task 5 adds this
        # ... the 26 dispatch branches, unchanged, except each `return [...]`
        # becomes `return types.CallToolResult(content=[...])` ...

    server = Server(
        "mcpbrain",
        version=__version__,
        instructions=config.render_project_instructions(config.read_config(home)),
        on_list_resources=on_list_resources,
        on_read_resource=on_read_resource,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )
    return server
```

The 26 `return [types.TextContent(type="text", text=json.dumps(out))]` lines each become:

```python
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=json.dumps(out))]
            )
```

Mechanical, but do it to all 26 — a missed one is a runtime `TypeError: handler returned list; expected BaseModel, dict, or None`, and `test_every_tool_round_trips_over_stdio` from Task 3 is what catches it.

Replace the `raise ValueError(f"unknown tool: {name}")` tail behaviour check: in 2.x, tool exceptions **propagate as protocol errors** rather than being converted to `CallToolResult(is_error=True)`. Verify against Task 3's `test_unknown_tool_reports_unknown_tool` and adjust that test's expectation if the error now arrives as an exception on the client rather than an `isError` result — keeping the assertion that the message names the unknown tool.

- [ ] **Step 3: Fix camelCase attribute READS**

Construction still accepts camelCase (`inputSchema=`, `mimeType=`) via pydantic aliases, but **reads** are snake_case in 2.x. Find and fix:

```bash
grep -rn "\.inputSchema\|\.mimeType\|\.isError\|\.structuredContent\|\.nextCursor\|\.protocolVersion\|\.serverInfo" \
  mcpbrain/ tests/ bin/
```
`.inputSchema` → `.input_schema`, `.isError` → `.is_error`, `.mimeType` → `.mime_type`, etc. Wire JSON stays camelCase, so only Python attribute access changes.

- [ ] **Step 4: Fix `McpError` → `MCPError`**

```bash
grep -rn "McpError" mcpbrain/ tests/ bin/
```
Renamed with no alias in 2.x. If there are zero hits, note that in the commit message rather than skipping the check.

- [ ] **Step 5: Retarget the contract guard**

In `tests/test_mcp_sdk_contract.py`, `test_lowlevel_server_exposes_the_api_mcp_server_targets` must now assert the 2.x surface, and `test_call_tool_validates_input_by_default` must be **replaced** (2.x has no such parameter) by a guard that mcpbrain's own validation is wired:

```python
def test_lowlevel_server_exposes_the_api_mcp_server_targets():
    """mcp_server.py's registration layer must actually be callable."""
    import inspect

    from mcp.server import Server

    params = inspect.signature(Server.__init__).parameters
    missing = [
        kw for kw in ("on_list_resources", "on_read_resource", "on_list_tools", "on_call_tool")
        if kw not in params
    ]
    assert not missing, (
        f"mcp.server.Server no longer accepts {missing} — build_server() will fail"
    )


def test_mcpbrain_validates_tool_arguments_itself():
    """mcp 2.x's low-level server validates nothing; we must.

    Replaces the 1.x test_call_tool_validates_input_by_default guard. If this
    ever fails, all 26 tools are accepting unvalidated arguments.
    """
    import pytest

    from mcpbrain.mcp_server import _validate_tool_arguments

    with pytest.raises(ValueError, match="unit_id"):
        _validate_tool_arguments("brain_enrich_push", {})
```

- [ ] **Step 6: Run against the 2.x venv**

Run:
```bash
/tmp/mcp2-dev/bin/python -m pytest tests/test_mcp_sdk_contract.py tests/test_mcp_build_server.py \
  tests/test_mcp_protocol_surface.py tests/test_mcp_server_stdio.py \
  tests/test_mcp_enrich_with_rules.py tests/test_mcp_server.py tests/test_mcp_resources.py \
  tests/test_mcp_capture_tools.py tests/test_mcp_enrich_meeting_tools.py \
  tests/test_mcp_default_owner.py tests/test_mcp_heartbeat.py tests/test_mcp_server_no_native.py -v
```
Expected: all pass. Task 5 must be complete for `test_mcpbrain_validates_tool_arguments_itself` to pass — implement Task 5 before this step if working strictly in order.

- [ ] **Step 7: Commit**

```bash
git add mcpbrain/mcp_server.py tests/test_mcp_sdk_contract.py tests/test_mcp_build_server.py
git commit -m "feat(mcp)!: port the low-level server to the mcp 2.x API

Decorators -> on_* constructor kwargs, handlers take (ctx, params) and
return full types.*Result models, ReadResourceContents -> ReadResourceResult,
camelCase attribute reads -> snake_case. The 26 tool schema literals and all
26 handler bodies are unchanged. Still serves the legacy handshake, so
existing Claude clients are unaffected."
```

---

## Task 5: Re-implement input validation that 2.x dropped

**The one silent-regression risk in this migration.** `mcp` 1.x validated every tool call against its `inputSchema` by default; 2.x's low-level server does not validate at all (`jsonschema` is imported only by the client). Without this task, all 26 tools start accepting malformed arguments — and the failure mode is a handler `KeyError`/`TypeError` surfacing as a generic protocol error, not a useful validation message.

**Files:**
- Modify: `mcpbrain/mcp_server.py`
- Modify: `pyproject.toml` (promote `jsonschema` to a direct dependency)
- Create: `tests/test_mcp_input_validation.py`

**Interfaces:**
- Produces: `_validate_tool_arguments(name: str, arguments: dict) -> None` — raises `ValueError` with a readable message when `arguments` violates the tool's declared `inputSchema`; returns `None` on success. Called at the top of `on_call_tool`.

- [ ] **Step 1: Write the failing test**

```python
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


def test_unknown_tool_is_rejected():
    with pytest.raises(ValueError, match="unknown tool"):
        _validate_tool_arguments("brain_nonexistent", {})


def test_every_tool_has_a_schema_to_validate_against():
    """No tool may silently skip validation for lack of a registered schema."""
    from mcpbrain.mcp_server import tool_schemas

    from tests.test_mcp_protocol_surface import TOOL_CALLS

    assert set(tool_schemas()) == set(TOOL_CALLS)
```

- [ ] **Step 2: Run to verify it fails**

Run: `/tmp/mcp2-dev/bin/python -m pytest tests/test_mcp_input_validation.py -v`
Expected: FAIL with `ImportError: cannot import name '_validate_tool_arguments'`

- [ ] **Step 3: Implement**

The schemas currently live inline in the 26 `types.Tool(...)` literals. To validate against them without duplicating them, hoist the name→schema mapping to module level and have both `on_list_tools` and the validator read it. Add to `mcp_server.py`:

```python
def tool_schemas() -> dict[str, dict]:
    """name -> inputSchema for every tool, the single source both the advertised
    tool list and argument validation read.

    Hoisted out of the tool literals so validation can never drift from what the
    server advertises: mcp 2.x's low-level server does no validation of its own
    (mcp 1.x's call_tool(validate_input=True) default did), so this mapping is
    the only thing standing between a malformed call and a handler KeyError.
    """
    return {
        "brain_search": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "..."},
                "limit": {"type": "integer", "description": "...", "default": 10},
            },
            "required": ["query"],
        },
        # ... one entry per tool, schemas moved verbatim out of the Tool literals ...
        "brain_enrich_push": push_input_schema(),
    }


def _validate_tool_arguments(name: str, arguments: dict) -> None:
    """Validate arguments against the tool's declared inputSchema.

    Raises ValueError with a readable, field-naming message. Deliberately does
    NOT fill in defaults or otherwise mutate `arguments`: brain_enrich_push's
    guards depend on distinguishing an absent field (None) from a present-but-
    empty one ([]), which default-injection would destroy.
    """
    # RULING (Josh, 2026-08-04, during the port): `on_call_tool` must CATCH this
    # ValueError and return `types.CallToolResult(..., isError=True)` rather than
    # letting it propagate. This plan originally mandated a bare raise; that turned
    # out to REGRESS the error path against mcp 1.x, which returned a clean
    # `_make_error_result("Input validation error: …")`. In 2.x a bare ValueError
    # falls through `handler_exception_to_error_data` (which maps only MCPError and
    # pydantic ValidationError) to `logger.exception(...)` + `ErrorData(code=0, …)`
    # — so every malformed model call writes a ~20-line traceback into the MCP log
    # and reads as an internal fault. An isError result also returns the message to
    # the conversation so the model can retry with corrected arguments.
    #
    # Scope the catch to THIS call only, never around the 26-branch dispatch: a
    # blanket `except ValueError` there would swallow a genuine ValueError raised
    # inside a handler and convert a real bug into a tidy error result.
    import jsonschema

    schemas = tool_schemas()
    if name not in schemas:
        raise ValueError(f"unknown tool: {name}")
    try:
        jsonschema.validate(arguments, schemas[name])
    except jsonschema.ValidationError as exc:
        field = ".".join(str(p) for p in exc.absolute_path) or (
            # a `required` violation reports the field in the message, not the path
            exc.message.split("'")[1] if "'" in exc.message else "arguments"
        )
        raise ValueError(f"invalid arguments for {name}: {field}: {exc.message}") from exc
```

Then rewrite `on_list_tools` to build its `types.Tool` list from `tool_schemas()` plus a parallel name→description mapping, so the schema is declared exactly once. Keep every existing description string verbatim — they are the tool documentation the model reads, and `brain_gardener_apply`'s in particular carries a real usage constraint ("Use only from the reference-gardener routine in auto-apply mode").

- [ ] **Step 4: Add `jsonschema` as a direct dependency**

It is currently only transitive (via `mcp`). Depending on it directly is now correct because mcpbrain imports it itself. In `pyproject.toml` dependencies:

```toml
  "jsonschema>=4.20",      # validates tool arguments against inputSchema; mcp 2.x's
                           # low-level server does no validation of its own (1.x's
                           # call_tool(validate_input=True) default did).
```

- [ ] **Step 5: Run the tests**

Run:
```bash
/tmp/mcp2-dev/bin/python -m pytest tests/test_mcp_input_validation.py \
  tests/test_mcp_protocol_surface.py tests/test_mcp_enrich_meeting_tools.py \
  tests/test_mcp_sdk_contract.py -v
```
Expected: all pass. `test_mcp_enrich_meeting_tools.py` is the one that pins `brain_enrich_push`'s four `None`-vs-`[]` guards (`tests/test_mcp_enrich_meeting_tools.py:249-303`) — if it regresses, the validator is mutating arguments.

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/mcp_server.py tests/test_mcp_input_validation.py pyproject.toml
git commit -m "fix(mcp): validate tool arguments ourselves after the 2.x port

mcp 1.x validated every call against inputSchema by default; 2.x's low-level
server validates nothing, so the port would have silently opened all 26 tools
to malformed arguments. Hoists schemas to tool_schemas() so the advertised
list and the validator cannot drift, and never injects defaults (which would
break brain_enrich_push's None-vs-[] guards)."
```

---

## Task 6: Raise the dependency pin and regenerate the lock

**Files:**
- Modify: `pyproject.toml:7-11`
- Modify: `uv.lock`

- [ ] **Step 1: Raise the ceiling**

Replace the `mcp>=1.2,<2` pin (and its now-historical comment) with:

```toml
  "mcp>=2.0,<3",   # 2.x low-level Server: handlers are on_* constructor kwargs
                   # returning full types.*Result models. `mcp` hard-pins
                   # mcp-types==<same version>, so they move in lockstep. The <3
                   # ceiling is deliberate: 2.0 deleted the 1.x decorator API with
                   # no shim, and an unpinned `uv tool install` re-resolve (incl.
                   # daily fleet auto-update) is how that reached users on
                   # 2026-08-04. tests/test_mcp_sdk_contract.py guards the surface.
```

- [ ] **Step 2: Regenerate and inspect the diff**

```bash
uv lock
grep -A2 '^name = "mcp"$' uv.lock
git diff --stat uv.lock
```
Expected: `mcp` resolves to 2.x; roughly **+5 new packages** (`httpx2`, `httpcore2`, `mcp-types`, `opentelemetry-api`, `truststore`). If the diff is far larger, stop and investigate before continuing — dependency weight ships to every fleet machine.

- [ ] **Step 3: Confirm cross-platform resolution, including the open Windows gate**

```bash
uv pip compile pyproject.toml --python-platform windows --quiet -o /dev/null && echo "windows x86_64 OK"
uv pip compile pyproject.toml --python-platform aarch64-pc-windows-msvc --quiet -o /dev/null && echo "windows arm64 OK"
```
Expected: both resolve. A native-wheel gap here would compound the already-open Windows HARDWARE QA GATE from 0.7.97.

- [ ] **Step 4: Reinstall locally and run the impacted tests on the real venv**

```bash
# BOTH extras: a bare `uv sync --extra dev` STRIPS fastembed/onnxruntime from the
# shared dev venv (found during the port), which silently breaks the embedder for
# every other test and for local recall.
uv sync --extra dev --extra daemon
.venv/bin/python -m pytest tests/test_mcp_sdk_contract.py tests/test_mcp_build_server.py \
  tests/test_mcp_protocol_surface.py tests/test_mcp_input_validation.py \
  tests/test_mcp_server_stdio.py tests/test_mcp_enrich_with_rules.py tests/test_mcp_server.py \
  tests/test_mcp_resources.py tests/test_mcp_capture_tools.py \
  tests/test_mcp_enrich_meeting_tools.py tests/test_mcp_default_owner.py \
  tests/test_mcp_heartbeat.py tests/test_mcp_server_no_native.py -v
ruff check mcpbrain/ tests/
```
Expected: all pass, ruff clean.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "build(deps): move to mcp 2.x (>=2.0,<3)

+5 packages (httpx2, httpcore2, mcp-types, opentelemetry-api, truststore).
Resolution verified for windows x86_64 and arm64. Ceiling kept explicit
because 2.0 deleted the 1.x API with no shim."
```

---

## Task 7: Verify against the real Claude Desktop

Tests prove the server is internally correct; only a live handshake proves wire compatibility with the client that broke on 2026-08-04. This is the acceptance gate.

**Files:** none (verification only)

- [ ] **Step 1: Install the local build into the real tool venv**

```bash
uv tool install --force ".[daemon]"
/Users/joshkemp/.local/share/uv/tools/mcpbrain/bin/python -c \
  "import importlib.metadata as m; print('mcp', m.version('mcp'))"
```
Expected: `mcp 2.x`. The `[daemon]` extra is required — a bare `.` install breaks the embedder (missing `fastembed`) and recall returns empty.

- [ ] **Step 2: Smoke-test the handshake directly**

```bash
echo '{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}' \
  | /Users/joshkemp/.local/share/uv/tools/mcpbrain/bin/mcpbrain mcp-server &
PID=$!; sleep 4; kill $PID 2>/dev/null; wait $PID 2>/dev/null
```
Expected: a JSON-RPC result with `"serverInfo":{"name":"mcpbrain","version":"0.7.112"}` — **mcpbrain's version, not the SDK's** (the Task 2 fix) — and no traceback.

- [ ] **Step 3: Restart Claude Desktop and read the log**

```bash
osascript -e 'tell application "Claude" to quit'; sleep 2; open -a Claude; sleep 8
tail -30 ~/Library/Logs/Claude/mcp-server-mcpbrain.log
```
Expected: a full clean handshake — `initialize` → result, `notifications/initialized`, `tools/list` → result, `resources/list` → result. No `Server transport closed unexpectedly`, no traceback.

- [ ] **Step 4: Record which protocol revision was negotiated**

```bash
grep -o '"protocolVersion":"[^"]*"' ~/Library/Logs/Claude/mcp-server-mcpbrain.log | tail -3
grep -c 'server/discover' ~/Library/Logs/Claude/mcp-server-mcpbrain.log
```
Expected: `2025-11-25` (the legacy handshake), `server/discover` count `0`. That is the **success** condition, not a shortfall — it confirms dual-era compatibility. If `server/discover` appears, Claude Desktop has moved to the stateless era and that is newsworthy: note it, because it changes the value of the deferred items below.

- [ ] **Step 5: Exercise the tools from a real client**

In Claude Desktop, confirm a `brain_search` call returns results and an `@`-resource (e.g. `memory.md`) reads correctly. A green test suite plus a clean handshake can still hide a broken result shape that only a real client renders.

- [ ] **Step 6: Record the verification**

No commit. Report the evidence: negotiated protocol revision, `serverInfo.version`, tools working, resources readable. **Do not release** — that is a separate explicit decision (see below).

---

## Task 8: Tool annotations for all 26 tools

Currently zero tools carry `annotations`. Two classifications are not cosmetic: `brain_gardener_apply` is a **synchronous full-file overwrite + `git commit`**, and `brain_enrich_units`/`brain_enrich_claim` *look* like reads but **acquire a 15-minute lease** — a client that treats them as safe retryable reads leaks leases. Today that's communicated only in prose inside a `description` string, where nothing can act on it.

**Files:**
- Modify: `mcpbrain/mcp_server.py`
- Create: `tests/test_mcp_tool_annotations.py`

**Interfaces:**
- Consumes: `tool_schemas() -> dict[str, dict]` from Task 5
- Produces: `tool_annotations() -> dict[str, types.ToolAnnotations]` — one entry per tool; `on_list_tools` attaches these to each `types.Tool`.

Verified API (mcp 2.0.0): `types.ToolAnnotations(title, read_only_hint, destructive_hint, idempotent_hint, open_world_hint)`; `types.Tool` carries `annotations`.

- [ ] **Step 1: Write the failing test**

```python
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


def test_no_tool_touches_the_open_world():
    """Every tool is local store, local files, or loopback HTTP.

    The daemon reaches Gmail/Drive/Calendar, but no MCP tool does directly. If a
    tool ever gains real external reach, this test must be updated deliberately.
    """
    for name, ann in tool_annotations().items():
        assert ann.open_world_hint is False, f"{name} claims external reach"


def test_annotations_are_attached_to_the_advertised_tools(mcp_env):
    """The annotations must reach the wire, not just exist in a dict."""
    from mcpbrain.mcp_server import build_server

    server = build_server(**mcp_env)
    # reuse the list_tools accessor established in tests/test_mcp_build_server.py
    from tests.test_mcp_build_server import list_tools_via_handler

    for tool in list_tools_via_handler(server):
        assert tool.annotations is not None, f"{tool.name} advertised without annotations"
```

Task 2 must expose its list-tools accessor as a reusable `list_tools_via_handler(server) -> list[types.Tool]` helper for this import to work; if it was written inline, refactor it into a module-level function there.

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_mcp_tool_annotations.py -v`
Expected: FAIL with `ImportError: cannot import name 'tool_annotations'`

- [ ] **Step 3: Implement**

```python
def tool_annotations() -> dict:
    """name -> ToolAnnotations for every tool.

    open_world_hint is False everywhere: each tool touches only the local store,
    local files, or the loopback control API. The daemon reaches Gmail/Drive/
    Calendar, but no MCP tool does directly.
    """
    from mcp import types

    def _ro(title: str) -> "types.ToolAnnotations":
        return types.ToolAnnotations(
            title=title, read_only_hint=True, destructive_hint=False,
            idempotent_hint=True, open_world_hint=False,
        )

    def _append(title: str) -> "types.ToolAnnotations":
        # Queued capture: each call appends a new envelope, so two calls create two.
        return types.ToolAnnotations(
            title=title, read_only_hint=False, destructive_hint=False,
            idempotent_hint=False, open_world_hint=False,
        )

    def _idempotent(title: str) -> "types.ToolAnnotations":
        return types.ToolAnnotations(
            title=title, read_only_hint=False, destructive_hint=False,
            idempotent_hint=True, open_world_hint=False,
        )

    def _lease(title: str) -> "types.ToolAnnotations":
        # Reads work but claims a 15-minute lease: not a safe read, not idempotent
        # (two calls hand out different units, by design).
        return types.ToolAnnotations(
            title=title, read_only_hint=False, destructive_hint=False,
            idempotent_hint=False, open_world_hint=False,
        )

    def _destructive(title: str) -> "types.ToolAnnotations":
        return types.ToolAnnotations(
            title=title, read_only_hint=False, destructive_hint=True,
            idempotent_hint=False, open_world_hint=False,
        )

    return {
        # --- read-only (12) ---
        "brain_search": _ro("Search the brain"),
        "brain_read": _ro("Read a chunk"),
        "brain_context": _ro("Profile an entity"),
        "brain_actions": _ro("List open actions"),
        "brain_graph": _ro("Traverse the graph"),
        "brain_proactive": _ro("List findings"),
        "brain_routine": _ro("Get routine instructions"),
        "brain_enrich_pull": _ro("Pull a named unit's payload"),
        "brain_enrich_pending": _ro("Count pending units"),
        "brain_meetings_today": _ro("Today's meetings"),
        "brain_meeting_pack_get": _ro("Get a meeting pack"),
        "brain_draft_context": _ro("Gather drafting context"),
        # --- additive, non-idempotent (6) ---
        "brain_ingest": _append("Capture a note or document"),
        "brain_action_create": _append("Create an action"),
        "brain_decision": _append("Record a decision"),
        "brain_note": _append("Record a note"),
        "brain_memory_write": _append("Write a memory"),
        "brain_draft_save": _append("Save a draft"),
        # --- mutating but idempotent (4) ---
        "brain_action_update": _idempotent("Update an action's status"),
        "brain_meeting_pack_upsert": _idempotent("Upsert a meeting pack"),
        "brain_finding_resolve": _idempotent("Resolve a finding"),
        # atomic tmp.replace(target) keyed on unit_id, so a repeat is a no-op
        "brain_enrich_push": _idempotent("Push extractions for a unit"),
        # --- lease-acquiring (2) ---
        "brain_enrich_units": _lease("Lease a batch of work units"),
        "brain_enrich_claim": _lease("Claim one work unit"),
        # --- destructive (2) ---
        # Overwrites an existing records file with caller-supplied content and
        # git-commits it, synchronously.
        "brain_gardener_apply": _destructive("Apply a gardener edit"),
        # Triggers an unbounded daemon drain+prepare cycle: the widest blast radius
        # of any tool.
        "brain_enrich_advance": _destructive("Wake the daemon to drain"),
    }
```

Then attach them in `on_list_tools`, alongside the schema and description lookups:

```python
        annotations = tool_annotations()
        return types.ListToolsResult(tools=[
            types.Tool(
                name=name,
                description=descriptions[name],
                inputSchema=schema,
                annotations=annotations[name],
            )
            for name, schema in tool_schemas().items()
        ])
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_mcp_tool_annotations.py tests/test_mcp_protocol_surface.py tests/test_mcp_build_server.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/mcp_server.py tests/test_mcp_tool_annotations.py
git commit -m "feat(mcp): annotate all 26 tools with safety hints

Makes two things machine-readable that were only prose before:
brain_gardener_apply overwrites a records file and git-commits synchronously,
and brain_enrich_units/claim acquire a 15-minute lease despite reading like
queries. open_world_hint is false everywhere -- no MCP tool reaches the
internet directly."
```

---

## Task 9: `outputSchema` + structured content, selectively

All 26 tools return `json.dumps(...)` inside a `TextContent`, so every one is a candidate — but **blanket adoption is wrong here**. `structured_content` ships *in addition to* `content`, so declaring it doubles the payload. For `brain_enrich_pull` (an ~11.5 KB rules blob against a 50,000-char soft limit) and `brain_routine` (routine markdown meant to be read verbatim), that's pure overhead on the two largest payloads in the surface.

Adopt it for the tools whose output is a small, stable, machine-consumed envelope; skip the two markdown carriers and document why.

**Files:**
- Modify: `mcpbrain/mcp_server.py`
- Create: `tests/test_mcp_structured_output.py`

**Interfaces:**
- Consumes: `tool_schemas()`, `tool_annotations()`
- Produces: `tool_output_schemas() -> dict[str, dict]` — entries only for tools that declare one; absence is meaningful and tested.

Verified API (mcp 2.0.0): `types.Tool.output_schema`, `types.CallToolResult.structured_content`.

- [ ] **Step 1: Write the failing test**

```python
"""Structured output for the tools whose result is a machine-read envelope.

Deliberately NOT universal: structured_content ships alongside content, so
declaring it on brain_enrich_pull (~11.5KB of rules) or brain_routine (routine
markdown) would double the two largest payloads for no gain -- their consumer is
an LLM reading prose, not a parser.
"""
import json

import pytest

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


@pytest.mark.asyncio
async def test_structured_content_is_returned_and_matches_its_schema(protocol_session):
    """A declared outputSchema must be honoured on the wire, not just advertised."""
    import jsonschema

    session, _ = protocol_session
    result = await session.call_tool("brain_note", {"text": "structured probe"})
    assert not result.is_error
    assert result.structured_content is not None, "declared outputSchema but sent none"
    jsonschema.validate(result.structured_content, tool_output_schemas()["brain_note"])
    # content must still ship, so clients that ignore structured output are unaffected
    assert json.loads(result.content[0].text) == result.structured_content


@pytest.mark.asyncio
async def test_unstructured_tool_sends_no_structured_content(protocol_session):
    session, _ = protocol_session
    result = await session.call_tool("brain_routine", {"name": "enrich"})
    assert not result.is_error
    assert result.structured_content is None
```

Reuse the `protocol_session` fixture from Task 3 (move it into `tests/conftest.py` if it isn't already shared).

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_mcp_structured_output.py -v`
Expected: FAIL with `ImportError: cannot import name 'tool_output_schemas'`

- [ ] **Step 3: Implement**

The capture family shares one envelope shape, so declare it once. Note this task also surfaces a real API smell worth recording but **not** fixing here: success is spelled `queued`/`written`/`ok`/`applied`/`resolved` across different tools. Changing those keys is a breaking change to every caller and belongs in its own change — declare the schemas as they actually are.

```python
def tool_output_schemas() -> dict[str, dict]:
    """name -> outputSchema, for tools whose result is a machine-read envelope.

    Absence is deliberate and tested: brain_routine and brain_enrich_pull carry
    markdown meant to be read verbatim, and structured_content ships *alongside*
    content, so declaring it there would double the two largest payloads in the
    surface for no consumer benefit.

    The inconsistent success keys below (queued/written/ok/applied/resolved) are
    described as-is, not normalised -- renaming them breaks every caller and is a
    separate change.
    """
    _queued = {
        "type": "object",
        "properties": {
            "queued": {"type": "boolean"},
            "path": {"type": "string"},
            "error": {"type": "string"},
        },
        "required": ["queued"],
    }
    return {
        "brain_ingest": _queued,
        "brain_action_create": _queued,
        "brain_action_update": _queued,
        "brain_decision": _queued,
        "brain_note": _queued,
        "brain_memory_write": _queued,
        "brain_enrich_push": {
            "type": "object",
            "properties": {
                "written": {"type": "boolean"},
                "path": {"type": "string"},
                "error": {"type": "string"},
            },
            "required": ["written"],
        },
        "brain_gardener_apply": {
            "type": "object",
            "properties": {
                "applied": {"type": "boolean"},
                "committed": {"type": "boolean"},
                "error": {"type": "string"},
            },
            "required": ["applied"],
        },
        "brain_finding_resolve": {
            "type": "object",
            "properties": {
                "resolved": {"type": "boolean"},
                "error": {"type": "string"},
            },
            "required": ["resolved"],
        },
        "brain_enrich_advance": {
            "type": "object",
            "properties": {"woken": {"type": "boolean"}, "error": {"type": "string"}},
        },
        "brain_enrich_pending": {
            "type": "object",
            "properties": {"pending": {"type": "integer"}},
            "required": ["pending"],
        },
        "brain_draft_save": {
            "type": "object",
            "properties": {"draft_record_id": {"type": "integer"}},
            "required": ["draft_record_id"],
        },
        "brain_meeting_pack_upsert": {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}, "error": {"type": "string"}},
            "required": ["ok"],
        },
    }
```

Attach in `on_list_tools` (`output_schema=` only when declared, so the field stays absent otherwise):

```python
        out_schemas = tool_output_schemas()
        tools = []
        for name, schema in tool_schemas().items():
            kwargs = {}
            if name in out_schemas:
                kwargs["outputSchema"] = out_schemas[name]
            tools.append(types.Tool(
                name=name, description=descriptions[name], inputSchema=schema,
                annotations=annotations[name], **kwargs,
            ))
        return types.ListToolsResult(tools=tools)
```

And in `on_call_tool`, set `structured_content` for exactly those tools. Do it once at the return boundary rather than in all 26 branches — refactor the branches to compute `out` and fall through to a single constructor:

```python
        # single return point: every branch above assigns `out`
        result_kwargs = {}
        if name in tool_output_schemas() and isinstance(out, dict):
            result_kwargs["structured_content"] = out
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(out))],
            **result_kwargs,
        )
```

This restructures `on_call_tool` from 26 `return` statements to 26 assignments plus one return — which also removes the class of bug where a missed branch returns a bare list. Keep `brain_actions`' early unconfigured-owner return as its own explicit `types.CallToolResult`, since its payload is a hardcoded JSON string rather than a dict.

- [ ] **Step 4: Run the tests**

Run:
```bash
.venv/bin/python -m pytest tests/test_mcp_structured_output.py tests/test_mcp_protocol_surface.py \
  tests/test_mcp_tool_annotations.py tests/test_mcp_capture_tools.py \
  tests/test_mcp_enrich_meeting_tools.py -v
```
Expected: all pass. The 26-tool round-trip from Task 3 is the guard that the single-return refactor didn't drop a branch.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/mcp_server.py tests/test_mcp_structured_output.py
git commit -m "feat(mcp): declare outputSchema + structured content for envelope tools

13 tools whose result is a small machine-read envelope now ship
structured_content alongside content. brain_routine and brain_enrich_pull
deliberately excluded: they carry markdown, and structured output would
double the two largest payloads in the surface. Collapses on_call_tool to a
single return point so a branch can no longer return an unwrapped list."
```

---

## Task 10: Expose routines and draft-reply as MCP prompts

`brain_routine` is a hand-rolled `prompts/get`: it JSON-wraps routine markdown and returns it from a *tool*, and its own docstring explains it exists that way because the Cowork/scheduled-task runtime cannot reliably resolve plugin skills. Prompts are the actual primitive for this. Exposing them also reaches **wheel-only installs that don't have the plugin**, plus any non-Claude MCP client — a real capability gain, not a refactor.

`brain_routine` is **retained**. Prompts are conventionally user-initiated, and scheduled tasks self-invoke; removing the tool would break the enrich/gardener/meeting-packs cadences.

**Files:**
- Modify: `mcpbrain/mcp_server.py`
- Create: `tests/test_mcp_prompts.py`

**Interfaces:**
- Produces: `prompt_definitions() -> dict[str, dict]` (name → `{title, description, arguments}`), plus `on_list_prompts`/`on_get_prompt` handlers registered on the `Server`.

Verified API (mcp 2.0.0): `Server(on_list_prompts=..., on_get_prompt=...)`; `types.Prompt(name, title, description, arguments, icons, meta)`; `types.PromptArgument(name, title, description, required)`; `types.PromptMessage(role, content)`; `types.GetPromptResult(description, messages, ...)`; `types.ListPromptsResult(prompts, ...)`.

- [ ] **Step 1: Write the failing test**

```python
"""The 4 routines + draft-reply, exposed as real MCP prompts.

Rationale: brain_routine already serves routine markdown from a *tool* because
the scheduled-task runtime can't resolve plugin skills. Prompts are the right
primitive, and they also reach wheel-only installs that have no plugin. The tool
stays -- scheduled tasks self-invoke and prompts are user-initiated.
"""
import pytest

from mcpbrain.mcp_server import prompt_definitions

ROUTINES = {"enrich", "meeting-packs", "gardener", "reference-gardener"}
EXPECTED = ROUTINES | {"draft-reply"}


def test_all_prompts_are_defined():
    assert set(prompt_definitions()) == EXPECTED


def test_draft_reply_declares_its_arguments():
    args = {a["name"]: a for a in prompt_definitions()["draft-reply"]["arguments"]}
    assert args["email_id"]["required"] is True
    assert args["intent"]["required"] is False


def test_routines_take_no_arguments():
    for name in ROUTINES:
        assert prompt_definitions()[name]["arguments"] == []


@pytest.mark.asyncio
async def test_list_prompts_over_the_protocol(protocol_session):
    session, _ = protocol_session
    names = {p.name for p in (await session.list_prompts()).prompts}
    assert names == EXPECTED


@pytest.mark.asyncio
async def test_get_prompt_returns_the_routine_markdown(protocol_session):
    """The prompt body must be the same text brain_routine serves."""
    import json

    session, _ = protocol_session
    result = await session.get_prompt("enrich", {})
    assert result.messages
    body = result.messages[0].content.text
    assert body.strip(), "empty prompt body"

    tool_out = json.loads(
        (await session.call_tool("brain_routine", {"name": "enrich"})).content[0].text
    )
    assert body == tool_out["instructions"], (
        "prompt and tool disagree on the routine text; they must share one source"
    )


@pytest.mark.asyncio
async def test_get_prompt_interpolates_draft_reply_arguments(protocol_session):
    session, _ = protocol_session
    result = await session.get_prompt(
        "draft-reply", {"email_id": "abc123", "intent": "decline politely"}
    )
    body = result.messages[0].content.text
    assert "abc123" in body and "decline politely" in body


@pytest.mark.asyncio
async def test_get_prompt_rejects_unknown_name(protocol_session):
    session, _ = protocol_session
    with pytest.raises(Exception):
        await session.get_prompt("no-such-prompt", {})


@pytest.mark.asyncio
async def test_missing_required_argument_is_rejected(protocol_session):
    session, _ = protocol_session
    with pytest.raises(Exception):
        await session.get_prompt("draft-reply", {})
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_mcp_prompts.py -v`
Expected: FAIL with `ImportError: cannot import name 'prompt_definitions'`

- [ ] **Step 3: Implement**

The routine bodies must come from `_routine_instructions()` — the same allowlisted reader `brain_routine` uses — so the two can never drift (pinned by the test above). `draft-reply` needs its own body; source it from `plugin/skills/mcpbrain-draft-reply/SKILL.md` so it is not a second copy of that workflow. Read that file to confirm its frontmatter format, then strip the frontmatter and interpolate the arguments.

```python
_ROUTINE_PROMPTS = ("enrich", "meeting-packs", "gardener", "reference-gardener")


def prompt_definitions() -> dict[str, dict]:
    """name -> {title, description, arguments} for every MCP prompt.

    The 4 routines mirror what brain_routine serves (same _routine_instructions
    source, so they cannot drift); draft-reply is the parameterized reply
    pipeline, exposed here so it also reaches installs without the plugin.
    """
    return {
        **{
            name: {
                "title": f"Run the {name} routine",
                "description": (
                    f"Full instructions for the {name} routine, to follow verbatim. "
                    "Self-contained: no skill or command resolution needed."
                ),
                "arguments": [],
            }
            for name in _ROUTINE_PROMPTS
        },
        "draft-reply": {
            "title": "Draft a reply in the owner's voice",
            "description": (
                "Draft a reply to an email using the 4-stage plan/draft/critique/"
                "voice pipeline, then save it to the brain."
            ),
            "arguments": [
                {
                    "name": "email_id",
                    "description": "The message id to reply to.",
                    "required": True,
                },
                {
                    "name": "intent",
                    "description": "Optional steer, e.g. 'decline politely'.",
                    "required": False,
                },
            ],
        },
    }


def _draft_reply_prompt_body(email_id: str, intent: str) -> str:
    """Render the draft-reply prompt from the plugin skill, so there's one source."""
    from mcpbrain import resources_dir  # or the existing packaged-data accessor

    raw = (resources_dir() / "draft-reply.md").read_text(encoding="utf-8")
    body = raw.split("---", 2)[-1].strip() if raw.startswith("---") else raw
    return (
        f"{body}\n\n---\n"
        f"email_id: {email_id}\n"
        f"intent: {intent or '(none given — infer from the thread)'}\n"
    )


async def get_prompt_body(name: str, arguments: dict) -> str:
    """Return a prompt's rendered text, rejecting unknown names and missing args."""
    defs = prompt_definitions()
    if name not in defs:
        raise ValueError(f"unknown prompt: {name}")
    for arg in defs[name]["arguments"]:
        if arg["required"] and not arguments.get(arg["name"]):
            raise ValueError(f"missing required argument for {name}: {arg['name']}")
    if name in _ROUTINE_PROMPTS:
        return _routine_instructions(name)
    return _draft_reply_prompt_body(
        arguments.get("email_id", ""), arguments.get("intent", "")
    )
```

`draft-reply.md` must ship as packaged data. Add it to the `[tool.setuptools.package-data]` `"mcpbrain"` list in `pyproject.toml` (the same mechanism `mcpbrain/routines/` and `enrich_prompt.md` already use) and copy the skill body in, or have `bin/sync_agents.py` keep the two byte-identical the way it already does for `enrich-batch.md`. Prefer extending `sync_agents.py` — a second hand-maintained copy of a workflow is exactly the drift `test_enrich_agent_rules_in_sync` exists to prevent. Add a matching sync test.

Register the handlers in `build_server`:

```python
    async def on_list_prompts(ctx, params) -> types.ListPromptsResult:
        return types.ListPromptsResult(prompts=[
            types.Prompt(
                name=name,
                title=spec["title"],
                description=spec["description"],
                arguments=[
                    types.PromptArgument(
                        name=a["name"], description=a["description"], required=a["required"]
                    )
                    for a in spec["arguments"]
                ],
            )
            for name, spec in prompt_definitions().items()
        ])

    async def on_get_prompt(ctx, params) -> types.GetPromptResult:
        body = await get_prompt_body(params.name, dict(params.arguments or {}))
        return types.GetPromptResult(
            description=prompt_definitions()[params.name]["description"],
            messages=[
                types.PromptMessage(
                    role="user", content=types.TextContent(type="text", text=body)
                )
            ],
        )
```

and add `on_list_prompts=on_list_prompts, on_get_prompt=on_get_prompt` to the `Server(...)` call. The SDK derives the `prompts` capability from the registered handlers, so no capability flag is needed.

- [ ] **Step 4: Run the tests**

Run:
```bash
.venv/bin/python -m pytest tests/test_mcp_prompts.py tests/test_mcp_enrich_meeting_tools.py \
  tests/test_mcp_protocol_surface.py -v
```
Expected: all pass. `test_mcp_enrich_meeting_tools.py` covers `brain_routine` — it must still work, since scheduled tasks depend on it.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/mcp_server.py tests/test_mcp_prompts.py pyproject.toml bin/sync_agents.py
git commit -m "feat(mcp): expose routines + draft-reply as MCP prompts

brain_routine was a hand-rolled prompts/get, serving routine markdown from a
tool because the scheduled-task runtime can't resolve plugin skills. Prompts
are the right primitive and also reach wheel-only installs with no plugin.
Routine bodies come from the same _routine_instructions source as the tool, so
they can't drift. brain_routine is retained -- scheduled tasks self-invoke."
```

---

## Task 11: Advertise and emit `resources/list_changed`

`create_initialization_options()` is called bare today, so `NotificationOptions.resources_changed` is `False` and the capability is never advertised. Meanwhile mcpbrain's *own* tools create new resource files mid-session: `brain_memory_write` adds `memory/<slug>.md`, `brain_gardener_apply` adds `reference/*.md`. A long-lived client never learns they exist.

**Honest scope:** `list_changed` fires when the resource **list** changes (files added or removed). Content changes to an existing URI — `hot.md`, `decisions.md` being rewritten — need `resources/subscribe`, which Anthropic documents as **not supported**. This task therefore delivers "new resources become visible", not "stale content refreshes". `subscribe` is **deliberately not implemented**: it would require per-URI content-hash tracking and a subscription registry for zero current consumers.

**Files:**
- Modify: `mcpbrain/mcp_server.py`
- Create: `tests/test_mcp_resource_notifications.py`

**Interfaces:**
- Consumes: `_resource_entries()`, `build_server`
- Produces: `_resource_fingerprint() -> frozenset[str]`; `watch_resources(server_session, interval_s)` — a background coroutine that polls and emits `send_resource_list_changed()` on change.

Verified API (mcp 2.0.0): `NotificationOptions(prompts_changed, resources_changed, tools_changed)`; `ServerSession.send_resource_list_changed()`; handlers reach it via `ctx.session`.

- [ ] **Step 1: Write the failing test**

```python
"""New resource files must be announced to a connected client.

Scope note: list_changed covers the resource LIST changing (a new memory/<slug>.md
or reference/*.md appearing). Content changes to an existing URI need
resources/subscribe, which Claude does not support and which we deliberately do
not implement -- it would mean per-URI hash tracking for zero consumers.
"""
import asyncio

import pytest

from mcpbrain.mcp_server import _resource_fingerprint, watch_resources


def test_fingerprint_changes_when_a_resource_appears(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    ctx = tmp_path / "context"
    ctx.mkdir()
    before = _resource_fingerprint()
    (ctx / "new-note.md").write_text("# new\n", encoding="utf-8")
    assert _resource_fingerprint() != before


def test_fingerprint_ignores_content_edits(tmp_path, monkeypatch):
    """Content changes are out of scope -- they'd need subscribe, not list_changed."""
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    ctx = tmp_path / "context"
    ctx.mkdir()
    f = ctx / "note.md"
    f.write_text("# one\n", encoding="utf-8")
    before = _resource_fingerprint()
    f.write_text("# two, different content\n", encoding="utf-8")
    assert _resource_fingerprint() == before


@pytest.mark.asyncio
async def test_watcher_notifies_once_per_change(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    ctx = tmp_path / "context"
    ctx.mkdir()

    class _RecordingSession:
        def __init__(self):
            self.calls = 0

        async def send_resource_list_changed(self):
            self.calls += 1

    session = _RecordingSession()
    task = asyncio.create_task(watch_resources(session, interval_s=0.01))
    try:
        await asyncio.sleep(0.05)
        assert session.calls == 0, "notified with no change"
        (ctx / "appeared.md").write_text("x", encoding="utf-8")
        await asyncio.sleep(0.05)
        assert session.calls == 1
        await asyncio.sleep(0.05)
        assert session.calls == 1, "re-notified without a further change"
    finally:
        task.cancel()


@pytest.mark.asyncio
async def test_watcher_survives_a_send_failure(tmp_path, monkeypatch):
    """A dead client must not kill the watcher or crash the server."""
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    (tmp_path / "context").mkdir()

    class _BrokenSession:
        def __init__(self):
            self.attempts = 0

        async def send_resource_list_changed(self):
            self.attempts += 1
            raise ConnectionError("client went away")

    session = _BrokenSession()
    task = asyncio.create_task(watch_resources(session, interval_s=0.01))
    try:
        (tmp_path / "context" / "a.md").write_text("x", encoding="utf-8")
        await asyncio.sleep(0.05)
        assert session.attempts >= 1
        assert not task.done(), "watcher died on a send failure"
    finally:
        task.cancel()


def test_capability_is_advertised(mcp_env):
    from mcpbrain.mcp_server import build_server

    server = build_server(**mcp_env)
    caps = server.create_initialization_options().capabilities
    assert caps.resources is not None
    assert caps.resources.listChanged is True
    assert caps.resources.subscribe is False, (
        "we do not implement subscribe; advertising it would promise updates we "
        "never send"
    )
```

`caps.resources.listChanged` may be `list_changed` on the 2.x models — use whichever the installed SDK exposes and keep both assertions.

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_mcp_resource_notifications.py -v`
Expected: FAIL with `ImportError: cannot import name '_resource_fingerprint'`

- [ ] **Step 3: Implement**

Poll rather than watch the filesystem: it avoids a native `watchfiles` dependency on a fleet with an open Windows gate, and `_resource_entries()` is a handful of globs over a few dozen files. 5 seconds is far below human reaction time for "I just saved a memory".

```python
_RESOURCE_POLL_INTERVAL_S = 5.0


def _resource_fingerprint() -> frozenset[str]:
    """The advertised resource SET, as a comparable value.

    Paths only, deliberately: list_changed is about the list, not content. A
    content edit to an existing URI would need resources/subscribe, which we do
    not implement.
    """
    return frozenset(str(p) for _, p in _resource_entries())


async def watch_resources(session, interval_s: float = _RESOURCE_POLL_INTERVAL_S) -> None:
    """Emit notifications/resources/list_changed when the resource set changes.

    Polls instead of watching the filesystem: no native dependency (relevant with
    the Windows QA gate open), and the set is a few dozen files. Runs for the life
    of the connection; a send failure is logged and swallowed so a disconnecting
    client never takes down the server.
    """
    import asyncio

    previous = _resource_fingerprint()
    while True:
        await asyncio.sleep(interval_s)
        current = _resource_fingerprint()
        if current == previous:
            continue
        previous = current   # update first: a failed send must not re-fire forever
        try:
            await session.send_resource_list_changed()
        except Exception:  # noqa: BLE001 - client may have gone away mid-poll
            _log.debug("resources/list_changed notification failed", exc_info=True)
```

Advertise the capability by passing `NotificationOptions` explicitly. `build_server` should own this so tests can assert it:

```python
    def _init_options():
        from mcp.server.lowlevel.server import NotificationOptions

        # resources_changed=True is backed by watch_resources() below. Do NOT set
        # subscribe: we never send resources/updated, and advertising it would
        # promise per-URI updates we don't implement.
        return server.create_initialization_options(
            notification_options=NotificationOptions(resources_changed=True)
        )

    server.mcpbrain_init_options = _init_options
```

Rather than bolting an attribute onto the SDK object, prefer returning both from `build_server` — change its contract to `build_server(...) -> tuple[Server, Callable[[], InitializationOptions]]` and update Tasks 2/3/8/9/10 call sites, or expose a module-level `init_options(server)` helper. Pick one and apply it consistently; do not leave two ways to build the options.

Start the watcher alongside the server in `main()`:

```python
    async def _run():
        async with mcp.server.stdio.stdio_server() as (r, w):
            async with anyio.create_task_group() as tg:
                # The watcher needs a live session, so it starts from the first
                # handler invocation rather than here; see build_server.
                await server.run(r, w, init_options(server))
```

The watcher needs a `ServerSession`, which only exists once a connection is established. The simplest correct wiring is to start it lazily from the first handler call that has `ctx.session`, guarded so it starts once:

```python
    _watcher_started = False

    async def _ensure_watcher(ctx) -> None:
        nonlocal _watcher_started
        if _watcher_started:
            return
        _watcher_started = True
        import asyncio
        asyncio.create_task(watch_resources(ctx.session))
```

Call `await _ensure_watcher(ctx)` at the top of `on_list_resources` — a client that never lists resources doesn't need change notifications. Add a test that the watcher starts exactly once across repeated `list_resources` calls.

- [ ] **Step 4: Run the tests**

Run:
```bash
.venv/bin/python -m pytest tests/test_mcp_resource_notifications.py tests/test_mcp_resources.py \
  tests/test_mcp_protocol_surface.py tests/test_mcp_build_server.py -v
```
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/mcp_server.py tests/test_mcp_resource_notifications.py
git commit -m "feat(mcp): advertise and emit resources/list_changed

mcpbrain's own tools create new resource files mid-session
(brain_memory_write -> memory/<slug>.md, brain_gardener_apply ->
reference/*.md) and clients never learned they existed. A 5s poll over the
advertised set (no native watcher dep, Windows gate is open) fires
list_changed on set changes. subscribe is deliberately NOT advertised: we
never send resources/updated, and Claude doesn't support it."
```

---

## Task 12: Progress notifications for the slow graph and drafting paths

Claude receives progress notifications and **resets its idle timer** on them even though it reportedly doesn't render them — which is the real benefit: it keeps a slow call from being reaped. Two tools have natural progress boundaries.

**Scope boundary:** `brain_search` — the most-called and most latency-sensitive tool — **cannot** report progress without plumbing it daemon → control API → MCP, which is a **different subsystem**. That is a separate plan (see below), not deferred scope from this one.

**Files:**
- Modify: `mcpbrain/mcp_server.py`
- Create: `tests/test_mcp_progress.py`

**Interfaces:**
- Produces: `_progress_reporter(ctx) -> Callable[[float, float | None, str | None], Awaitable[None]]` — returns an async reporter that no-ops when the client sent no `progressToken`.

Verified API (mcp 2.0.0): `ctx.meta` is a `RequestParamsMeta` TypedDict carrying `progress_token: NotRequired[ProgressToken]`; `ServerSession.send_progress_notification(progress_token, progress, total=None, message=None, related_request_id=None)`.

- [ ] **Step 1: Write the failing test**

```python
"""Progress for the two tools with natural boundaries.

Claude doesn't render progress but does reset its idle timer on it, so this is
about slow calls not being reaped. brain_search is out of scope: it's one opaque
loopback HTTP call and progress would need daemon-side plumbing (separate plan).
"""
import pytest

from mcpbrain.mcp_server import _progress_reporter


class _Session:
    def __init__(self):
        self.sent = []

    async def send_progress_notification(self, progress_token, progress, total=None,
                                        message=None, related_request_id=None):
        self.sent.append((progress_token, progress, total, message))


class _Ctx:
    def __init__(self, meta):
        self.session = _Session()
        self.meta = meta


@pytest.mark.asyncio
async def test_reporter_no_ops_without_a_progress_token():
    """A client that didn't ask for progress must get none."""
    ctx = _Ctx(meta=None)
    report = _progress_reporter(ctx)
    await report(1, 3, "step one")
    assert ctx.session.sent == []


@pytest.mark.asyncio
async def test_reporter_sends_when_a_token_was_supplied():
    ctx = _Ctx(meta={"progress_token": "tok-1"})
    report = _progress_reporter(ctx)
    await report(1, 3, "hop 1 of 3")
    assert ctx.session.sent == [("tok-1", 1, 3, "hop 1 of 3")]


@pytest.mark.asyncio
async def test_reporter_swallows_send_failures():
    """A progress failure must never fail the tool call itself."""
    ctx = _Ctx(meta={"progress_token": "tok-1"})

    async def _boom(*a, **k):
        raise ConnectionError("gone")

    ctx.session.send_progress_notification = _boom
    report = _progress_reporter(ctx)
    await report(1, 3, "hop 1")  # must not raise


@pytest.mark.asyncio
async def test_brain_graph_reports_progress_per_hop(protocol_session_with_progress):
    """hops=3 should produce progress updates, not silence."""
    session, progress = protocol_session_with_progress
    await session.call_tool("brain_graph", {"entity": "Someone", "hops": 3})
    assert progress, "brain_graph sent no progress for a 3-hop traversal"
    assert all(p.total == 3 for p in progress)


@pytest.mark.asyncio
async def test_brain_draft_context_reports_stage_progress(protocol_session_with_progress):
    session, progress = protocol_session_with_progress
    await session.call_tool("brain_draft_context", {"email_id": "nope"})
    messages = [p.message for p in progress]
    assert messages, "brain_draft_context sent no stage progress"
```

`protocol_session_with_progress` is a variant of Task 3's fixture that passes a `progress_callback` to `session.call_tool` and collects the notifications. Read `ClientSession.call_tool`'s signature on the installed SDK for the exact parameter name and shape.

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_mcp_progress.py -v`
Expected: FAIL with `ImportError: cannot import name '_progress_reporter'`

- [ ] **Step 3: Implement**

```python
def _progress_reporter(ctx):
    """Return an async progress reporter, or a no-op if none was requested.

    Progress only exists if the client sent a progressToken in the request's
    _meta. A failure to report must never fail the tool call, so sends are
    swallowed -- progress is advisory.
    """
    token = (ctx.meta or {}).get("progress_token")
    if token is None:
        async def _noop(progress: float, total=None, message=None) -> None:
            return None
        return _noop

    async def _report(progress: float, total=None, message=None) -> None:
        try:
            await ctx.session.send_progress_notification(
                token, progress, total=total, message=message
            )
        except Exception:  # noqa: BLE001 - advisory only; never fail the call
            _log.debug("progress notification failed", exc_info=True)

    return _report
```

Thread it into the two handlers. `make_brain_graph`'s BFS loop (`mcp_server.py:173-188`, `for _ in range(depth)`) gains an optional `on_hop` callback; `make_brain_draft_context` gains an optional `on_stage`. Both default to `None` so every existing caller and test is unaffected:

```python
        if name == "brain_graph":
            report = _progress_reporter(ctx)
            hops = arguments.get("hops", 1)

            async def _on_hop(completed: int) -> None:
                await report(completed, hops, f"hop {completed} of {hops}")

            out = await graph(
                arguments["entity"], hops,
                at_time=arguments.get("at_time"),
                include_invalidated=arguments.get("include_invalidated", False),
                on_hop=_on_hop,
            )
```

For `brain_draft_context`, report the four real stages `draft.draft_context` already moves through (email lookup → voice rules → samples → critique), with the critique stage being the slow one worth announcing.

- [ ] **Step 4: Run the tests**

Run:
```bash
.venv/bin/python -m pytest tests/test_mcp_progress.py tests/test_mcp_server.py \
  tests/test_mcp_protocol_surface.py -v
```
Expected: all pass. `test_mcp_server.py` exercises `make_brain_graph` directly — the new `on_hop` parameter must be optional or it breaks those callers.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/mcp_server.py tests/test_mcp_progress.py
git commit -m "feat(mcp): report progress from brain_graph and brain_draft_context

Claude resets its idle timer on progress notifications, so this keeps slow
calls from being reaped. Per-hop for graph traversal, per-stage for drafting.
No-ops when the client sent no progressToken, and a failed send never fails
the tool call. brain_search is excluded by design: progress there needs
daemon-side plumbing (see the follow-up plan note)."
```

---

## Task 13: Final full-surface verification against real Claude Desktop

Task 7 verified the port didn't break anything. This verifies the *finished* surface — annotations, structured output, prompts, and notifications — against the real client.

**Files:** none (verification only)

- [ ] **Step 1: Reinstall and confirm the SDK**

```bash
uv tool install --force ".[daemon]"
/Users/joshkemp/.local/share/uv/tools/mcpbrain/bin/python -c \
  "import importlib.metadata as m; print('mcp', m.version('mcp'))"
```

- [ ] **Step 2: Restart Claude Desktop and confirm the expanded handshake**

```bash
osascript -e 'tell application "Claude" to quit'; sleep 2; open -a Claude; sleep 8
tail -40 ~/Library/Logs/Claude/mcp-server-mcpbrain.log
```
Expected: `initialize` → result, `notifications/initialized`, `tools/list` → result, `resources/list` → result, **and now `prompts/list` → result**. No traceback, no unexpected transport close.

- [ ] **Step 3: Confirm each new capability from the client side**

- A tool call returns results, and `serverInfo.version` is mcpbrain's version.
- An `@`-resource reads correctly.
- The prompts appear (in Claude Code they surface as slash commands; in Desktop, wherever prompts are exposed).
- Call `brain_memory_write`, then confirm the new `memory/<slug>.md` becomes visible **without reconnecting** — the `list_changed` payoff.
- Call `brain_graph` with `hops=3` and confirm it completes rather than being reaped.

- [ ] **Step 4: Confirm the daemon side is unaffected**

```bash
mcpbrain doctor
```
Expected: `Daemon Connected`, `Records Ready`, and the MCP heartbeat fresh. The MCP server writes `mcp_heartbeat.json` on startup; a stale heartbeat means Desktop isn't actually connecting.

- [ ] **Step 5: Report, do not release**

Report the evidence: negotiated protocol revision, `serverInfo.version`, prompts listed, `list_changed` observed working, progress not reaped. **Releasing is a separate explicit decision** — see the release gate below.

---

## Follow-up plan (genuinely separate subsystem)

**`brain_search` progress + cancellation** needs progress plumbed daemon → control API → MCP server. `brain_search` is one opaque `ControlClient.recall` HTTP call with a 5s timeout, and the daemon is single-process — a cadence pass can pin it for minutes and starve the control API (the 0.7.105 finding). Adding progress means touching `daemon.py`, `control_api.py`, and `control_client.py`, which is a different subsystem with its own risks. It also overlaps the still-unfixed cadence/GIL contention (finding #3) and the stalled-cadence bug noted in 0.7.110. Worth doing, worth doing on its own.

**Explicitly not worth pursuing at all:** Sampling, Roots, and Logging were **deprecated** in the `2026-07-28` revision (SEP-2577, ≥12-month removal window) with suggested migrations "integrate an LLM provider API directly" and "use stderr". Do not build on them. `resources/subscribe` is supported by the SDK but documented as **not supported** by Claude; Task 11 deliberately does not advertise it.

---

## Release gate — NOT part of this plan

Per `CLAUDE.md`, releasing is an all-users action requiring explicit instruction. Two facts to weigh before shipping, which are the reviewer's call, not this plan's:

1. **`mcp` 2.0.0 is 7 days old** (released 2026-07-28; became the default install 2026-08-02). The fleet auto-updates daily from `mcpbrain-dist`, so a release propagates fast and largely unattended.
2. **The Windows HARDWARE QA GATE from 0.7.97 is still OPEN.** Task 6 Step 3 verifies Windows *resolution* only, not a real install.

The `mcp>=1.2,<2` pin shipped in 0.7.112 already stops the outage for existing users, so there is no time pressure forcing this out. Releasing when ready beats releasing fast.

---

## Self-Review

**Spec coverage.** Every research finding maps to a task. Correctness: SDK API break → Tasks 1, 4; `serverInfo` version leak → Task 2 Step 4; untested dispatch layer → Task 3; `brain_search` fallthrough and `None` arguments → Task 3 Step 4; lost input validation → Task 5; `brain_enrich_push`'s generated schema → Global Constraints + Task 5 (`tool_schemas()` calls `push_input_schema()`); `None`-vs-`[]` guards → Task 5 Steps 1, 5; dependency weight and Windows resolution → Task 6 Steps 2, 3; wire compatibility → Tasks 7, 13. Surface upgrades: annotations → Task 8; `outputSchema`/structured content → Task 9; prompts → Task 10; `resources/list_changed` → Task 11; progress → Task 12. Three exclusions are principled, not deferrals, and each states its evidence: `resources/subscribe` (no Claude consumer; would need per-URI hash tracking — Task 11), `brain_search` progress (different subsystem: daemon + control API — Follow-up plan), and Sampling/Roots/Logging (deprecated in the 2026-07-28 revision — Follow-up plan).

**Placeholder scan.** Several steps require reading the installed SDK or an existing file before finalising, and each says so explicitly with the assertion to preserve, rather than hiding a gap: Task 2 Step 1's list-tools accessor (`request_handlers_probe_list_tools` is a named stand-in), Task 3 Step 2's `stdio_client` stderr parameter, Task 10 Step 3's `SKILL.md` frontmatter format, Task 11 Step 1's `listChanged`-vs-`list_changed` field spelling, and Task 12 Step 1's `call_tool` progress-callback parameter. Task 4 Step 2, Task 5 Step 3, and Task 9 Step 3 describe the repeated 26-entry bodies as "moved verbatim" because they are literal moves of existing code, not new content to author.

**Type consistency.** `build_server(store, draft_store, client, home)` is used identically in Tasks 2, 3, 4, 8, 9, 11 — with one deliberate contract change flagged in Task 11 Step 3 (it must also yield the `InitializationOptions` factory, and that step requires picking one approach and updating all call sites rather than leaving two). `tool_schemas() -> dict[str, dict]` (Task 5) is consumed by Tasks 8, 9. `tool_annotations()` (Task 8) and `tool_output_schemas()` (Task 9) are both consumed by `on_list_tools`. `_validate_tool_arguments(name, arguments) -> None` is defined in Task 5 and called in Task 4's `on_call_tool`. `TOOL_CALLS` is defined in Task 3 and imported by Task 5's `test_every_tool_has_a_schema_to_validate_against` and Task 8's annotation-completeness test, which is what keeps all three in sync. `list_tools_via_handler(server)` must be a module-level helper in Task 2 (Task 8 imports it) — noted in Task 8 Step 1.
