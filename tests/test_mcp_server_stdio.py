"""Task 3.3 — MCP-as-spawned-process stdio integration test.

Proves Claude Desktop can spawn `python -m mcpbrain.mcp_server` over stdio
against a live store: real subprocess, real MCP transport (no mocking),
real bge embedder seeding the store. Issues `initialize`, lists tools, and
calls `brain_search`, asserting the seeded chunk comes back.

Slow: the spawned subprocess loads the bge model from a cold start (~3-3.5 min),
even when the model is already cached on disk. Marked @pytest.mark.slow so
`pytest -m "not slow"` skips it; it still runs in the default suite.
"""

import asyncio
import datetime as _dt
import json
import os
import sys
from pathlib import Path

import pytest

# Product root = the dir holding the `mcpbrain` package (tests/ -> parent).
PRODUCT_ROOT = Path(__file__).resolve().parent.parent


def _seed_store(home: Path) -> str:
    """Build a real store under `home` and index one recognisable chunk.

    Returns the doc_id of the seeded chunk. Uses the REAL bge embedder so the
    vector dim matches what the spawned server will load.
    """
    from mcpbrain.embed import get_embedder
    from mcpbrain.index import index_pending
    from mcpbrain.store import Store

    emb = get_embedder("bge-small")
    store = Store(home / "brain.sqlite3", dim=emb.dim)
    store.init()
    doc_id = "d-budget-stdio"
    store.upsert_chunk(
        doc_id,
        "the annual budget review for Acme",
        "h-budget-stdio",
        {"source_type": "gmail"},
    )
    index_pending(store, emb)  # populate vec + fts so search works
    return doc_id


async def _run_session(home: Path):
    """Spawn the server over stdio, initialize, list tools, call brain_search.

    Returns (tool_names, search_payload) where search_payload is the parsed
    JSON from the brain_search TextContent.
    """
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    # Explicit minimal env: don't leak developer credentials into the
    # subprocess, and keep the test reproducible. HOME is required so the
    # spawned bge load finds its cached HF model under HOME/.cache (otherwise
    # it would re-download). A raw subprocess does NOT inherit pyproject's
    # pythonpath, so PYTHONPATH must carry the product root for `-m
    # mcpbrain.mcp_server` to import.
    env = {
        "HOME": os.environ.get("HOME", ""),
        "PATH": os.environ.get("PATH", ""),
        "MCPBRAIN_HOME": str(home),
        "MCPBRAIN_EMBEDDER": "bge-small",
        "PYTHONPATH": str(PRODUCT_ROOT),
    }

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcpbrain.mcp_server"],
        env=env,
    )

    # Generous read timeout: the subprocess loads bge on first request.
    timeout = _dt.timedelta(seconds=60)
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


@pytest.mark.slow
def test_stdio_spawn_initialize_and_brain_search(tmp_path):
    home = tmp_path / "mcpbrain_home"
    home.mkdir()
    seeded_doc_id = _seed_store(home)

    tool_names, payload = asyncio.run(_run_session(home))

    # initialize + tools list
    assert {"brain_search", "brain_read", "brain_context", "brain_graph", "brain_actions"} <= tool_names

    # brain_search over the live store finds the seeded chunk
    assert isinstance(payload, list)
    assert any(r.get("doc_id") == seeded_doc_id for r in payload), (
        f"expected {seeded_doc_id} in {payload}"
    )
