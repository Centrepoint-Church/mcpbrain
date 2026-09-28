import io

from pptx import Presentation
from pptx.util import Inches

from mcpbrain.sync.blocks import Heading, Paragraph, TableBlock
from mcpbrain.sync.extract_office import extract_blocks_from_pptx


def _pptx() -> bytes:
    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[5])          # title only
    s.shapes.title.text = "Q3 Ministry Review"
    group = s.shapes.add_group_shape()
    tb = group.shapes.add_textbox(Inches(1), Inches(2), Inches(4), Inches(1))
    tb.text_frame.text = "Inside a group"
    rows = s.shapes.add_table(2, 2, Inches(1), Inches(4), Inches(4), Inches(1)).table
    rows.cell(0, 0).text, rows.cell(0, 1).text = "Campus", "Attendance"
    rows.cell(1, 0).text, rows.cell(1, 1).text = "North", "140"
    s.notes_slide.notes_text_frame.text = "Mention the Northgate Trust grant."
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def test_slide_heading_group_table_and_notes():
    blocks = extract_blocks_from_pptx(_pptx())
    assert blocks[0] == Heading(2, "Q3 Ministry Review", label="Slide 1: ")
    assert Paragraph("Inside a group") in blocks
    assert TableBlock([["Campus", "Attendance"], ["North", "140"]]) in blocks
    assert blocks[-1] == Paragraph("Mention the Northgate Trust grant.", label="Notes: ")


def test_garbage_input_returns_empty_list():
    assert extract_blocks_from_pptx(b"not a pptx") == []


def test_old_pptx_text_covers_every_new_slide_chunk():
    """Final review I1: the old extractor wrote 'Slide N\\n<title>'; the span for
    'Slide N: <title>' must be the title alone, so reflow can prove coverage."""
    import random

    from mcpbrain import reflow
    from mcpbrain.sync.blocks import render
    from mcpbrain.sync.normalise import Chunk
    from tests.oracles.chunking_v0 import chunk_text_v0
    rnd = random.Random(3)
    words = "alpha beta gamma delta budget review staff campus report minutes".split()
    prs = Presentation()
    for n in range(30):
        s = prs.slides.add_slide(prs.slide_layouts[1])
        s.shapes.title.text = f"Topic {n}"
        s.placeholders[1].text = "\n".join(
            " ".join(rnd.choice(words) for _ in range(12)) for _ in range(5))
    buf = io.BytesIO()
    prs.save(buf)
    data = buf.getvalue()
    parts = []
    for n, slide in enumerate(Presentation(io.BytesIO(data)).slides, start=1):
        lines = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    t = "".join(r.text for r in para.runs).strip()
                    if t:
                        lines.append(t)
        if lines:
            parts.append(f"Slide {n}\n" + "\n".join(lines))
    old = [{"doc_id": f"gdrive-F-{i}", "text": t, "metadata": {"chunk_index": i},
            "enriched": 1, "enriched_version": 3, "enrich_state": None, "salience": None,
            "memory_tier": None, "memory_type": None}
           for i, t in enumerate(chunk_text_v0("\n\n".join(parts)))]
    new = [Chunk(f"gdrive-F-{i}", r.text, "h", {}, r.spans)
           for i, r in enumerate(render(extract_blocks_from_pptx(data)))]
    p = reflow.plan(old, new)
    assert all(r.covered and r.enriched for r in p.rows)
    assert new[0].text.startswith("Slide 1: Topic 0")


def test_slide_without_a_title_gets_a_label_only_heading():
    from mcpbrain.sync.blocks import render
    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[6])          # blank: no title placeholder
    s.shapes.add_textbox(Inches(1), Inches(1), Inches(3), Inches(1)).text_frame.text = \
        "Untitled body"
    buf = io.BytesIO()
    prs.save(buf)
    blocks = extract_blocks_from_pptx(buf.getvalue())
    assert blocks == [Heading(2, "", label="Slide 1"), Paragraph("Untitled body")]
    (r,) = render(blocks)
    assert r.text == "Slide 1\n\nUntitled body"
    assert r.spans == ["Untitled body"]                     # the label is never a span
