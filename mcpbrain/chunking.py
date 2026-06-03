import hashlib
import re
import unicodedata


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
    """Lower-case, collapse runs of non-alphanumerics into single hyphens.

    "Taryn Hamilton" -> "taryn-hamilton"; "ACC (National)" -> "acc-national".
    Empty or all-non-alphanumeric input returns "" (callers skip empty slugs).
    None / non-str input also returns "" (a present-but-null JSON name yields
    Python None, which would otherwise crash on .lower()).
    """
    if not name or not isinstance(name, str):
        return ""
    # Fold diacritics (NFKD decompose, drop combining marks) so accented and
    # ASCII spellings of the same name collapse to one slug ("Chané" -> "chane").
    name = unicodedata.normalize("NFKD", name)
    name = "".join(ch for ch in name if not unicodedata.combining(ch))
    s = re.sub(r"[^a-z0-9]+", "-", name.lower())
    return s.strip("-")


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def chunk_text(text: str, max_tokens: int = 500, overlap: int = 50) -> list[str]:
    max_chars = max_tokens * 4
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks, current = [], ""
    for para in paragraphs:
        if len(current) + len(para) + 2 <= max_chars:
            current += ("\n\n" + para) if current else para
        else:
            if current:
                chunks.append(current)
            if len(para) > max_chars:
                # Word-split path: seeds each new chunk with the last `overlap` words of the
                # previous chunk (NOT the full chunk). Paragraph-boundary chunks never overlap.
                # Faithful port of src/embedder.py chunk_text.
                words, current = para.split(), ""
                for w in words:
                    if len(current) + len(w) + 1 <= max_chars:
                        current += (" " + w) if current else w
                    else:
                        chunks.append(current)
                        current = " ".join(current.split()[-overlap:]) + " " + w
            else:
                current = para
    if current:
        chunks.append(current)
    return chunks if chunks else [text[:max_chars]]
