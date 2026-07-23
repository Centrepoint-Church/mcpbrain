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


def test_orphan_and_missing_org_review_rules_documented():
    """Task 2.2: the AI-adjudicator prompt must carry rules + verdict schema
    for both review kinds, in both the canonical prompt and its byte-identical
    copy in the enrich-batch subagent (kept in sync by bin/sync_agents.py)."""
    import pathlib
    root = pathlib.Path(__file__).parent.parent
    for fname in ["mcpbrain/enrich_prompt.md", "plugin/agents/enrich-batch.md"]:
        text = (root / fname).read_text()
        assert "Orphan-entity review rules" in text, f"orphan rules missing from {fname}"
        assert "Missing-org review rules" in text, f"missing-org rules missing from {fname}"
        for key in (
            "review_orphan",
            "review_missing_org",
            "finding_id",
            "ref_id",
            "suppress",
            "assign",
            "external",
            "taxonomy",
        ):
            assert key in text, f"{key!r} not found in {fname}"


def test_missing_org_rule_flags_document_category_anti_pattern():
    """Task 2.3 gate fix: a live adjudication run wrongly `assign`ed a person to
    an org based on a document/chunk category tag (e.g. a bracketed `[ACC]`
    label) plus the person co-occurring with an org name in a document *about*
    that org (an MOU/contract), not any statement of the person's own
    affiliation. The rule text must explicitly warn against conflating
    document-level categorization with personal-affiliation evidence, in both
    the canonical prompt and its byte-identical enrich-batch copy."""
    import pathlib
    root = pathlib.Path(__file__).parent.parent
    for fname in ["mcpbrain/enrich_prompt.md", "plugin/agents/enrich-batch.md"]:
        text = (root / fname).read_text()
        lower = text.lower()
        assert "document" in lower and "categor" in lower, (
            f"document-categorization anti-pattern guidance missing from {fname}"
        )
        assert "own affiliation" in lower or "personal affiliation" in lower, (
            f"personal-affiliation distinction missing from {fname}"
        )


def test_ownerless_and_org_hygiene_review_rules_documented():
    """Task 3.2: the AI-adjudicator prompt must carry rules + verdict schema
    for the ownerless-action review kind (Task 3.1, applier shipped without
    its own prompt rules) and the three bundled org-hygiene review kinds
    (this task), in both the canonical prompt and its byte-identical copy in
    the enrich-batch subagent (kept in sync by bin/sync_agents.py)."""
    import pathlib
    root = pathlib.Path(__file__).parent.parent
    for fname in ["mcpbrain/enrich_prompt.md", "plugin/agents/enrich-batch.md"]:
        text = (root / fname).read_text()
        assert "Ownerless-action review rules" in text, f"ownerless rules missing from {fname}"
        assert "Org-hygiene review rules" in text, f"org-hygiene rules missing from {fname}"
        for key in (
            "review_ownerless",
            "review_org",
            "finding_id",
            "ref_id",
            "owner",
            "waiting_on",
            "unowned",
            "lint:ambiguous_org",
            "lint:duplicate_org",
            "org_unrecognised",
            "canonicalize",
            "add_to_config",
            "canonical_org",
            "taxonomy",
        ):
            assert key in text, f"{key!r} not found in {fname}"


def test_duplicate_org_canonicalize_risk_judgment_documented():
    """duplicate_org canonicalize is a bulk org-field rewrite (never a merge/
    delete), but the prompt must still tell the adjudicator to weigh whether
    a short/acronym-like variant is more likely a genuinely different org
    than a typo — not rubber-stamp every fuzzy match the lint check surfaces."""
    import pathlib
    root = pathlib.Path(__file__).parent.parent
    for fname in ["mcpbrain/enrich_prompt.md", "plugin/agents/enrich-batch.md"]:
        text = (root / fname).read_text()
        lower = text.lower()
        assert "acronym" in lower, f"acronym-vs-typo guidance missing from {fname}"
        assert "genuinely" in lower and "misspelling" in lower, (
            f"distinct-org guidance missing from {fname}"
        )


def test_coordinator_runs_on_sonnet_for_auto_mode():
    # The scheduled/hourly enrich task must run the COORDINATOR on Sonnet: Claude Code
    # scheduled tasks only offer Auto permission mode on Sonnet, and a Haiku coordinator
    # would stall on permission prompts unattended. Executor subagents stay Haiku.
    for p in ("mcpbrain/routines/enrich.md",):
        text = Path(p).read_text().lower()
        assert "coordinator" in text and "sonnet" in text
        assert "auto permission mode" in text        # the reason the coordinator is Sonnet
        assert "haiku" in text                        # subagents still run on Haiku
