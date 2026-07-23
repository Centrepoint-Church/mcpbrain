"""Task 5 — `with_rules` must round-trip through the MCP protocol layer.

`make_brain_enrich_pull`'s inner function already honours `with_rules`
(covered by tests/test_mcp_enrich_meeting_tools.py). The gap this test
targets is one layer up: the `brain_enrich_pull` tool's `inputSchema` never
declared `with_rules`, and the dispatch handler never forwarded it from
`arguments` into the call — so over real MCP (stdio session, list_tools /
call_tool) the argument was silently dropped and the response always
carried the ~11KB rules block regardless of what a caller passed.

Mirrors the stdio session harness in test_mcp_server_stdio.py (spawn the
server as a subprocess over stdio, real ClientSession, no mocking).
"""

import asyncio
import datetime as _dt
import json
import os
import sys
from pathlib import Path

# Product root = the dir holding the `mcpbrain` package (tests/ -> parent).
PRODUCT_ROOT = Path(__file__).resolve().parent.parent


def _seed_unit(home: Path):
    q = home / "enrich_queue"
    (q / "units").mkdir(parents=True, exist_ok=True)
    (q / "context.json").write_text(json.dumps({"owner_name": "Josh"}))
    (q / "units" / "u-abc.json").write_text(json.dumps(
        {"unit_id": "u-abc", "kind": "thread", "threads": [{"thread_id": "t1"}]}))


async def _run_session(home: Path):
    """Spawn the server over stdio, initialize, list tools, call brain_enrich_pull
    twice (with_rules=False, then default). Returns (pull_tool, payload_no_rules,
    payload_default)."""
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

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
            pull_tool = next(t for t in tools_result.tools if t.name == "brain_enrich_pull")

            no_rules_result = await session.call_tool(
                "brain_enrich_pull", {"unit_id": "u-abc", "with_rules": False}
            )
            assert not no_rules_result.isError, (
                f"brain_enrich_pull returned error: {no_rules_result.content}"
            )
            payload_no_rules = json.loads(no_rules_result.content[0].text)

            default_result = await session.call_tool(
                "brain_enrich_pull", {"unit_id": "u-abc"}
            )
            assert not default_result.isError, (
                f"brain_enrich_pull returned error: {default_result.content}"
            )
            payload_default = json.loads(default_result.content[0].text)

            return pull_tool, payload_no_rules, payload_default


def test_pull_schema_declares_and_dispatch_forwards_with_rules(tmp_path):
    home = tmp_path / "mcpbrain_home"
    home.mkdir()
    _seed_unit(home)

    pull_tool, payload_no_rules, payload_default = asyncio.run(_run_session(home))

    # Schema gap: the tool's inputSchema must declare with_rules as an accepted arg.
    assert "with_rules" in pull_tool.inputSchema["properties"]

    # Dispatch gap: with_rules=False over the protocol must actually suppress
    # the rules block (not silently stay True).
    assert "rules" not in payload_no_rules

    # Default (no with_rules passed) stays True.
    assert "rules" in payload_default
