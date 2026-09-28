import pymupdf

from mcpbrain.sync.blocks import Heading, Paragraph, TableBlock
from mcpbrain.sync.extract_pdf import extract_blocks_from_pdf

LEFT = "Left column talks about the Northgate Trust grant and its reporting dates. " * 3
RIGHT = "Right column covers the Southbank hall booking and cleaning roster. " * 3


def _two_column_pdf() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    # Insert the RIGHT column first so content-stream order is wrong.
    page.insert_textbox(pymupdf.Rect(310, 72, 560, 400), RIGHT, fontsize=10)
    page.insert_textbox(pymupdf.Rect(40, 72, 290, 400), LEFT, fontsize=10)
    return doc.tobytes()


def _heading_pdf() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Annual Report", fontsize=22)
    page.insert_textbox(pymupdf.Rect(72, 90, 520, 300),
                        "The year in review for every campus and ministry. " * 4, fontsize=10)
    return doc.tobytes()


def _table_pdf() -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_textbox(pymupdf.Rect(72, 40, 520, 70), "Capital works budget below.", fontsize=10)
    rows = [["Item", "Cost"], ["Chairs", "120"], ["Lighting", "900"]]
    x0, y0, w, h = 72, 100, 150, 24
    for r, row in enumerate(rows):
        for c, val in enumerate(row):
            rect = pymupdf.Rect(x0 + c * w, y0 + r * h, x0 + (c + 1) * w, y0 + (r + 1) * h)
            page.draw_rect(rect, color=(0, 0, 0), width=0.8)
            page.insert_text((rect.x0 + 4, rect.y0 + 16), val, fontsize=10)
    return doc.tobytes()


def test_two_columns_come_out_in_reading_order():
    blocks = extract_blocks_from_pdf(_two_column_pdf())
    text = " ".join(b.text for b in blocks if isinstance(b, Paragraph))
    assert text.index("Left column") < text.index("Right column")


def test_large_font_line_becomes_heading():
    blocks = extract_blocks_from_pdf(_heading_pdf())
    assert isinstance(blocks[0], Heading) and blocks[0].text == "Annual Report"
    assert any(isinstance(b, Paragraph) and "year in review" in b.text for b in blocks)


def test_ruled_table_becomes_table_block_without_duplicate_text():
    blocks = extract_blocks_from_pdf(_table_pdf())
    tables = [b for b in blocks if isinstance(b, TableBlock)]
    assert tables, blocks
    assert ["Chairs", "120"] in tables[0].rows
    paras = " ".join(b.text for b in blocks if isinstance(b, Paragraph))
    assert "Chairs" not in paras
    assert "Capital works budget" in paras


def test_garbage_is_empty():
    assert extract_blocks_from_pdf(b"not a pdf") == []


def test_ocr_path_splits_on_blank_lines_without_from_text(monkeypatch):
    """Scanned-page OCR text becomes Paragraphs by splitting on blank lines
    directly in extract_pdf — blocks.from_text is still a stub owned by
    another unit, so this path must not depend on it."""
    doc = pymupdf.open()
    doc.new_page()  # blank page: no text layer -> looks scanned
    content = doc.tobytes()

    monkeypatch.setattr("mcpbrain.sync.extractors.is_scanned_pdf", lambda *a, **k: True)
    monkeypatch.setattr("mcpbrain.sync.extractors._tesseract_available", lambda: True)
    monkeypatch.setattr(
        "mcpbrain.sync.extractors._ocr_page",
        lambda page: "First paragraph.\n\nSecond paragraph.",
    )

    blocks = extract_blocks_from_pdf(content)
    paras = [b for b in blocks if isinstance(b, Paragraph)]
    assert [p.text for p in paras] == ["First paragraph.", "Second paragraph."]
