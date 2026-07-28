"""Gmail message normalisation — raw dict -> list[Chunk].

Converts a Gmail messages.get(format=full) response into indexable chunks.
No Google API calls here; this module is pure data transformation.
"""

import base64
import re
from dataclasses import dataclass

from mcpbrain.chunking import CHUNKER_VERSION, chunk_text, content_hash, has_content


@dataclass
class Chunk:
    doc_id: str
    text: str
    content_hash: str
    metadata: dict


# ---------------------------------------------------------------------------
# Constants — ported verbatim from src/ingest_gmail.py
# ---------------------------------------------------------------------------

_SIGNATURE_DELIMITERS = ['\n-- \n', '\n--\n']

_SIGNATURE_OPENERS = [
    '\nregards,', '\nkind regards,', '\nwarm regards,', '\nbest regards,',
    '\nbest,', '\nthanks,', '\nthank you,', '\ncheers,', '\nblessings,',
    '\nin christ,', '\nyours sincerely,', '\nsincerely,', '\nwarmly,',
    '\nmany thanks,',
]

_REPLY_CHAIN_PATTERNS = [
    re.compile(
        r'\nOn (?:Mon|Tue|Wed|Thu|Fri|Sat|Sun|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|\d)'
        r'.{0,250}?wrote:\s*\n', re.DOTALL),
    re.compile(r'\n-{3,}\s*Original Message\s*-{3,}', re.IGNORECASE),
    re.compile(r'\n-{5,}\s*Forwarded message\s*-{5,}', re.IGNORECASE),
    re.compile(r'\n_{10,}'),
    re.compile(r'\nFrom: .+\nSent: .+\nTo: ', re.IGNORECASE),
]


# ---------------------------------------------------------------------------
# Helper functions — ported verbatim from src/ingest_gmail.py
# ---------------------------------------------------------------------------

def get_header(headers_list: list, name: str) -> str:
    for h in headers_list:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _find_part_text(payload: dict, mime_type: str) -> str:
    if payload.get("mimeType") == mime_type:
        data = payload.get("body", {}).get("data")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    for part in payload.get("parts", []):
        result = _find_part_text(part, mime_type)
        if result:
            return result
    return ""


# Tags that end a line of prose. Everything else is INLINE and is stripped to
# nothing, keeping the surrounding words in one sentence (I5): the fallback used
# to emit '\n' for EVERY tag, so `<p>Hi <b>Sam</b>, can you confirm
# <a href=…>the booking</a>?</p>` came out as five separate lines — every bold
# name, link and <span> shredded the sentence it sat in, which wrecks both the
# embedding and the signature/quote heuristics that read whole lines.
_BLOCK_TAGS = frozenset({
    "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt",
    "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3",
    "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre",
    "section", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
})

# One HTML tag: group 1 is its name when it has one (so `<!-- … -->` and stray
# `<3` style junk fall through to the inline/strip branch).
_HTML_TAG = re.compile(r"<\s*/?\s*([A-Za-z][A-Za-z0-9]*)?[^>]*>")


def _tag_replacement(m: re.Match) -> str:
    return "\n" if (m.group(1) or "").lower() in _BLOCK_TAGS else ""


def strip_html(html: str) -> str:
    """Convert HTML email body to plain text. bs4 if available, else regex.

    The regex fallback replaces every BLOCK-level tag with a newline (not a
    space): quoted history in HTML mail is delimited by tag boundaries (a
    `<div>` "wrote:" line, a `<blockquote>`), and `strip_reply_chains`'s
    patterns anchor on literal '\\n' — collapsing tags to spaces would fuse the
    whole message onto one line and make every quote boundary invisible to that
    regex. Inline tags (`<b>`, `<a>`, `<span>`, …) are stripped to nothing so a
    sentence stays on one line — see _BLOCK_TAGS.
    """
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "head"]):
            tag.decompose()
        text = soup.get_text(separator="\n")
    except Exception:
        text = _HTML_TAG.sub(_tag_replacement, html)
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


# Lines that belong to a quote's attribution block rather than to a reply
# written under it. Matched against the TAIL only (everything after the reply
# marker), where a genuine bottom-post is the only thing that should survive.
_QUOTE_HEADER_LINE = re.compile(
    r'^\s*(from|sent|to|cc|bcc|subject|date|on|reply-to)\b.*$', re.IGNORECASE)

# A bottom-post shorter than this is boilerplate ("Sent from my iPhone",
# "[Quoted text hidden]"), not content. Err toward dropping: a false rescue
# re-introduces the same noise on every reply in the corpus, while a false drop
# costs one short line.
_MIN_BOTTOM_POST_CHARS = 40


