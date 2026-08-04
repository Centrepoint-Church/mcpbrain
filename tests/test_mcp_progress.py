"""Progress for the two tools with natural boundaries.

Claude doesn't render progress but does reset its idle timer on it, so this is
about slow calls not being reaped. brain_search is out of scope: it's one opaque
loopback HTTP call and progress would need daemon-side plumbing (separate plan).

No pytest-asyncio in this repo (see conftest.py's protocol_session docstring):
every test here is a plain sync function driving its own event loop with
asyncio.run(...). A test decorated with the unknown `@pytest.mark.asyncio`
mark would collect and "pass" without its body ever running -- that mark does
not exist in this suite's plugin set, so pytest just records it as an unknown
mark and the coroutine is never awaited (a "coroutine was never awaited"
RuntimeWarning is the tell). Running with -W error::RuntimeWarning is part of
this file's verification precisely to prove that isn't happening here.
"""
import asyncio
import json

from mcpbrain.mcp_server import _progress_reporter


class _Session:
    def __init__(self):
        self.sent = []

    async def send_progress_notification(self, progress_token, progress, total=None,
                                        message=None, related_request_id=None):
        self.sent.append((progress_token, progress, total, message))


class _Ctx:
    def __init__(self, meta):
        self.session = _Session()
        self.meta = meta


def test_reporter_no_ops_without_a_progress_token():
    """A client that didn't ask for progress must get none."""
    async def _body():
        ctx = _Ctx(meta=None)
        report = _progress_reporter(ctx)
        await report(1, 3, "step one")
        assert ctx.session.sent == []
    asyncio.run(_body())


def test_reporter_sends_when_a_token_was_supplied():
    async def _body():
        ctx = _Ctx(meta={"progress_token": "tok-1"})
        report = _progress_reporter(ctx)
        await report(1, 3, "hop 1 of 3")
        assert ctx.session.sent == [("tok-1", 1, 3, "hop 1 of 3")]
    asyncio.run(_body())


def test_reporter_swallows_send_failures():
    """A progress failure must never fail the tool call itself."""
    async def _body():
        ctx = _Ctx(meta={"progress_token": "tok-1"})

        async def _boom(*a, **k):
            raise ConnectionError("gone")

        ctx.session.send_progress_notification = _boom
        report = _progress_reporter(ctx)
        await report(1, 3, "hop 1")  # must not raise
    asyncio.run(_body())


def _seed_graph_chain(store, home):
    """someone -> e1 -> e2, a real 3-hop-deep chain.

    The brief's literal test called brain_graph with {"entity": "Someone",
    "hops": 3} against an EMPTY store: find_entity("Someone") resolves to
    nothing (or resolves but has no relations), relations_for() returns
    nothing, the BFS's frontier goes empty, and `if not frontier: break`
    fires after hop 1 -- at most a single progress update, enough to pass a
    bare `assert progress` while proving nothing about PER-HOP reporting.

    Seeding a 3-entity chain keeps the frontier non-empty going into hops 2
    and 3 (relations_for("e2") adds nothing new, but that only prevents a
    4th hop -- hops=3 still runs all 3 iterations of the BFS loop), so all 3
    on_hop calls genuinely fire.
    """
    store.upsert_entity("someone", "Someone", "person")
    store.upsert_entity("e1", "Contact One", "person")
    store.upsert_entity("e2", "Contact Two", "person")
    store.add_relation("someone", "knows", "e1")
    store.add_relation("e1", "knows", "e2")


def test_brain_graph_reports_progress_per_hop(protocol_session_with_progress):
    """hops=3 must produce 3 progress updates, one per hop, not silence."""
    async def _body():
        async with protocol_session_with_progress(seed=_seed_graph_chain) as (session, progress):
            result = await session.call_tool("brain_graph", {"entity": "Someone", "hops": 3})
            assert not result.is_error, result.content
        return progress
    progress = asyncio.run(_body())

    assert len(progress) == 3, f"brain_graph should report once per hop for a 3-hop traversal, got {progress}"
    assert all(p.total == 3 for p in progress), progress
    assert [p.progress for p in progress] == [1, 2, 3], progress


def test_brain_graph_progress_total_is_capped_not_raw_hops(protocol_session_with_progress):
    """hops=5 (above GRAPH_MAX_HOPS=3) must still report total=3, not total=5.

    make_brain_graph caps the actual traversal at GRAPH_MAX_HOPS regardless of
    a larger `hops` argument -- so the genuinely-known bound to report is the
    capped value, not the raw argument. A caller reporting the raw `hops`
    would emit 3 progress events but every one claiming total=5: a client
    rendering progress would sit at "3 of 5" and never see completion. The
    hops=3 test above can't catch this because raw and capped coincide there.
    """
    async def _body():
        async with protocol_session_with_progress(seed=_seed_graph_chain) as (session, progress):
            result = await session.call_tool("brain_graph", {"entity": "Someone", "hops": 5})
            assert not result.is_error, result.content
        return progress
    progress = asyncio.run(_body())

    assert len(progress) == 3, f"expected exactly 3 hops (the cap), got {progress}"
    assert all(p.total == 3 for p in progress), (
        f"total must be the capped hop count (3), not the raw hops argument (5): {progress}"
    )


def _seed_draftable_email(store, home):
    """A real email_context row + draft_critic enabled, so draft_context's
    full stage sequence (email lookup -> voice rules -> samples -> critique)
    genuinely runs.

    The brief's literal test called brain_draft_context with
    email_id="nope" -- a message_id no store has, so draft_context returns
    {"error": "email not found"} at the very first stage and never reaches
    voice_rules/samples/critique. That would pass a bare `assert messages`
    off a single early report, proving nothing about STAGE progress. Seeding
    an actual email_context row lets the lookup succeed and the rest of the
    pipeline run; enabling 'draft_critic' in config makes the fourth
    (deliberately slow) stage fire too.
    """
    store.upsert_email_context(
        "m1", subject="Hall booking", sender="Sam <s@x.com>", thread_id="t1",
        date_iso="2026-06-01", summary="asks about booking Hall B for the retreat",
    )
    (home / "config.json").write_text(json.dumps({"draft_critic": True}))


def test_brain_draft_context_reports_stage_progress(protocol_session_with_progress):
    """All 4 real draft_context stages report, in order: email lookup, voice
    rules, samples, critique (the slow one, deliberately announced before its
    blocking subprocess call rather than after it)."""
    async def _body():
        async with protocol_session_with_progress(seed=_seed_draftable_email) as (session, progress):
            result = await session.call_tool("brain_draft_context", {"email_id": "m1"})
            assert not result.is_error, result.content
        return progress
    progress = asyncio.run(_body())

    messages = [p.message for p in progress]
    assert messages == [
        "looking up email",
        "loading voice rules",
        "gathering thread samples",
        "running voice/coverage critique",
    ], messages
    assert all(p.total == 4 for p in progress), progress
    assert [p.progress for p in progress] == [1, 2, 3, 4], progress
