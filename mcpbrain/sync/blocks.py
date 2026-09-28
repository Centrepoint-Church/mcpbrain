"""Structured extraction output and the one renderer that chunks it.

Extractors (sync/extract_pdf.py, sync/extract_office.py, sync/rtf.py) return an
ordered list of Blocks instead of a flat string, so reading order, paragraphs,
headings and tables survive to the chunker (2026-09-24 extraction-fidelity
spec). `render` is the single place a Block list becomes chunks; `to_text` is
the flat-string view kept for callers that only want text.

CONTRACT (Stage 0): types and constants are final; `to_text`, `from_text` and
`render` are implemented by unit 1a.
"""
import re
from dataclasses import dataclass, field

from mcpbrain.chunking import has_content, prose_max_chars, split_long_paragraph
from mcpbrain.sync import tabular

# Bump a MIME's number whenever its extractor's OUTPUT changes; the reflow
# selector re-chunks every owner of that MIME below the new number, and the
# shared-drive ingest-cache fingerprint includes it.
EXTRACTION_VERSIONS: dict[str, int] = {
    "application/pdf": 1,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": 1,
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": 1,
    "application/rtf": 1,
    "application/vnd.google-apps.document": 1,
    "application/vnd.google-apps.presentation": 1,
}

TRAIL_SEP = " › "


def extraction_version(mime: str) -> int:
    return EXTRACTION_VERSIONS.get(mime or "", 0)


@dataclass
class Heading:
    level: int
    text: str


@dataclass
class Paragraph:
    text: str


@dataclass
class TableBlock:
    rows: list[list[str]]
    caption: str = ""


class PartialBlocks(list):
    """list[Block] from an extraction that died partway (see extractors.PartialTables)."""


@dataclass
class Rendered:
    text: str
    meta: dict = field(default_factory=dict)
    spans: list[str] = field(default_factory=list)


def _table_line(row: list[str]) -> str:
    return " | ".join(c for c in row)


def to_text(blocks) -> str:
    """Flat text: blocks joined by blank lines, table rows as 'a | b'.
    Returns extractors.PartialText when given PartialBlocks."""
    from mcpbrain.sync.extractors import PartialText
    parts: list[str] = []
    for b in blocks:
        if isinstance(b, (Heading, Paragraph)):
            if b.text.strip():
                parts.append(b.text.strip())
        elif isinstance(b, TableBlock):
            lines = [_table_line(r) for r in b.rows if any(c.strip() for c in r)]
            if lines:
                parts.append("\n".join(lines))
    text = "\n\n".join(parts)
    return PartialText(text) if isinstance(blocks, PartialBlocks) else text


_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


def from_text(text: str) -> list:
    """Blank-line paragraphs; a single-line '#'-prefixed paragraph is a Heading."""
    out: list = []
    for para in re.split(r"\n\s*\n", text or ""):
        para = para.strip("\n")
        if not para.strip():
            continue
        lines = para.split("\n")
        m = _MD_HEADING.match(lines[0].strip())
        if m and len(lines) == 1:
            out.append(Heading(len(m.group(1)), m.group(2).strip()))
        else:
            out.append(Paragraph(para))
    return out


@dataclass
class _Piece:
    text: str
    spans: list[str]
    kind: str            # "heading" | "para" | "table"
    trail: str


def _lost_cell_pieces(values: list[str], trail: str, max_chars: int) -> list[_Piece]:
    """Emit cell/header text a row or caption rendering could not carry
    verbatim -- truncated by _fit_row_sentence's shrinking cell cap, dropped
    past _MAX_FIELDS_PER_ROW, or containing a newline _cell collapsed to a
    space -- as its own bounded piece.

    A span must be a literal substring of its chunk's text (spec Sec 2/3:
    reflow's coverage proof reads spans, so a false span is silent content
    loss). The safe response to "this cell didn't survive verbatim in its
    row's sentence" is not to just drop the span -- it is to also emit the
    cell in full somewhere, so the source text is never lost from retrieval
    even though it no longer rides inside the row's own rendering.
    """
    out: list[_Piece] = []
    seen: set[str] = set()
    for v in values:
        if not v or v in seen:
            continue
        seen.add(v)
        parts = [v] if len(v) <= max_chars else split_long_paragraph(v, max_chars, overlap=0)
        out.extend(_Piece(p, [p], "table", trail) for p in parts)
    return out