def _truncate_at_reply_marker(text: str) -> str:
    """Cut `text` at the FIRST _REPLY_CHAIN_PATTERNS marker inside it.

    The '>' gate in strip_reply_chains establishes that the message contains
    '>'-quoting somewhere at/after the outermost reply marker, but a real message
    can contain BOTH: a '>'-quoted line immediately followed by an unquoted
    Outlook/forward block (thread crossing mail clients, or Gmail collapsing only
    part of the history). Taking everything after the last '>' line then rescued
    that unquoted block wholesale and attributed the ORIGINAL author's prose to
    the replying sender — the very misattribution the C1 gate exists to prevent,
    still reachable one level down.

    So the marker patterns are re-applied to the rescued region itself, exactly as
    they are at the top level. The search runs against a '\\n'-prefixed copy
    because every pattern anchors on a literal preceding newline and the marker is
    commonly the rescued tail's FIRST line (the offset is undone below); a match at
    that synthetic newline means the whole region is quoted history and nothing
    survives.
    """
    probe = "\n" + text
    cut = len(probe)
    for pattern in _REPLY_CHAIN_PATTERNS:
        m = pattern.search(probe)
        if m and m.start() < cut:
            cut = m.start()
    return text if cut == len(probe) else text[:max(cut - 1, 0)]


def _bottom_posted_reply(lines: list[str]) -> str:
    """Prose written BELOW a quoted chain.

    `lines` are the text lines strictly after the LAST '>'-quoted line of the
    quote (see strip_reply_chains): the quote's own body is therefore already
    gone, and what is left is its trailing attribution lines plus, if the sender
    bottom-posted, their actual message.

    The candidate region is then cut at any reply marker found INSIDE it
    (_truncate_at_reply_marker) — mixed '>'-quoted / unquoted history otherwise
    slips the caller's '>' gate — and the length floor is applied AFTER that cut,
    so a region that is nothing but quoted history rescues nothing at all
    (the same "err toward dropping" policy as _MIN_BOTTOM_POST_CHARS).

    Only sound where the quote really was '>'-prefixed — the caller enforces
    that. Callers must not apply this to HTML-derived text, where the quote is
    markup rather than '>' prefixes and the whole quoted history would survive
    as if it were new prose (see extract_body_with_signature).
    """
    kept = [ln for ln in lines
            if ln.strip() and not _QUOTE_HEADER_LINE.match(ln)]
    joined = _truncate_at_reply_marker("\n".join(kept)).strip()
    return joined if len(joined) >= _MIN_BOTTOM_POST_CHARS else ""


def strip_reply_chains(text: str, *, rescue_bottom_post: bool = True) -> str:
    """Remove quoted history, keeping BOTH the text above the quote and any reply
    written below it (A3).

    The old implementation returned `text[:earliest]`, correct for top-posting —
    the overwhelmingly common case — and silently discarding every bottom-posted
    reply along with the quote it sat under.

    C1: the rescue only runs when the quote was demonstrably '>'-PREFIXED. Four
    of the five _REPLY_CHAIN_PATTERNS match plain-text quoting that carries no
    '>' at all (Outlook/Exchange `-----Original Message-----`, forwarded-message
    banners, underscore rules, bare `From:/Sent:/To:` blocks — ordinary
    inter-org and vendor mail). There, everything after the marker is the
    ORIGINAL author's words, and rescuing it appended the entire quoted message
    to the reply, attributed in the graph to the REPLYING sender: finding D's
    duplication back, plus misattributed commitments — strictly worse than the
    A3 defect the rescue fixes. So with no '>' evidence next to the marker we
    fall back to the old `text[:earliest]` behaviour and lose the (rare) genuine
    bottom-post, matching this function's stated preference elsewhere
    (_MIN_BOTTOM_POST_CHARS: "err toward dropping").

    The '>' evidence is necessary but not sufficient: a message can mix
    '>'-quoted history with an unquoted Outlook/forward block, which passes this
    gate and then gets rescued from below the last '>' line. _bottom_posted_reply
    re-applies the marker patterns inside the rescued region for that case.
    """
    # Blank the '>' lines in place rather than deleting them, so the quote's
    # EXTENT is still locatable below; `blanked` keeps one entry per input line.
    raw_lines = text.split("\n")
    quote_lines = {i for i, ln in enumerate(raw_lines) if ln.startswith(">")}
    blanked = "\n".join("" if i in quote_lines else ln
                        for i, ln in enumerate(raw_lines))

    earliest = len(blanked)
    for pattern in _REPLY_CHAIN_PATTERNS:
        m = pattern.search(blanked)
        if m and m.start() < earliest:
            earliest = m.start()
    # The \n{3,} collapse is applied to the head only (it used to run over the
    # whole text before the search). It can never create or destroy a pattern
    # match — it always leaves at least one blank line, so two non-blank lines
    # never become adjacent — and the head is stripped either way.
    head = re.sub(r'\n{3,}', '\n\n', blanked[:earliest]).strip()
    if not rescue_bottom_post or earliest == len(blanked):
        return head
    # Every _REPLY_CHAIN_PATTERNS entry anchors on a literal '\n', so `earliest`
    # points AT that newline: it terminates line `marker_line`, and the marker's
    # own text starts on the next one.
    marker_line = blanked.count("\n", 0, earliest)
    quoted_after = [i for i in quote_lines if i >= marker_line]
    if not quoted_after:
        return head          # no '>' evidence — do not rescue (see above)
    tail = _bottom_posted_reply(raw_lines[max(quoted_after) + 1:])
    return f"{head}\n\n{tail}".strip() if tail else head


