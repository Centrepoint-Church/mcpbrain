"""Row-group chunking for tabular sources (XLSX, CSV, Google Sheets).

A table is not prose, and chunking it like prose destroys it. Character-split
CSV produces headerless fragments — a real mid-table chunk from the live 2026
Budget read `,,Internet & IT support,"7,000",583,,"7,000",583,,,0,` with the
column names stranded in chunk 0. Neither the embedding nor a model reading that
chunk can tell whether 583 is a monthly figure, an actual or a variance.

This module emits, per sheet:

  1. one SUMMARY chunk — file, sheet, dimensions, column names and per-column
     numeric totals, so a broad question has something to match; and
  2. N ROW-GROUP chunks, each repeating the sheet name, its row range and the
     header row, so every chunk is independently interpretable.

XLSX, CSV downloads and Google Sheets exports all converge on `Table` before
they reach the renderer, so there is one chunking implementation rather than
three.
"""

import csv
import io
import logging
import re
from dataclasses import dataclass, field

from mcpbrain.chunking import has_content

log = logging.getLogger(__name__)

# MIME types whose content is a table. Must stay in step with the entries in
# drive._MIME_EXTRACTION_META whose content_subtype is "table" — tabular cannot
# import drive (drive imports tabular), so
# test_table_mimes_agrees_with_the_drive_extraction_meta_table is the guard.
TABLE_MIMES = frozenset({
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.google-apps.spreadsheet",
    "application/vnd.ms-excel",   # legacy .xls (A2) — read via xlrd
    "text/csv",
    "application/csv",
    "text/tab-separated-values",
})

# Target size of one rendered row-group chunk. Matches chunk_text's EFFECTIVE
# default budget so a table chunk and a prose chunk fit the same 512-token
# embedder window. 1800, not 2000 (I8): embed.contextual_prefix (default ON) adds
# ~100 chars of provenance to every gdrive/gmail passage at embed time and
# index.EMBED_WINDOW_CHARS measures the PREFIXED text, so a 2,000-char table
# chunk overflows the real window and loses its tail (B3). Same number as
# semantic.SEMANTIC_MAX_CHARS and chunking's max_tokens*4 - _PREFIX_HEADROOM_CHARS
# — one chunking policy across the codebase. Public: Drive files and email
# attachments both render tables and must agree.
CHUNK_CHARS = 1800

# Longest a single rendered cell may be before elision. One runaway cell (a
# pasted paragraph in a spreadsheet) must not blow a whole row group on its own.
_MAX_CELL_CHARS = 300


@dataclass
class Table:
    """One sheet, already normalised (no empty rows, no trailing empty columns).

    `rows_total` is how many non-empty data rows the SOURCE had, which is not
    len(rows) when the char budget cut the read short — that difference is
    exactly what `truncated` reports, and what makes clipping visible in chunk
    metadata instead of invisible for months (B1).
    """
    sheet: str
    header: list[str]
    rows: list[list[str]] = field(default_factory=list)
    rows_total: int = 0
    truncated: bool = False


def is_tabular(mime: str) -> bool:
    return mime in TABLE_MIMES


