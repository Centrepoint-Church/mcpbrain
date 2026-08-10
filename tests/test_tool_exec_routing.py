"""Routing: flag on -> daemon; flag off or daemon down -> local. Never a crash.

Tasks 9 and 10 of the tool-registry/thin-adapter plan. Three layers are pinned
here, bottom-up, because a break in any one of them is invisible from the others:

1. The FLAG (`config.tool_exec_in_daemon`) -- default ON, local kill-switch.
2. The DAEMON-SIDE executor (`Daemon.call_tool`) + `POST /api/tool` +
   `ControlClient.call_tool` -- the round trip, without any MCP involvement.
3. The MCP-SIDE routing decision in `on_call_tool` -- over a real stdio
   session, with and without a daemon listening.

No pytest-asyncio in this suite (see test_mcp_server_stdio.py): every async body
is driven by a plain `asyncio.run(...)`, so an un-awaited coroutine surfaces as a
RuntimeWarning (the suite is run with `-W error::RuntimeWarning`) instead of a
test that passes while asserting nothing.
"""

import asyncio
import json

import pytest

from mcpbrain.control_api import ControlServer

# A value no local Store read could ever produce, so "did this reach the daemon?"
# is answered by the payload itself rather than by a mock's call count alone.
ROUTED_MARKER = "routed-through-the-daemon"

# The routing table, spelled out rather than derived from the code under test.
# Deriving it would make the partition test tautological; spelled out, a 27th
# tool has to be classified HERE by hand, which is the decision we want forced.
ROUTED_TOOLS = frozenset({
    "brain_read", "brain_context", "brain_actions", "brain_proactive",
    "brain_finding_resolve", "brain_gardener_apply", "brain_draft_save",
    "brain_meetings_today", "brain_meeting_pack_get", "brain_meeting_pack_upsert",
})

# Store-touching, and DELIBERATELY not routed. Both report progress through
# `ctx.session.send_progress_notification` on the live MCP ServerSession, which
# exists only inside one request's own event loop; /api/tool is a single blocking
# round trip with no channel back mid-call, so routing them would silently drop
# the per-hop / per-stage notifications tests/test_mcp_progress.py pins
# end-to-end. This is the ONE exception to "the daemon holds the only Store
# handle", it is scoped to exactly these two names, and the test below exists to
# keep it that size. Fixing it properly needs fine-grained per-hop daemon calls
# -- docs/superpowers/specs/2026-08-10-tool-registry-thin-adapter-followups.md.
PROGRESS_LOCAL_TOOLS = frozenset({"brain_graph", "brain_draft_context"})

# Everything that never needed a Store in the first place, so routing it would
# only make it fail when the daemon is down.
LOCAL_TOOLS = PROGRESS_LOCAL_TOOLS | frozenset({
    # the capture tools: queue a file in MCPBRAIN_HOME, no store
    "brain_ingest", "brain_action_create", "brain_action_update",
    "brain_decision", "brain_note", "brain_memory_write",
    # already daemon-executed, via /api/recall rather than /api/tool
    "brain_search",
    # pure prompt text
    "brain_routine",
    # enrichment spool: plain file I/O under MCPBRAIN_HOME
    "brain_enrich_units", "brain_enrich_pull", "brain_enrich_push",
    "brain_enrich_advance", "brain_enrich_claim", "brain_enrich_pending",
})


def _daemon(tmp_path, store=None):
    """A real Daemon with a real (empty) Store and no embedder.

    `Daemon.call_tool` touches neither the embedder nor the sync loop, so None
    is a faithful stand-in for the former; an explicit SingleWriterLock keeps
    the lock file inside tmp_path rather than the developer's real app dir.
    """
    from mcpbrain.daemon import Daemon, SingleWriterLock
    from mcpbrain.store import Store

    if store is None:
        store = Store(tmp_path / "brain.sqlite3", dim=4, read_only=False)
        store.init()
    return store, Daemon(store, None, services={},
                         lock=SingleWriterLock(tmp_path / "d.lock"))


class _RoutingDaemon:
    """Minimal daemon stand-in for the control API's /api/tool handler.

    Mirrors test_mcp_server_stdio.py's `_FakeDaemon` (which stubs only
    `.search()` for /api/recall): only the one method the endpoint under test
    reaches is implemented, and it returns ROUTED_MARKER so the assertion is
    about where the answer came FROM, not merely that a call happened.
    """

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def call_tool(self, name: str, arguments: dict):
        self.calls.append((name, arguments))
        return {"doc_id": arguments.get("doc_id", ""), "text": ROUTED_MARKER}


# --- 1. the flag ------------------------------------------------------------

def test_flag_defaults_on(tmp_path):
    from mcpbrain import config
    assert config.tool_exec_in_daemon(str(tmp_path)) is True


def test_local_kill_switch_wins(tmp_path):
    """An install must always be able to shut this off for itself."""
    from mcpbrain import config
    config.write_config(str(tmp_path), {"tool_exec_in_daemon": False})
    assert config.tool_exec_in_daemon(str(tmp_path)) is False


