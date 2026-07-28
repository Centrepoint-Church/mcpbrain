"""Tables are not prose.

Splitting a table on character count produces headerless fragments. A real
mid-table chunk from the live 2026 Budget read:

    ,,Internet & IT support,"7,000",583,,"7,000",583,,,0,,,0,

The column names live in chunk 0 and every subsequent chunk is orphaned
numbers — uninterpretable by the embedder and by a model reading it.
"""
from mcpbrain.sync import tabular

_HEADER = ["Date", "Account", "Description", "Amount"]
_ROWS = [
    ["2024-03-01", "4521", "Office supplies", "42.00"],
    ["2024-03-02", "4521", "Printer toner", "88.50"],
    ["2024-03-03", "6100", "Venue hire", "1200.00"],
]


def _table(header=None, rows=None, *, sheet="GL Detail", truncated=False, total=None):
    rows = _ROWS if rows is None else rows
    return tabular.Table(sheet=sheet, header=header or _HEADER, rows=rows,
                         rows_total=total if total is not None else len(rows),
                         truncated=truncated)


def test_every_row_group_chunk_repeats_the_header():
    chunks = tabular.render_chunks([_table()], file_name="Ledger.xlsx", max_chars=120)
    rows = [(t, m) for t, m in chunks if m["table_role"] == "rows"]

    assert len(rows) >= 2, "max_chars=120 should force more than one row group"
    for text, _meta in rows:
        assert "| Date | Account | Description | Amount |" in text, (
            "a row-group chunk without its header is the B2 defect: orphaned "
            f"numbers with no column names. Got:\n{text}"
        )


def test_each_row_group_names_its_sheet_and_row_range():
    chunks = tabular.render_chunks([_table()], file_name="Ledger.xlsx", max_chars=120)
    text, meta = next((t, m) for t, m in chunks if m["table_role"] == "rows")

    assert "GL Detail" in text
    assert meta["sheet"] == "GL Detail"
    assert meta["row_start"] == 1
    assert meta["rows_total"] == 3


def test_a_summary_chunk_is_emitted_for_each_sheet():
    """Broad questions ('how big is the Harvestnet budget?') have nothing good
    to match without one; specific lookups match a row group instead."""
    chunks = tabular.render_chunks([_table()], file_name="Ledger.xlsx", max_chars=2000)
    summaries = [(t, m) for t, m in chunks if m["table_role"] == "summary"]

    assert len(summaries) == 1
    text, meta = summaries[0]
    assert "Ledger.xlsx" in text and "GL Detail" in text
    assert "Date, Account, Description, Amount" in text
    assert "1330.50" in text, "numeric columns should be totalled in the summary"
    assert meta["sheet"] == "GL Detail"


def test_normalise_rows_drops_empty_rows_and_trims_trailing_columns():
    """B1: the old extractor appended empty rows verbatim and never bounded
    width. One file — Fixed Assett Register 2023 onwards.xlsx — produced 17,281
    chunks of ~2,000 chars each, 300-500 pipes per chunk, zero alphanumerics."""
    rows = [["Name", "Amount", "", ""],
            ["", "", "", ""],
            ["Rent", "500", "", ""]]

    out = tabular.normalise_rows(rows)

    assert out == [["Name", "Amount"], ["Rent", "500"]]


def test_an_interior_blank_column_is_kept():
    """Only TRAILING columns are trimmed: an interior spacer (between a budget's
    actuals and its variance) carries meaning, and dropping it would misalign
    every header against its values."""
    rows = [["Name", "", "Amount"], ["Rent", "", "500"]]

    assert tabular.normalise_rows(rows) == rows


def test_no_content_free_chunk_is_ever_emitted():
    from mcpbrain.chunking import has_content

    rows = [["", "", ""], ["", "", ""]]
    table = tabular.Table(sheet="Empty", header=["", "", ""], rows=rows,
                          rows_total=2, truncated=False)

    for text, _meta in tabular.render_chunks([table], file_name="X.xlsx", max_chars=2000):
        assert has_content(text), f"content-free chunk emitted:\n{text!r}"


