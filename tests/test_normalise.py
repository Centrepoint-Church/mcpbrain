"""Tests for mcpbrain.sync.normalise — pure unit tests, no personal data."""

import base64


from mcpbrain.chunking import content_hash
from mcpbrain.sync.normalise import Chunk, normalise_gmail


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode()


def plain_msg(mid: str, subject: str, sender: str, body: str,
              extra_headers: list | None = None) -> dict:
    headers = [
        {"name": "Subject", "value": subject},
        {"name": "From", "value": sender},
    ]
    if extra_headers:
        headers.extend(extra_headers)
    return {
        "id": mid,
        "threadId": "t-" + mid,
        "labelIds": ["INBOX"],
        "payload": {
            "mimeType": "text/plain",
            "headers": headers,
            "body": {"data": b64(body)},
        },
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_simple_plaintext_message_one_chunk():
    msg = plain_msg("abc123", "Team update", "alice@example.com", "Short message body here.")
    chunks = normalise_gmail(msg)

    assert len(chunks) == 1
    c = chunks[0]
    assert isinstance(c, Chunk)
    assert c.doc_id == "gmail-abc123-body-0"
    assert c.metadata["source_type"] == "gmail"
    assert c.metadata["content_type"] == "email_body"
    assert c.metadata["subject"] == "Team update"
    assert c.metadata["sender"] == "alice@example.com"
    assert c.metadata["chunk_index"] == 0


def test_signature_is_stripped_into_metadata():
    body = "Quick update on the roster.\n\nRegards,\nSam Chen\nOperations"
    msg = plain_msg("sig001", "Roster", "sam@example.com", body)
    chunks = normalise_gmail(msg)

    assert len(chunks) >= 1
    c = chunks[0]
    # Body text must not contain the sign-off
    assert "Regards," not in c.text
    # Signature captured in metadata
    assert "Regards," in c.metadata["signature_block"]


def test_reply_chain_truncated():
    body = (
        "My reply here.\n\n"
        "On Mon, 7 Apr 2026 at 10:00, Someone <x@example.com> wrote:\n"
        "> old quoted text\n"
        "> more old content\n"
    )
    msg = plain_msg("reply001", "Re: topic", "bob@example.com", body)
    chunks = normalise_gmail(msg)

    assert len(chunks) >= 1
    combined = " ".join(c.text for c in chunks)
    assert "My reply here" in combined
    assert "old quoted text" not in combined


def test_multipart_prefers_plaintext_part():
    plain_body = "Plain text version of the email."
    html_body = "<p>HTML version of the email.</p>"
    msg = {
        "id": "multi001",
        "threadId": "t-multi001",
        "labelIds": ["INBOX"],
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "Subject", "value": "Multipart test"},
                {"name": "From", "value": "carol@example.com"},
            ],
            "body": {},
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": b64(plain_body)},
                },
                {
                    "mimeType": "text/html",
                    "body": {"data": b64(html_body)},
                },
            ],
        },
    }
    chunks = normalise_gmail(msg)

    assert len(chunks) >= 1
    combined = " ".join(c.text for c in chunks)
    assert "Plain text version" in combined


def test_html_only_message_converts_to_text():
    html_body = "<p>Budget meeting Friday</p>"
    msg = {
        "id": "html001",
        "threadId": "t-html001",
        "labelIds": ["INBOX"],
        "payload": {
            "mimeType": "text/html",
            "headers": [
                {"name": "Subject", "value": "Budget"},
                {"name": "From", "value": "dave@example.com"},
            ],
            "body": {"data": b64(html_body)},
        },
    }
    chunks = normalise_gmail(msg)

    assert len(chunks) >= 1
    combined = " ".join(c.text for c in chunks)
    assert "Budget meeting Friday" in combined


def test_empty_body_returns_no_chunks():
    msg = {
        "id": "empty001",
        "threadId": "t-empty001",
        "labelIds": ["INBOX"],
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "Subject", "value": "Empty"},
                {"name": "From", "value": "ghost@example.com"},
            ],
            "body": {},
        },
    }
    chunks = normalise_gmail(msg)
    assert chunks == []


