from mcpbrain import enrich_blocks, mcp_server, prepare

# Importing these registers their BLOCK_DRAINERS entries, exactly as
# daemon.py:55-58 does at startup. Without them the registry is
# under-populated and the invariant test below passes vacuously.
import mcpbrain.profile_synth  # noqa: F401
import mcpbrain.community_synth  # noqa: F401
import mcpbrain.memory_distil  # noqa: F401
import mcpbrain.profile_audit  # noqa: F401
from mcpbrain.drain import BLOCK_DRAINERS


def test_unit_blocks_is_merge_review_plus_answer_and_review_blocks():
    assert enrich_blocks.UNIT_BLOCKS == (
        "merge_review", *enrich_blocks.ANSWER_BLOCKS, *enrich_blocks.REVIEW_BLOCKS)


def test_push_blocks_is_answer_plus_review_blocks():
    assert enrich_blocks.PUSH_BLOCKS == (
        *enrich_blocks.ANSWER_BLOCKS, *enrich_blocks.REVIEW_BLOCKS)


def test_consumers_derive_from_single_source():
    assert mcp_server._PUSH_BLOCKS == enrich_blocks.PUSH_BLOCKS
    assert prepare._UNIT_BLOCKS == enrich_blocks.UNIT_BLOCKS


def test_merge_review_is_a_unit_block_not_a_push_block():
    assert "merge_review" in enrich_blocks.UNIT_BLOCKS
    assert "merge_review" not in enrich_blocks.PUSH_BLOCKS


def test_every_registered_drainer_key_is_pushable():
    """A drainer whose key brain_enrich_push refuses can never fire. This
    drift is what stranded the review_* families: cadence produced the work,
    write_units dropped it, push would have refused the answer, and the
    drainers sat registered and never invoked."""
    unpushable = set(BLOCK_DRAINERS) - set(enrich_blocks.PUSH_BLOCKS)
    assert unpushable == set(), (
        f"drainers registered for keys push will not accept: {sorted(unpushable)}")


def test_every_review_block_has_a_drainer():
    missing = set(enrich_blocks.REVIEW_BLOCKS) - set(BLOCK_DRAINERS)
    assert missing == set(), f"review blocks with no drainer: {sorted(missing)}"