def test_the_org_overlay_can_turn_it_off_fleet_wide(tmp_path):
    """The flag is a fleet_flag, not a bare config read: an org-config overlay
    must be able to disable it everywhere without touching each install."""
    from mcpbrain import config
    config.write_config(str(tmp_path), {"org_config": {"flags": {"tool_exec_in_daemon": False}}})
    assert config.tool_exec_in_daemon(str(tmp_path)) is False


# --- 2. the daemon-side executor and its endpoint ---------------------------

def test_daemon_executes_brain_read_against_its_own_store(tmp_path):
    """The daemon half of the seam: it reads the chunk with ITS store handle."""
    store, daemon = _daemon(tmp_path)
    store.upsert_chunk("d-1", "the annual budget review", "h1", {"source_type": "gmail"})
    out = daemon.call_tool("brain_read", {"doc_id": "d-1"})
    assert out["text"] == "the annual budget review"
    assert out["metadata"]["source_type"] == "gmail"


def test_daemon_refuses_a_tool_it_does_not_route(tmp_path):
    """A capture tool (or a typo) must fail loudly, not silently return None.

    The six filesystem-only capture tools deliberately stay in the MCP server;
    if one ever arrives here it means the routing table and the seam disagree.
    """
    _store, daemon = _daemon(tmp_path)
    with pytest.raises(ValueError, match="brain_note"):
        daemon.call_tool("brain_note", {"text": "x"})


def test_daemon_revalidates_arguments_defensively(tmp_path):
    """Validation lives at the MCP boundary; the daemon re-checks anyway, so a
    disagreement between the two is a loud ValueError rather than a KeyError
    from deep inside a handler."""
    _store, daemon = _daemon(tmp_path)
    with pytest.raises(ValueError, match="brain_read"):
        daemon.call_tool("brain_read", {})


def test_daemon_awaits_an_async_handler(tmp_path):
    """Every routed tool except brain_read is an `async def` handler.

    brain_read alone is a bare synchronous `store.get_chunk`, so without this
    the await path would ship untested and Task 10 would discover it broken.
    """
    _store, daemon = _daemon(tmp_path)

    async def _handler(arguments):
        return {"awaited": arguments["doc_id"]}

    daemon._routed_tool_handlers()["brain_read"] = _handler
    assert daemon.call_tool("brain_read", {"doc_id": "d-9"}) == {"awaited": "d-9"}


def test_the_endpoint_round_trips_through_the_control_client(tmp_path):
    """POST /api/tool + ControlClient.call_tool, with no MCP server involved."""
    from mcpbrain.control_client import ControlClient

    daemon = _RoutingDaemon()
    srv = ControlServer(daemon, home=str(tmp_path))
    srv.start()
    try:
        out = ControlClient(home=tmp_path).call_tool("brain_read", {"doc_id": "d-2"})
    finally:
        srv.stop()
    assert out["text"] == ROUTED_MARKER
    assert daemon.calls == [("brain_read", {"doc_id": "d-2"})]


def test_a_daemon_side_failure_is_not_reported_as_an_absent_daemon(tmp_path):
    """A reached-but-failing daemon must NOT look like a missing one.

    urllib raises HTTPError (a URLError subclass) for a 4xx/5xx, which
    `_request` maps to DaemonUnavailable for every other endpoint. If
    call_tool inherited that, a genuine handler fault would be reported to the
    model as "the daemon is not running" and the real cause would be lost.
    """
    from mcpbrain.control_client import ControlClient, DaemonUnavailable, ToolExecutionError

    class _Boom:
        def call_tool(self, name, arguments):
            raise RuntimeError("handler exploded")

    srv = ControlServer(_Boom(), home=str(tmp_path))
    srv.start()
    try:
        with pytest.raises(ToolExecutionError, match="handler exploded"):
            ControlClient(home=tmp_path).call_tool("brain_read", {"doc_id": "d-3"})
    finally:
        srv.stop()
    assert not issubclass(ToolExecutionError, DaemonUnavailable)


def test_a_refused_tool_stays_a_tool_failure(tmp_path):
    """The counterweight to the parametrised test below: /api/tool's own 400.

    A ValueError out of `Daemon.call_tool` (unknown tool, or arguments the two
    halves disagree about) is a named, actionable refusal OF the call -- 400 is
    therefore deliberately not in _TRANSPORT_STATUSES, so this must not be
    softened into "the daemon is not running" along with them.
    """
    from mcpbrain.control_client import ControlClient, ToolExecutionError

    class _Refuses:
        def call_tool(self, name, arguments):
            raise ValueError(f"{name} is not executed in the daemon")

    srv = ControlServer(_Refuses(), home=str(tmp_path))
    srv.start()
    try:
        with pytest.raises(ToolExecutionError, match="not executed in the daemon"):
            ControlClient(home=tmp_path).call_tool("brain_note", {"text": "x"})
    finally:
        srv.stop()


