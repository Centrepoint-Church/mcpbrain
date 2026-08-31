import hashlib
import re
import unicodedata


# Version of the chunking pipeline that produced a chunk. Stamped into every
# chunk's metadata at write time, and the FLOOR for the chunker_version fed to
# pipeline_fingerprint (which keys the shared-drive ingest-cache artifact
# filename AND gates ingest_cache.try_import).
#
# 1 -> 2 (spec 2, 2026-07-28): chunk_text no longer emits empty or oversize
#   chunks; tabular sources are chunked by header-repeating row group instead of
#   character-split; content-free text is never written as a chunk.
#
# 2 -> 3: the tabular renderer redesign (schema-enriched row sentences,
#   replacing the fixed-width markdown grid) and normalise_rows' minimum-
#   row-support fix both change what a "correctly chunked" table looks like --
#   every existing chunk below this version gets re-fetched and re-rendered by
#   bin/repair.py's generalized reingest-stale sweep.
#
# Bumping this is what makes a chunking change VISIBLE, in two ways:
#   * `WHERE COALESCE(chunker_version, 0) < CHUNKER_VERSION` is the
#     level-triggered selector bin/repair.py walks; and
#   * it invalidates stale fleet ingest-cache artifacts — but only because
#     `ingest_cache.effective_chunker_version` FLOORS the org pin's value at
#     this constant. The fingerprint is keyed off the fleet-DISTRIBUTED pin, and
#     the live pin sets chunker_version explicitly, so bumping this constant
#     alone would otherwise change nothing about the cache (config.fleet_pin's
#     default only applies when the key is absent). See that function.
# Bump it whenever chunk boundaries or chunk admission change.
CHUNKER_VERSION = 3

# The version floor below which EVERY content type is considered stale --
# these chunks predate the 2026-07-28 headroom fix (1 -> 2) and have needed
# re-chunking since before this release, regardless of source or subtype.
# CHUNKER_VERSION (3) only ADDITIONALLY invalidates table-subtype content (the
# phantom-column rendering fix, which changed nothing about prose chunking).
# store.stale_chunker_ids uses both floors so a chunker_version bump doesn't
# accidentally mark every historical prose chunk in the corpus stale too --
# chunk_text (which renders every non-table chunk) is unchanged by the 2->3
# bump, so re-fetching prose already at version 2 would burn real API quota
# for a byte-identical re-chunk and zero benefit.
PRIOR_CHUNKER_VERSION = 2


# Leading honorifics stripped from a name so "Ps Joel" / "Pastor Joel Chelliah"
# canonicalise to the bare name. Matched case-insensitively with any trailing
# full-stop removed.
_HONORIFICS = {"pastor", "ps", "pr", "rev", "reverend", "dr", "mr", "mrs", "ms", "miss",
               "sis", "sister", "bro", "brother", "aunty", "uncle"}


def _canonical_name(name) -> str:
    """Collapse whitespace and strip a leading honorific from a name.

    None-safe. A bare honorific with no following word is left unchanged so we
    don't erase a name down to nothing. Lives beside slugify (no Gemini
    dependency); re-exported from enrich.py for backwards compatibility.
    """
    s = " ".join((name or "").split())   # None-safe + collapse whitespace
    if not s:
        return ""
    parts = s.split(" ")
    head = parts[0].rstrip(".").lower()
    if head in _HONORIFICS and len(parts) > 1:
        return " ".join(parts[1:])
    return s


_NAME_TOKEN_MIN = 4


def name_tokens(name: str) -> list[str]:
    """Distinctive (>= 4 char) lowercase alphanumeric tokens of a name.

    Shared by drain._name_grounded ("is this extracted name present in the
    source?") and prepare's context scoping ("which known people does this unit
    mention?"). Same heuristic run in opposite directions — one owner so they
    cannot drift.
    """
    return [t for t in re.split(r"[^a-z0-9]+", (name or "").strip().lower())
            if len(t) >= _NAME_TOKEN_MIN]


def name_in_text(name: str, haystack_lower: str) -> bool:
    """True when the full name, or any distinctive token of it, is in the text."""
    n = (name or "").strip().lower()
    if not n:
        return False
    if n in haystack_lower:
        return True
    return any(t in haystack_lower for t in name_tokens(n))


