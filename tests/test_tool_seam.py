"""Pins the Store-access seam the thin adapter is built on.

The whole design of Phase 4 rests on one fact: exactly which tools hold a
`Store` handle today. Those 12 move behind `POST /api/tool`; the other 14 stay
in the MCP server process. That fact was a comment in the plan; here it is an
executable assertion, so a tool gaining or losing Store access fails loudly
instead of silently changing which side of the adapter it belongs on.

Two mechanisms, because the codebase has two:

1. 11 of the 12 are `make_brain_*(store, ...)` / `(draft_store, ...)` factories
   in `mcpbrain/tools.py` -- derivable from the signature.
2. `brain_read` is NOT a factory at all. It is dispatched inline in
   `on_call_tool` as a bare `store.get_chunk(arguments["doc_id"])` (see the
   comment above its `declare(...)` in tools.py). It is genuinely
   Store-touching -- the plan's move-list is right -- but signature inspection
   cannot see it, so mechanism 2 parses the dispatch chain instead.

Mechanism 2 is deliberately written as a SWEEP over every branch, not as a
lookup of the one branch we know about. Pinning only `brain_read` would leave
the file blind to the case it exists to prevent: someone adds a 13th tool
dispatched inline with a `store.` call, every assertion here still passes, and
the adapter's routing table silently loses a tool. So the sweep asserts that the
set of inline Store-touching branches is a SUBSET of STORE_TOUCHING -- a new one
has to be a declared move-list member or the test fails.

Do not "fix" a failure here by editing the constant. A failure means the seam
moved, which means the adapter's routing table is wrong.
"""

import inspect
import re
from pathlib import Path

MCP_SERVER = Path(__file__).resolve().parent.parent / "mcpbrain" / "mcp_server.py"

# The plan's "Move behind /api/tool" list, verbatim.
STORE_TOUCHING = {
    "brain_read", "brain_context", "brain_actions", "brain_graph",
    "brain_proactive", "brain_finding_resolve", "brain_draft_context",
    "brain_draft_save", "brain_meetings_today", "brain_meeting_pack_get",
    "brain_meeting_pack_upsert", "brain_gardener_apply",
}

# The one member of STORE_TOUCHING with no make_brain_* factory to inspect.
INLINE_DISPATCHED = {"brain_read"}


def _factory_derived_store_touching() -> set[str]:
    from mcpbrain import tools as ms  # factories live here, not mcp_server, since the seam fix

    actual = set()
    for name in dir(ms):
        if not name.startswith("make_brain_"):
            continue
        params = inspect.signature(getattr(ms, name)).parameters
        if "store" in params or "draft_store" in params:
            actual.add(name.removeprefix("make_"))
    return actual


def test_the_store_touching_set_is_exactly_what_the_plan_assumes():
    """Pins the seam. If a tool gains or loses Store access, this fails loudly
    rather than silently changing which side of the adapter it belongs on."""
    actual = _factory_derived_store_touching()
    expected = STORE_TOUCHING - INLINE_DISPATCHED
    assert actual == expected, f"seam moved: {actual ^ expected}"


def _dispatch_branches() -> dict[str, str]:
    """Every `if/elif name == "<tool>":` branch of on_call_tool, name -> body.

    A branch runs from its own `if`/`elif` line to the next line at the SAME
    indentation that starts a new `elif name ==` or the closing `else:`, so a
    body containing nested if/elif of its own (brain_actions, brain_graph) is
    captured whole rather than truncated at its first inner branch.
    """
    src = MCP_SERVER.read_text(encoding="utf-8").splitlines()
    head = re.compile(r'^(\s+)(?:if|elif) name == "(\w+)":\s*$')

    starts: list[tuple[int, str, str]] = []   # (line index, indent, tool name)
    for i, line in enumerate(src):
        m = head.match(line)
        if m:
            starts.append((i, m.group(1), m.group(2)))

    branches: dict[str, str] = {}
    for n, (i, indent, name) in enumerate(starts):
        end = len(src)
        for j in range(i + 1, len(src)):
            nxt = src[j]
            if not nxt.strip():
                continue
            # Same-indentation `elif name ==` / `else:` terminates this branch.
            if (nxt.startswith(indent) and not nxt[len(indent):len(indent) + 1].isspace()
                    and re.match(r'(?:elif name ==|else:)', nxt[len(indent):])):
                end = j
                break
        else:
            end = starts[n + 1][0] if n + 1 < len(starts) else len(src)
        branches[name] = "\n".join(src[i + 1:end])
    return branches


