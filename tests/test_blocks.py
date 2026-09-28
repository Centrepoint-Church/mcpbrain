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