@pytest.mark.parametrize("status,body", [
    # The auth gate: bare headers, NO body. `exc.read() or b"{}"` parses that
    # into a clean `{}`, which is why a body-shape check alone is not enough --
    # `{}` is indistinguishable from a handler error with no message.
    (401, b""),
    # Version skew: an MCP server on a newer wheel calling a daemon with no
    # /api/tool route. A well-formed error envelope that no handler produced.
    (404, b'{"error": "not found"}'),
    (403, b""),                            # the non-loopback guard
    (413, b'{"error": "body too large"}'),  # over the 1 MiB body cap
])
def test_a_pre_handler_rejection_is_unavailable_not_a_tool_failure(
        tmp_path, monkeypatch, status, body):
    """401/403/404/413 never reached a handler, so they are NOT ToolExecutionError.

    Every one of them is reachable in production without any handler bug: a
    daemon restart that rewrites control_port/control_token non-atomically (401),
    a non-loopback caller (403), an MCP server newer than the daemon it calls
    (404), an argument payload over the control API's 1 MiB cap (413). None has a
    local-execution analogue, and ToolExecutionError is caught NOWHERE -- so
    misclassifying these would propagate a raised exception out of on_call_tool
    and put a code=0 traceback in the fleet's MCP log, instead of the readable
    "daemon not reachable" isError result `run_tool` produces for
    DaemonUnavailable.
    """
    import io
    import urllib.error

    from mcpbrain.control_client import ControlClient, DaemonUnavailable, ToolExecutionError

    (tmp_path / "control_port").write_text("9")
    (tmp_path / "control_token").write_text("t")

    def _raise(*_a, **_kw):
        raise urllib.error.HTTPError("http://127.0.0.1:9/api/tool", status,
                                     "rejected", {}, io.BytesIO(body))

    monkeypatch.setattr("urllib.request.urlopen", _raise)
    with pytest.raises(DaemonUnavailable) as caught:
        ControlClient(home=tmp_path).call_tool("brain_read", {"doc_id": "d-5"})
    assert not isinstance(caught.value, ToolExecutionError)


def test_the_client_reports_an_absent_daemon_as_unavailable(tmp_path):
    """No control_port/control_token in home == daemon not running."""
    from mcpbrain.control_client import ControlClient, DaemonUnavailable

    with pytest.raises(DaemonUnavailable):
        ControlClient(home=tmp_path).call_tool("brain_read", {"doc_id": "d-4"})


def test_a_stuck_handler_is_a_timeout_not_an_absent_daemon(tmp_path, monkeypatch):
    """A running daemon that never answers must not be reported as missing.

    `socket.timeout` IS `TimeoutError` IS `OSError`, so the read timeout lands in
    the same handler as ECONNREFUSED unless it is classified first -- and Task 10
    routed two tools that can genuinely block past TOOL_CALL_TIMEOUT_S:
    brain_gardener_apply shells out to git with NO timeout (indefinite on a stale
    records-repo `.git/index.lock`, as drain.py's own comment documents) and
    brain_meetings_today makes a live Calendar call.

    The blocked handler thread here IS the production shape: ThreadingHTTPServer
    means one wedged call does not wedge the control API, which is exactly why
    "the daemon is not running" would be a false diagnosis.
    """
    import threading

    from mcpbrain.control_client import ControlClient, DaemonTimeout, DaemonUnavailable

    released = threading.Event()

    class _Stuck:
        def call_tool(self, name, arguments):
            released.wait(30)   # like git on a stale index.lock; bounded so a
            return {"applied": True}   # failing test cannot hang the suite

    monkeypatch.setattr(ControlClient, "TOOL_CALL_TIMEOUT_S", 0.5)
    srv = ControlServer(_Stuck(), home=str(tmp_path))
    srv.start()
    try:
        with pytest.raises(DaemonTimeout) as caught:
            ControlClient(home=tmp_path).call_tool(
                "brain_gardener_apply", _MINIMAL_ARGS["brain_gardener_apply"])
    finally:
        released.set()
        srv.stop()

    # Still a DaemonUnavailable: the tray, is_running() and recall() all act on
    # "did I get an answer?" alone, and turning this into a sibling class would
    # convert their `except DaemonUnavailable` into an uncaught raise.
    assert isinstance(caught.value, DaemonUnavailable)
    assert "0.5s" in str(caught.value), caught.value


# --- 3. the MCP-side routing decision, over a real stdio session ------------

def test_routed_tool_reaches_the_daemon(protocol_session):
    """brain_read with the flag on must produce a daemon call, not a local Store read."""
    daemon = _RoutingDaemon()
    srv = ControlServer(daemon, home=str(protocol_session.home))
    srv.start()

    async def _body():
        async with protocol_session() as (session, stderr_path):
            result = await session.call_tool("brain_read", {"doc_id": "d-routed"})
            assert not result.is_error, (
                f"routed brain_read errored: {[c.text for c in result.content]}")
            return json.loads(result.content[0].text)

    try:
        payload = asyncio.run(_body())
    finally:
        srv.stop()

    # The store in this home is empty, so a LOCAL read would have returned null.
    assert payload["text"] == ROUTED_MARKER, f"brain_read did not route: {payload}"
    assert daemon.calls == [("brain_read", {"doc_id": "d-routed"})]


def test_daemon_down_returns_isError_not_a_crash(protocol_session):
    """A store tool with no daemon must return a readable error result.

    The six capture tools stay local precisely so they keep working here; a
    store tool cannot, but it must degrade to isError with a message naming the
    daemon, never an unhandled exception.
    """
    async def _run():
        async with protocol_session() as (session, stderr_path):
            result = await session.call_tool("brain_read", {"doc_id": "anything"})
            assert result.is_error, f"expected isError with no daemon; got {result}"
            text = " ".join(c.text for c in result.content).lower()
            assert "daemon" in text, f"the error must name the daemon; got: {text}"
            assert "traceback" not in text, f"an unhandled exception leaked: {text}"

    asyncio.run(_run())