def _inline_store_touching_branches() -> set[str]:
    """Tools whose on_call_tool branch reaches into a Store handle directly.

    `store.` / `draft_store.` only, not the pre-bound handler closures
    (`draft_save_fn`, `meeting_pack_upsert`, ...) -- those are mechanism 1's
    territory, already covered by their factory signature.
    """
    return {name for name, body in _dispatch_branches().items()
            if re.search(r'\b(?:draft_)?store\.', body)}


def test_the_dispatch_chain_is_parseable():
    """Anti-vacuity guard for the two tests below.

    If `_dispatch_branches` silently matches nothing -- refactor, reformat,
    dict-based dispatch -- then a subset assertion over an empty set passes and
    the sweep becomes a null instrument that reports success forever. So the
    parser's own output is pinned first: it must find every declared tool.
    """
    from mcpbrain import tools  # noqa: F401 -- import populates the registry
    from mcpbrain.tool_registry import registry

    found = set(_dispatch_branches())
    missing = set(registry()) - found
    assert not missing, (
        "the on_call_tool dispatch chain no longer parses -- these declared "
        f"tools have no recognisable branch: {sorted(missing)}. The sweep in "
        "test_no_undeclared_inline_store_touching_branch is blind until this "
        "parser is repaired; do NOT delete it to go green.")


def test_brain_read_is_store_touching_via_inline_dispatch():
    """brain_read has no factory, so mechanism 1 cannot see it. It IS routed
    through /api/tool (Task 9), and this pins the reason it has to be: the local
    fallback branch in on_call_tool reaches straight into the Store for it.

    A failure means that branch stopped touching `store` directly -- so the
    parser below has lost the only tool it can see, and mechanism 2 is silently
    blind. Re-point the parser at wherever brain_read's local read went; do not
    drop brain_read from the move-list to go green."""
    inline = _inline_store_touching_branches()
    assert "brain_read" in inline, (
        "brain_read's dispatch branch no longer touches the Store directly -- the "
        "inline-dispatch parser can no longer see it, so the sweep below is "
        f"blind. Found: {sorted(inline)}")


def test_no_undeclared_inline_store_touching_branch():
    """The sweep. Any tool dispatched inline with a direct Store call must be a
    declared member of the move-list.

    This is the assertion that catches a 13th such tool being added. Pinning
    only brain_read (above) would let a new inline `store.` branch appear with
    every other test in this file still green, while the adapter's routing table
    quietly lost a tool -- the exact failure class this file exists to prevent.
    """
    undeclared = _inline_store_touching_branches() - STORE_TOUCHING
    assert not undeclared, (
        f"{sorted(undeclared)} touch the Store directly in on_call_tool but are "
        "not in STORE_TOUCHING. Either they must move behind /api/tool (add them "
        "to the constant AND to the adapter's routing) or the branch should not "
        "be reaching into the Store.")


def test_every_store_touching_tool_is_a_declared_tool():
    """A name in the move-list that is not in the registry would route nowhere."""
    from mcpbrain import tools  # noqa: F401 -- import populates the registry
    from mcpbrain.tool_registry import registry

    missing = STORE_TOUCHING - set(registry())
    assert not missing, f"move-list names absent from the registry: {sorted(missing)}"


def test_all_24_factories_are_accounted_for():
    """Guards the arithmetic the plan quotes: 24 factories, 11 of them holding a
    Store handle, +1 inline-dispatched = the 12 that move."""
    from mcpbrain import tools as ms

    factories = {n for n in dir(ms) if n.startswith("make_brain_")}
    assert len(factories) == 24, f"expected 24 factories, found {len(factories)}"
    assert len(_factory_derived_store_touching()) == 11
    assert len(STORE_TOUCHING) == 12
