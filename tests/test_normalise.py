"""Tests for mcpbrain.sync.normalise — pure unit tests, no personal data."""

import base64


from mcpbrain.chunking import content_hash
from mcpbrain.sync.normalise import Chunk, normalise_gmail


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode()


def _message(*, headers, body, mime="text/plain", msg_id="m1", thread_id="t1"):
    return {"id": msg_id, "threadId": thread_id, "labelIds": ["INBOX"],
            "payload": {"mimeType": mime,
                        "headers": [{"name": n, "value": v} for n, v in headers],
                        "body": {"data": _b64(body)}}}


def _html_payload(html):
    return {"mimeType": "text/html", "headers": [], "body": {"data": _b64(html)}}


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
    """List-Unsubscribe header marks bulk mail.

    A4: this used to assert normalise_gmail(msg) == [] — the message was
    DROPPED at ingest, irreversibly, before anything downstream could see it.
    That is gone: bulk mail is now ingested and stamped `bulk: True` in
    metadata so prepare.should_enrich can cold-mark it instead (embedded,
    searchable, never graph-extracted, reversible). See
    test_bulk_mail_is_ingested_and_marked_rather_than_dropped below.
    """
    msg = plain_msg(
        "nl001", "Weekly digest", "news@example.com", _BODY,
        extra_headers=[{"name": "List-Unsubscribe", "value": "<mailto:unsub@example.com>"}],
    )
    chunks = normalise_gmail(msg)
    assert chunks, "bulk mail must be ingested, not discarded at the door"
    assert chunks[0].metadata["bulk"] is True


def test_mailing_list_listid_filtered():
    """List-Id header marks a mailing list. See A4 note above — marked, not dropped."""
    msg = plain_msg(
        "ml001", "List post", "list@example.com", _BODY,
        extra_headers=[{"name": "List-Id", "value": "<team.lists.example.com>"}],
    )
    chunks = normalise_gmail(msg)
    assert chunks
    assert chunks[0].metadata["bulk"] is True


def test_precedence_bulk_filtered():
    """Precedence: bulk marks bulk mail. See A4 note above — marked, not dropped."""
    msg = plain_msg(
        "prec001", "Bulk mailer", "bulk@example.com", _BODY,
        extra_headers=[{"name": "Precedence", "value": "bulk"}],
    )
    chunks = normalise_gmail(msg)
    assert chunks
    assert chunks[0].metadata["bulk"] is True


def test_auto_submitted_filtered():
    """Auto-Submitted: auto-replied marks bulk mail; Auto-Submitted: no does not.

    See A4 note above — the auto-reply case used to be dropped; now marked.
    """
    # Auto-reply — should be marked bulk, not dropped
    msg_auto = plain_msg(
        "auto001", "Auto reply", "auto@example.com", _BODY,
        extra_headers=[{"name": "Auto-Submitted", "value": "auto-replied"}],
    )
    chunks_auto = normalise_gmail(msg_auto)
    assert chunks_auto
    assert chunks_auto[0].metadata["bulk"] is True

    # Explicitly marked not-auto — should NOT be filtered or marked bulk
    msg_no = plain_msg(
        "auto002", "Human reply", "human@example.com", _BODY,
        extra_headers=[{"name": "Auto-Submitted", "value": "no"}],
    )
    chunks_no = normalise_gmail(msg_no)
    assert len(chunks_no) >= 1
    assert "bulk" not in chunks_no[0].metadata


def test_out_of_office_subject_filtered():
    """Out-of-office subject marks bulk mail. See A4 note above — marked, not dropped."""
    msg = plain_msg(
        "ooo001", "Out of office: back Monday", "staff@example.com", _BODY,
    )
    chunks = normalise_gmail(msg)
    assert chunks
    assert chunks[0].metadata["bulk"] is True


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


# ---------------------------------------------------------------------------
# Task 5: reply rescue, bulk marking, chunk_total, recipient counts
# ---------------------------------------------------------------------------

