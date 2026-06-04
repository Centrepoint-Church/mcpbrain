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
