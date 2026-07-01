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


def test_enrichment_skill_body_names_contract():
    """The shipped enrichment skill body (cowork/enrichment.md) is what now runs
    as the mcpbrain-enrichment personal skill (see skills.py). It must keep naming
    the contract fields + file roles so it can't drift from contract.py.

    (Replaces the old wizard `spec-task` check — the wizard no longer embeds the
    prompt inline; the canonical copy lives in cowork/enrichment.md.)
    """
    body = (Path(__file__).parent.parent / "mcpbrain" / "cowork" / "enrichment.md").read_text()
    for field in ("thread_id", "org", "content_type", "entities", "actions",
                  "relations", "resolved_action_ids", "merge_answers"):
        assert field in body, f"enrichment.md must name the contract field {field!r}"
    assert "pending.json" in body and "enrich_inbox" in body


def test_prompt_scopes_entities_to_body():
    text = PROMPT_PATH.read_text().lower()
    assert "already creates an entity for every message sender" in text
    assert "body" in text  # entities are the body-mentioned delta


def test_coordinator_runs_on_sonnet_for_auto_mode():
    # The scheduled/hourly enrich task must run the COORDINATOR on Sonnet: Claude Code
    # scheduled tasks only offer Auto permission mode on Sonnet, and a Haiku coordinator
    # would stall on permission prompts unattended. Executor subagents stay Haiku.
    for p in ("mcpbrain/routines/enrich.md",
              "plugin/skills/mcpbrain-backfill/SKILL.md"):
        text = Path(p).read_text().lower()
        assert "coordinator" in text and "sonnet" in text
        assert "auto permission mode" in text        # the reason the coordinator is Sonnet
        assert "haiku" in text                        # subagents still run on Haiku