def test_a_reply_written_below_the_quote_survives():
    """A3: strip_reply_chains kept only text[:earliest], so a bottom-posted
    reply was thrown away along with the quote it sat under."""
    from mcpbrain.sync.normalise import strip_reply_chains

    # The leading '\n' is required: every _REPLY_CHAIN_PATTERNS entry anchors
    # on a literal preceding newline, so without it the "On ... wrote:" line
    # sitting at index 0 never matches at all — earliest stays len(text), and
    # OLD and NEW code would return byte-identical output (a non-discriminating
    # test). A real Gmail body commonly has this leading blank line when the
    # quote is the very first thing in the message.
    text = ("\nOn Mon, 2 Jun 2026 at 09:14, Sam <sam@example.com> wrote:\n"
            "> Can you confirm the Hall B booking for Sunday?\n"
            "> Sam\n\n"
            "Yes — Hall B is confirmed for Sunday the 8th, 9am to 1pm. "
            "I have put Priya down as the contact on the day.\n")

    out = strip_reply_chains(text)

    assert "Hall B is confirmed" in out, "the bottom-posted reply was discarded"
    assert "Can you confirm" not in out, "the quote itself must still be stripped"
    assert "wrote:" not in out, (
        "the quote's own attribution line must be filtered out of the "
        "rescued tail by _QUOTE_HEADER_LINE, not survive alongside the reply"
    )


def test_a_short_sign_off_below_a_quote_is_not_treated_as_a_reply():
    """Err toward dropping: 'Sent from my iPhone' under a quote is not content,
    and rescuing it would re-introduce boilerplate on every reply in the corpus."""
    from mcpbrain.sync.normalise import strip_reply_chains

    text = ("Thanks!\n"
            "On Mon, 2 Jun 2026 at 09:14, Sam <sam@example.com> wrote:\n"
            "> long quoted thing\n\nSent from my iPhone\n")

    assert strip_reply_chains(text).strip() == "Thanks!"


def test_html_mail_does_not_get_the_bottom_post_rescue():
    """The rescue is only sound where '>' quoting was stripped first. In HTML
    mail the quote is markup, so a tail-rescue would re-ingest the entire quoted
    history as if it were new prose."""
    from mcpbrain.sync.normalise import extract_body_with_signature

    html = ("<p>Short answer: yes.</p>"
            "<div>On Mon, 2 Jun 2026 at 09:14, Sam wrote:</div>"
            "<blockquote>The whole previous thread, at length, "
            "repeated verbatim for many lines.</blockquote>")

    body, _sig = extract_body_with_signature(_html_payload(html))

    assert "Short answer: yes." in body
    assert "repeated verbatim" not in body


# ---------------------------------------------------------------------------
# strip_html fallback: tag boundaries must become newlines, not spaces.
#
# bs4 is confirmed NOT a dependency of this project (absent from
# pyproject.toml, uv.lock, and this venv), so the regex fallback below is the
# only path that actually runs in any real deployment — "bs4 if available" is
# effectively dead code. The fallback used to collapse every tag to a single
# space, which fused an HTML message onto one line with no '\n' anywhere; every
# _REPLY_CHAIN_PATTERNS entry and every _SIGNATURE_OPENERS entry require a
# literal preceding '\n' to match at all, so reply-chain stripping AND
# signature extraction were both structurally unreachable for HTML mail before
# this fix — not just the one rescue-flag scenario above.
# ---------------------------------------------------------------------------

def test_strip_html_turns_tag_boundaries_into_newlines():
    from mcpbrain.sync.normalise import strip_html

    out = strip_html("<p>Line one</p><p>Line two</p>")

    assert out == "Line one\nLine two"


def test_strip_html_quote_boundary_is_stripped_by_the_reply_chain_regex():
    """The newline strip_html now inserts at the closing </div> before the
    <blockquote> is exactly what lets _REPLY_CHAIN_PATTERNS's '\\n...wrote:\\s*\\n'
    fire on HTML-derived text — with the old space-joining fallback this never
    matched, so the whole quoted blockquote survived as if it were new prose."""
    from mcpbrain.sync.normalise import strip_html, strip_reply_chains

    html = ("<p>Short answer: yes.</p>"
            "<div>On Mon, 2 Jun 2026 at 09:14, Sam wrote:</div>"
            "<blockquote>The whole previous thread, at length, "
            "repeated verbatim for many lines.</blockquote>")

    out = strip_reply_chains(strip_html(html), rescue_bottom_post=False)

    assert out == "Short answer: yes."


