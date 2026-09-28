from mcpbrain.sync.blocks import (
    Heading, Paragraph, TableBlock, PartialBlocks, render, to_text, from_text,
    extraction_version,
)
from mcpbrain.sync.extractors import is_partial


def test_extraction_versions():
    assert extraction_version("application/pdf") == 1
    assert extraction_version("text/plain") == 0


def test_short_document_is_one_chunk_joined_by_blank_lines():
    blocks = [Heading(1, "Budget"), Paragraph("Line one\nLine two"), Paragraph("Second")]
    out = render(blocks)
    assert len(out) == 1
    assert out[0].text == "Budget\n\nLine one\nLine two\n\nSecond"
    assert out[0].meta["heading_trail"] == "Budget"
    assert out[0].spans == ["Budget", "Line one\nLine two", "Second"]


def test_heading_trail_nests_and_pops():
    body = "word " * 300
    blocks = [Heading(1, "A"), Paragraph(body), Heading(2, "A1"), Paragraph(body),
              Heading(1, "B"), Paragraph(body)]
    trails = [r.meta.get("heading_trail") for r in render(blocks)]
    assert "A › A1" in trails
    assert trails[-1] == "B"


def test_heading_flushes_a_more_than_half_full_chunk():
    blocks = [Paragraph("x " * 500), Heading(1, "Next"), Paragraph("y " * 100)]
    out = render(blocks)
    assert out[1].text.startswith("Next")


def test_table_renders_as_row_sentences_with_cells_as_spans():
    t = TableBlock([["Item", "Cost"], ["Chairs", "120"], ["", "5"]], caption="")
    out = render([Heading(1, "Capital works"), t])
    joined = "\n".join(r.text for r in out)
    assert "Item: Chairs" in joined and "Cost: 120" in joined
    assert "Capital works" in joined
    spans = [s for r in out for s in r.spans]
    assert "Chairs" in spans and "120" in spans
    assert "Item: Chairs" not in spans          # synthesised text is not a span


def test_oversize_paragraph_is_split_without_collapsing_lines():
    para = "\n".join(f"Row {i} value" for i in range(400))
    out = render([Paragraph(para)])
    assert len(out) > 1
    assert all(len(r.text) <= 1800 for r in out)
    assert all("\n" in r.text for r in out)


def test_to_text_and_partial():
    b = PartialBlocks([Paragraph("a"), TableBlock([["h1", "h2"], ["x", "y"]])])
    t = to_text(b)
    assert is_partial(t)
    assert "a" in t and "x | y" in t


def test_from_text_headings_and_paragraphs():
    blocks = from_text("# Title\n\nBody one\nstill one\n\n## Sub\n\nBody two")
    assert blocks == [Heading(1, "Title"), Paragraph("Body one\nstill one"),
                      Heading(2, "Sub"), Paragraph("Body two")]


def test_empty_and_contentless_blocks_are_dropped():
    assert render([Paragraph("   "), Paragraph("---")]) == []


def test_table_render_stays_within_budget_and_spans_are_substrings():
    """Fix round 1: a single wide row's unbounded 'a | b | c' line, and an
    unbounded caption, could each alone push a table chunk past max_chars;
    and a cell _row_sentence truncates/drops was still claimed as a span even
    though it no longer appears verbatim in the emitted text."""
    max_chars = 400
    long_cell_a = "A" * 300
    long_cell_b = "B" * 300
    wide_row_table = TableBlock([["h1", "h2"], [long_cell_a, long_cell_b]])
    single_row_wide = TableBlock([[long_cell_a, long_cell_b, "C" * 300]])
    long_caption_table = TableBlock([["h1", "h2"], ["v1", "v2"]], caption="Section " * 100)

    for block in (wide_row_table, single_row_wide, long_caption_table):
        out = render([block], max_chars=max_chars)
        assert out, "expected at least one chunk"
        for r in out:
            assert len(r.text) <= max_chars
            for span in r.spans:
                assert span in r.text


def test_table_row_with_long_header_label_stays_within_budget():
    """Fix round 2: _fit_row_sentence shrinks only the CELL VALUE (to a
    5-char floor), never the header LABEL, so one oversize header alone can
    make a row's rendered sentence longer than the whole chunk budget --
    reproduced at the production budget (1800) as well as a tight one (200)."""
    for max_chars in (1800, 200):
        t = TableBlock([["Header " * 300, "h2"], ["v1", "v2"]])
        out = render([t], max_chars=max_chars)
        assert out, "expected at least one chunk"
        for r in out:
            assert len(r.text) <= max_chars
            for span in r.spans:
                assert span in r.text


def test_table_row_with_many_fields_stays_within_budget():
    """Fix round 2: the per-field 'H: v; ' overhead, multiplied across many
    columns, can exceed the chunk budget even when every individual header
    and cell is short on its own."""
    header = [f"Column Header Number {i:03d} With Extra Padding Text" for i in range(45)]
    row = [f"v{i}" for i in range(45)]
    for max_chars in (1800, 200):
        t = TableBlock([header, row])
        out = render([t], max_chars=max_chars)
        assert out, "expected at least one chunk"
        for r in out:
            assert len(r.text) <= max_chars
            for span in r.spans:
                assert span in r.text


# -- final review I1: synthetic label prefixes are rendered, never spans -------

def test_label_is_rendered_but_is_not_a_span():
    from mcpbrain.sync.blocks import Heading, Paragraph, render, to_text
    blocks = [Heading(2, "Topic", label="Slide 1: "), Paragraph("body line"),
              Paragraph("remember the grant", label="Notes: ")]
    out = render(blocks)
    assert out[0].text == "Slide 1: Topic\n\nbody line\n\nNotes: remember the grant"
    assert out[0].spans == ["Topic", "body line", "remember the grant"]
    assert "Slide 1: Topic" in to_text(blocks) and "Notes: remember" in to_text(blocks)


def test_label_only_heading_renders_with_no_span():
    from mcpbrain.sync.blocks import Heading, Paragraph, render
    out = render([Heading(2, "", label="Slide 3"), Paragraph("body words")])
    assert out[0].text.startswith("Slide 3\n\nbody words")
    assert out[0].spans == ["body words"]
    assert out[0].meta["heading_trail"] == "Slide 3"


def test_default_label_keeps_existing_constructions_unchanged():
    from mcpbrain.sync.blocks import Heading, Paragraph
    assert Heading(1, "A") == Heading(1, "A", label="")
    assert Paragraph("x").label == ""
