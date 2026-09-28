"""PDF -> Blocks: reading order (sort=True), headings by font size, ruled
tables via find_tables(), scanned pages via the tesseract fallback in
extractors. CONTRACT (Stage 0); implemented by unit 1b."""


def extract_blocks_from_pdf(content_bytes: bytes) -> list:
    """[] on open failure; blocks.PartialBlocks if extraction died partway."""
    raise NotImplementedError("extract_blocks_from_pdf: unit 1b")
