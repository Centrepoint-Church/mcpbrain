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


# ---------------------------------------------------------------------------
# Scanned-PDF detection + tesseract OCR (Q5)
# ---------------------------------------------------------------------------

def _make_pdf_long_text_bytes() -> bytes:
    import fitz
    doc = fitz.open(); page = doc.new_page()
    page.insert_text((72, 72),
                     "This is a budget report with plenty of real text on the page, "
                     "well above the scanned-PDF character threshold per page.")
    data = doc.tobytes(); doc.close()
    return data


def test_is_scanned_pdf_true_for_blank():
    from mcpbrain.sync.extractors import is_scanned_pdf
    assert is_scanned_pdf(_make_pdf_no_text_bytes()) is True


def test_is_scanned_pdf_false_for_text_pdf():
    from mcpbrain.sync.extractors import is_scanned_pdf
    assert is_scanned_pdf(_make_pdf_long_text_bytes()) is False


def test_digital_pdf_returns_text_layer():
    from mcpbrain.sync.extractors import extract_text_from_pdf
    out = extract_text_from_pdf(_make_pdf_long_text_bytes())
    assert "budget report" in out.lower()


def test_scanned_pdf_degrades_to_empty_without_tesseract(monkeypatch):
    """No tesseract resolvable → a scanned PDF yields '' (graceful), never raises."""
    import mcpbrain.sync.extractors as ex
    monkeypatch.setattr(ex, "_tesseract_bin", lambda: "")
    out = ex.extract_text_from_pdf(_make_pdf_no_text_bytes())
    assert out.strip() == ""


def test_scanned_pdf_uses_ocr_output_when_available(monkeypatch):
    """Control-flow: a no-text page routes through _ocr_page and its text is used."""
    import mcpbrain.sync.extractors as ex
    monkeypatch.setattr(ex, "_tesseract_cache", True)          # pretend tesseract present
    monkeypatch.setattr(ex, "_ocr_page", lambda page: "OCR RECOVERED BUDGET")
    out = ex.extract_text_from_pdf(_make_pdf_no_text_bytes())
    assert "OCR RECOVERED BUDGET" in out


def test_ocr_roundtrip_with_real_tesseract():
    """End-to-end OCR of an image-only PDF — runs only where tesseract is installed."""
    import shutil
    import pytest
    if not shutil.which("tesseract"):
        pytest.skip("tesseract not installed")
    from PIL import Image, ImageDraw, ImageFont
    import fitz
    from mcpbrain.sync.extractors import extract_text_from_pdf, is_scanned_pdf

    img = Image.new("RGB", (900, 240), "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 96)
    except Exception:
        font = ImageFont.load_default()
    draw.text((30, 60), "CENTREPOINT", fill="black", font=font)
    pbuf = io.BytesIO(); img.save(pbuf, format="PNG")

    doc = fitz.open(); page = doc.new_page(width=900, height=240)
    page.insert_image(fitz.Rect(0, 0, 900, 240), stream=pbuf.getvalue())
    pdf = doc.tobytes(); doc.close()

    assert is_scanned_pdf(pdf) is True
    out = extract_text_from_pdf(pdf)
    assert "centrepoint" in out.lower()