def extract_signature_block(text: str) -> tuple[str, str]:
    if not text:
        return "", ""
    earliest = len(text)
    for delim in _SIGNATURE_DELIMITERS:
        idx = text.find(delim)
        if 0 <= idx < earliest:
            earliest = idx
    lower = text.lower()
    for opener in _SIGNATURE_OPENERS:
        idx = lower.find(opener)
        if idx != -1 and idx < earliest and idx > len(text) * 0.3:
            earliest = idx
    if earliest == len(text):
        return text.strip(), ""
    return text[:earliest].strip(), text[earliest:].strip()


def extract_body_with_signature(payload: dict) -> tuple[str, str]:
    """Return (stripped_body, signature_block). Plain text first, HTML fallback.
    Runs reply-chain stripping before signature extraction.

    The bottom-post rescue is enabled for the plain-text branch only: it relies
    on '>' quoting having been stripped first, which is meaningless for HTML.
    """
    text = _find_part_text(payload, "text/plain")
    if text and len(text.strip()) > 10:
        text = strip_reply_chains(text)
        return extract_signature_block(text)
    html = _find_part_text(payload, "text/html")
    if html:
        text = strip_reply_chains(strip_html(html), rescue_bottom_post=False)
        return extract_signature_block(text)
    return "", ""


# ---------------------------------------------------------------------------
# Bulk / newsletter / auto-reply filter
# ---------------------------------------------------------------------------

def _is_bulk_or_auto(headers: list, subject: str) -> bool:
    """True for newsletters / mailing lists / marketing / auto-replies — generic bulk mail.
    Header-based so it generalises across users (no person-specific subject list)."""
    if get_header(headers, "list-unsubscribe"):
        return True
    if get_header(headers, "list-id"):
        return True
    if get_header(headers, "precedence").lower() in ("bulk", "list", "junk"):
        return True
    auto = get_header(headers, "auto-submitted").lower()
    if auto and auto != "no":
        return True
    s = subject.lower().strip()
    if s.startswith(("out of office", "automatic reply", "auto-reply")):
        return True
    return False


# ---------------------------------------------------------------------------
# Locked-interface entry point
# ---------------------------------------------------------------------------

def _note(report: dict | None, reason: str) -> None:
    if report is not None:
        report[reason] = report.get(reason, 0) + 1


def normalise_gmail(raw: dict, *, report: dict | None = None) -> list[Chunk]:
    """Raw Gmail message (messages.get format=full) -> list[Chunk].
    doc_id = gmail-<id>-body-<i>. Empty body -> [].

    `report`, when passed, is mutated in place to {reason: count} for every
    message that produced no chunks. Without it a drop is invisible: sync_gmail
    counts a bulk-filtered message as processed either way (A4).
    """
    msg_id = raw["id"]
    payload = raw.get("payload", {})
    headers = payload.get("headers", [])
    subject = get_header(headers, "subject")
    # A4: this used to `return []` for bulk mail. The drop is gone — see the
    # `bulk` stamp below and prepare.should_enrich, which is the gate that
    # actually belongs in this role.
    bulk = _is_bulk_or_auto(headers, subject)
    body, signature_block = extract_body_with_signature(payload)
    if not body:
        _note(report, "empty_body")
        return []
    to = get_header(headers, "to")
    cc = get_header(headers, "cc")
    base_metadata = {
        "source_type": "gmail",
        "chunker_version": CHUNKER_VERSION,
        "message_id": msg_id,
        "thread_id": raw.get("threadId", ""),
        "subject": subject[:200],
        "sender": get_header(headers, "from")[:200],
        # C6: was to[:300]/cc[:300], which loses most recipients of an all-staff
        # email. The counts are kept separately so a truncation that DOES happen
        # is still visible rather than inferred from a clipped string.
        "to": to[:2000],
        "cc": cc[:2000],
        "to_count": to.count("@"),
        "cc_count": cc.count("@"),
        "date": get_header(headers, "date")[:80],
        "labels": ",".join(raw.get("labelIds", []))[:200],
        "signature_block": signature_block[:500],
    }
    if bulk:
        # Marked, not dropped. prepare.should_enrich reads this and cold-marks
        # the chunk: embedded and searchable, never graph-extracted, reversible.
        # A header-based signal (List-Id / List-Unsubscribe / Precedence) is
        # strictly stronger than the Gmail CATEGORY_* labels should_enrich
        # already checks, so this improves that gate as well as replacing this
        # one.
        base_metadata["bulk"] = True
    pieces = [c for c in chunk_text(body) if has_content(c)]
    out = []
    for i, chunk in enumerate(pieces):
        meta = {**base_metadata, "content_type": "email_body",
                "chunk_index": i, "chunk_total": len(pieces)}
        out.append(Chunk(doc_id=f"gmail-{msg_id}-body-{i}", text=chunk,
                         content_hash=content_hash(chunk), metadata=meta))
    return out
