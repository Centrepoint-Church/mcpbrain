"""FROZEN copy of chunking.chunk_text as of 0.7.131 — the oracle for the
byte-identity property in tests/test_chunking_split.py. Never edit."""
from mcpbrain.chunking import _PREFIX_HEADROOM_CHARS  # noqa: F401  (used by chunk_text)


def _hard_split(word: str, max_chars: int) -> list[str]:
    """Split a single whitespace-free token that is itself longer than the whole
    budget (a base64 blob, a minified line, a long URL). Without this the
    word-split path has no way to make progress and emits the token whole."""
    if len(word) <= max_chars:
        return [word]
    return [word[i:i + max_chars] for i in range(0, len(word), max_chars)]


def _split_paragraph(para: str, max_chars: int, overlap: int) -> list[str]:
    """Split one over-long paragraph on word boundaries.

    Two guarantees the previous implementation broke (B6): no emitted chunk is
    empty, and none exceeds max_chars. The old code appended `current`
    unconditionally on overflow — including on the first iteration when it was
    still "" — then seeded the next chunk with `overlap` words PLUS the oversize
    word without re-checking the budget.

    The overlap seed is kept whenever it still leaves room for the next piece,
    preserving the contract pinned by
    test_word_split_chunks_overlap_and_lose_nothing; it is dropped only in the
    hard-split case, where by construction no overlap can fit beside a piece
    that already fills the whole budget.
    """
    out: list[str] = []
    current = ""
    for word in para.split():
        for piece in _hard_split(word, max_chars):
            if not current:
                current = piece
            elif len(current) + 1 + len(piece) <= max_chars:
                current += " " + piece
            else:
                out.append(current)
                tail = " ".join(current.split()[-overlap:])
                current = (f"{tail} {piece}"
                           if len(tail) + 1 + len(piece) <= max_chars else piece)
    if current:
        out.append(current)
    return out


def chunk_text_v0(text: str, max_tokens: int = 500, overlap: int = 50) -> list[str]:
    """Split text into embeddable chunks on paragraph boundaries.

    Every returned chunk is non-empty and at most `max_tokens * 4` characters,
    and in fact at most `max_tokens * 4 - _PREFIX_HEADROOM_CHARS` whenever that
    is a meaningful reduction — see _PREFIX_HEADROOM_CHARS. The signature is
    locked, so the reservation happens inside.
    """
    max_chars = max_tokens * 4
    # Not applied when the whole requested budget is comparable to the headroom:
    # eating most of a deliberately tiny budget would change what such a caller
    # gets for no gain, since those chunks are nowhere near the embedder window.
    if max_chars >= _PREFIX_HEADROOM_CHARS * 4:
        max_chars -= _PREFIX_HEADROOM_CHARS
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        if len(current) + len(para) + 2 <= max_chars:
            current += ("\n\n" + para) if current else para
        elif len(para) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            pieces = _split_paragraph(para, max_chars, overlap)
            chunks.extend(pieces[:-1])
            current = pieces[-1] if pieces else ""
        else:
            if current:
                chunks.append(current)
            current = para
    if current:
        chunks.append(current)
    return [c for c in chunks if c] or ([text[:max_chars]] if text.strip() else [])
