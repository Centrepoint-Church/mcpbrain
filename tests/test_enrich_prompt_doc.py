"""Tests for the shipped extractor prompt doc (mcpbrain/enrich_prompt.md).

The same prose feeds two faces of one contract: the claude_pool-backed driver
on Nexus, and the Cowork project on the Mac. This test pins that the doc exists
and names the contract field set plus the two file roles, so the prompt can't
silently drift away from the schema in contract.py.
"""

from pathlib import Path

PROMPT_PATH = Path(__file__).parent.parent / "mcpbrain" / "enrich_prompt.md"


def test_prompt_doc_exists_and_names_contract():
    assert PROMPT_PATH.exists(), "mcpbrain/enrich_prompt.md must be shipped"
    text = PROMPT_PATH.read_text()

    # Contract field names the extraction envelope and batch wrapper carry.
    for field in (
        "thread_id",
        "org",
        "content_type",
        "entities",
        "actions",
        "relations",
        "resolved_action_ids",
        "merge_answers",
    ):
        assert field in text, f"prompt must name the contract field {field!r}"

    # The two file roles the session reads from and writes to.
    assert "pending.json" in text, "prompt must name the pending.json input"
    assert "enrich_inbox" in text, "prompt must name the enrich_inbox output"


def test_four_block_keys_documented():
    import pathlib
    root = pathlib.Path(__file__).parent.parent
    for fname in ["mcpbrain/enrich_prompt.md", "docs/cowork-task.md"]:
        text = (root / fname).read_text()
        for key in ["profile_synthesis", "community_synthesis", "memory_distil", "profile_audit"]:
            assert key in text, f"{key} not found in {fname}"


def test_wizard_spec_matches_cowork_doc():
    """The wizard's copy-paste Task field embeds the cowork-task.md Task body.

    The wizard page is what actually gets pasted into Cowork on a fresh
    install, so it must carry the same spec as the canonical doc — this pins
    the two together (the wizard copy drifted once, missing the four block
    sections added in Phase 2 Task 9).
    """
    import html
    import re

    root = Path(__file__).parent.parent
    doc = (root / "docs" / "cowork-task.md").read_text()
    body = re.search(r"## Task.*?\n```\n(.*?)\n```\n", doc, re.DOTALL).group(1)
    page = (root / "mcpbrain" / "wizard" / "index.html").read_text()
    pre = re.search(r'<pre id="spec-task" class="spec">(.*?)</pre>',
                    page, re.DOTALL).group(1)
    assert html.unescape(pre) == body, (
        "wizard spec-task drifted from docs/cowork-task.md — re-embed the "
        "Task body (HTML-escaped) into mcpbrain/wizard/index.html")