# Near-duplicate action fingerprint normalisation (memory_db.py:1748-1777).
# Single source of truth shared by graph_write (insert path) and store (text
# rewrite path) — both must produce byte-for-byte equal fingerprints for the
# near-duplicate guard to work. Lives here beside slugify because chunking is
# dependency-free (stdlib only), so neither caller risks a circular import.
_DEDUP_TITLE_CHARS = re.compile(r"[^\w\s]+")
_DEDUP_STOPWORDS = {
    "the", "a", "an", "and", "or", "to", "of", "for", "with", "on", "in",
    "at", "by", "is", "are", "was", "be", "have", "has", "that", "this",
    "it", "i", "you", "we", "do", "not",
}


def _normalise_title_for_dedup(text: str) -> str:
    """Lowercase, drop punctuation, drop short stopwords (memory_db.py:1756)."""
    if not text:
        return ""
    s = _DEDUP_TITLE_CHARS.sub(" ", text.lower())
    return " ".join(t for t in s.split() if t and t not in _DEDUP_STOPWORDS)


def action_fingerprint(text: str) -> str:
    """SHA1 of normalised action text (memory_db.py:1766).

    The near-duplicate guard depends on graph_write (insert) and store (text
    rewrite) computing identical fingerprints; both import this function so the
    normalisation and SHA stay in lockstep.
    """
    norm = _normalise_title_for_dedup(text)
    return hashlib.sha1(norm.encode()).hexdigest() if norm else ""


def slugify(name: str) -> str:
    """Lower-case, collapse runs of non-alphanumerics into single hyphens, truncate to 80 chars.

    "Taryn Hamilton" -> "taryn-hamilton"; "ACC (National)" -> "acc-national".
    Empty or all-non-alphanumeric input returns "" (callers skip empty slugs).
    None / non-str input also returns "" (a present-but-null JSON name yields
    Python None, which would otherwise crash on .lower()).
    Truncates to 80 characters to keep entity_id columns manageable.
    """
    if not name or not isinstance(name, str):
        return ""
    # Fold diacritics (NFKD decompose, drop combining marks) so accented and
    # ASCII spellings of the same name collapse to one slug ("Chané" -> "chane").
    name = unicodedata.normalize("NFKD", name)
    name = "".join(ch for ch in name if not unicodedata.combining(ch))
    s = re.sub(r"[^a-z0-9]+", "-", name.lower())
    return s.strip("-")[:80]


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


import json as _json  # noqa: E402 — grouped after stdlib-only section

# Allowed thread content types. Single owner: contract.py imports from here
# rather than re-declaring it so the enum can't drift from the gate.
_VALID_CONTENT_TYPES = {"request", "update", "decision", "fyi", "notification"}

# Allowed declared entity types. A model-declared type outside this set is
# clamped to "topic".
_VALID_TYPES = ("person", "org", "project", "topic")

# Structural junk patterns applied to both person AND org.
_STRUCTURAL_JUNK = [
    re.compile(r"^(Re|Fwd|FW|RE|FWD)\s*:", re.IGNORECASE),
    re.compile(r"https?://"),
    re.compile(r"\w+@\w+\.\w+"),
    re.compile(r"[|{}\[\]<>]"),
]

# Numeric junk patterns applied to person ONLY.
_NUMERIC_JUNK = [
    re.compile(r"\d{4}"),
    re.compile(r"\d{2,}/\d{2,}"),
]


def _is_junk_entity(name: str, etype: str) -> bool:
    """Reject obviously-bad person/org entities."""
    if etype not in ("person", "org"):
        return False
    name = (name or "").strip()
    if len(name) < 2 or len(name) > 60:
        return True
    for pattern in _STRUCTURAL_JUNK:
        if pattern.search(name):
            return True
    if etype == "person":
        for pattern in _NUMERIC_JUNK:
            if pattern.search(name):
                return True
    return False


def _strip_fences(raw: str) -> str:
    s = raw.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z0-9]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()


def _parse_first_json_object(raw: str) -> dict:
    """Parse the first complete JSON OBJECT from raw, ignoring trailing content."""
    s = _strip_fences(raw)
    decoder = _json.JSONDecoder()
    pos = 0
    while True:
        start = s.find("{", pos)
        if start == -1:
            raise ValueError("no JSON object in model output")
        try:
            obj, _end = decoder.raw_decode(s[start:])
        except _json.JSONDecodeError:
            pos = start + 1
            continue
        if isinstance(obj, dict):
            return obj
        pos = start + 1


