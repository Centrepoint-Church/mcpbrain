"""PDF -> Blocks: reading order (sort=True), headings by font size, ruled
tables via find_tables(), scanned pages via the tesseract fallback in
extractors. CONTRACT (Stage 0); implemented by unit 1b."""

import logging

from mcpbrain.sync.blocks import Heading, PartialBlocks, Paragraph, TableBlock

log = logging.getLogger(__name__)

_HEADING_RATIO = 1.2       # span size >= 1.2x the body size reads as a heading
_HEADING_MAX_CHARS = 200


def _split_ocr_paragraphs(text: str) -> list:
    """OCR text -> Paragraphs, split on blank lines.

    Deliberately does not use blocks.from_text: that function is still a
    stub owned by another unit at the time this module was written, and the
    OCR path only ever needs this trivial blank-line split.
    """
    return [Paragraph(p.strip()) for p in text.split("\n\n") if p.strip()]


def _page_blocks(page):
    """(y0, item) pairs for one page in reading order, plus span sizes
    weighted by character count (for the document's body size).

    `item` is either a TableBlock or a ("_text", text, max_size) tuple to be
    classified as Heading/Paragraph once the document's body size is known
    (the body size is a whole-document statistic, so classification can't
    happen per-page).
    """
    import fitz  # pymupdf

    tables = []
    try:
        for t in page.find_tables().tables:
            rows = [[("" if c is None else str(c)).strip() for c in r] for r in t.extract()]
            if rows:
                tables.append((fitz.Rect(t.bbox), TableBlock(rows)))
    except Exception as exc:  # noqa: BLE001 — table detection is best-effort
        log.debug("pdf: find_tables failed on page %s: %s", page.number, exc)

    out: list = []
    sizes: list = []
    d = page.get_text("dict", sort=True)
    for blk in d.get("blocks", []):
        if blk.get("type") != 0:
            continue
        x0, y0, x1, y1 = blk["bbox"]
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        if any(r.x0 <= cx <= r.x1 and r.y0 <= cy <= r.y1 for r, _ in tables):
            continue  # text belongs to a table; find_tables owns it
        lines, max_size = [], 0.0
        for ln in blk.get("lines", []):
            txt = "".join(s.get("text", "") for s in ln.get("spans", []))
            for s in ln.get("spans", []):
                n = len(s.get("text", "").strip())
                sizes.extend([float(s.get("size", 0))] * max(n, 0))
                max_size = max(max_size, float(s.get("size", 0)))
            if txt.strip():
                lines.append(txt.rstrip())
        if lines:
            out.append((y0, ("_text", "\n".join(lines), max_size)))
    for r, tb in tables:
        out.append((r.y0, tb))
    out.sort(key=lambda p: p[0])
    return out, sizes


def extract_blocks_from_pdf(content_bytes: bytes) -> list:
    """PDF -> Blocks in reading order (`sort=True`), headings by font size,
    ruled tables via find_tables(). Scanned pages keep the tesseract fallback
    and become Paragraphs (OCR text split on blank lines). [] on open
    failure; blocks.PartialBlocks if extraction died partway.
    """
    # Imported inside the function: extractors.py will later re-export this
    # module, so a top-level import here would cycle.
    from mcpbrain.sync.extractors import (
        _OCR_MIN_PAGE_CHARS,
        _ocr_page,
        _tesseract_available,
        is_scanned_pdf,
    )

    try:
        import fitz  # pymupdf
        doc = fitz.open(stream=content_bytes, filetype="pdf")
    except Exception as exc:
        log.warning("pdf: open failed: %s", exc)
        return []

    out: list = []
    try:
        pages_text = [page.get_text() for page in doc]
        scanned = is_scanned_pdf(content_bytes, pages=pages_text)
        if scanned and not _tesseract_available():
            log.warning(
                "pdf: looks scanned (%d pages, %d text chars) and tesseract is "
                "unavailable — returning the text layer only",
                len(pages_text), sum(len(p or "") for p in pages_text),
            )
        per_page: list = []
        all_sizes: list = []
        for i, page in enumerate(doc):
            if (scanned
                    and len((pages_text[i] or "").strip()) < _OCR_MIN_PAGE_CHARS
                    and _tesseract_available()):
                ocr = _ocr_page(page)
                if not ocr:
                    log.warning("pdf: OCR produced nothing for page %d", i + 1)
                per_page.append([(0.0, b) for b in _split_ocr_paragraphs(ocr or "")])
                continue
            items, sizes = _page_blocks(page)
            per_page.append(items)
            all_sizes.extend(sizes)

        body = sorted(all_sizes)[len(all_sizes) // 2] if all_sizes else 0.0
        heading_sizes = sorted(
            {
                round(b[2], 1)
                for items in per_page
                for _, b in items
                if isinstance(b, tuple) and body and b[2] >= body * _HEADING_RATIO
            },
            reverse=True,
        )
        for items in per_page:
            for _, b in items:
                if isinstance(b, tuple):
                    _, text, size = b
                    if (body and size >= body * _HEADING_RATIO
                            and len(text) <= _HEADING_MAX_CHARS and "\n" not in text.strip()):
                        level = (heading_sizes.index(round(size, 1)) + 1
                                 if round(size, 1) in heading_sizes else 1)
                        out.append(Heading(min(level, 6), text.strip()))
                    else:
                        out.append(Paragraph(text))
                else:
                    out.append(b)
        return out
    except Exception as exc:
        log.warning("pdf: extraction failed after %d blocks: %s", len(out), exc)
        return PartialBlocks(out) if out else []
    finally:
        doc.close()
