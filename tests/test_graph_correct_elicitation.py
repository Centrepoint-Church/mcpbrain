"""The inferred-correction elicitation gate (Task 7)."""
import asyncio
from types import SimpleNamespace

from mcpbrain import mcp_server


class _Session:
    def __init__(self, caps, action="accept", content=None, raises=False):
        self.client_capabilities = caps
        self._action, self._content, self._raises = action, content, raises
        self.sent = []

    async def elicit_form(self, message, requested_schema, related_request_id=None):
        self.sent.append((message, requested_schema))
        if self._raises:
            raise RuntimeError("no back channel")
        return SimpleNamespace(action=self._action, content=self._content)


_FORM = SimpleNamespace(elicitation=SimpleNamespace(form=SimpleNamespace(), url=None))
_ARGS = {"op": "set_field", "basis": "inferred", "reason": "newer signature",
         "entity_id": "dana-okafor", "field": "org", "value": "Southbank Community Trust"}


def _run(session):
    return asyncio.run(mcp_server._confirm_correction(SimpleNamespace(session=session), _ARGS))


def test_accept_confirms():
    s = _Session(_FORM, "accept", {"confirm": True})
    assert _run(s) == {"via": "elicitation"}
    message, schema = s.sent[0]
    assert "Set org of dana-okafor to 'Southbank Community Trust'" in message
    assert "newer signature" in message
    assert schema["properties"]["confirm"]["type"] == "boolean"


def test_accept_with_confirm_unticked_is_a_decline():
    assert _run(_Session(_FORM, "accept", {"confirm": False})) == {"declined": True}


def test_decline_and_cancel():
    assert _run(_Session(_FORM, "decline")) == {"declined": True}
    assert _run(_Session(_FORM, "cancel")) == {"cancelled": True}


def test_no_capability_means_none():
    assert _run(_Session(SimpleNamespace(elicitation=None))) is None
    assert _run(_Session(None)) is None
    url_only = SimpleNamespace(elicitation=SimpleNamespace(form=None, url=SimpleNamespace()))
    assert _run(_Session(url_only)) is None


def test_bare_elicitation_capability_counts_as_form():
    """Pre-2025-11-25 clients advertise `elicitation: {}`, which means form mode."""
    bare = SimpleNamespace(elicitation=SimpleNamespace(form=None, url=None))
    assert _run(_Session(bare, "accept", {"confirm": True})) == {"via": "elicitation"}


def test_transport_failure_falls_back_to_pending():
    assert _run(_Session(_FORM, raises=True)) is None


def test_malformed_arguments_do_not_raise_while_building_the_message():
    """The advertised schema only requires `op` now (Task 6) -- a model can send
    `basis: inferred` with none of the per-op payload fields. `describe`/
    `payload_from_args` would KeyError on those; the gate must not let that
    escape, so the daemon's own "refused" result is what reaches the model
    instead of an internal error swallowing the whole call."""
    s = _Session(_FORM, "accept", {"confirm": True})
    incomplete = {"op": "set_field", "basis": "inferred", "reason": "r"}
    assert asyncio.run(mcp_server._confirm_correction(SimpleNamespace(session=s), incomplete)) is None
    assert s.sent == []


# --- dispatch-level: on_call_tool wired through build_server + a fake client -

def _dispatch_env(tmp_path, monkeypatch, session):
    """build_server with a real (empty) Store and a fake client that records
    every call_tool invocation -- so "the daemon was never reached" is
    provable, not assumed. Follows test_tool_exec_routing.py's _dispatch/
    _lazy_env pattern rather than inventing new plumbing.
    """
    from mcp import types

    from mcpbrain.mcp_server import build_server
    from mcpbrain.store import Store

    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    store = Store(tmp_path / "brain.sqlite3", dim=4, read_only=False)
    store.init()
    with store._connect(write=True) as db:
        db.execute("INSERT INTO entities(id,name,type) VALUES('dana-okafor','Dana Okafor','person')")

    class _FakeClient:
        def __init__(self):
            self.calls = []

        def call_tool(self, name, arguments, confirmation=None):
            self.calls.append((name, arguments, confirmation))
            raise NotImplementedError("must not be reached")

    client = _FakeClient()
    server = build_server(store, store, client, str(tmp_path))
    entry = server.get_request_handler("tools/call")

    class _Ctx:
        meta = None

        def __init__(self, session):
            self.session = session

    async def _call(name, arguments):
        return await entry.handler(
            _Ctx(session), types.CallToolRequestParams(name=name, arguments=arguments))

    return client, _call


def test_dispatch_cancelled_elicitation_never_reaches_the_daemon(tmp_path, monkeypatch):
    client, call = _dispatch_env(tmp_path, monkeypatch, _Session(_FORM, "cancel"))

    async def _body():
        return await call("brain_graph_correct", _ARGS)
    result = asyncio.run(_body())

    assert client.calls == []
    import json
    payload = json.loads(result.content[0].text)
    assert payload["status"] == "not_applied"


def test_dispatch_daemon_failure_after_accepted_elicitation_is_an_error(tmp_path, monkeypatch):
    from mcpbrain.control_client import DaemonUnavailable

    client, call = _dispatch_env(tmp_path, monkeypatch, _Session(_FORM, "accept", {"confirm": True}))

    def _boom(name, arguments, confirmation=None):
        client.calls.append((name, arguments, confirmation))
        raise DaemonUnavailable("no daemon")
    client.call_tool = _boom

    async def _body():
        return await call("brain_graph_correct", _ARGS)
    result = asyncio.run(_body())

    assert result.is_error
    assert client.calls and client.calls[0][2] == {"via": "elicitation"}