def test_capture_tools_still_work_with_no_daemon(protocol_session):
    """The reason the seam is 'Store access' and not 'everything'."""
    async def _run():
        async with protocol_session() as (session, stderr_path):
            result = await session.call_tool("brain_note", {"text": "still working"})
            assert not result.is_error, [c.text for c in result.content]
            payload = json.loads(result.content[0].text)
            assert payload.get("queued") is True, payload

    asyncio.run(_run())


def test_the_kill_switch_restores_the_local_read(protocol_session):
    """Flag off with no daemon: brain_read must work from the local Store.

    This is the fallback path the default-ON flag is a kill switch FOR, and the
    only test that proves the local branch still executes.
    """
    from mcpbrain import config
    from mcpbrain.embed import embedder_dim
    from mcpbrain.store import Store

    home = protocol_session.home
    config.write_config(str(home), {"tool_exec_in_daemon": False})
    Store(home / "brain.sqlite3", dim=embedder_dim("bge-small"),
          read_only=False).upsert_chunk("d-local", "read locally", "h1", {})

    async def _run():
        async with protocol_session() as (session, stderr_path):
            result = await session.call_tool("brain_read", {"doc_id": "d-local"})
            assert not result.is_error, [c.text for c in result.content]
            payload = json.loads(result.content[0].text)
            assert payload["text"] == "read locally", payload

    asyncio.run(_run())


# --- 4. Task 10: the rest of the Store-touching tools -----------------------

def _seeded(tmp_path, monkeypatch):
    """A real Daemon over a real Store with one row per routed read.

    MCPBRAIN_HOME is redirected FIRST and unconditionally: the daemon resolves
    `home` for brain_draft_save / brain_meetings_today from `app_dir()`, and
    brain_meetings_today's dashboard.calendar_today(home) loads Google
    credentials from that home -- so without this, a unit test on a developer
    box would authenticate with the real token and issue a live Calendar
    request. tmp_path has no token, so it degrades to [] as intended.
    """
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    store, daemon = _daemon(tmp_path)
    store.upsert_chunk("d-1", "the annual budget review", "h1", {"source_type": "gmail"})
    store.upsert_entity("sam", "Sam Taylor", "person")
    store.add_unified_action(text="book Hall B", owner="Sam Taylor", status="open")
    store.record_finding("memory_promotion", "ref-1", summary="a durable preference",
                         severity="info")
    store.upsert_meeting_pack(event_id="ev-1", event_title="Standup",
                              event_date="2026-08-10", pack_text="# pack",
                              attendees=["Sam Taylor"], cowork_session="t",
                              context_hash="hash-1")
    return store, daemon


def test_the_routing_table_partitions_the_whole_advertised_surface():
    """Every advertised tool is classified: routed, or local for a stated reason.

    The point is the 27th tool. A tool added to the registry without a routing
    decision would otherwise just quietly execute in the MCP server (the `else`
    of every `if config.tool_exec_in_daemon(...)`) and re-open the
    multiple-writable-handle hole this phase closed, with nothing failing.
    """
    from mcpbrain import tools as _tools  # noqa: F401 -- populates the registry
    from mcpbrain.tool_registry import registry

    advertised = set(registry())
    assert not (ROUTED_TOOLS & LOCAL_TOOLS), ROUTED_TOOLS & LOCAL_TOOLS
    assert ROUTED_TOOLS | LOCAL_TOOLS == advertised, (
        f"unclassified={advertised - (ROUTED_TOOLS | LOCAL_TOOLS)} "
        f"stale={(ROUTED_TOOLS | LOCAL_TOOLS) - advertised}"
    )


def test_the_daemon_routes_exactly_the_routed_tools(tmp_path, monkeypatch):
    """The daemon's handler table IS the routing table -- no more, no less."""
    _store, daemon = _seeded(tmp_path, monkeypatch)
    assert set(daemon._routed_tool_handlers()) == set(ROUTED_TOOLS)


def test_the_progress_tools_are_refused_by_the_daemon(tmp_path, monkeypatch):
    """The exception is enforced on BOTH sides, not just by the MCP dispatch.

    If a future edit routes one of these from the MCP side, this makes it a loud
    400 ("tool not routed to the daemon") instead of a call that succeeds while
    silently emitting no progress -- which is exactly the failure a client would
    never report, because a dropped notification looks like a slow tool.
    """
    _store, daemon = _seeded(tmp_path, monkeypatch)
    for name in sorted(PROGRESS_LOCAL_TOOLS):
        with pytest.raises(ValueError, match=name):
            daemon.call_tool(name, {"entity": "Sam Taylor", "email_id": "m1"})


def test_daemon_executes_brain_context(tmp_path, monkeypatch):
    _store, daemon = _seeded(tmp_path, monkeypatch)
    out = daemon.call_tool("brain_context", {"entity": "Sam Taylor"})
    assert out["entity"]["name"] == "Sam Taylor", out
    # `mode` and `community_id` are separate kwargs, so a mapping that dropped
    # `mode` would still pass the profile assertion above.
    assert daemon.call_tool("brain_context", {"mode": "communities"}) == []