def test_truncation_reaches_chunk_metadata():
    """So doctor and the dashboard can report 'N sheets clipped' instead of it
    being invisible for months."""
    table = _table(rows=_ROWS, truncated=True, total=47_900)

    chunks = tabular.render_chunks([table], file_name="X.xlsx", max_chars=2000)

    for _text, meta in chunks:
        assert meta["truncated"] is True
        assert meta["rows_total"] == 47_900
        assert meta["rows_captured"] == 3


def test_tables_from_csv_reads_a_google_sheets_export():
    """Google Sheets export as text/csv (drive.py:43) and CSV downloads pass
    through verbatim, so the CSV path must produce the same Table shape as the
    XLSX path — one renderer, three sources."""
    tables = tabular.tables_from_csv("Name,Amount\nRent,500\nPower,120\n",
                                     sheet="Budget", char_budget=100_000)

    assert len(tables) == 1
    assert tables[0].header == ["Name", "Amount"]
    assert tables[0].rows == [["Rent", "500"], ["Power", "120"]]
    assert tables[0].truncated is False


def test_the_csv_char_budget_marks_truncation_rather_than_lying():
    big = "A,B\n" + "".join(f"row{i},{i}\n" for i in range(5000))

    tables = tabular.tables_from_csv(big, sheet="Big", char_budget=500)

    assert tables[0].truncated is True
    assert tables[0].rows_total == 5000
    assert 0 < len(tables[0].rows) < 5000


def test_a_runaway_cell_cannot_blow_the_chunk_budget():
    """One pasted paragraph in a spreadsheet cell must not push a whole row
    group past the embedder window on its own."""
    table = _table(header=["Note"], rows=[["x" * 5000]], total=1)

    for text, _meta in tabular.render_chunks([table], file_name="X.xlsx",
                                             max_chars=2000):
        assert len(text) <= 2500, f"chunk of {len(text)} chars from one cell"


def test_a_wide_table_cannot_blow_the_chunk_budget_on_a_single_row():
    """Important #2 (review): 30 columns, each elided only down to
    _MAX_CELL_CHARS(300), render a single row line alone longer than any
    reasonable chunk budget — verified pre-fix at ~9,500 chars against a
    2,000-char budget, with the packing loop unable to split a row across
    chunks. Every emitted chunk must stay within a sane bound of max_chars,
    not blow it by ~5x. (test_a_runaway_cell_cannot_blow_the_chunk_budget
    above covers the one-column case only, which the old 300-char-per-cell
    cap already handled — this is the many-columns case that slipped through.)
    """
    header = [f"Col{i}" for i in range(30)]
    row = ["x" * 350 for _ in range(30)]
    table = tabular.Table(sheet="Wide", header=header, rows=[row],
                          rows_total=1, truncated=False)

    chunks = tabular.render_chunks([table], file_name="Wide.xlsx", max_chars=2000)
    rows = [(t, m) for t, m in chunks if m["table_role"] == "rows"]

    assert rows, "expected at least one row-group chunk"
    for text, _meta in rows:
        assert len(text) <= 2500, (
            f"wide-table row chunk of {len(text)} chars from a 2000 budget "
            "— a single row line overflowed the chunk on its own"
        )


def test_table_mimes_agrees_with_the_drive_extraction_meta_table():
    """tabular.TABLE_MIMES and drive._MIME_EXTRACTION_META's 'table' subtype are
    two lists of the same thing in two modules (tabular cannot import drive —
    drive imports tabular). This guard is what keeps them honest."""
    from mcpbrain.sync import drive

    from_drive = {m for m, (_meth, sub, _c) in drive._MIME_EXTRACTION_META.items()
                  if sub == "table"}

    assert from_drive == set(tabular.TABLE_MIMES)