def has_content(text: str) -> bool:
    """True when `text` carries at least one alphanumeric character.

    The generic no-content guard. B1's empty-spreadsheet chunks are ~2,000-char
    strings of '| | | | |' — 66,653 of them, 37% of the live store, every one
    embedded and none matchable by any query.

    `str.isalnum()` per character, not a `[A-Za-z0-9]` regex: a sheet of Chinese
    or accented names is content and must not be discarded as noise.
    """
    return any(ch.isalnum() for ch in text)


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


# The BGE window is 512 tokens ≈ 2,000 characters, and embed.contextual_prefix
# (default ON) prepends ~100 chars of provenance to every gmail/gdrive passage at
# embed time — measured against the SAME window (index.EMBED_WINDOW_CHARS, which
# checks the PREFIXED text). So a chunk packed to the full max_tokens*4 tips over
# it and B3's silent tail-truncation applies to the bulk of the corpus, not an
# edge case. Reserve the headroom here, at write time, exactly as
# semantic.SEMANTIC_MAX_CHARS (1800) already does for the one document
# chunk_text cannot bound.
_PREFIX_HEADROOM_CHARS = 200

# The separator chunk_text splits paragraphs on — and therefore the one any
# caller that REASSEMBLES a chunked body must re-join with (store.note_chunks,
# thread_enrich._join_with_gaps).
#
# chunk_text is not a lossless splitter and is not meant to be: _split_paragraph
# duplicates the last `overlap` words across a boundary and paragraph whitespace
# is stripped/collapsed, both correct for its embedding callers, which never
# rejoin. A caller that DOES rejoin (drain.drain_captures' note path,
# bin/rechunk_notes.py) must verify `CHUNK_JOIN.join(chunk_text(t)) == t` before
# treating the split as a faithful representation of `t`.
CHUNK_JOIN = "\n\n"


# Separators a lossless split prefers to break AFTER, strongest first. The
# separator stays with the piece that precedes it, which is what makes
# "".join(pieces) reproduce the input byte-for-byte.
_LOSSLESS_BREAKS = ("\n\n", "\n", " ")


# Per-piece char budget for a losslessly-split note. Derived from the SAME
# window chunk_text lands on (max_tokens*4 minus the contextual-prefix
# headroom) so the two paths cannot drift apart on the embedder's 512-token
# window -- a note piece is embedded exactly like any other chunk.
NOTE_MAX_CHARS = 500 * 4 - _PREFIX_HEADROOM_CHARS


def split_lossless(text: str, max_chars: int = NOTE_MAX_CHARS) -> list[str]:
    """Split `text` into pieces of at most `max_chars` such that
    ``"".join(split_lossless(t, n)) == t`` for ANY input.

    This is the counterpart to chunk_text, not a replacement for it. chunk_text
    is a RETRIEVAL chunker: it strips and collapses paragraph whitespace and,
    via _split_paragraph, duplicates the last `overlap` words across a boundary.
    Both are correct for callers that only ever embed the pieces and never
    rejoin them. They make it structurally unable to round-trip a paragraph
    larger than the budget -- measured on the live store, that is EVERY one of
    the 930 captured notes still carrying an unembedded tail, and no `overlap`
    setting changes it (overlap=0 rescues zero of them).

    Notes need both properties at once: the whole body embedded, and the exact
    original recoverable, because store.note_chunks() serves the reassembled
    text AS the note and bin/rechunk_notes.py deletes the only whole-body row.
    Carrying each break's separator on the preceding piece and rejoining with
    "" gives losslessness by construction rather than by verification.

    Breaks are preferred after a blank line, then a newline, then a space, and
    fall back to a hard cut for a token with no separator at all (a base64 blob,
    a minified line) -- still lossless, since nothing is inserted or dropped.
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if len(text) <= max_chars:
        return [text] if text else []
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if n - i <= max_chars:
            out.append(text[i:])
            break
        window = text[i:i + max_chars]
        cut = -1
        for sep in _LOSSLESS_BREAKS:
            # rfind, and +len(sep) so the separator ends the CURRENT piece.
            found = window.rfind(sep)
            if found > 0:
                cut = found + len(sep)
                break
        if cut <= 0:
            cut = max_chars          # unbreakable run: hard cut
        out.append(text[i:i + cut])
        i += cut
    return out


def chunk_text(text: str, max_tokens: int = 500, overlap: int = 50) -> list[str]:
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
