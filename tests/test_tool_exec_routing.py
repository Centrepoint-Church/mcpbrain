"""Routing: flag on -> daemon; flag off or daemon down -> local. Never a crash.

Task 9 of the tool-registry/thin-adapter plan. Three layers are pinned here,
bottom-up, because a break in any one of them is invisible from the others:

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
