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


def _table_pieces(t: TableBlock, trail: str, max_chars: int) -> list[_Piece]:
    rows = tabular.normalise_rows([[("" if c is None else str(c)).strip() for c in r]
                                   for r in t.rows])
    if not rows:
        return []
    if len(rows) == 1:
        line = _table_line(rows[0])
        return [_Piece(line, [c for c in rows[0] if c], "table", trail)] if has_content(line) else []
    header, body = rows[0], rows[1:]
    where = f" (in {trail})" if trail else ""
    caption = f"Table{': ' + t.caption if t.caption else ''}{where}"
    budget = max_chars - len(caption) - 1
    pieces, cur, cur_cells = [], [], []
    for r in body:
        sent = tabular._fit_row_sentence(header, r, budget)
        if cur and len("\n".join(cur + [sent])) > budget:
            pieces.append(_Piece(caption + "\n" + "\n".join(cur), cur_cells, "table", trail))
            cur, cur_cells = [], []
        cur.append(sent)
        cur_cells.extend(c for c in r if c)
    if cur:
        pieces.append(_Piece(caption + "\n" + "\n".join(cur), cur_cells, "table", trail))
    # header cells are source text too: attribute them to the first piece
    if pieces:
        pieces[0].spans = [c for c in header if c] + pieces[0].spans
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