def _table_pieces(t: TableBlock, trail: str, max_chars: int) -> list[_Piece]:
    rows = tabular.normalise_rows([[("" if c is None else str(c)).strip() for c in r]
                                   for r in t.rows])
    if not rows:
        return []

    dropped: list[str] = []   # cell/header text no piece could carry verbatim

    if len(rows) == 1:
        cells = [c for c in rows[0] if c]
        line = _table_line(rows[0])
        if not has_content(line):
            return []
        # A single wide row is rendered as one 'a | b | c' line, same as
        # to_text -- but that line is unbounded, so route it through the
        # same bounded splitter as an oversize paragraph rather than
        # emitting one chunk that can be many times max_chars.
        parts = [line] if len(line) <= max_chars else split_long_paragraph(line, max_chars, overlap=0)
        pieces = [_Piece(p, [c for c in cells if c in p], "table", trail) for p in parts]
        claimed = {c for p in pieces for c in p.spans}
        dropped.extend(c for c in cells if c not in claimed)
        pieces.extend(_lost_cell_pieces(dropped, trail, max_chars))
        return pieces

    header, body = rows[0], rows[1:]
    where = f" (in {trail})" if trail else ""
    caption_full = f"Table{': ' + t.caption if t.caption else ''}{where}"
    # A caption with no length bound (a long user-supplied caption, or a deep
    # heading trail) could eat the whole budget or push it negative, in which
    # case _fit_row_sentence's own floor can't save the piece -- the caption
    # itself would already exceed max_chars before a single row is added.
    # Keep at least half the budget for rows by truncating the caption.
    cap_floor = max(max_chars // 2, 1)
    max_caption_len = max(max_chars - cap_floor - 1, 0)
    caption = (caption_full[:max_caption_len] if len(caption_full) > max_caption_len
               else caption_full)
    budget = max_chars - len(caption) - 1
    pieces: list[_Piece] = []
    cur: list[str] = []
    cur_cells: list[str] = []

    def flush_rows():
        if not cur:
            return
        text = caption + "\n" + "\n".join(cur)
        spans = [c for c in cur_cells if c in text]
        dropped.extend(c for c in cur_cells if c not in text)
        pieces.append(_Piece(text, spans, "table", trail))

    for r in body:
        sent = tabular._fit_row_sentence(header, r, budget)
        if len(sent) > budget:
            # _fit_row_sentence only shrinks CELL VALUES (to a 5-char floor);
            # it cannot do anything about an oversize HEADER LABEL or the
            # "H: v; " per-field overhead, so the row sentence itself can
            # still be longer than the whole chunk budget. Flush whatever is
            # already batched, then bound this row the same way an oversize
            # single-row line is bounded: split it directly, deriving each
            # piece's spans from what THAT piece actually contains, and
            # re-emitting (via _lost_cell_pieces, below) any cell text that
            # ends up in none of them.
            flush_rows()
            cur, cur_cells = [], []
            row_cells = [c for c in r if c]
            row_parts = split_long_paragraph(sent, max_chars, overlap=0)
            row_pieces = [_Piece(p, [c for c in row_cells if c in p], "table", trail)
                          for p in row_parts]
            claimed = {c for p in row_pieces for c in p.spans}
            dropped.extend(c for c in row_cells if c not in claimed)
            pieces.extend(row_pieces)
            continue
        if cur and len("\n".join(cur + [sent])) > budget:
            flush_rows()
            cur, cur_cells = [], []
        cur.append(sent)
        cur_cells.extend(c for c in r if c)
    flush_rows()
    # header cells are source text too: attribute them to the first piece,
    # but only the ones it actually contains verbatim.
    if pieces:
        header_present = [c for c in header if c and c in pieces[0].text]
        dropped.extend(c for c in header if c and c not in pieces[0].text)
        pieces[0].spans = header_present + pieces[0].spans
    pieces.extend(_lost_cell_pieces(dropped, trail, max_chars))
    return pieces


def render(blocks, *, max_chars: int | None = None) -> list[Rendered]:
    """Blocks -> chunks with heading_trail meta and source spans."""
    max_chars = max_chars or prose_max_chars()
    trail: list[tuple[int, str]] = []
    pieces: list[_Piece] = []
    for b in blocks:
        tr = TRAIL_SEP.join(t for _, t in trail)
        if isinstance(b, Heading):
            text = b.text.strip()
            if not has_content(text):
                continue
            while trail and trail[-1][0] >= b.level:
                trail.pop()
            trail.append((b.level, text[:120]))
            pieces.append(_Piece(text[:max_chars], [text[:max_chars]], "heading",
                                 TRAIL_SEP.join(t for _, t in trail)))
        elif isinstance(b, Paragraph):
            text = b.text.strip("\n")
            if not has_content(text):
                continue
            parts = [text] if len(text) <= max_chars else split_long_paragraph(text, max_chars)
            pieces.extend(_Piece(p, [p], "para", tr) for p in parts)
        elif isinstance(b, TableBlock):
            pieces.extend(_table_pieces(b, tr, max_chars))

    out: list[Rendered] = []
    cur: list[_Piece] = []

    def flush():
        if not cur:
            return
        text = "\n\n".join(p.text for p in cur)
        meta = {}
        tr = cur[0].trail
        if tr:
            meta["heading_trail"] = tr[:300]
        out.append(Rendered(text, meta, [s for p in cur for s in p.spans]))
        cur.clear()

    for p in pieces:
        size = sum(len(x.text) + 2 for x in cur) + len(p.text)
        if cur and (size > max_chars
                    or (p.kind == "heading" and size - len(p.text) > max_chars // 2)
                    or (p.kind == "table" and cur[-1].kind != "table" and size > max_chars)):
            flush()
        cur.append(p)
    flush()
    return [r for r in out if has_content(r.text)]
