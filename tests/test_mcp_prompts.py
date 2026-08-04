"""The 4 routines + draft-reply, exposed as real MCP prompts.

Rationale: brain_routine already serves routine markdown from a *tool* because
the scheduled-task runtime can't resolve plugin skills. Prompts are the right
primitive, and they also reach wheel-only installs that have no plugin. The tool
stays -- scheduled tasks self-invoke and prompts are user-initiated.

No pytest-asyncio in this repo (see tests/conftest.py's protocol_session
docstring) -- every protocol round-trip below opens the shared
`protocol_session` @asynccontextmanager factory inside a plain
asyncio.run(...), never `@pytest.mark.asyncio`.
"""
import asyncio

from mcpbrain.mcp_server import prompt_definitions

ROUTINES = {"enrich", "meeting-packs", "gardener", "reference-gardener"}
EXPECTED = ROUTINES | {"draft-reply"}


def test_all_prompts_are_defined():
    assert set(prompt_definitions()) == EXPECTED


def test_draft_reply_declares_its_arguments():
    args = {a["name"]: a for a in prompt_definitions()["draft-reply"]["arguments"]}
    assert args["email_id"]["required"] is True
    assert args["intent"]["required"] is False


def test_routines_take_no_arguments():
    for name in ROUTINES:
        assert prompt_definitions()[name]["arguments"] == []


def test_list_prompts_over_the_protocol(protocol_session):
    async def _body():
        async with protocol_session() as (session, _stderr_path):
            names = {p.name for p in (await session.list_prompts()).prompts}
            assert names == EXPECTED
    asyncio.run(_body())


def test_get_prompt_returns_the_routine_markdown(protocol_session):
    """The prompt body must be the same text brain_routine serves."""
    import json

    async def _body():
        async with protocol_session() as (session, _stderr_path):
            result = await session.get_prompt("enrich", {})
            assert result.messages
            body = result.messages[0].content.text
            assert body.strip(), "empty prompt body"

            tool_out = json.loads(
                (await session.call_tool("brain_routine", {"name": "enrich"})).content[0].text
            )
            assert body == tool_out["instructions"], (
                "prompt and tool disagree on the routine text; they must share one source"
            )
    asyncio.run(_body())


def test_get_prompt_interpolates_draft_reply_arguments(protocol_session):
    async def _body():
        async with protocol_session() as (session, _stderr_path):
            result = await session.get_prompt(
                "draft-reply", {"email_id": "abc123", "intent": "decline politely"}
            )
            body = result.messages[0].content.text
            assert "abc123" in body and "decline politely" in body
    asyncio.run(_body())


def test_get_prompt_rejects_unknown_name(protocol_session):
    async def _body():
        async with protocol_session() as (session, _stderr_path):
            try:
                await session.get_prompt("no-such-prompt", {})
            except Exception:
                return
            raise AssertionError("expected an error for an unknown prompt name")
    asyncio.run(_body())


def test_missing_required_argument_is_rejected(protocol_session):
    async def _body():
        async with protocol_session() as (session, _stderr_path):
            try:
                await session.get_prompt("draft-reply", {})
            except Exception:
                return
            raise AssertionError("expected an error for a missing required argument")
    asyncio.run(_body())


def _draft_reply_skill_file():
    from pathlib import Path
    return (Path(__file__).resolve().parents[1] / "plugin" / "skills"
            / "mcpbrain-draft-reply" / "SKILL.md")


def test_draft_reply_skill_in_sync():
    # plugin/ does not ship in the wheel, so the canonical draft-reply prompt
    # body lives under mcpbrain/prompts/ (package-data) and the plugin skill
    # carries a *copy* between markers, below its own YAML frontmatter.
    # bin/sync_agents.py regenerates that copy; this pins byte-equality so the
    # two can never drift, the same contract test_enrich_agent_rules_in_sync
    # enforces for the enrich-batch rules.
    from mcpbrain.mcp_server import _draft_reply_canonical_body

    text = _draft_reply_skill_file().read_text()
    b, e = "<!-- DRAFT-REPLY-BODY:BEGIN -->", "<!-- DRAFT-REPLY-BODY:END -->"
    embedded = text[text.index(b) + len(b):text.index(e)].strip()
    assert embedded and embedded == _draft_reply_canonical_body()
