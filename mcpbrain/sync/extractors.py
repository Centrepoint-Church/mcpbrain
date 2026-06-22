"""Binary file text extractors for Drive sync.

Handles: PDF (text layer + optional OCR), DOCX (paragraphs + tables), XLSX
(first 200 rows per sheet). All imports are lazy so the module loads even if
a dependency is missing — failed extractions return "".

tesseract is an optional external binary for scanned-PDF OCR. It is NOT a pip
dependency. If tesseract is absent, image-only PDFs degrade gracefully to "".
"""

import io
import shutil


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

def extract_text_from_pdf(content_bytes: bytes) -> str:
    """PDF text via pymupdf; OCR fallback (tesseract) for image/scanned pages.

    OCR only runs when tesseract is on PATH; otherwise degrades to the text
    layer (empty for scanned PDFs). tesseract is an optional external
    dependency.
    """
    try:
        import fitz  # pymupdf
        doc = fitz.open(stream=content_bytes, filetype="pdf")
    except Exception:
        return ""
    try:
        pages = [page.get_text() for page in doc]
        text = "\n\n".join(pages)
        if len(text.strip()) >= max(20, 20 * len(pages)):
            return text
        ocr_available = _tesseract_available()
        ocr_pages = []
        for page in doc:
            page_text = page.get_text().strip()
            if len(page_text) >= 20 or not ocr_available:
                ocr_pages.append(page_text)
            else:
                try:
                    tp = page.get_textpage_ocr(language="eng", dpi=200)
                    ocr_pages.append(page.get_text(textpage=tp).strip() or page_text)
                except Exception:
                    ocr_pages.append(page_text)
        return "\n\n".join(ocr_pages)
    except Exception:
        return ""
    finally:
        doc.close()  # guaranteed close on every path once the doc is open


_tesseract_cache = None


def _tesseract_available() -> bool:
    global _tesseract_cache
    if _tesseract_cache is None:
        _tesseract_cache = shutil.which("tesseract") is not None
    return _tesseract_cache


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------

def extract_text_from_docx(content_bytes: bytes) -> str:
    """Extract text from DOCX bytes, including table content."""
    try:
        from docx import Document
        doc = Document(io.BytesIO(content_bytes))
        parts = [p.text for p in doc.paragraphs if p.text.strip()]
        for table in doc.tables:
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells if c.text.strip()]
                if cells:
                    parts.append(" | ".join(cells))
        return "\n".join(parts)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# XLSX
# ---------------------------------------------------------------------------

def _rows_to_markdown(rows: list[list[str]]) -> str:
    """Render rows as a GitHub-flavored markdown table.

    First non-empty row is the header; a separator row follows. Ragged rows are
    padded to the table width and pipes are escaped so the table stays well-formed.
    Preserves column→value structure for embedding/recall instead of collapsing it
    to ' | '-joined prose.
    """
    rows = [r for r in rows if any((c or "").strip() for c in r)]
    if not rows:
        return ""
    width = max(len(r) for r in rows)

    def _cell(c: str) -> str:
        return (c or "").replace("|", "\\|").replace("\n", " ").strip()

    def _line(r: list[str]) -> str:
        cells = [_cell(c) for c in r] + [""] * (width - len(r))
        return "| " + " | ".join(cells[:width]) + " |"

    sep = "| " + " | ".join(["---"] * width) + " |"
    return "\n".join([_line(rows[0]), sep, *(_line(r) for r in rows[1:])])


def extract_text_from_xlsx(content_bytes: bytes) -> str:
    """Extract XLSX bytes as one markdown table per sheet (headers + first 200 rows).

    Per-type structural extraction (Q5): preserves table shape as markdown rather
    than flat ' | '-joined lines, so column structure survives into the embedding/
    recall layer. Each sheet is labelled and rendered independently.
    """
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(content_bytes), read_only=True, data_only=True)
        parts = []
        for name in wb.sheetnames:
            ws = wb[name]
            rows: list[list[str]] = []
            for n, row in enumerate(ws.iter_rows(values_only=True)):
                if n >= 200:
                    rows.append([f"... ({name} truncated at 200 rows)"])
                    break
                rows.append([str(c) if c is not None else "" for c in row])
            table = _rows_to_markdown(rows)
            if table:
                parts.append(f"### Sheet: {name}\n{table}")
        wb.close()
        return "\n\n".join(parts)
    except Exception:
        return ""
