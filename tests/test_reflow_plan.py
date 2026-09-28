from mcpbrain.reflow import lineage_key, norm, plan, stitch
from mcpbrain.sync.normalise import Chunk


def _old(i, text, total, enriched=1, state=None, fid="F", version=3):
    return {"doc_id": f"gdrive-{fid}-{i}", "text": text,
            "metadata": {"source_type": "gdrive", "file_id": fid, "chunk_index": i,
                         "chunk_total": total},
            "enriched": enriched, "enriched_version": version, "enrich_state": state,
            "salience": 0.5, "memory_tier": "warm", "memory_type": "semantic"}


def _new(i, text, spans=None, fid="F"):
    return Chunk(f"gdrive-{fid}-{i}", text, "h", {"source_type": "gdrive", "file_id": fid,
                 "chunk_index": i}, spans or [text])


def test_stitch_removes_word_overlap():
    a = "one two three four five six"
    b = "five six seven eight"
    o, offs = stitch([a, b])
    assert o == "one two three four five six seven eight"
    # Offsets partition the stitched text: each input owns what it CONTRIBUTED,
    # i.e. its normalised text minus the overlap prefix the seam dropped.
    assert offs[0] == (0, len(a))
    assert offs[1] == (o.index("seven"), len(o))
    assert o[offs[0][0]:offs[0][1]] == norm(a)
    assert o[offs[1][0]:offs[1][1]] == norm(b)[len("five six "):]


def test_stitch_offsets_invariant_on_messy_whitespace():
    texts = ["  alpha\n\nbeta  gamma\tdelta ", "gamma delta epsilon", "zeta", "", "eta theta"]
    o, offs = stitch(texts)
    assert o == "alpha beta gamma delta epsilon zeta eta theta"
    dropped = [0, len("gamma delta "), 0, 0, 0]
    for t, (s, e), d in zip(texts, offs, dropped):
        assert o[s:e] == norm(t)[d:]
    # a partition: spans are ordered and never overlap
    assert all(offs[i][1] <= offs[i + 1][0] for i in range(len(offs) - 1))


def test_stitch_whole_chunk_inside_overlap_gets_empty_span():
    o, offs = stitch(["a b c d", "c d"])
    assert o == "a b c d"
    assert offs[1] == (len(o), len(o))


def test_lineage_keys():
    assert lineage_key("gdrive-F-3", {"source_type": "gdrive"}) == "gdrive-F"
    assert lineage_key("gmail-M-att-2-0", {"source_type": "gmail"}) == "gmail-M-att-2"
    assert lineage_key("cal-E", {"source_type": "calendar", "event_id": "E"}) == "cal-E"
    assert lineage_key("cal-E-1", {"source_type": "calendar", "event_id": "E"}) == "cal-E"


def test_full_coverage_inherits_and_remaps():
    old = [_old(0, "alpha beta gamma delta", 2), _old(1, "gamma delta epsilon zeta", 2)]
    new = [_new(0, "alpha beta\ngamma", ["alpha beta\ngamma"]),
           _new(1, "delta epsilon zeta", ["delta epsilon zeta"])]
    p = plan(old, new)
    assert p.content_equal
    assert all(r.covered and r.enriched == 1 for r in p.rows)
    assert p.remap["gdrive-F-0"] == "gdrive-F-0"
    assert p.remap["gdrive-F-1"] in {"gdrive-F-0", "gdrive-F-1"}
    assert p.deletes == []


def test_identical_overlapping_chunks_remap_to_identity():
    """chunk_text word-splits with a 50-word overlap. An unchanged re-chunk with
    the same boundaries must map i -> i, not onto the chunk whose tail holds the
    overlap (an inclusive start offset would map every i onto i-1)."""
    w = [f"w{n}" for n in range(30)]
    texts = [" ".join(w[0:12]), " ".join(w[8:22]), " ".join(w[18:30])]
    old = [_old(i, t, 3) for i, t in enumerate(texts)]
    p = plan(old, [_new(i, t) for i, t in enumerate(texts)])
    assert p.remap == {f"gdrive-F-{i}": f"gdrive-F-{i}" for i in range(3)}
    assert set(p.reasons.values()) == {"exact"}
    assert p.content_equal and all(r.covered and r.enriched for r in p.rows)


def test_new_text_is_uncovered_and_not_inherited():
    old = [_old(0, "alpha beta", 1)]
    new = [_new(0, "alpha beta"), _new(1, "Notes: brand new speaker notes")]
    p = plan(old, new)
    assert p.rows[0].covered and not p.rows[1].covered
    assert p.rows[1].enriched == 0 and p.rows[1].enrich_state is None
    assert p.rows[1].salience is None and p.rows[1].memory_tier is None
    assert not p.content_equal
    assert p.unequal == ["gdrive-F"]


def test_partial_word_is_not_coverage():
    """'pha bet' occurs inside 'alpha beta' as characters but not as words."""
    p = plan([_old(0, "alpha beta", 1)], [_new(0, "pha bet")])
    assert not p.rows[0].covered


def test_unenriched_old_means_not_enriched_new():
    old = [_old(0, "alpha beta", 1, enriched=0)]
    p = plan(old, [_new(0, "alpha beta")])
    assert p.rows[0].covered and p.rows[0].enriched == 0
    assert p.rows[0].enriched_version == 0


def test_mixed_enrichment_is_not_enriched():
    old = [_old(0, "alpha beta", 2), _old(1, "gamma delta", 2, enriched=0)]
    p = plan(old, [_new(0, "alpha beta gamma delta")])
    assert p.rows[0].covered and p.rows[0].enriched == 0


