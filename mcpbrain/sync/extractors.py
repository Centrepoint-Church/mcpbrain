"""Binary file text extractors for Drive sync.

Handles: PDF (text layer + optional OCR), DOCX (paragraphs + tables), XLSX
(first 200 rows per sheet). All imports are lazy so the module loads even if
a dependency is missing — failed extractions return "".

Scanned/image-only PDFs are OCR'd page-by-page via the standalone `tesseract`
CLI (render page → PNG → `tesseract … stdout`). This deliberately does NOT use
pymupdf's built-in get_textpage_ocr, which needs MuPDF compiled with Tesseract
integration + TESSDATA_PREFIX — the pip wheel usually isn't, so it fails on a
plain `brew install tesseract`. The CLI path works with any tesseract install.
tesseract is an optional external binary (NOT a pip dependency); if it is absent,
image-only PDFs degrade gracefully to whatever text layer exists ('' when none).
"""

import io
import os
import shutil
import subprocess
import tempfile

_OCR_MIN_PAGE_CHARS = 20   # a page with fewer real chars is treated as image-only
_OCR_DPI = 200             # render resolution for OCR (quality vs speed)


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

def is_scanned_pdf(content_bytes: bytes, *, chars_per_page_threshold: int = 50) -> bool:
    """True when a PDF looks scanned/image-only (avg text-layer chars/page is low).

    Mirrors ops-brain's pdf_scanned_check. Used to decide whether OCR is worth
    attempting; returns False on any open error (caller falls back to text layer).
    """
    try:
        import fitz  # pymupdf
        doc = fitz.open(stream=content_bytes, filetype="pdf")
    except Exception:
        return False
    try:
        n = doc.page_count
        if n == 0:
            return False
        total = sum(len(page.get_text() or "") for page in doc)
        return (total / n) < chars_per_page_threshold
    except Exception:
        return False
    finally:
        doc.close()


def extract_text_from_pdf(content_bytes: bytes) -> str:
    """PDF text via pymupdf; per-page OCR fallback (tesseract CLI) for scanned pages.

    Digital PDFs return their text layer directly. For a low-text (scanned) PDF,
    each page with no usable text layer is rendered and OCR'd via the tesseract
    CLI. OCR only runs when tesseract is on PATH; otherwise the text layer is
    returned as-is ('' for a fully-scanned PDF).
    """
    try:
        import fitz  # pymupdf
        doc = fitz.open(stream=content_bytes, filetype="pdf")
    except Exception:
        return ""
    try:
        pages = [page.get_text() for page in doc]
        text = "\n\n".join(pages)
        # Digital PDF with a real text layer → done, no OCR needed.
        if len(text.strip()) >= max(_OCR_MIN_PAGE_CHARS, _OCR_MIN_PAGE_CHARS * len(pages)):
            return text
        if not _tesseract_available():
            return text  # degrade to the (possibly empty) text layer
        out = []
        for i, page in enumerate(doc):
            page_text = (pages[i] if i < len(pages) else page.get_text()).strip()
            if len(page_text) >= _OCR_MIN_PAGE_CHARS:
                out.append(page_text)
            else:
                out.append(_ocr_page(page) or page_text)
        return "\n\n".join(out)
    except Exception:
        return ""
    finally:
        doc.close()  # guaranteed close on every path once the doc is open


def _ocr_page(page) -> str:
    """Render one pymupdf page to PNG and OCR it via the tesseract CLI.

    Returns the recognised text, or '' on any failure (missing binary, render
    error, non-zero exit, timeout). Never raises — OCR is best-effort.
    """
    tess = _tesseract_bin()
    if not tess:
        return ""
    try:
        png = page.get_pixmap(dpi=_OCR_DPI).tobytes("png")
    except Exception:
        return ""
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(png)
            tmp = f.name
        proc = subprocess.run(
            [tess, tmp, "stdout", "-l", "eng"],
            capture_output=True, text=True, timeout=120,
        )
        return (proc.stdout or "").strip() if proc.returncode == 0 else ""
    except Exception:
        return ""
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


# Common absolute locations checked in addition to PATH. The daemon runs under
# launchd/systemd with a MINIMAL PATH that usually excludes Homebrew, so
# shutil.which("tesseract") alone would miss a `brew install`ed binary.
_TESSERACT_FALLBACK_PATHS = (
    "/opt/homebrew/bin/tesseract",   # Apple Silicon Homebrew
    "/usr/local/bin/tesseract",      # Intel Homebrew
    "/usr/bin/tesseract",            # Linux distro packages
)

_tesseract_cache = None  # cached resolved path (str) or "" when absent


def _tesseract_bin() -> str:
    """Resolve the tesseract binary: PATH first, then known install locations.

    Returns the path, or '' if not found. Cached. The fallback paths matter
    because the daemon's launchd/systemd PATH typically omits Homebrew dirs.
    Set TESSERACT_BIN to override explicitly.
    """
    global _tesseract_cache
    if _tesseract_cache is None:
        env = os.environ.get("TESSERACT_BIN", "")
        found = env or shutil.which("tesseract") or ""
        if not found:
            for p in _TESSERACT_FALLBACK_PATHS:
                if os.path.exists(p):
                    found = p
                    break
        _tesseract_cache = found
    return _tesseract_cache


def _tesseract_available() -> bool:
    return bool(_tesseract_bin())


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