def test_daemon_executes_brain_actions(tmp_path, monkeypatch):
    _store, daemon = _seeded(tmp_path, monkeypatch)
    out = daemon.call_tool("brain_actions", {"owner": "Sam Taylor"})
    assert [a["text"] for a in out] == ["book Hall B"], out
    # `status` must be forwarded, not hardcoded to the default.
    assert daemon.call_tool("brain_actions", {"owner": "Sam Taylor",
                                             "status": "done"}) == []


def test_daemon_executes_brain_proactive(tmp_path, monkeypatch):
    _store, daemon = _seeded(tmp_path, monkeypatch)
    out = daemon.call_tool("brain_proactive", {})
    assert [f["ref_id"] for f in out] == ["ref-1"], out
    # both filter kwargs, so neither can be silently dropped
    assert daemon.call_tool("brain_proactive", {"finding_type": "other"}) == []
    assert daemon.call_tool("brain_proactive", {"severity": "critical"}) == []


def test_daemon_executes_brain_finding_resolve(tmp_path, monkeypatch):
    """A WRITE through the daemon's own handle, verified in the store."""
    store, daemon = _seeded(tmp_path, monkeypatch)
    fid = store.open_findings()[0]["id"]
    out = daemon.call_tool("brain_finding_resolve",
                           {"finding_id": fid, "outcome": "promoted",
                            "note": "wrote the memory file"})
    assert out == {"resolved": True, "finding_id": fid, "outcome": "promoted"}, out
    assert store.get_finding(fid)["resolved_at"], "the write did not land"


def test_daemon_executes_brain_draft_save(tmp_path, monkeypatch):
    store, daemon = _seeded(tmp_path, monkeypatch)
    out = daemon.call_tool("brain_draft_save", {
        "email_id": "m1", "thread_id": "t1", "intent": "reply",
        "final_draft": "Happy to help.",
    })
    saved = store.get_draft(out["draft_record_id"])
    # Every required argument checked in the stored row: a mapping that crossed
    # thread_id and intent would still return a draft_record_id.
    assert (saved["email_id"], saved["thread_id"], saved["intent"],
            saved["draft_text"]) == ("m1", "t1", "reply", "Happy to help.")


def test_daemon_executes_brain_meeting_pack_get(tmp_path, monkeypatch):
    _store, daemon = _seeded(tmp_path, monkeypatch)
    out = daemon.call_tool("brain_meeting_pack_get", {"event_id": "ev-1"})
    assert out["pack_text"] == "# pack" and out["context_hash"] == "hash-1", out
    assert daemon.call_tool("brain_meeting_pack_get",
                            {"event_id": "nope"}) == {"found": False}


def test_daemon_executes_brain_meeting_pack_upsert(tmp_path, monkeypatch):
    store, daemon = _seeded(tmp_path, monkeypatch)
    assert daemon.call_tool("brain_meeting_pack_upsert", {
        "event_id": "ev-2", "event_title": "Elders", "event_date": "2026-08-11",
        "pack_text": "# elders", "attendees": ["Sam Taylor"],
        "context_hash": "hash-2",
    }) == {"ok": True}
    row = store.get_meeting_pack("ev-2")
    # context_hash is the whole point of the tool (the next hourly run skips on
    # it) and attendees is the one non-scalar argument, so both are easy to drop
    # in an argument->kwargs mapping and neither would fail {"ok": True}.
    assert (row["event_title"], row["event_date"], row["pack_text"],
            row["context_hash"]) == ("Elders", "2026-08-11", "# elders", "hash-2")
    assert json.loads(row["attendees"]) == ["Sam Taylor"]


def test_daemon_executes_brain_meetings_today(tmp_path, monkeypatch):
    """No Google token in this home, so calendar_today degrades to []."""
    _store, daemon = _seeded(tmp_path, monkeypatch)
    assert daemon.call_tool("brain_meetings_today", {}) == []


def test_daemon_executes_brain_gardener_apply(tmp_path, monkeypatch):
    """The attribution guard, which is the only part that reads the store.

    Chosen over the happy path deliberately: a successful apply needs a real git
    records repo, while this branch exercises the daemon's store handle AND four
    of the seven arguments (lane, asserts_person_role, attribution_source,
    attribution_doc_id) in one call, with no write.

    Note the handler's `unknown lane` branch is NOT testable from here: `lane`
    carries an enum in the inputSchema, so an unknown value is refused by
    Daemon.call_tool's own re-validation before the handler runs. `lane` is
    instead shown to be forwarded by the two valid values taking visibly
    different paths.
    """
    _store, daemon = _seeded(tmp_path, monkeypatch)
    out = daemon.call_tool("brain_gardener_apply", {
        "lane": "context", "filename": "identity.md", "content": "x",
        "asserts_person_role": True, "attribution_source": "signature",
        "attribution_quote": "the annual budget review",
        "attribution_doc_id": "d-missing",
    })
    assert out["applied"] is False and "d-missing" in out["error"], out
    # The same call against a doc that DOES exist gets past the store lookup and
    # into quote verification -- proving attribution_doc_id reached get_chunk.
    out = daemon.call_tool("brain_gardener_apply", {
        "lane": "context", "filename": "identity.md", "content": "x",
        "asserts_person_role": True, "attribution_source": "signature",
        "attribution_quote": "not in that chunk at all",
        "attribution_doc_id": "d-1",
    })
    assert out["applied"] is False and "d-missing" not in out["error"], out
    # lane='reference' takes the other branch entirely, and its error carries the
    # filename -- so this one call pins both of those arguments.
    out = daemon.call_tool("brain_gardener_apply", _MINIMAL_ARGS["brain_gardener_apply"])
    assert out["applied"] is False, out
    assert "reference/drift-check.md" in out["error"], out


