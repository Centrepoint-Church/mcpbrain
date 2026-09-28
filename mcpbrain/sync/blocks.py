"""Structured extraction output and the one renderer that chunks it.

Extractors (sync/extract_pdf.py, sync/extract_office.py, sync/rtf.py) return an
ordered list of Blocks instead of a flat string, so reading order, paragraphs,
headings and tables survive to the chunker (2026-09-24 extraction-fidelity
spec). `render` is the single place a Block list becomes chunks; `to_text` is
the flat-string view kept for callers that only want text.

CONTRACT (Stage 0): types and constants are final; `to_text`, `from_text` and
`render` are implemented by unit 1a.
"""
from dataclasses import dataclass, field

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


def to_text(blocks) -> str:
    """Flat text: blocks joined by blank lines, table rows as 'a | b'.
    Returns extractors.PartialText when given PartialBlocks."""
    raise NotImplementedError("blocks.to_text: unit 1a")


def from_text(text: str) -> list:
    """Blank-line paragraphs; a single-line '#'-prefixed paragraph is a Heading."""
    raise NotImplementedError("blocks.from_text: unit 1a")


def render(blocks, *, max_chars: int | None = None) -> list[Rendered]:
    """Blocks -> chunks with heading_trail meta and source spans."""
    raise NotImplementedError("blocks.render: unit 1a")
