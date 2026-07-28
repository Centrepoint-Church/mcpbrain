"""A PDF emailed to the user is invisible to the brain, while the byte-identical
file in Drive is extracted normally. `_find_part_text` returns only text/plain
and text/html parts, and there was no attachment-handling code anywhere in the
repo — grep for `attachment`/`attachmentId` over the Gmail path returned zero
matches. Likely the single largest content gap in the store.
"""
import base64

from mcpbrain.sync import attachments


def _msg(parts, msg_id="m1", thread_id="t1"):
    return {"id": msg_id, "threadId": thread_id, "labelIds": ["INBOX"],
            "payload": {"mimeType": "multipart/mixed",
                        "headers": [{"name": "Subject", "value": "Invoice"},
                                    {"name": "From", "value": "a@b.com"},
                                    {"name": "Date",
                                     "value": "Tue, 02 Jun 2026 16:30:01 +0800"}],
                        "parts": parts}}


def _part(filename, mime, attachment_id="att-1", size=1024):
    return {"filename": filename, "mimeType": mime,
            "body": {"attachmentId": attachment_id, "size": size}}


def test_attachment_parts_are_found_at_any_nesting_depth():
    payload = {"parts": [
        {"mimeType": "text/plain", "filename": "", "body": {"data": ""}},
        {"mimeType": "multipart/related", "filename": "", "parts": [
            _part("Budget.pdf", "application/pdf")]},
    ]}

    assert [p["filename"] for p in attachments.iter_attachment_parts(payload)] \
        == ["Budget.pdf"]


def test_a_body_part_is_not_an_attachment():
    """A part with no filename is the message BODY, already handled by
    _find_part_text; treating it as an attachment would double-ingest it."""
    payload = {"parts": [{"mimeType": "text/plain", "filename": "",
                          "body": {"data": "abc"}}]}

    assert attachments.iter_attachment_parts(payload) == []


def test_an_inline_image_is_not_ingested():
    assert attachments.iter_attachment_parts(
        {"parts": [_part("signature-logo.png", "image/png")]}) == []


def test_an_oversized_attachment_is_skipped():
    assert attachments.iter_attachment_parts(
        {"parts": [_part("Huge.pdf", "application/pdf", size=80 * 1024 * 1024)]}) == []


def test_only_the_first_n_attachments_of_one_message_are_taken():
    parts = [_part(f"f{i}.pdf", "application/pdf", attachment_id=f"a{i}")
             for i in range(30)]

    found = attachments.iter_attachment_parts({"parts": parts})

    assert len(found) == attachments._MAX_ATTACHMENTS_PER_MESSAGE


def test_each_part_carries_its_own_stable_index():
    """`index` is part of the doc_id, so it must be assigned where the parts are
    discovered — not by the caller — or a direct normalise_attachment call has
    no index at all."""
    parts = [_part("a.pdf", "application/pdf", attachment_id="a0"),
             _part("b.pdf", "application/pdf", attachment_id="a1")]

    found = attachments.iter_attachment_parts({"parts": parts})

    assert [p["index"] for p in found] == [0, 1]


def test_a_pdf_attachment_becomes_chunks_carrying_its_message_and_thread(monkeypatch):
    monkeypatch.setattr(attachments, "_EXTRACTORS",
                        {"application/pdf": lambda b: "Total due: 4,200.00"})
    raw = _msg([_part("Invoice.pdf", "application/pdf")])
    part = attachments.iter_attachment_parts(raw["payload"])[0]

    chunks = attachments.normalise_attachment(raw, part, b"%PDF-fake")

    assert len(chunks) == 1
    c = chunks[0]
    assert c.doc_id == "gmail-m1-att-0-0"
    assert "Total due: 4,200.00" in c.text
    assert c.metadata["source_type"] == "gmail", (
        "attachments must share the gmail source_type so they join their thread "
        "for enrichment and expansion rather than becoming orphans"
    )
    assert c.metadata["message_id"] == "m1"
    assert c.metadata["thread_id"] == "t1"
    assert c.metadata["content_type"] == "email_attachment"
    assert c.metadata["attachment_name"] == "Invoice.pdf"
    assert c.metadata["date"].startswith("Tue, 02 Jun 2026"), (
        "the parent's date must be propagated or the chunk is date-blind and "
        "recency_decay returns its neutral 0.5 fallback"
    )


def test_a_spreadsheet_attachment_uses_the_row_group_chunker(monkeypatch):
    """An emailed budget must not be character-split any more than a Drive one."""
    from mcpbrain.sync.tabular import Table

    monkeypatch.setattr(
        attachments, "_TABLE_EXTRACTORS",
        {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
         lambda b, char_budget: [Table(sheet="Budget", header=["Item", "Amount"],
                                       rows=[["Rent", "500"], ["Power", "120"]],
                                       rows_total=2, truncated=False)]})
    raw = _msg([_part("Budget.xlsx",
                      "application/vnd.openxmlformats-officedocument."
                      "spreadsheetml.sheet")])
    part = attachments.iter_attachment_parts(raw["payload"])[0]

    chunks = attachments.normalise_attachment(raw, part, b"fake")
    rowtext = next(c.text for c in chunks if c.metadata.get("table_role") == "rows")

    assert "| Item | Amount |" in rowtext


def test_fetch_and_normalise_reports_an_unsupported_attachment_type():
    class _Store:
        def __init__(self):
            self.changes = []

        def record_change(self, kind, ref_id="", summary=""):
            self.changes.append((kind, ref_id, summary))

    store = _Store()

    chunks = attachments.fetch_and_normalise(
        object(), _msg([_part("Archive.zip", "application/zip")]), store=store)

    assert chunks == []
    assert store.changes and "application/zip" in store.changes[0][2]


def test_fetch_and_normalise_pulls_the_bytes_and_extracts(monkeypatch):
    monkeypatch.setattr(attachments, "_EXTRACTORS",
                        {"application/pdf": lambda b: b.decode()})
    payload = base64.urlsafe_b64encode(b"extracted words here").decode()

    class _Service:
        def users(self):
            return self

        def messages(self):
            return self

        def attachments(self):
            return self

        def get(self, userId, messageId, id):
            assert (messageId, id) == ("m1", "att-1")
            return self

        def execute(self):
            return {"data": payload, "size": 20}

    chunks = attachments.fetch_and_normalise(
        _Service(), _msg([_part("Notes.pdf", "application/pdf")]))

    assert chunks and "extracted words here" in chunks[0].text


def test_one_failing_attachment_does_not_kill_the_others(monkeypatch):
    monkeypatch.setattr(attachments, "_EXTRACTORS",
                        {"application/pdf": lambda b: "ok"})

    class _Service:
        def users(self):
            return self

        def messages(self):
            return self

        def attachments(self):
            return self

        def get(self, userId, messageId, id):
            self._id = id
            return self

        def execute(self):
            if self._id == "a0":
                raise RuntimeError("network")
            return {"data": base64.urlsafe_b64encode(b"fine").decode()}

    raw = _msg([_part("bad.pdf", "application/pdf", attachment_id="a0"),
                _part("good.pdf", "application/pdf", attachment_id="a1")])

    chunks = attachments.fetch_and_normalise(_Service(), raw)

    assert [c.doc_id for c in chunks] == ["gmail-m1-att-1-0"]
