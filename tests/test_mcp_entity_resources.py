"""mcpbrain://entity resources, template and completion in the MCP server (Task 11)."""
import asyncio

import pytest

from mcpbrain import mcp_server


@pytest.fixture(autouse=True)
def _cold_cache(monkeypatch):
    # list_entity_resources caches for 10 minutes; each test starts cold.
    monkeypatch.setattr(mcp_server, "_entity_list_cache", None)


class _Client:
    def __init__(self, down=False):
        self.down = down

    def _check(self):
        if self.down:
            from mcpbrain.control_client import DaemonUnavailable
            raise DaemonUnavailable("down")

    def entity_resources(self):
        self._check()
        return [{"id": "dana-okafor", "name": "Dana Okafor", "type": "person", "org": "Northgate Trust"}]

    def entity_resource(self, eid):
        self._check()
        return {"id": "dana-okafor", "markdown": "# Dana Okafor\n"} if eid in ("dana-okafor", "d-okafor") else None

    def search_entities(self, q):
        return [{"id": "dana-okafor"}, {"id": "dana-smith"}]

    def reply_needed(self, q):
        return ["19a2b3"]


def test_lists_entities_with_titles():
    res = asyncio.run(mcp_server.list_entity_resources(_Client()))
    assert [str(r.uri) for r in res] == ["mcpbrain://entity/dana-okafor"]
    assert res[0].name == "Dana Okafor" and res[0].title == "Dana Okafor, person, Northgate Trust"


def test_daemon_down_lists_nothing_rather_than_failing():
    assert asyncio.run(mcp_server.list_entity_resources(_Client(down=True))) == []


def test_read_entity_and_unknown():
    assert asyncio.run(mcp_server.read_entity_resource(_Client(), "mcpbrain://entity/d-okafor")) == "# Dana Okafor\n"
    with pytest.raises(ValueError, match="unknown entity"):
        asyncio.run(mcp_server.read_entity_resource(_Client(), "mcpbrain://entity/nobody"))


def test_schemes_are_isolated(tmp_path, monkeypatch):
    """A file:// uri never reaches the daemon; a mcpbrain:// uri never reads a file."""
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    with pytest.raises(ValueError):
        asyncio.run(mcp_server.read_entity_resource(_Client(), f"file://{tmp_path}/config.json"))
    with pytest.raises(ValueError):
        asyncio.run(mcp_server.read_context_resource("mcpbrain://entity/dana-okafor"))


def test_protocol_lists_template_and_completes_empty(protocol_session):
    """Real stdio round-trip: no daemon in this subprocess, so the template still
    lists (it's static) while completion degrades to an empty list (Task 11's
    "empty on failure" rule) rather than raising or hanging."""
    from mcp import types

    async def _body():
        async with protocol_session() as (session, _stderr_path):
            templates = await session.list_resource_templates()
            assert [t.uri_template for t in templates.resource_templates] == [
                mcp_server.ENTITY_TEMPLATE
            ]

            result = await session.complete(
                ref=types.ResourceTemplateReference(uri=mcp_server.ENTITY_TEMPLATE),
                argument={"name": "id", "value": "da"},
            )
            assert result.completion.values == []

    asyncio.run(_body())