# --- 5. the observable success criterion: no Store in the MCP server ---------

# Smallest schema-valid argument set per routed tool, shared by section 5 and
# section 6 so the two cannot drift. brain_actions carries an explicit owner
# because an empty one hits on_call_tool's "Install not configured" early return
# and never reaches the routing decision at all.
_MINIMAL_ARGS = {
    "brain_read": {"doc_id": "d-1"},
    "brain_context": {"entity": "Sam Taylor"},
    "brain_actions": {"owner": "Sam Taylor"},
    "brain_proactive": {},
    "brain_finding_resolve": {"finding_id": 1, "outcome": "dismissed"},
    # lane='reference' with a file that does not exist: the handler raises
    # FileNotFoundError before writing anything, so this is schema-valid AND
    # side-effect-free (which section 6 needs, since it calls twice).
    "brain_gardener_apply": {"lane": "reference", "filename": "drift-check.md",
                             "content": "x"},
    "brain_draft_save": {"email_id": "m1", "thread_id": "t1", "intent": "reply",
                         "final_draft": "hi"},
    "brain_meetings_today": {},
    "brain_meeting_pack_get": {"event_id": "ev-1"},
    "brain_meeting_pack_upsert": {"event_id": "ev-2", "event_title": "Elders",
                                  "event_date": "2026-08-11", "pack_text": "# e"},
}


class _NoProgressCtx:
    """A request context that asked for NO progress.

    `meta = None` is the shape `_progress_reporter` reads for "the client never
    supplied a progressToken", so it returns its genuine no-op and `.session` is
    never touched. That is what makes the two progress tools drivable without a
    live ServerSession -- ctx=None is NOT usable for them (`(ctx.meta or {})`
    raises AttributeError on None), which is why this exists rather than the None
    the other dispatch-layer tests pass.
    """

    meta = None
    session = None


def _dispatch(server):
    """Invoke the registered tools/call handler directly, with no transport."""
    from mcp import types

    entry = server.get_request_handler("tools/call")

    async def _call(name, arguments):
        return await entry.handler(
            _NoProgressCtx(),
            types.CallToolRequestParams(name=name, arguments=arguments))

    return _call


class _CountingStore:
    """Records every Store CONSTRUCTION, by read_only-ness, and opens nothing.

    Counting constructions is a stronger assertion than counting write
    connections, not a weaker one: `Store.__init__` is inert (it opens nothing --
    `_connect` does that per operation), so "no Store object was constructed"
    necessarily implies "no connection of any kind was opened through one".
    """

    def __init__(self):
        self.opened: list[bool] = []   # read_only flag per construction

    def __call__(self, *_a, read_only=True, **_kw):
        self.opened.append(read_only)
        return self

    def get_chunk(self, doc_id):       # the one method a fallback path may hit
        return {"doc_id": doc_id, "text": "read from a local Store"}

    @property
    def writable(self) -> list[bool]:
        return [ro for ro in self.opened if ro is False]


def _lazy_env(tmp_path, monkeypatch):
    """build_server wired with mcp_server.store_handles(), plus a counter.

    This is the real production wiring -- main() calls exactly store_handles()
    -- with `mcpbrain.store.Store` swapped for a counter, which is why the
    assertions below are about the process's actual behaviour rather than about
    a shape invented for the test.
    """
    from mcpbrain import mcp_server
    from mcpbrain import store as _store_mod
    from mcpbrain.control_client import ControlClient

    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    counter = _CountingStore()
    monkeypatch.setattr(_store_mod, "Store", counter)
    store, draft_store = mcp_server.store_handles()
    server = mcp_server.build_server(store, draft_store,
                                     ControlClient(home=tmp_path), str(tmp_path))
    return counter, _dispatch(server)


def test_no_store_handle_when_routing_is_enabled(tmp_path, monkeypatch):
    """The observable success criterion for the thin adapter.

    Flag ON: the MCP server opens no Store for any routed tool -- not even
    read-only -- because build_server's registration pass performs no Store
    access and run_tool never calls the local branch. Flag OFF (kill switch) it
    must, because the local fallback needs one; that direction is the test below.

    NOT "no Store at all": brain_graph and brain_draft_context never route (see
    PROGRESS_LOCAL_TOOLS), so they still resolve a handle regardless of the flag.
    The point of this test is that the exception is that size and no larger.
    """
    counter, call = _lazy_env(tmp_path, monkeypatch)

    daemon = _RoutingDaemon()
    srv = ControlServer(daemon, home=str(tmp_path))
    srv.start()
    try:
        async def _body():
            for name in sorted(ROUTED_TOOLS):
                await call(name, _MINIMAL_ARGS[name])
        asyncio.run(_body())
    finally:
        srv.stop()

    assert [n for n, _a in daemon.calls] == sorted(ROUTED_TOOLS), (
        f"not every routed tool reached the daemon: {daemon.calls}")
    assert counter.opened == [], (
        f"the MCP server opened {len(counter.opened)} Store handle(s) while "
        f"routing (read_only flags: {counter.opened})")


