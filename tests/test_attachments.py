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

    assert "Item: Rent; Amount: 500" in rowtext
    assert "Item: Power; Amount: 120" in rowtext


def test_an_emailed_legacy_xls_is_ingested_like_a_drive_one():
    """Review finding: .xls and .eml were added to the DRIVE path (A2) but not to
    the attachment path, so an emailed legacy budget was dropped while the
    byte-identical file in Drive extracted fine.

    That is A1's asymmetry — 'a PDF emailed to the user is invisible while the
    byte-identical file in Drive is extracted normally' — reintroduced in
    miniature by the very task that fixed it. Uses the real fixture rather than a
    stub so it also pins that the xlrd path is genuinely reachable from here."""
    import pathlib

    data = (pathlib.Path(__file__).parent / "fixtures" / "legacy_budget.xls").read_bytes()
    raw = _msg([_part("Budget.xls", "application/vnd.ms-excel")])
    part = attachments.iter_attachment_parts(raw["payload"])[0]

    chunks = attachments.normalise_attachment(raw, part, data)

    assert chunks, ".xls attachment produced no chunks"
    rowtext = next(c.text for c in chunks if c.metadata.get("table_role") == "rows")
    assert "Item: Rent; Amount: 500" in rowtext, (
        "an .xls attachment must use the row-group chunker")
    assert chunks[0].metadata["extraction_method"] == "spreadsheet"


def test_an_emailed_eml_is_ingested_like_a_drive_one():
    """Same finding, prose half: a forwarded .eml attachment is common and was
    dropped, while the same file in Drive extracted."""
    raw_eml = (b"From: sam@example.com\r\nSubject: Hall B booking\r\n"
               b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
               b"Confirmed for Sunday the 8th, 9am to 1pm.\r\n")
    raw = _msg([_part("forwarded.eml", "message/rfc822")])
    part = attachments.iter_attachment_parts(raw["payload"])[0]

    chunks = attachments.normalise_attachment(raw, part, raw_eml)

    assert chunks, ".eml attachment produced no chunks"
    assert "Confirmed for Sunday" in chunks[0].text
    assert "Hall B booking" in chunks[0].text


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

        def execute(self, num_retries=0):
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

        def execute(self, num_retries=0):
            if self._id == "a0":
                raise RuntimeError("network")
            return {"data": base64.urlsafe_b64encode(b"fine").decode()}

    raw = _msg([_part("bad.pdf", "application/pdf", attachment_id="a0"),
                _part("good.pdf", "application/pdf", attachment_id="a1")])

    chunks = attachments.fetch_and_normalise(_Service(), raw)

    assert [c.doc_id for c in chunks] == ["gmail-m1-att-1-0"]


def test_a_tabular_attachment_is_tagged_content_subtype_table(monkeypatch):
    """I1: prepare.should_enrich's tabular gate reads `content_subtype`, which
    normalise_drive stamps per-MIME but this module never did — so an emailed
    workbook's row-group chunks all reached the extractor. `table_role` (the
    renderer's own marker) is what makes it a table chunk."""
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

    assert chunks
    assert all(c.metadata.get("content_subtype") == "table" for c in chunks), \
        [c.metadata.get("content_subtype") for c in chunks]


def test_a_prose_attachment_is_not_tagged_as_a_table(monkeypatch):
    """The discriminator for the stamp: only chunks that came out of
    tabular.render_chunks get it."""
    monkeypatch.setattr(attachments, "_EXTRACTORS",
                        {"application/pdf": lambda b: "Board minutes. " * 30})
    raw = _msg([_part("Minutes.pdf", "application/pdf")])
    part = attachments.iter_attachment_parts(raw["payload"])[0]

    chunks = attachments.normalise_attachment(raw, part, b"fake")

    assert chunks
    assert all("content_subtype" not in c.metadata for c in chunks)


def test_a_bulk_parents_attachment_inherits_the_bulk_flag(monkeypatch):
    """I1: normalise_gmail stamps `bulk` from List-Id/List-Unsubscribe/Precedence
    so should_enrich cold-marks newsletter bodies. Without the same stamp here, a
    newsletter's attached flyer was graph-extracted while the body it arrived with
    was cold-marked."""
    monkeypatch.setattr(attachments, "_EXTRACTORS",
                        {"application/pdf": lambda b: "Our latest offers. " * 30})
    raw = _msg([_part("Flyer.pdf", "application/pdf")])
    raw["payload"]["headers"].append(
        {"name": "List-Unsubscribe", "value": "<mailto:x@y.z>"})
    part = attachments.iter_attachment_parts(raw["payload"])[0]

    chunks = attachments.normalise_attachment(raw, part, b"fake")

    assert chunks
    assert all(c.metadata.get("bulk") is True for c in chunks)


def test_an_ordinary_parents_attachment_is_not_marked_bulk(monkeypatch):
    monkeypatch.setattr(attachments, "_EXTRACTORS",
                        {"application/pdf": lambda b: "Board minutes. " * 30})
    raw = _msg([_part("Minutes.pdf", "application/pdf")])
    part = attachments.iter_attachment_parts(raw["payload"])[0]

    chunks = attachments.normalise_attachment(raw, part, b"fake")

    assert chunks
    assert all("bulk" not in c.metadata for c in chunks)


def test_a_tsv_attachment_is_parsed_on_tabs(monkeypatch):
    """I4: text/tab-separated-values is routed through tables_from_csv, which used
    csv.reader's default comma — so a TSV parsed as ONE column holding the whole
    row, and _MAX_CELL_CHARS then truncated it at 300 chars per row."""
    tsv = ("Account\tDescription\tAmount\n"
           "4521\tVenue hire\t1450.00\n"
           "6100\tCatering\t320.50\n")
    raw = _msg([_part("ledger.tsv", "text/tab-separated-values")])
    part = attachments.iter_attachment_parts(raw["payload"])[0]

    chunks = attachments.normalise_attachment(raw, part, tsv.encode())
    rowtext = next(c.text for c in chunks if c.metadata.get("table_role") == "rows")

    assert "Account: 4521; Description: Venue hire; Amount: 1450.00" in rowtext
    assert "Account: 6100; Description: Catering; Amount: 320.50" in rowtext


def test_a_calendar_invite_is_skipped_silently():
    """Decision (2026-07-30): .ics attachments are not extracted, because the
    meeting is already ingested first-hand by the calendar sync — with attendees,
    times and recurrence. Parsing the attached copy would duplicate that content
    under a second identity, the same two-namespaces problem that broke calendar
    enrichment in 0.7.98.

    Silently, not via the skip report: Google attaches BOTH text/calendar and
    application/ics to every invite, so a reported skip is two log rows per
    invite — hundreds of rows of noise across a full-history backfill for a
    settled decision."""
    for mime in ("text/calendar", "application/ics"):
        found = attachments.iter_attachment_parts(
            {"parts": [_part("invite.ics", mime)]})
        assert found == [], f"{mime} should not even be listed as an attachment"


def test_a_real_attachment_beside_an_invite_still_arrives():
    """The discriminator: skipping .ics must not skip the agenda PDF that came
    with it."""
    found = attachments.iter_attachment_parts({"parts": [
        _part("invite.ics", "text/calendar", attachment_id="a0"),
        _part("Agenda.pdf", "application/pdf", attachment_id="a1"),
    ]})

    assert [p["filename"] for p in found] == ["Agenda.pdf"]
    assert found[0]["index"] == 0, "the surviving attachment must be index 0"
