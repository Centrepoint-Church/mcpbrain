"""Single source of truth for the enrichment block-type sets.

ANSWER_BLOCKS — the optional answer blocks a subagent may push via
brain_enrich_push (beyond extractions + merge_answers), each drained by the
daemon.

REVIEW_BLOCKS — the review/curator families. The daemon's review and curator
cadences PRODUCE these as work units, the subagent answers under the SAME key
(unlike merge_review, whose answer key is merge_answers), and
drain.BLOCK_DRAINERS applies the verdicts.

UNIT_BLOCKS — the block-unit kinds the producer (prepare.write_units) emits.
PUSH_BLOCKS — the answer keys brain_enrich_push accepts and forwards.

INVARIANT: every drain.BLOCK_DRAINERS key must appear in PUSH_BLOCKS, and every
REVIEW_BLOCKS entry must have a drainer. A drainer whose key push refuses can
never fire. That exact drift stranded the review_* families for weeks — the
cadence produced the work, write_units silently dropped it because the keys
were absent from UNIT_BLOCKS, and push would have refused the answers anyway.
tests/test_enrich_blocks.py enforces both directions.
"""

ANSWER_BLOCKS = ("synthesis", "profile_synthesis", "community_synthesis",
                 "memory_distil", "profile_audit")

REVIEW_BLOCKS = ("review_orphan", "review_missing_org", "review_ownerless",
                 "review_org", "org_merge_review")

UNIT_BLOCKS = ("merge_review", *ANSWER_BLOCKS, *REVIEW_BLOCKS)

PUSH_BLOCKS = (*ANSWER_BLOCKS, *REVIEW_BLOCKS)
