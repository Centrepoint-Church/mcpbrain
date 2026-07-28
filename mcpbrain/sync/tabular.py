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

# Target size of one rendered row-group chunk. Matches chunk_text's default
# budget (max_tokens=500 * 4) so a table chunk and a prose chunk fit the same
# 512-token embedder window. Public: Drive files and email attachments both
# render tables and must agree.
CHUNK_CHARS = 2000

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
    """Drop entirely-empty rows and trim trailing all-empty columns.

    Only TRAILING columns: an interior blank column can be meaningful (a spacer
    between a budget's actuals and its variance), and dropping it would misalign
    the header against its values.
    """
    kept = [r for r in rows if any((c or "").strip() for c in r)]
    width = 0
    for r in kept:
        for i, c in enumerate(r):
            if (c or "").strip():
                width = max(width, i + 1)
    return [r[:width] for r in kept]


def tables_from_csv(text: str, *, sheet: str = "Sheet1",
                    char_budget: int) -> list[Table]:
    """Parse CSV text into a single Table, bounded by `char_budget`.

    Serves both CSV downloads and Google Sheets exports (drive.py exports
    spreadsheets as text/csv), so those two paths and the XLSX path share one
    renderer downstream.
    """
    rows = normalise_rows(list(csv.reader(io.StringIO(text))))
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


def _cell(value: str) -> str:
    out = (value or "").replace("|", "\\|").replace("\n", " ").strip()
    return out[:_MAX_CELL_CHARS] + "…" if len(out) > _MAX_CELL_CHARS else out


def _md_row(row: list[str], width: int) -> str:
    cells = [_cell(c) for c in row] + [""] * (width - len(row))
    return "| " + " | ".join(cells[:width]) + " |"


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
        # description column is not a total worth reporting.
        if values and len(values) >= max(1, len(t.rows) // 2):
            totals.append(f"{name or f'col{i}'} {sum(values):.2f}")
    if totals:
        lines.append("Totals: " + ", ".join(totals))
    if t.truncated:
        lines.append(f"NOTE: only {len(t.rows)} of {t.rows_total} rows captured.")
    return "\n".join(lines)


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
        # Reserve room for the title, header and separator that every group
        # repeats, so a group's TOTAL size respects max_chars.
        overhead = len(header_line) + len(sep_line) + 80
        group: list[str] = []
        start = 1
        for n, row in enumerate(t.rows, start=1):
            line = _md_row(row, width)
            if group and overhead + sum(len(g) + 1 for g in group) + len(line) > max_chars:
                out.append(_emit(t, header_line, sep_line, group, base,
                                 start, start + len(group) - 1))
                start, group = n, []
            group.append(line)
        if group:
            out.append(_emit(t, header_line, sep_line, group, base,
                             start, start + len(group) - 1))
    return [(text, meta) for text, meta in out if has_content(text)]


def _emit(t: Table, header_line: str, sep_line: str, group: list[str],
          base: dict, row_start: int, row_end: int) -> tuple[str, dict]:
    title = f"### Sheet: {t.sheet} — rows {row_start}–{row_end} of {t.rows_total}"
    text = "\n".join([title, header_line, sep_line, *group])
    return text, {**base, "table_role": "rows",
                  "row_start": row_start, "row_end": row_end}