def test_enriched_version_is_the_oldest_overlapped():
    old = [_old(0, "alpha beta", 2, version=3), _old(1, "gamma delta", 2, version=2)]
    p = plan(old, [_new(0, "alpha beta gamma delta")])
    assert p.rows[0].enriched == 1 and p.rows[0].enriched_version == 2


def test_fewer_new_chunks_deletes_and_maps_old_ids():
    old = [_old(i, f"part{i} words here", 3) for i in range(3)]
    new = [_new(0, "part0 words here\npart1 words here\npart2 words here",
                ["part0 words here", "part1 words here", "part2 words here"])]
    p = plan(old, new)
    assert sorted(p.deletes) == ["gdrive-F-1", "gdrive-F-2"]
    assert set(p.remap.values()) == {"gdrive-F-0"}
    assert all(p.reasons[k] == "exact" for k in p.remap)


def test_more_new_chunks_splits_and_remaps_by_start():
    old = [_old(0, "a1 a2 a3 a4 b1 b2 b3 b4", 1)]
    new = [_new(0, "a1 a2 a3 a4"), _new(1, "b1 b2 b3 b4")]
    p = plan(old, new)
    assert p.remap == {"gdrive-F-0": "gdrive-F-0"}
    assert p.deletes == [] and all(r.covered and r.enriched for r in p.rows)


def test_plan_repeated_text_maps_monotonically():
    boiler = "Confidential disclaimer text"
    old = [_old(0, f"{boiler} first", 2), _old(1, f"{boiler} second", 2)]
    new = [_new(0, f"{boiler} first"), _new(1, f"{boiler} second")]
    p = plan(old, new)
    assert p.remap == {"gdrive-F-0": "gdrive-F-0", "gdrive-F-1": "gdrive-F-1"}


def test_removed_repeated_header_maps_nearest():
    old = [_old(0, "Header one alpha", 3), _old(1, "Header", 3), _old(2, "gamma delta", 3)]
    new = [_new(0, "Header one alpha"), _new(1, "gamma delta")]
    p = plan(old, new)
    assert p.remap["gdrive-F-2"] == "gdrive-F-1"
    assert p.remap["gdrive-F-1"] in {"gdrive-F-0", "gdrive-F-1"}
    assert p.deletes == ["gdrive-F-2"]
    # every old id maps to a new chunk that exists
    assert set(p.remap.values()) <= {"gdrive-F-0", "gdrive-F-1"}


def test_plan_gap_in_old_chunks_is_safe():
    old = [_old(0, "alpha beta", 4), _old(2, "epsilon zeta", 4)]      # 1 and 3 missing
    new = [_new(0, "alpha beta"), _new(1, "gamma delta"), _new(2, "epsilon zeta")]
    p = plan(old, new)
    assert [r.covered for r in p.rows] == [True, False, True]
    assert p.remap["gdrive-F-2"] == "gdrive-F-2"


def test_old_order_is_chunk_index_not_list_order():
    old = [_old(1, "gamma delta", 2), _old(0, "alpha beta", 2)]
    p = plan(old, [_new(0, "alpha beta"), _new(1, "gamma delta")])
    assert p.content_equal
    assert p.remap == {"gdrive-F-0": "gdrive-F-0", "gdrive-F-1": "gdrive-F-1"}


def test_table_chunk_covered_by_cell_values():
    old = [_old(0, "Item Cost Chairs 120", 1)]
    new = [_new(0, "Table (in Budget)\nItem: Chairs; Cost: 120", ["Item", "Cost", "Chairs", "120"])]
    assert plan(old, new).rows[0].covered


def test_table_cell_matches_row_sentence_punctuation():
    old = [_old(0, "Item: Chairs; Cost: 120", 1)]
    new = [_new(0, "Item | Cost\nChairs | 120", ["Item", "Cost", "Chairs", "120"])]
    assert plan(old, new).rows[0].covered


def test_largest_overlap_old_supplies_state():
    old = [_old(0, "a b c d e f g h", 2, state="cold"), _old(1, "i j", 2, state=None)]
    new = [_new(0, "a b c d e f g h i j")]
    p = plan(old, new)
    assert p.rows[0].enrich_state == "cold"


def test_empty_string_state_reads_as_hot():
    p = plan([_old(0, "alpha beta", 1, state="")], [_new(0, "alpha beta")])
    assert p.rows[0].enrich_state is None


def test_lineage_gone_maps_to_first_new_chunk():
    old = [{**_old(0, "body text", 1), "doc_id": "gmail-M-body-0",
            "metadata": {"source_type": "gmail", "chunk_index": 0}},
           {**_old(0, "attachment words", 1), "doc_id": "gmail-M-att-0-0",
            "metadata": {"source_type": "gmail", "chunk_index": 0}}]
    new = [Chunk("gmail-M-body-0", "body text", "h", {"source_type": "gmail"}, ["body text"])]
    p = plan(old, new)
    assert p.remap["gmail-M-att-0-0"] == "gmail-M-body-0"
    assert p.reasons["gmail-M-att-0-0"] == "lineage_gone"
    assert p.unequal == ["gmail-M-att-0"] and not p.content_equal
    assert p.deletes == ["gmail-M-att-0-0"]


def test_no_new_chunks_refuses():
    import pytest
    with pytest.raises(ValueError):
        plan([_old(0, "alpha", 1)], [])


def test_norm():
    assert norm("  a\n\tb  c ") == "a b c"
