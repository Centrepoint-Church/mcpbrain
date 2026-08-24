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


def test_row_sentences_use_schema_enriched_format():
    chunks = tabular.render_chunks([_table()], file_name="Ledger.xlsx", max_chars=1800)
    rows = [(t, m) for t, m in chunks if m["table_role"] == "rows"]

    assert rows, "expected at least one row-group chunk"
    text, _meta = rows[0]
    assert "Date: 2024-03-01" in text
    assert "Account: 4521" in text
    assert "Description: Office supplies" in text
    assert "Amount: 42.00" in text
    # No markdown grid artifacts survive.
    assert "| Date | Account" not in text
    assert "---" not in text


def test_empty_cells_are_never_rendered():
    header = ["Item", "Cost", "Notes"]
    rows = [["Chair", "50", ""]]  # Notes is empty for this row
    table = tabular.Table(sheet="S", header=header, rows=rows, rows_total=1)

    chunks = tabular.render_chunks([table], file_name="f.xlsx", max_chars=1800)
    text = [t for t, m in chunks if m["table_role"] == "rows"][0]

    assert "Notes" not in text, "an empty cell must not render at all"


def test_phantom_wide_row_stays_under_chunk_chars_regardless():
    # Belt-and-suspenders: even without Task 10's normalise_rows fix, one
    # anomalous row with thousands of non-empty phantom cells must not blow
    # the chunk budget, because the field-count safety valve bounds it.
    header = ["Item"] + [f"col{i}" for i in range(5000)]
    phantom_row = ["Chairs"] + [f"v{i}" for i in range(5000)]  # all non-empty
    table = tabular.Table(sheet="S", header=header, rows=[phantom_row], rows_total=1)

    chunks = tabular.render_chunks([table], file_name="f.xlsx", max_chars=1800)
    for text, meta in chunks:
        if meta["table_role"] == "rows":
            assert len(text) <= 1800 + 200, (
                f"row-group chunk of {len(text)} chars exceeds budget even "
                "with the field-count safety valve")


def test_row_with_too_many_fields_gets_elided():
    header = ["Item"] + [f"col{i}" for i in range(60)]
    row = ["Chairs"] + [f"v{i}" for i in range(60)]
    table = tabular.Table(sheet="S", header=header, rows=[row], rows_total=1)

    chunks = tabular.render_chunks([table], file_name="f.xlsx", max_chars=100_000)
    text = [t for t, m in chunks if m["table_role"] == "rows"][0]

    assert "more fields" in text


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


def test_normalise_rows_ignores_a_single_outlier_wide_row():
    # 700 normal rows (4 real columns) plus one title-banner row with a
    # single stray non-empty cell at column 19999 -- the exact reproducing
    # case from the live investigation (a Fixed Assets Register spreadsheet).
    header = ["Item", "Cost", "Date", "Location"]
    normal_rows = [["Chair", "50", "2024-01-01", "Hall A"] for _ in range(700)]
    outlier = [""] * 19999 + ["FIXED ASSETS REGISTER as at 31 December 2022"]
    rows = [header] + normal_rows + [outlier]

    out = tabular.normalise_rows(rows)

    # The outlier row is trimmed to the real width, not the other way around.
    assert all(len(r) <= 4 for r in out), (
        f"expected every row trimmed to width<=4, got a row of length "
        f"{max(len(r) for r in out)}")
    # After trimming, the outlier row becomes all-empty and must be dropped.
    assert all(any(c.strip() for c in r) for r in out), (
        "every row must have at least one non-empty cell")


def test_normalise_rows_keeps_a_genuinely_sparse_real_column():
    # A "notes" column only 5 of 50 rows populate is still real -- it must
    # NOT be dropped just because it's a minority.
    header = ["Item", "Cost", "Notes"]
    rows_without_notes = [["Chair", "50", ""] for _ in range(45)]
    rows_with_notes = [["Desk", "200", "damaged"] for _ in range(5)]
    rows = [header] + rows_without_notes + rows_with_notes

    out = tabular.normalise_rows(rows)

    assert all(len(r) == 3 for r in out), (
        "the Notes column (5/50 rows, above the support floor of 2) must survive")


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


def test_a_tsv_parses_into_its_real_columns():
    """I4: TABLE_MIMES includes text/tab-separated-values and both drive.py and
    attachments.py route TSV through this function, which always used csv.reader's
    default comma — so a real TSV rendered as ONE column holding the whole
    tab-separated row, and the 300-char-per-cell cap then silently discarded
    everything past that width per row."""
    tsv = ("Account\tDescription\tAmount\n"
           "4521\tVenue hire for the winter conference\t1450.00\n"
           "6100\tCatering\t320.50\n")

    tables = tabular.tables_from_csv(tsv, sheet="ledger", char_budget=10_000,
                                     delimiter="\t")

    assert len(tables) == 1
    t = tables[0]
    assert t.header == ["Account", "Description", "Amount"]
    assert t.rows == [["4521", "Venue hire for the winter conference", "1450.00"],
                      ["6100", "Catering", "320.50"]]