def test_long_body_splits_into_numbered_chunks():
    # Generate a body long enough to exceed chunk_text's default max_tokens=500
    # chunk_text uses max_chars = max_tokens * 4 = 2000. Two paragraphs of ~1500 chars each.
    paragraph = ("word " * 300).strip()  # ~1500 chars
    body = paragraph + "\n\n" + paragraph

    msg = plain_msg("long001", "Long email", "eve@example.com", body)
    chunks = normalise_gmail(msg)

    assert len(chunks) >= 2, f"Expected at least 2 chunks, got {len(chunks)}"

    # doc_ids must be contiguous and correctly formatted
    for i, c in enumerate(chunks):
        assert c.doc_id == f"gmail-long001-body-{i}", (
            f"chunk {i} has unexpected doc_id: {c.doc_id}"
        )

    # Each content_hash must match the chunk text
    for c in chunks:
        assert c.content_hash == content_hash(c.text), (
            f"content_hash mismatch for {c.doc_id}"
        )


# ---------------------------------------------------------------------------
# Bulk / newsletter / auto-reply filter tests
# ---------------------------------------------------------------------------

_BODY = "Some real email content that should produce a chunk."


def test_newsletter_list_unsubscribe_filtered():
    """List-Unsubscribe header must suppress indexing entirely."""
    msg = plain_msg(
        "nl001", "Weekly digest", "news@example.com", _BODY,
        extra_headers=[{"name": "List-Unsubscribe", "value": "<mailto:unsub@example.com>"}],
    )
    assert normalise_gmail(msg) == []


def test_mailing_list_listid_filtered():
    """List-Id header marks a mailing list — must be filtered."""
    msg = plain_msg(
        "ml001", "List post", "list@example.com", _BODY,
        extra_headers=[{"name": "List-Id", "value": "<team.lists.example.com>"}],
    )
    assert normalise_gmail(msg) == []


def test_precedence_bulk_filtered():
    """Precedence: bulk must be filtered."""
    msg = plain_msg(
        "prec001", "Bulk mailer", "bulk@example.com", _BODY,
        extra_headers=[{"name": "Precedence", "value": "bulk"}],
    )
    assert normalise_gmail(msg) == []


def test_auto_submitted_filtered():
    """Auto-Submitted: auto-replied must be filtered; Auto-Submitted: no must NOT be."""
    # Auto-reply — should be filtered
    msg_auto = plain_msg(
        "auto001", "Auto reply", "auto@example.com", _BODY,
        extra_headers=[{"name": "Auto-Submitted", "value": "auto-replied"}],
    )
    assert normalise_gmail(msg_auto) == []

    # Explicitly marked not-auto — should NOT be filtered
    msg_no = plain_msg(
        "auto002", "Human reply", "human@example.com", _BODY,
        extra_headers=[{"name": "Auto-Submitted", "value": "no"}],
    )
    assert len(normalise_gmail(msg_no)) >= 1


def test_out_of_office_subject_filtered():
    """Subject starting with 'out of office' (case-insensitive) must be filtered."""
    msg = plain_msg(
        "ooo001", "Out of office: back Monday", "staff@example.com", _BODY,
    )
    assert normalise_gmail(msg) == []


def test_normal_personal_email_not_filtered():
    """A plain personal email with no bulk headers must produce at least one chunk."""
    msg = plain_msg(
        "pers001", "Catch up Thursday?", "friend@example.com",
        "Hey, are you free Thursday afternoon to catch up?",
    )
    chunks = normalise_gmail(msg)
    assert len(chunks) >= 1


def test_briefing_without_bulk_headers_not_filtered():
    """A morning-briefing subject with NO bulk/list/auto headers must still be indexed.

    This locks the decision that we filter on headers only — not on subject keywords
    like 'morning briefing'. A daily briefing sent as a normal personal email is real
    content and must reach the index.
    """
    msg = plain_msg(
        "brief001",
        "Morning briefing: Briefing for Fri 29 May",
        "ops-brain@example.org",
        "Today's priorities: review budget, confirm venue, send board pack.",
    )
    chunks = normalise_gmail(msg)
    assert len(chunks) >= 1, (
        "Morning briefing without bulk headers must NOT be filtered — "
        "filter is header-based only"
    )
