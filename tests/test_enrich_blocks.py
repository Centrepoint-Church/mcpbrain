from mcpbrain import enrich_blocks, mcp_server, prepare


def test_unit_blocks_is_merge_review_plus_answer_blocks():
    assert enrich_blocks.UNIT_BLOCKS == ("merge_review", *enrich_blocks.ANSWER_BLOCKS)


def test_consumers_derive_from_single_source():
    assert mcp_server._ENRICH_ANSWER_BLOCKS == enrich_blocks.ANSWER_BLOCKS
    assert prepare._UNIT_BLOCKS == enrich_blocks.UNIT_BLOCKS


def test_merge_review_is_a_unit_block_not_an_answer_block():
    assert "merge_review" in enrich_blocks.UNIT_BLOCKS
    assert "merge_review" not in enrich_blocks.ANSWER_BLOCKS