def test_the_tsv_delimiter_comes_from_the_mime_type():
    assert tabular.delimiter_for_mime("text/tab-separated-values") == "\t"
    assert tabular.delimiter_for_mime("text/csv") == ","
    assert tabular.delimiter_for_mime("application/csv") == ","


def test_an_identifier_column_is_not_totalled():
    """I3: _summary_text summed every mostly-numeric column, so a GL/account-code
    column was reported as `Totals: Account 15142.00` beside the genuine monetary
    totals — a fabricated figure stated as fact in the one chunk written to answer
    "how big is this budget"."""
    t = tabular.Table(
        sheet="Budget", header=["Account", "Description", "Amount"],
        rows=[["4521", "Venue hire", "1450.00"],
              ["6100", "Catering", "320.50"],
              ["6200", "Printing", "89.00"]],
        rows_total=3, truncated=False)

    summary = tabular.render_chunks([t], file_name="2026 Budget.xlsx",
                                    max_chars=2000)[0][0]
    totals = next(ln for ln in summary.splitlines() if ln.startswith("Totals:"))

    assert "Amount 1859.50" in totals, totals
    assert "Account" not in totals, (
        f"summing account codes produced a meaningless figure: {totals}"
    )


def test_a_uniform_width_code_column_is_not_totalled_even_without_a_hint_header():
    """The second signal: whole numbers, all the same digit width, three or more
    of them. A header like '2026' says nothing, but 4521/6100/6200 is a code
    block, not a set of quantities."""
    t = tabular.Table(
        sheet="GL", header=["2026", "Spend"],
        rows=[["4521", "12.5"], ["6100", "300.75"], ["6200", "9.25"]],
        rows_total=3, truncated=False)

    summary = tabular.render_chunks([t], file_name="gl.xlsx", max_chars=2000)[0][0]
    totals = next(ln for ln in summary.splitlines() if ln.startswith("Totals:"))

    assert "Spend 322.50" in totals, totals
    assert "2026 " not in totals, totals


def test_a_quantity_word_in_the_header_overrides_the_identifier_shape():
    """'Invoice Amount' is a quantity even though 'invoice' is an identifier word,
    and same-width integers are ordinary for money."""
    t = tabular.Table(
        sheet="Invoices", header=["Invoice Amount"],
        rows=[["1200"], ["3400"], ["5600"]], rows_total=3, truncated=False)

    summary = tabular.render_chunks([t], file_name="inv.xlsx", max_chars=2000)[0][0]

    assert "Invoice Amount 10200.00" in summary


def test_a_fully_packed_table_chunk_fits_the_embedder_window_once_prefixed():
    """I8: CHUNK_CHARS, chunk_text's budget and index.EMBED_WINDOW_CHARS were all
    2000, but EMBED_WINDOW_CHARS measures the PREFIXED text and
    embed.contextual_prefix (default ON) adds ~100 chars at embed time — so a
    fully-packed table chunk overflowed the real window on nearly every batch and
    B3's tail truncation stayed open. CHUNK_CHARS now reserves the same headroom
    semantic.SEMANTIC_MAX_CHARS does."""
    from mcpbrain.embed import contextual_prefix
    from mcpbrain.index import EMBED_WINDOW_CHARS

    meta = {"source_type": "gdrive", "file_name": "2026 Operating Budget.xlsx",
            "folder_path": "Finance/Budgets/2026", "modified": "2026-06-02",
            "org": "Centrepoint Church"}
    prefix = contextual_prefix(meta)
    assert len(prefix) > 100, f"prefix too short to discriminate: {len(prefix)}"

    t = tabular.Table(
        sheet="Operating", header=[f"Column {i}" for i in range(6)],
        rows=[[f"cell {r}-{c} value" for c in range(6)] for r in range(120)],
        rows_total=120, truncated=False)
    chunks = tabular.render_chunks([t], file_name=meta["file_name"],
                                   max_chars=tabular.CHUNK_CHARS)

    biggest = max(len(text) for text, _m in chunks)
    assert biggest > tabular.CHUNK_CHARS - 200, (
        f"no chunk got close to the budget ({biggest}) — the test would not "
        f"discriminate"
    )
    assert biggest + len(prefix) <= EMBED_WINDOW_CHARS, (
        f"prefixed table chunk is {biggest + len(prefix)} chars, over the "
        f"{EMBED_WINDOW_CHARS}-char embedder window"
    )
