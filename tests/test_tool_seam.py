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
   cannot see it, so it gets its own assertion against the dispatch source.

Do not "fix" a failure here by editing the constant. A failure means the seam
moved, which means the adapter's routing table is wrong.
"""

import inspect
import re
from pathlib import Path

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


def test_brain_read_is_store_touching_via_inline_dispatch():
    """brain_read has no factory, so the assertion above cannot see it. It still
    belongs on the daemon side of the adapter, because on_call_tool reaches
    straight into the Store for it. Pinned against the dispatch source: if that
    branch stops touching `store`, brain_read leaves the move-list."""
    src = (Path(__file__).resolve().parent.parent
           / "mcpbrain" / "mcp_server.py").read_text(encoding="utf-8")
    branch = re.search(
        r'if name == "brain_read":\n(.*?)\n\s*elif name ==', src, re.S)
    assert branch, "the brain_read dispatch branch is no longer recognisable"
    assert "store." in branch.group(1), (
        "brain_read's dispatch branch no longer touches the Store -- it may not "
        f"belong behind /api/tool any more. Branch body: {branch.group(1)!r}")


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
