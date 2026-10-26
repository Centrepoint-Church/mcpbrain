"""Row-group chunking for tabular sources (XLSX, CSV, Google Sheets).

A table is not prose, and chunking it like prose destroys it. Character-split
CSV produces headerless fragments — a real mid-table chunk from the live 2026
Budget read `,,Internet & IT support,"7,000",583,,"7,000",583,,,0,` with the
column names stranded in chunk 0. Neither the embedding nor a model reading that
chunk can tell whether 583 is a monthly figure, an actual or a variance.

This module emits, per sheet:

  1. one SUMMARY chunk — file, sheet, dimensions, column names and per-column
     numeric totals, so a broad question has something to match; and
  2. N ROW-GROUP chunks, each repeating the sheet name and its row range, with
     every row rendered as its own schema-enriched "Header: Value; ..."
     sentence (empty cells never rendered) so every chunk — and every row
     within it — is independently interpretable without a shared header row.

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


# A column counts as real if at least this many DISTINCT rows have non-empty
# content there. Flat, not proportional to table size: a single anomalous
# row (a title banner, a stray far-right formatted-but-blank cell -- both
# routine in real Excel files, since Excel's "used range" is inflated by
# formatting alone) can never clear 2 on its own, which is all defeating the
# original bug requires. A proportional floor (e.g. len(kept)//100) sounds
# more "statistically sound" but is actively wrong here: on a real 700+ row
# ledger it climbs well past 2, and a genuinely rare-but-real column (used by
# a few dozen rows, not the single outlier this exists to catch) gets treated
# as phantom too. 2 is the minimum that still defeats the bug, at any table
# size.
_MIN_COLUMN_SUPPORT = 2


def normalise_rows(rows: list[list[str]]) -> list[list[str]]:
    """Drop entirely-empty rows and trim trailing columns with no real support.

    Only TRAILING columns: an interior blank column can be meaningful (a
    spacer between a budget's actuals and its variance), and dropping it
    would misalign the header against its values.

    A column counts as real only if at least _MIN_COLUMN_SUPPORT distinct
    rows have non-empty content there -- NOT simply "the max index any row
    reaches". A single anomalous row can never clear that floor on its own,
    so it can no longer single-handedly dictate the whole table's width the
    way a bare max did. A genuinely sparse-but-real column (used by a
    legitimate minority of rows, any table size) still clears the floor and
    survives -- a median would have dropped it if used by fewer than half
    the rows, which is why this isn't a median.

    A row that HAD real content, but only in a column that didn't clear the
    floor, becomes empty after trimming and is dropped along with the
    genuinely-empty rows below -- the same fate as the reproducing case (a
    title-banner row whose entire content was one phantom far-right cell).
    This is the accepted cost of frequency-based phantom-detection: a
    single-row column is structurally indistinguishable from a phantom one
    by frequency alone, which is exactly why the floor exists.
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
    floor = _MIN_COLUMN_SUPPORT
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
    return [r[:width] for r in kept if any((c or "").strip() for c in r[:width])]


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
    """Normalise one cell's value for rendering into a row sentence.

    No pipe-escaping: that was a markdown-grid artifact (cells used to sit
    inside `| ... |` columns, where a literal `|` would misalign the grid).
    Row sentences aren't `|`-delimited, so escaping here only injected a
    spurious backslash into any cell that legitimately contained a pipe
    character -- noise reaching the embedder for no benefit. `_cell`'s only
    caller is `_row_sentence`; `_summary_text` builds its lines directly and
    never calls this.
    """
    out = (value or "").replace("\n", " ").strip()
    return out[:max_cell_chars] + "…" if len(out) > max_cell_chars else out


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


def _clip(entry: str, budget: int) -> str:
    """Hard-clip one list entry that alone exceeds `budget` (a pathological
    single header); everything else is split between entries, never inside."""
    return entry if len(entry) <= budget else entry[:max(1, budget - 1)] + "…"


def _pack_entries(prefix: str, entries: list[str], budget: int) -> list[list[str]]:
    """Greedily group `entries` (joined by ', ') so `prefix + joined` <= budget.

    The first group is measured with `prefix`; callers pass the budget for it.
    Returns at least one group when `entries` is non-empty."""
    groups: list[list[str]] = []
    cur: list[str] = []
    for e in entries:
        e = _clip(e, max(1, budget - len(prefix)))
        if cur and len(prefix) + len(", ".join([*cur, e])) > budget:
            groups.append(cur)
            cur = []
        cur.append(e)
    if cur:
        groups.append(cur)
    return groups


def _summary_texts(file_name: str, t: Table, max_chars: int) -> list[str]:
    """Summary chunk text(s) for one sheet, every one <= `max_chars`.

    A sheet whose summary fits renders as ONE chunk, byte-identical to the
    original single-text design. A wide sheet (the live store has 64-68 column
    sheets whose `Columns:` line alone reached 5,400 chars) keeps its title,
    `Rows:`, `Totals:` and NOTE in the first chunk and carries the remaining
    column names in continuation chunks."""
    title = f"### Sheet summary: {t.sheet} ({file_name})"
    rows_line = f"Rows: {t.rows_total} · Columns: {len(t.header)}"
    names = [h for h in t.header if h.strip()]
    columns_line = f"Columns: {', '.join(names)}"
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
    totals_line = "Totals: " + ", ".join(totals) if totals else ""
    note = (f"NOTE: only {len(t.rows)} of {t.rows_total} rows captured."
            if t.truncated else "")

    whole = "\n".join(x for x in [title, rows_line, columns_line,
                                  totals_line, note] if x)
    if len(whole) <= max_chars:
        return [whole]

    # Wide sheet. Totals get at most half the first chunk (the remainder spills
    # to their own continuation chunks); columns fill what is left of it.
    head = [title, rows_line]
    head_len = len("\n".join(head))
    tot_groups = _pack_entries("Totals: ", totals,
                               max(1, (max_chars - head_len) // 2))
    first_totals = ("Totals: " + ", ".join(tot_groups[0])) if tot_groups else ""
    tot_rest = tot_groups[1:]
    tail = [x for x in [first_totals, note] if x]
    col_budget = max_chars - head_len - len("\n".join(tail)) - len(tail) - 1
    col_groups = _pack_entries("Columns: ", names, max(len("Columns: ") + 1,
                                                        col_budget))
    cont_title = f"### Sheet summary: {t.sheet} ({file_name}) — columns cont."
    texts = []
    first_cols = ("Columns: " + ", ".join(col_groups[0])) if col_groups else ""
    texts.append("\n".join(x for x in [*head, first_cols, *tail] if x))
    # Continuation groups re-pack against the full budget (no head/totals).
    rest_names = [e for g in col_groups[1:] for e in g]
    for g in _pack_entries("Columns: ", rest_names,
                           max_chars - len(cont_title) - 1):
        texts.append(f"{cont_title}\nColumns: {', '.join(g)}")
    tot_title = f"### Sheet summary: {t.sheet} ({file_name}) — totals cont."
    rest_totals = [e for g in tot_rest for e in g]
    for g in _pack_entries("Totals: ", rest_totals,
                           max_chars - len(tot_title) - 1):
        texts.append(f"{tot_title}\nTotals: {', '.join(g)}")
    return texts


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

    `header_line`/`sep_line` are "" in the schema-enriched design (there is no
    shared header/separator line any more) — filtered out here so the size
    measured here matches exactly what `_emit` joins (it filters the same way),
    rather than over-counting two blank-line separators that never actually
    appear in the emitted text.
    """
    return len("\n".join(line for line in [title, header_line, sep_line, *rows] if line))


_MAX_FIELDS_PER_ROW = 40  # generous for a real spreadsheet, tight enough to
                          # bound a phantom-column sheet even without
                          # normalise_rows' own fix (defense in depth).


def _row_pairs(header: list[str], row: list[str],
               max_cell_chars: int = _MAX_CELL_CHARS) -> list[str]:
    """The 'Header: Value' fields of one row (non-empty cells only), with the
    40-field valve applied."""
    pairs = []
    for i, value in enumerate(row):
        v = (value or "").strip()
        if not v:
            continue
        h = header[i] if i < len(header) and header[i].strip() else f"col{i}"
        pairs.append(f"{h}: {_cell(v, max_cell_chars)}")
    if len(pairs) > _MAX_FIELDS_PER_ROW:
        extra = len(pairs) - _MAX_FIELDS_PER_ROW
        pairs = pairs[:_MAX_FIELDS_PER_ROW] + [f"(+{extra} more fields)"]
    return pairs


def _row_sentence(header: list[str], row: list[str],
                  max_cell_chars: int = _MAX_CELL_CHARS) -> str:
    """Render one row as 'Header: Value; Header: Value; ...' for non-empty
    cells only -- an empty cell is simply never rendered, which is what
    makes this immune to phantom trailing columns by construction. No shared
    width/header_line/sep_line across the sheet: one anomalous row can never
    inflate another row's output.

    `max_cell_chars` defaults to the module cap but can be shrunk by the
    caller (see render_chunks) for THIS row alone -- unlike the deleted
    _fit_row, there is no header_line/sep_line dependency here, so shrinking
    one row's cells cannot affect any other row's rendering.
    """
    return "; ".join(_row_pairs(header, row, max_cell_chars))


def _fit_row_sentence(header: list[str], row: list[str], max_chars: int) -> str:
    """_row_sentence, shrinking this row's own cell cap if the sentence ALONE
    would overflow max_chars.

    Belt-and-suspenders alongside the field-count valve in _row_sentence: a
    table with few enough columns to dodge that valve but long cell content
    (e.g. 30 columns of pasted paragraphs) could otherwise render a single
    row line longer than the whole chunk budget, with nothing in the packing
    loop able to split one row across chunks -- the exact failure mode
    _fit_row used to guard, minus its header_line/sep_line coupling. Cap is
    halved until the row fits or hits a 5-char floor (never truncated to
    nothing -- a barely-legible cell still beats a missing field).
    """
    cap = _MAX_CELL_CHARS
    line = _row_sentence(header, row, cap)
    while len(line) > max_chars and cap > 5:
        cap = max(5, cap // 2)
        line = _row_sentence(header, row, cap)
    return line


def _row_title(t: Table, n: int, cont: bool) -> str:
    return f"### Sheet: {t.sheet} — row {n} of {t.rows_total}" + (
        " (cont.)" if cont else "")


def _split_wide_row(t: Table, n: int, row: list[str], max_chars: int,
                    base: dict) -> list[tuple[str, dict]]:
    """Render a row that overflows even at the cell floor as several chunks.

    Cells keep the NORMAL cap (nothing is truncated to force a fit); the row is
    split at field boundaries into pieces that each fit the budget. Every piece
    carries `row_start == row_end == n`."""
    budget = max_chars - len(_row_title(t, n, True)) - 1
    groups = _pack_entries("", _row_pairs(t.header, row), budget)
    out = []
    for k, g in enumerate(groups):
        text = "\n".join([_row_title(t, n, k > 0), "; ".join(g)])
        out.append((text, {**base, "table_role": "rows",
                           "row_start": n, "row_end": n}))
    return out


def render_chunks(tables: list[Table], *, file_name: str,
                  max_chars: int) -> list[tuple[str, dict]]:
    """Render Tables to (chunk_text, metadata_extras) pairs.

    Each row renders as a schema-enriched sentence (see _row_sentence) --
    matches the RAG-chunking research finding that row-wise "schema-enriched"
    sentences outperform raw markdown-grid table embeddings for retrieval,
    and is immune to the phantom-column bug by construction (an empty cell
    is never rendered, so there's no shared width to compute or get wrong).
    Content-free chunks are never emitted. No chunk exceeds `max_chars`: a wide
    summary spills its column list into continuation chunks and a row too wide
    for one chunk is split at field boundaries (see _split_wide_row).
    """
    out: list[tuple[str, dict]] = []
    for t in tables:
        base = {"sheet": t.sheet, "rows_total": t.rows_total,
                "rows_captured": len(t.rows), "truncated": t.truncated}
        summaries = _summary_texts(file_name, t, max_chars)
        for k, text in enumerate(summaries, start=1):
            extra = {"summary_part": k} if len(summaries) > 1 else {}
            out.append((text, {**base, "table_role": "summary", **extra}))

        group: list[str] = []
        start = 1
        for n, row in enumerate(t.rows, start=1):
            row_budget = max_chars - len(_title(t, start, n)) - 1
            line = _fit_row_sentence(t.header, row, row_budget)
            candidate = group + [line]
            if group and _rendered_size(_title(t, start, n), "", "",
                                        candidate) > max_chars:
                out.append(_emit(t, "", "", group, base, start, start + len(group) - 1))
                start, group = n, []
                # The group just reset, so re-fit against the new (smaller)
                # row_start -- the row_end digit width rarely changes, but
                # this keeps the fit exact rather than reusing a stale
                # estimate.
                row_budget = max_chars - len(_title(t, start, n)) - 1
                line = _fit_row_sentence(t.header, row, row_budget)
            if len(line) > row_budget:
                # Too wide even at the cell floor: split, don't truncate.
                if group:
                    out.append(_emit(t, "", "", group, base, start,
                                     start + len(group) - 1))
                out.extend(_split_wide_row(t, n, row, max_chars, base))
                start, group = n + 1, []
                continue
            group.append(line)
        if group:
            out.append(_emit(t, "", "", group, base, start, start + len(group) - 1))
    return [(text, meta) for text, meta in out if has_content(text)]


def _emit(t: Table, header_line: str, sep_line: str, group: list[str],
          base: dict, row_start: int, row_end: int) -> tuple[str, dict]:
    """Join title (+ header/sep lines, when non-empty) and the row group.

    `header_line`/`sep_line` are always "" in the schema-enriched design
    (render_chunks passes them that way) -- filtering falsy lines out of the
    join, rather than joining "" placeholders verbatim, avoids leaving two
    stray blank lines between the title and the first row.
    """
    title = _title(t, row_start, row_end)
    text = "\n".join(line for line in [title, header_line, sep_line, *group] if line)
    return text, {**base, "table_role": "rows",
                  "row_start": row_start, "row_end": row_end}