def normalise_rows(rows: list[list[str]]) -> list[list[str]]:
    """Drop entirely-empty rows and trim trailing columns with no real support.

    Only TRAILING columns: an interior blank column can be meaningful (a
    spacer between a budget's actuals and its variance), and dropping it
    would misalign the header against its values.

    A column counts as real only if at least max(2, len(kept)//100) distinct
    rows have non-empty content there -- NOT simply "the max index any row
    reaches". A single anomalous row (a title banner, a stray far-right
    formatted-but-blank cell -- both routine in real Excel files, since
    Excel's "used range" is inflated by formatting alone) can never clear a
    support floor of 2 on its own, so it can no longer single-handedly
    dictate the whole table's width the way a bare max did. A genuinely
    sparse-but-real column (used by a legitimate minority of rows) still
    clears the floor and survives -- a median would have dropped it if used
    by fewer than half the rows, which is why this isn't a median.
    """
    kept = [r for r in rows if any((c or "").strip() for c in r)]
    if not kept:
        return []
    max_len = max(len(r) for r in kept)
    support = [0] * max_len
    for r in kept:
        for i, c in enumerate(r):
            if (c or "").strip():
                support[i] += 1
    floor = max(2, len(kept) // 100)
    width = 0
    for i in range(max_len - 1, -1, -1):
        if support[i] >= floor:
            width = i + 1
            break
    if width == 0:
        # No column clears the floor (e.g. a genuinely tiny 1-2 row table) --
        # fall back to the simple max so a small legitimate table isn't
        # wrongly trimmed to zero width.
        for r in kept:
            for i, c in enumerate(r):
                if (c or "").strip():
                    width = max(width, i + 1)
    return [r[:width] for r in kept]


def delimiter_for_mime(mime: str) -> str:
    """Field delimiter for a delimited-text MIME type.

    I4: TABLE_MIMES includes text/tab-separated-values, and both drive.py and
    attachments.py routed TSV through `tables_from_csv` with csv.reader's default
    comma — a real TSV parsed as ONE column holding the whole row, which
    _MAX_CELL_CHARS then truncated at 300 chars, silently discarding the rest of
    every row. Lives here so both call sites cannot drift.
    """
    return "\t" if mime == "text/tab-separated-values" else ","


def tables_from_csv(text: str, *, sheet: str = "Sheet1",
                    char_budget: int, delimiter: str = ",") -> list[Table]:
    """Parse delimited text into a single Table, bounded by `char_budget`.

    Serves both CSV downloads and Google Sheets exports (drive.py exports
    spreadsheets as text/csv), so those two paths and the XLSX path share one
    renderer downstream. `delimiter` is driven by the caller's known MIME type
    (see delimiter_for_mime) rather than sniffed: the MIME is authoritative and a
    sniffer can be wrong on a one-column file.
    """
    rows = normalise_rows(list(csv.reader(io.StringIO(text), delimiter=delimiter)))
    if not rows:
        return []
    header, data = rows[0], rows[1:]
    kept, used = [], 0
    for row in data:
        size = sum(len(c) + 1 for c in row)
        if used + size > char_budget and kept:
            break
        kept.append(row)
        used += size
    if len(kept) < len(data):
        log.warning("csv sheet %r truncated at %d of %d rows (char budget %d)",
                    sheet, len(kept), len(data), char_budget)
    return [Table(sheet=sheet, header=header, rows=kept,
                  rows_total=len(data), truncated=len(kept) < len(data))]


def _cell(value: str, max_cell_chars: int = _MAX_CELL_CHARS) -> str:
    out = (value or "").replace("|", "\\|").replace("\n", " ").strip()
    return out[:max_cell_chars] + "…" if len(out) > max_cell_chars else out


def _md_row(row: list[str], width: int, max_cell_chars: int = _MAX_CELL_CHARS) -> str:
    cells = [_cell(c, max_cell_chars) for c in row] + [""] * (width - len(row))
    return "| " + " | ".join(cells[:width]) + " |"


# Header words that name an IDENTIFIER, and words that name a QUANTITY. Checked
# as whole words against the header, quantity first (so "Invoice Amount" and
# "Account Balance" are still totalled).
_ID_HEADER_WORDS = frozenset({
    "code", "codes", "id", "ids", "ref", "reference", "account", "acct",
    "number", "no", "num", "year", "invoice", "phone", "mobile", "postcode",
    "zip", "abn", "acn", "gl", "index", "row", "line", "abr",
})
_QUANTITY_HEADER_WORDS = frozenset({
    "amount", "amt", "total", "totals", "subtotal", "budget", "budgeted",
    "actual", "actuals", "cost", "costs", "price", "value", "qty", "quantity",
    "count", "hours", "fee", "fees", "income", "expense", "expenses", "balance",
    "variance", "paid", "spend", "revenue", "salary", "wages", "rate", "gst",
    "tax", "$", "sum", "ytd", "forecast",
})

_HEADER_WORD = re.compile(r"[a-z$]+")


def _looks_like_identifier_column(name: str, values: list[float]) -> bool:
    """True when a mostly-numeric column holds CODES rather than quantities (I3).

    _summary_text used to sum every mostly-numeric column, so a GL/account-code
    column (4521, 6100, 6200…) was reported as `Totals: Account 15142.00` beside
    the genuine monetary totals — a confident, meaningless figure in the one
    chunk written specifically to answer "how big is this budget".

    Two signals, either sufficient, with a quantity-word override on both:
      * the header names an identifier ("Account", "GL Code", "Invoice No"); or
      * every value is a whole number of the SAME digit width, there are at least
        three of them, and that width is >= 3 — the shape of a code block.

    Deliberate limits: a genuine quantity column of same-width integers under an
    uninformative header ("Jun" over four-digit dollar amounts) loses its total,
    and an identifier column headed with a quantity word keeps one. That trade is
    the right way round — a missing total costs a broad-match hint, a fabricated
    total is a wrong answer stated as fact.
    """
    words = set(_HEADER_WORD.findall((name or "").lower()))
    if words & _QUANTITY_HEADER_WORDS:
        return False
    if words & _ID_HEADER_WORDS:
        return True
    if len(values) < 3 or not all(float(v).is_integer() for v in values):
        return False
    widths = {len(str(abs(int(v)))) for v in values}
    return len(widths) == 1 and widths.pop() >= 3


def _summary_text(file_name: str, t: Table) -> str:
    lines = [f"### Sheet summary: {t.sheet} ({file_name})",
             f"Rows: {t.rows_total} · Columns: {len(t.header)}",
             f"Columns: {', '.join(h for h in t.header if h.strip())}"]
    totals = []
    for i, name in enumerate(t.header):
        values = []
        for r in t.rows:
            raw = (r[i] if i < len(r) else "").replace(",", "").replace("$", "").strip()
            try:
                values.append(float(raw))
            except ValueError:
                continue
        # Only total a column that is MOSTLY numeric: a stray year in a
        # description column is not a total worth reporting. And only if the
        # numbers are quantities rather than codes (I3).
        if (values and len(values) >= max(1, len(t.rows) // 2)
                and not _looks_like_identifier_column(name, values)):
            totals.append(f"{name or f'col{i}'} {sum(values):.2f}")
    if totals:
        lines.append("Totals: " + ", ".join(totals))
    if t.truncated:
        lines.append(f"NOTE: only {len(t.rows)} of {t.rows_total} rows captured.")
    return "\n".join(lines)


def _title(t: Table, row_start: int, row_end: int) -> str:
    return f"### Sheet: {t.sheet} — rows {row_start}–{row_end} of {t.rows_total}"


def _rendered_size(title: str, header_line: str, sep_line: str,
                   rows: list[str]) -> int:
    """Exact length of a row-group chunk, not a guessed constant.

    The title's length depends on `t.sheet` and the digit width of the row
    range/total, so a fixed fudge factor either under- or over-estimates it —
    on the brief's own max_chars=120 example a "+80" guess was itself larger
    than the whole budget. Measuring the real joined text keeps the packing
    decision honest.
    """
    return len("\n".join([title, header_line, sep_line, *rows]))


def _fit_row(t: Table, header_line: str, sep_line: str, row: list[str],
            width: int, row_start: int, row_end: int, max_chars: int) -> str:
    """Render one row, shrinking its cells if the row ALONE — together with
    this group's title/header/separator — would overflow max_chars.

    Without this, a wide table (many columns, each cell elided only down to
    `_MAX_CELL_CHARS`) can render a single row line longer than the entire
    chunk budget: 30 long columns at the default 300-char-per-cell cap alone
    produced a 9,500+ char chunk against a 2,000-char budget, with nothing in
    the packing loop able to split a single row across chunks. Cell width is
    halved until the row fits or hits a 5-char floor (never truncated to
    nothing — a same-if-illegible cell still beats a missing column).
    """
    title = _title(t, row_start, row_end)
    fixed = len(title) + 1 + len(header_line) + 1 + len(sep_line) + 1
    budget = max_chars - fixed
    cap = _MAX_CELL_CHARS
    line = _md_row(row, width, cap)
    while len(line) > budget and cap > 5:
        cap = max(5, cap // 2)
        line = _md_row(row, width, cap)
    return line


def render_chunks(tables: list[Table], *, file_name: str,
                  max_chars: int) -> list[tuple[str, dict]]:
    """Render Tables to (chunk_text, metadata_extras) pairs.

    Every row-group chunk repeats the sheet name, its row range and the header,
    so it is independently interpretable. Content-free chunks are never emitted.
    """
    out: list[tuple[str, dict]] = []
    for t in tables:
        base = {"sheet": t.sheet, "rows_total": t.rows_total,
                "rows_captured": len(t.rows), "truncated": t.truncated}
        out.append((_summary_text(file_name, t), {**base, "table_role": "summary"}))

        width = max([len(t.header)] + [len(r) for r in t.rows]) if t.rows else len(t.header)
        header_line = _md_row(t.header, width)
        sep_line = "| " + " | ".join(["---"] * width) + " |"
        group: list[str] = []
        start = 1
        for n, row in enumerate(t.rows, start=1):
            line = _fit_row(t, header_line, sep_line, row, width, start, n, max_chars)
            candidate = group + [line]
            if group and _rendered_size(_title(t, start, n), header_line, sep_line,
                                        candidate) > max_chars:
                out.append(_emit(t, header_line, sep_line, group, base,
                                 start, start + len(group) - 1))
                start, group = n, []
                # The group just reset, so re-fit against the new (smaller)
                # row_start — the row_end digit width rarely changes, but this
                # keeps the fit exact rather than reusing a stale estimate.
                line = _fit_row(t, header_line, sep_line, row, width, start, n, max_chars)
            group.append(line)
        if group:
            out.append(_emit(t, header_line, sep_line, group, base,
                             start, start + len(group) - 1))
    return [(text, meta) for text, meta in out if has_content(text)]


def _emit(t: Table, header_line: str, sep_line: str, group: list[str],
          base: dict, row_start: int, row_end: int) -> tuple[str, dict]:
    title = _title(t, row_start, row_end)
    text = "\n".join([title, header_line, sep_line, *group])
    return text, {**base, "table_role": "rows",
                  "row_start": row_start, "row_end": row_end}