def test_the_exception_tools_hold_a_read_only_handle_either_way(tmp_path, monkeypatch):
    """brain_graph / brain_draft_context resolve a store, and it is READ-ONLY.

    They cannot route, so a local handle is unavoidable -- but a WRITABLE one
    would be, because a writable connection in this process is the whole
    condition Phase 4 removes. brain_draft_context in particular used to be
    wired with the writable handle despite only reading.
    """
    counter, call = _lazy_env(tmp_path, monkeypatch)

    # An unknown entity / email is fine: _CountingStore implements no query
    # methods, so the FIRST attribute access resolves the lazy handle and then
    # raises AttributeError inside the handler's own try/except. Resolving the
    # handle is the whole assertion, and it happens either way.
    async def _body():
        await call("brain_graph", {"entity": "nobody"})
        await call("brain_draft_context", {"email_id": "m1"})
    asyncio.run(_body())

    assert counter.opened, "neither exception tool resolved a Store at all"
    assert counter.writable == [], (
        f"a WRITABLE handle was opened for read-only local tools: {counter.opened}")

    # ...and the flag makes no difference to them, which is what "the exception
    # is unconditional" means. A second env so the counts are read fresh.
    from mcpbrain import config
    counter, call = _lazy_env(tmp_path, monkeypatch)
    config.write_config(str(tmp_path), {"tool_exec_in_daemon": False})
    asyncio.run(_body())
    assert counter.writable == [], counter.opened


def test_the_kill_switch_opens_the_local_handles_it_needs(tmp_path, monkeypatch):
    """Flag OFF: the same routed tool must open a Store instead of a socket.

    The counterweight -- without it, "no Store was opened" would also pass for a
    build that never wired the fallback at all.
    """
    from mcpbrain import config

    counter, call = _lazy_env(tmp_path, monkeypatch)
    config.write_config(str(tmp_path), {"tool_exec_in_daemon": False})

    async def _body():
        return await call("brain_read", {"doc_id": "d-local"})
    result = asyncio.run(_body())

    assert not result.is_error, [c.text for c in result.content]
    assert json.loads(result.content[0].text)["text"] == "read from a local Store"
    assert counter.opened, "the kill switch opened no Store"


# --- 6. local and routed must agree, tool by tool ---------------------------

def _both_paths_env(tmp_path, monkeypatch):
    """One home, one sqlite file, an MCP dispatch AND a real daemon behind it.

    The argument->kwargs mapping now exists TWICE -- in mcp_server's dispatch
    (the kill-switch path) and in Daemon._routed_tool_handlers (the routed one)
    -- because mcpbrain/tools.py is deliberately kept a pure, mcp-free registry
    and so cannot own a shared one. That duplication is the drift risk this
    fixture exists to close: the same call, over the same rows, both ways.
    """
    from mcpbrain.control_client import ControlClient
    from mcpbrain.mcp_server import build_server

    store, daemon = _seeded(tmp_path, monkeypatch)
    srv = ControlServer(daemon, home=str(tmp_path))
    srv.start()
    # Same Store object on both sides: the comparison is about the argument
    # mapping and the result envelope, not about two stores holding equal rows.
    server = build_server(store, store, ControlClient(home=tmp_path), str(tmp_path))
    return store, srv, _dispatch(server)


@pytest.mark.parametrize("name,arguments", [
    ("brain_read", {"doc_id": "d-1"}),
    ("brain_context", {"entity": "Sam Taylor"}),
    ("brain_context", {"mode": "communities"}),
    ("brain_actions", {"owner": "Sam Taylor"}),
    ("brain_actions", {"owner": "Sam Taylor", "status": "done"}),
    ("brain_proactive", {}),
    ("brain_proactive", {"severity": "info"}),
    ("brain_meeting_pack_get", {"event_id": "ev-1"}),
    ("brain_meetings_today", {}),
    ("brain_gardener_apply", _MINIMAL_ARGS["brain_gardener_apply"]),
])
def test_a_routed_read_returns_exactly_what_the_local_path_returns(
        tmp_path, monkeypatch, name, arguments):
    """Byte-identical results from both paths, per tool.

    Restricted to calls with no side effect on purpose: running a write twice
    genuinely produces different results (a second draft gets the next
    draft_record_id), so the write tools are pinned by their daemon-side effect
    tests in section 4 instead. brain_gardener_apply is included because its
    argument set here fails the guard before writing anything -- and it is the
    tool with the most arguments to get wrong.
    """
    from mcpbrain import config

    _store, srv, call = _both_paths_env(tmp_path, monkeypatch)
    try:
        async def _body():
            routed = await call(name, arguments)
            config.write_config(str(tmp_path), {"tool_exec_in_daemon": False})
            local = await call(name, arguments)
            return routed, local
        routed, local = asyncio.run(_body())
    finally:
        srv.stop()

    assert not routed.is_error, [c.text for c in routed.content]
    assert not local.is_error, [c.text for c in local.content]
    assert routed.content[0].text == local.content[0].text, (
        f"{name} disagrees between the routed and local paths:\n"
        f"  routed={routed.content[0].text}\n  local ={local.content[0].text}")