def test_strip_html_signature_opener_activates_after_a_tag_boundary():
    """extract_signature_block's openers ('\\nregards,' etc.) also anchor on a
    literal preceding '\\n' — with the old space-joining fallback, a signature
    written as its own HTML paragraph could never be recognised as one."""
    from mcpbrain.sync.normalise import extract_signature_block, strip_html

    html = "<p>Quick note on the roster.</p><p>Regards,</p><p>Sam Chen</p>"

    body, signature = extract_signature_block(strip_html(html))

    assert body == "Quick note on the roster."
    assert "Regards," in signature
    assert "Sam Chen" in signature


def test_bulk_mail_is_ingested_and_marked_rather_than_dropped():
    """A4: _is_bulk_or_auto returned [] for anything with List-Id /
    List-Unsubscribe / Precedence: bulk — most vendor and ministry-platform mail
    — and sync_gmail counted it as processed anyway, so the loss was invisible.

    The drop is now gone entirely. prepare.should_enrich already cold-marks
    promotional email (embedded and searchable, never graph-extracted, fully
    reversible) and has shipped since 0.7.65 with ~40% of the corpus gated and
    no recall impact. The ingest-time drop was the same idea done worse:
    irreversibly, before anything could see the content."""
    from mcpbrain.sync.normalise import normalise_gmail

    raw = _message(headers=[("Subject", "Weekly digest"),
                            ("List-Unsubscribe", "<mailto:x@y.z>")],
                   body="Some newsletter body text worth keeping.")

    chunks = normalise_gmail(raw)

    assert chunks, "bulk mail must be ingested, not discarded at the door"
    assert chunks[0].metadata["bulk"] is True, (
        "and marked, so should_enrich can cold-mark it instead of spending "
        "Haiku on a newsletter"
    )


def test_ordinary_mail_is_not_marked_bulk():
    from mcpbrain.sync.normalise import normalise_gmail

    chunks = normalise_gmail(_message(headers=[("Subject", "Hall B")],
                                      body="Can you confirm Sunday?"))

    assert "bulk" not in chunks[0].metadata


def test_the_salience_gate_cold_marks_header_bulk_mail():
    """The other half: the signal has to be ACTED on, or removing the drop just
    sends newsletters to Haiku. The header signal is strictly stronger than the
    Gmail CATEGORY_* labels should_enrich already checks — Gmail's categoriser
    misses plenty of list mail that carries List-Id."""
    from mcpbrain.prepare import should_enrich

    assert should_enrich({"metadata": {"source_type": "gmail", "bulk": True,
                                       "labels": "INBOX"}}) is False
    assert should_enrich({"metadata": {"source_type": "gmail",
                                       "labels": "INBOX"}}) is True


def test_empty_body_is_still_reported():
    from mcpbrain.sync.normalise import normalise_gmail

    report: dict = {}
    assert normalise_gmail(_message(headers=[("Subject", "s")], body=""),
                           report=report) == []
    assert report == {"empty_body": 1}


def test_recipient_lists_are_not_clipped_at_300_chars():
    """C6: to[:300]/cc[:300] loses most recipients of an all-staff email."""
    from mcpbrain.sync.normalise import normalise_gmail

    recipients = ", ".join(f"person{i}@centrepoint.church" for i in range(60))
    meta = normalise_gmail(_message(headers=[("Subject", "All staff"),
                                             ("To", recipients)],
                                    body="Team update."))[0].metadata

    assert meta["to"].count("@") >= 50
    assert meta["to_count"] == 60


def test_every_gmail_chunk_records_its_document_chunk_count():
    """C1, gmail side."""
    from mcpbrain.sync.normalise import normalise_gmail

    body = "\n\n".join(f"Paragraph {i} " + "word " * 300 for i in range(4))
    chunks = normalise_gmail(_message(headers=[("Subject", "Long")], body=body))

    assert len(chunks) > 1
    assert all(c.metadata["chunk_total"] == len(chunks) for c in chunks)
