"""Single source of truth for the enrichment block-type sets.

ANSWER_BLOCKS — the optional answer blocks a subagent may push via
brain_enrich_push (beyond extractions + merge_answers), each drained by the
daemon. UNIT_BLOCKS — the block-unit kinds the producer emits: merge_review
plus every answer block. Keeping both here means adding a block type is a
one-line change instead of editing mcp_server + prepare in lockstep.

NOTE: drain.BLOCK_DRAINERS (review_*/org_merge_review) is a SEPARATE registry
for review/curator blocks and is intentionally not derived from here.
"""

ANSWER_BLOCKS = ("synthesis", "profile_synthesis", "community_synthesis",
                 "memory_distil", "profile_audit")

UNIT_BLOCKS = ("merge_review", *ANSWER_BLOCKS)
