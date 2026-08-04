"""New resource files must be announced to a connected client.

Scope note: list_changed covers the resource LIST changing (a new memory/<slug>.md
or reference/*.md appearing -- exactly what brain_memory_write and
brain_gardener_apply produce mid-session). Content changes to an existing URI
need resources/subscribe, which Claude does not support and which we deliberately
do not implement -- it would mean per-URI hash tracking plus a subscription
registry for zero consumers. test_fingerprint_ignores_content_edits pins that
boundary on purpose.

No pytest-asyncio in this repo (see tests/conftest.py's protocol_session), so the
async bodies below are driven by a plain asyncio.run(...) -- an
@pytest.mark.asyncio here would be an unknown mark and the coroutine would never
be awaited, i.e. a test that passes while asserting nothing.
"""
import asyncio
from pathlib import Path

from mcpbrain.mcp_server import _resource_fingerprint, init_options, watch_resources


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


def test_watcher_notifies_once_per_change(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    ctx = tmp_path / "context"
    ctx.mkdir()

    class _RecordingSession:
        def __init__(self):
            self.calls = 0

        async def send_resource_list_changed(self):
            self.calls += 1

    session = _RecordingSession()

    async def _body():
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
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(_body())


def test_watcher_survives_a_send_failure(tmp_path, monkeypatch):
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

    async def _body():
        task = asyncio.create_task(watch_resources(session, interval_s=0.01))
        try:
            # Yield first: create_task only SCHEDULES the coroutine, so writing
            # before the watcher has taken its baseline fingerprint would leave it
            # with nothing to detect.
            await asyncio.sleep(0.02)
            (tmp_path / "context" / "a.md").write_text("x", encoding="utf-8")
            await asyncio.sleep(0.05)
            assert session.attempts >= 1
            assert not task.done(), "watcher died on a send failure"
            # The fingerprint is advanced BEFORE the send, so a permanently
            # broken client is retried once per CHANGE, not once per poll.
            attempts_after_failure = session.attempts
            await asyncio.sleep(0.05)
            assert session.attempts == attempts_after_failure
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(_body())


def test_capability_is_advertised(mcp_env):
    from mcpbrain.mcp_server import build_server

    server = build_server(**mcp_env)
    caps = init_options(server).capabilities
    assert caps.resources is not None
    # mcp 2.x's ResourcesCapability spells the field list_changed (wire alias
    # listChanged); see test_capability_reaches_the_wire for the serialised form.
    assert caps.resources.list_changed is True
    assert caps.resources.subscribe is False, (
        "we do not implement subscribe; advertising it would promise updates we "
        "never send"
    )


def test_capability_reaches_the_wire(protocol_session):
    """The unit assertion above would still pass if main() called
    create_initialization_options() bare, so check the real handshake."""

    async def _body():
        async with protocol_session() as (session, stderr_path):
            caps = session.server_capabilities
            assert caps is not None, Path(stderr_path).read_text()
            assert caps.resources is not None, Path(stderr_path).read_text()
            assert caps.resources.list_changed is True
            assert caps.resources.subscribe is False

    asyncio.run(_body())


def test_watcher_starts_once_across_repeated_list_resources(mcp_env, monkeypatch):
    """The lazy start must be guarded: a client lists resources many times."""
    from mcp import types

    from mcpbrain import mcp_server

    started = []

    async def _fake_watch(session, interval_s=None):
        started.append(session)
        await asyncio.sleep(3600)  # stand in for "runs for the connection"

    monkeypatch.setattr(mcp_server, "watch_resources", _fake_watch)
    server = mcp_server.build_server(**mcp_env)
    entry = server.get_request_handler("resources/list")

    class _Ctx:
        session = object()

    async def _body():
        for _ in range(3):
            result = await entry.handler(_Ctx(), types.PaginatedRequestParams())
            assert result.resources is not None
        await asyncio.sleep(0.01)  # let the created task(s) actually enter the body
        spawned = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for t in spawned:
            t.cancel()
        await asyncio.gather(*spawned, return_exceptions=True)

    asyncio.run(_body())
    assert len(started) == 1, f"spawned {len(started)} watchers, expected 1"
