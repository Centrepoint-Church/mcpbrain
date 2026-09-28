"""DOCX and PPTX -> Blocks. CONTRACT (Stage 0); implemented by unit 1c."""


def extract_blocks_from_docx(content_bytes: bytes) -> list:
    """Body in order (w:p, w:tbl); headings; merged cells once; empty cells
    kept; headers first / footers last, each once; text boxes. [] on failure."""
    raise NotImplementedError("extract_blocks_from_docx: unit 1c")


def extract_blocks_from_pptx(content_bytes: bytes) -> list:
    """Heading per slide ('Slide N: <title>'), grouped shapes recursively,
    tables, speaker notes as a trailing 'Notes: ' Paragraph. [] on failure."""
    raise NotImplementedError("extract_blocks_from_pptx: unit 1c")