def test_a_routed_write_lands_in_the_daemons_store(protocol_session):
    """End-to-end over a real stdio session: a routed WRITE reaches the store.

    Section 4 proves Daemon.call_tool writes, and section 5 proves the MCP server
    routes; this is the seam between them for a write, through a real subprocess
    with the flag at its shipped default.
    """
    from mcpbrain.daemon import Daemon, SingleWriterLock
    from mcpbrain.embed import embedder_dim
    from mcpbrain.store import Store

    home = protocol_session.home
    store = Store(home / "brain.sqlite3", dim=embedder_dim("bge-small"),
                  read_only=False)
    srv = ControlServer(
        Daemon(store, None, services={},
               lock=SingleWriterLock(home / "d.lock")), home=str(home))
    srv.start()

    async def _run():
        async with protocol_session() as (session, _stderr):
            result = await session.call_tool("brain_meeting_pack_upsert", {
                "event_id": "ev-routed", "event_title": "Elders",
                "event_date": "2026-08-11", "pack_text": "# routed",
                "context_hash": "h-routed",
            })
            assert not result.is_error, [c.text for c in result.content]
            assert json.loads(result.content[0].text) == {"ok": True}

    try:
        asyncio.run(_run())
    finally:
        srv.stop()

    row = store.get_meeting_pack("ev-routed")
    assert row and row["pack_text"] == "# routed", row


def test_a_progress_tool_still_works_with_the_flag_on_and_no_daemon(protocol_session):
    """The exception, end-to-end: brain_graph must NOT return "daemon unreachable".

    Every other Store-touching tool degrades to isError here (see
    test_daemon_down_returns_isError_not_a_crash). These two must not, because
    they never route -- so this is the one test that would fail if someone
    "finished the job" by routing them.
    """
    async def _run():
        async with protocol_session() as (session, _stderr):
            result = await session.call_tool("brain_graph", {"entity": "nobody"})
            assert not result.is_error, [c.text for c in result.content]
            # An unknown entity legitimately returns {} -- the point is that the
            # local read-only handle was resolved and queried, not routed.
            assert json.loads(result.content[0].text) == {}

    asyncio.run(_run())


# --- 7. a stuck routed call must not be diagnosed as an absent daemon -------

def test_the_stuck_and_absent_diagnoses_differ_at_the_mcp_boundary(tmp_path, monkeypatch):
    """The two failure modes must be told apart in what the MODEL/user reads.

    Both are isError results, so a caller can only act on the text. If a stuck
    call said "not reachable, check the daemon is running (`mcpbrain doctor`)" the
    user would restart a daemon that doctor reports healthy, while the real cause
    -- a git child blocked on a stale `.git/index.lock`, or a hung Calendar call
    -- sat there untouched. Driven through the real dispatch layer so the
    classification is proven where it is consumed, not just where it is raised.
    """
    import threading

    from mcpbrain.control_client import ControlClient

    released = threading.Event()

    class _Stuck:
        def call_tool(self, name, arguments):
            released.wait(30)
            return {"applied": True}

    monkeypatch.setattr(ControlClient, "TOOL_CALL_TIMEOUT_S", 0.5)
    _counter, call = _lazy_env(tmp_path, monkeypatch)
    args = _MINIMAL_ARGS["brain_gardener_apply"]

    srv = ControlServer(_Stuck(), home=str(tmp_path))
    srv.start()
    try:
        stuck = asyncio.run(call("brain_gardener_apply", args))
    finally:
        released.set()
        srv.stop()
    # Same home, same tool, same arguments -- only the daemon is gone now, so any
    # difference below is the classification and nothing else. Repointed at a
    # port nothing listens on rather than relying on srv.stop(), which only ends
    # the serve_forever loop and leaves the listening socket accepting into its
    # backlog -- so the client would time out again instead of being refused.
    # ECONNREFUSED is also the case that must NOT be read as a timeout, so this
    # exercises both sides of the new branch.
    import socket
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
    (tmp_path / "control_port").write_text(str(dead_port))
    absent = asyncio.run(call("brain_gardener_apply", args))

    stuck_text = " ".join(c.text for c in stuck.content)
    absent_text = " ".join(c.text for c in absent.content)
    assert stuck.is_error and absent.is_error, (stuck, absent)
    assert stuck_text != absent_text, stuck_text
    # The stuck message says the daemon IS up and points at the real causes...
    assert "did not answer in time" in stuck_text, stuck_text
    assert "index.lock" in stuck_text, stuck_text
    # ...and must NOT repeat the absent-daemon advice, which is the actual bug.
    assert "not reachable" not in stuck_text, stuck_text
    assert "mcpbrain doctor" not in stuck_text, stuck_text
    # The absent case is unchanged: still the doctor advice it has always given.
    assert "not reachable" in absent_text and "mcpbrain doctor" in absent_text, absent_text
