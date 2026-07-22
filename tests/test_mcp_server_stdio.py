"""Task 3.3 / Task 4 — MCP-as-spawned-process stdio integration test.

Proves Claude Desktop can spawn `python -m mcpbrain.mcp_server` over stdio:
real subprocess, real MCP transport (no mocking). Issues `initialize`, lists
tools, and calls `brain_search`.

Task 4 routed brain_search through the daemon's loopback control API instead
of an in-process embedder (mcp_server.py must import no fastembed/onnxruntime
so it can ship as a native-dep-free Desktop Extension). So this test starts a
real ControlServer, backed by a lightweight fake daemon, in the same
MCPBRAIN_HOME the spawned subprocess reads its control_port/control_token
from — proving the full round trip (subprocess -> stdio -> brain_search ->
HTTP loopback -> daemon.search) without loading any embedding model. This also
makes the test fast: no bge cold start, so it no longer needs @pytest.mark.slow.
"""

import asyncio
import datetime as _dt
import json
import os
import sys
from pathlib import Path

from mcpbrain.control_api import ControlServer

# Product root = the dir holding the `mcpbrain` package (tests/ -> parent).
PRODUCT_ROOT = Path(__file__).resolve().parent.parent

SEEDED_DOC_ID = "d-budget-stdio"


class _FakeDaemon:
    """Minimal daemon stand-in: only `.search()` is reachable, via the
    control API's /api/recall handler, which is all ControlClient.recall()
    (and therefore brain_search) calls through."""

    def search(self, query: str, limit: int = 5, *, expand: bool = False) -> list[dict]:
        return ([{"doc_id": SEEDED_DOC_ID, "score": 1.0,
                  "text": "the annual budget review for Acme"}] if query else [])


async def _run_session(home: Path):
    """Spawn the server over stdio, initialize, list tools, call brain_search.

    Returns (tool_names, search_payload) where search_payload is the parsed
    JSON from the brain_search TextContent.
    """
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    # Explicit minimal env: don't leak developer credentials into the
    # subprocess, and keep the test reproducible. A raw subprocess does NOT
    # inherit pyproject's pythonpath, so PYTHONPATH must carry the product
    # root for `-m mcpbrain.mcp_server` to import.
    env = {
        "HOME": os.environ.get("HOME", ""),
        "PATH": os.environ.get("PATH", ""),
        "MCPBRAIN_HOME": str(home),
        "PYTHONPATH": str(PRODUCT_ROOT),
    }

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcpbrain.mcp_server"],
        env=env,
    )

    timeout = _dt.timedelta(seconds=15)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=timeout) as session:
            await session.initialize()
            tools_result = await session.list_tools()
            tool_names = {t.name for t in tools_result.tools}
            call_result = await session.call_tool(
                "brain_search", {"query": "budget", "limit": 5}
            )
            # call_tool returns a CallToolResult with .content = [TextContent].
            assert call_result.content, (
                f"brain_search returned empty content: {call_result}"
            )
            assert not call_result.isError, (
                f"brain_search returned error: {call_result.content}"
            )
            text = call_result.content[0].text
            payload = json.loads(text)
            return tool_names, payload


def test_stdio_spawn_initialize_and_brain_search(tmp_path):
    home = tmp_path / "mcpbrain_home"
    home.mkdir()

    daemon = _FakeDaemon()
    srv = ControlServer(daemon, home=str(home))
    srv.start()
    try:
        tool_names, payload = asyncio.run(_run_session(home))
    finally:
        srv.stop()

    # initialize + tools list
    assert {"brain_search", "brain_read", "brain_context", "brain_graph", "brain_actions"} <= tool_names

    # brain_search round-tripped through the real control API to the fake daemon
    assert isinstance(payload, list)
    assert any(r.get("doc_id") == SEEDED_DOC_ID for r in payload), (
        f"expected {SEEDED_DOC_ID} in {payload}"
    )
