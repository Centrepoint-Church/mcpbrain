"""Gmail message normalisation — raw dict -> list[Chunk].

Converts a Gmail messages.get(format=full) response into indexable chunks.
No Google API calls here; this module is pure data transformation.
"""

import base64
import re
from dataclasses import dataclass

from mcpbrain.chunking import chunk_text, content_hash


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


def strip_html(html: str) -> str:
    """Convert HTML email body to plain text. bs4 if available, else regex."""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "head"]):
            tag.decompose()
        text = soup.get_text(separator="\n")
        lines = [line.strip() for line in text.splitlines()]
        return "\n".join(line for line in lines if line)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", html)
        return re.sub(r"\s+", " ", text).strip()


def strip_reply_chains(text: str) -> str:
    text = re.sub(r'(?m)^>.*$', '', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    earliest = len(text)
    for pattern in _REPLY_CHAIN_PATTERNS:
        m = pattern.search(text)
        if m and m.start() < earliest:
            earliest = m.start()
    return text[:earliest].strip()


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
    Runs reply-chain stripping before signature extraction."""
    text = _find_part_text(payload, "text/plain")
    if text and len(text.strip()) > 10:
        text = strip_reply_chains(text)
        return extract_signature_block(text)
    html = _find_part_text(payload, "text/html")
    if html:
        text = strip_reply_chains(strip_html(html))
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

def normalise_gmail(raw: dict) -> list[Chunk]:
    """Raw Gmail message (messages.get format=full) -> list[Chunk].
    doc_id = gmail-<id>-body-<i>. Empty body -> []."""
    msg_id = raw["id"]
    payload = raw.get("payload", {})
    headers = payload.get("headers", [])
    subject = get_header(headers, "subject")
    if _is_bulk_or_auto(headers, subject):
        return []
    body, signature_block = extract_body_with_signature(payload)
    if not body:
        return []
    base_metadata = {
        "source_type": "gmail",
        "message_id": msg_id,
        "thread_id": raw.get("threadId", ""),
        "subject": subject[:200],
        "sender": get_header(headers, "from")[:200],
        "to": get_header(headers, "to")[:300],
        "cc": get_header(headers, "cc")[:300],
        "date": get_header(headers, "date")[:80],
        "labels": ",".join(raw.get("labelIds", []))[:200],
        "signature_block": signature_block[:500],
    }
    out = []
    for i, chunk in enumerate(chunk_text(body)):
        meta = {**base_metadata, "content_type": "email_body", "chunk_index": i}
        out.append(Chunk(doc_id=f"gmail-{msg_id}-body-{i}", text=chunk,
                         content_hash=content_hash(chunk), metadata=meta))
    return out
