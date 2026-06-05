"""Tests for mcpbrain.sync.extractors — real in-memory files, no network."""

import io



# ---------------------------------------------------------------------------
# Helpers to build real binary files in memory
# ---------------------------------------------------------------------------

def _make_docx_bytes() -> bytes:
    from docx import Document
    doc = Document()
    doc.add_paragraph("Quarterly budget review")
    table = doc.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "Revenue"
    table.rows[0].cells[1].text = "Expenses"
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _make_xlsx_bytes() -> bytes:
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Budget"
    ws.append(["Category", "Amount"])
    ws.append(["Salaries", 120000])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _make_pdf_with_text_bytes() -> bytes:
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Budget report Q3")
    data = doc.tobytes()
    doc.close()
    return data


def _make_pdf_no_text_bytes() -> bytes:
    """A PDF with a page that has no text layer."""
    import fitz
    doc = fitz.open()
    doc.new_page()   # blank page — no text inserted
    data = doc.tobytes()
    doc.close()
    return data


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_docx_roundtrip():
    """DOCX with a paragraph and a table row extracts both."""
    from mcpbrain.sync.extractors import extract_text_from_docx
    text = extract_text_from_docx(_make_docx_bytes())
    assert "Quarterly budget review" in text
    assert "Revenue" in text
    assert "Expenses" in text


def test_xlsx_roundtrip():
    """XLSX with a header row and a data row extracts headers, values, and sheet name."""
    from mcpbrain.sync.extractors import extract_text_from_xlsx
    text = extract_text_from_xlsx(_make_xlsx_bytes())
    assert "Sheet:" in text
    assert "Category" in text
    assert "Amount" in text
    assert "120000" in text


def test_pdf_text_layer():
    """A PDF with a real text layer extracts text without needing OCR."""
    from mcpbrain.sync.extractors import extract_text_from_pdf
    text = extract_text_from_pdf(_make_pdf_with_text_bytes())
    assert "Budget report Q3" in text


def test_pdf_no_text_layer_degrades_without_tesseract(monkeypatch):
    """Scanned/image PDF with no text layer: when tesseract unavailable, returns
    empty string (or whitespace) without raising. Documents graceful degradation."""
    import mcpbrain.sync.extractors as extractors_mod
    monkeypatch.setattr(extractors_mod, "_tesseract_cache", False)

    text = extractors_mod.extract_text_from_pdf(_make_pdf_no_text_bytes())

    # Must not raise; result is empty (no text layer, no OCR)
    assert isinstance(text, str)
    assert text.strip() == ""


def test_extractors_return_empty_on_garbage():
    """Garbage bytes fed to each extractor returns '' without crashing."""
    from mcpbrain.sync.extractors import (
        extract_text_from_pdf,
        extract_text_from_docx,
        extract_text_from_xlsx,
    )
    garbage = b"not a real file"
    assert extract_text_from_pdf(garbage) == ""
    assert extract_text_from_docx(garbage) == ""
    assert extract_text_from_xlsx(garbage) == ""
