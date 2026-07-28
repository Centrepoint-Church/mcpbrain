# Ingestion Correctness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix every defect in `docs/superpowers/specs/2026-07-27-ingestion-defects-findings.md` sections A (content that never arrives), B1–B8 (content that arrives degraded) and C (provenance), so that a repair backfill (spec 3) re-imports correct content instead of re-importing the same defects.

**Architecture:** No new subsystem. Every fix lands in the existing ingest path — `sync/normalise.py`, `sync/drive.py`, `sync/gmail.py`, `sync/extractors.py`, `chunking.py`, `semantic.py` — plus two new focused modules: `sync/tabular.py` (row-group chunking for spreadsheets/CSV) and `sync/attachments.py` (email attachment extraction). The one structural change is that tabular sources stop going through `chunk_text` and get their own header-repeating chunker; everything else is a correctness fix inside a function that already exists.

**Tech Stack:** Python 3.11, SQLite (`sqlite-vec` + FTS5), `openpyxl`, `python-docx`, `pymupdf`, `python-pptx` (new dependency), Google API Python client, pytest + pytest-xdist, ruff.

---

## Global Constraints

- **Work on `main`, commit as you go.** No worktrees, no feature branches. Do **not** push and do **not** release — Josh decides both separately.
- **Do not run the full suite yourself.** Josh runs `pytest tests/` full-repo runs. You scope test runs to the files you edited plus directly impacted files, named explicitly in each task's test step.
- **`uv run pytest …`** — bare `pytest`/`python` is not on PATH in this environment.
- **Ruff must pass on `mcpbrain/` and on every test file you touch.** `tests/` as a whole has 86 pre-existing errors; do not fix them, and do not add to them. Gate command: `uv run ruff check mcpbrain/ tests/<files you touched>`.
- **Do not touch the daemon scheduling work.** `daemon.py`, `prepare.py`'s budget/`bulk_section` plumbing, and `test_bulk_lock_fairness.py` are finished and unpushed. If a task needs a `bulk_section` threaded somewhere, follow the existing pattern exactly (`bulk_section=None` → `nullcontext`, one section per item, never around network I/O).
- **No repair, no migration, no backfill in this plan.** Fixing the extractor is spec 2; re-extracting the 338 truncated sheets and the 9,353 legacy files, and purging the 66,653 empty chunks, is spec 3. Anything here that would rewrite existing rows is out of scope — the only exception is Task 7's orphan delete, which is a *write-time* invariant, not a sweep.
- **Every new behaviour that changes ingest VOLUME gets a config flag and a counter.** The store is already 11.9 GB with 37% content-free chunks. A change that could grow it ships measurable, and defaults to the setting that does not grow it, unless the change is pure gain (attachments) or pure shrink (empty-chunk guard).
- **`normalise_gmail` / `normalise_drive` / `normalise_calendar` are locked interfaces** — their positional signature and `list[Chunk]` return must not change. New inputs go in as keyword-only optional arguments.
- Version bumping and the five version files are **not** part of this plan (that is the release runbook, and there is no release here).

---

## Design decisions carried in from conversation

The findings register is deliberately a register, not a design — it proposes no fixes. These decisions were agreed with Josh in the session that produced it, and are not re-litigated here:

1. **The 200-row cap is replaced by a character budget, not a bigger row cap.** Row count is the wrong bound: 200 rows of a bloated grid is megabytes of empty pipes, while 200 rows of a general ledger is a fraction of the real content. A character budget bounds the thing that actually costs (embedding + storage) and adapts to shape.
2. **Tabular content is chunked by row group with a repeated header**, not by character count. This is the single biggest quality win in the plan: it fixes all 338 truncated files *and* every table already in the store, not only the giant ones.
3. **Each sheet also gets one summary chunk** (dimensions, column names, numeric totals) so broad questions ("how big is the Harvestnet budget?") have something to match, while specific lookups ("what did we budget for Tithe.ly?") match a row-group chunk.
4. **Text with no alphanumeric character is never a chunk.** This is a generic guard, not a spreadsheet-specific one.
5. **Truncation becomes visible in metadata** (`truncated`, `rows_captured`, `rows_total`), not just a marker buried in the text, so `doctor` and the dashboard can report "N sheets clipped".
6. **The known risk, accepted:** a 50k-row ledger becomes ~4,000 semantically-similar row chunks, which could crowd recall. CLAUDE.md records the 0.7.101 decision *not* to exclude tabular from recall (roster and calendar tables are often the actual answer), and headers make these chunks genuinely interpretable rather than noise. Spec 3's gold gate is where this gets measured; this plan does not re-embed anything, so it cannot regress the existing gold number.

---

## File structure

**New files**

| File | Responsibility |
|---|---|
| `mcpbrain/sync/tabular.py` | The only place that knows how a table becomes chunks: parse the canonical sheet-serialisation, drop empty rows, trim empty columns, render a summary chunk and header-repeating row-group chunks. |
| `mcpbrain/sync/attachments.py` | The only place that knows how an email attachment becomes chunks: which parts are attachments, which MIME types are extractable, size/count caps, and the pure `normalise_attachment`. |
| `mcpbrain/sync/ingest_report.py` | One tiny durable seam for "we dropped something": `record_skip(store, kind, ref_id, detail)`. Keeps eight call sites from each inventing their own logging. |
| `tests/test_tabular.py` | Row-group chunking, header repetition, summary chunk, budget, empty handling. |
| `tests/test_attachments.py` | Attachment part discovery, routing, caps, doc_id shape. |
| `tests/test_ingest_visibility.py` | Every silent drop in the findings register is now counted or logged. |

**Modified files**

| File | Change |
|---|---|
| `mcpbrain/chunking.py` | `chunk_text` never emits empty or oversize chunks (B6); new `has_content()` (design decision 4). |
| `mcpbrain/sync/extractors.py` | XLSX serialises to the canonical tabular form with a char budget (B1); PPTX extractor added (A2); extraction failures logged rather than swallowed (B7); scanned-PDF degradation logged (A5). |
| `mcpbrain/sync/drive.py` | Tabular chunking route (B1/B2); MIME table coverage (A2); `folder_path` (C5); `chunk_total` (C1); orphan delete on shrink (B5). |
| `mcpbrain/sync/normalise.py` | Bottom-posted reply rescue (A3); bulk-mail drop made visible (A4); recipient cap raised (C6); `chunk_total` (C1). |
| `mcpbrain/sync/gmail.py` | Attachment fetch wired into both sync and backfill (A1); skip reporting (A4). |
| `mcpbrain/sync/calendar.py` | Long descriptions go through `chunk_text` (E); `chunk_total` (C1). |
| `mcpbrain/semantic.py` | Enriched chunks carry `date`, `message_id`, and the right `source_type` (C2/C3/C4). |
| `mcpbrain/graph_write.py` | Passes the lead's ISO date and message id into `build_semantic_doc` (C2/C3). |
| `mcpbrain/retrieval_expand.py` | Thread expansion sorts deterministically (B4). |
| `mcpbrain/store.py` | `thread_chunks` docstring corrected; no behaviour change (B4). |
| `mcpbrain/thread_enrich.py` | Partial-document gap marker (B8). |
| `mcpbrain/config.py` | Four new flags (below). |
| `pyproject.toml` | `python-pptx` dependency. |

**New config flags** (all read via the existing `read_config(home).get(...)` pattern; `gmail_ingest_bulk` additionally via `fleet_flag` so it is fleet-flippable):

| Flag | Default | Why that default |
|---|---|---|
| `gmail_attachments` | **ON** | Pure content gain, the largest gap in the register. |
| `gmail_ingest_bulk` | **OFF** | Would grow the store by an unmeasured amount; the counter ships live in both modes so the volume is knowable before flipping. |
| `sheet_char_budget` | `2_000_000` | Per-sheet backstop, ~16k typical rows. Only bites on genuinely enormous real content once empty rows are dropped. |
| `drive_folder_path` | **ON** | One extra cached Drive call per unseen folder; fixes a dead field in a default-ON retrieval feature. |

---

### Task 1: Chunking correctness

Foundation for everything else — Tasks 2, 4, 5, 6 and 8 all produce chunks through these helpers.

**Files:**
- Modify: `mcpbrain/chunking.py:154-179`
- Test: `tests/test_chunking.py` (extend)

**Interfaces:**
- Consumes: nothing.
- Produces: `chunk_text(text, max_tokens=500, overlap=50) -> list[str]` (unchanged signature, stronger guarantees: no chunk is empty, no chunk exceeds `max_tokens * 4` characters); `has_content(text: str) -> bool`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_chunking.py`:

```python
def test_a_token_longer_than_the_budget_emits_neither_empty_nor_oversize_chunks():
    """B6: the word-split path appended `current` while it was still "" (a
    zero-length chunk), then let the following chunk exceed max_chars. Verified
    live: 6 zero-length and 36 sub-5-char chunks exist in the store."""
    text = "x" * 5000  # one whitespace-free token, well over max_chars=2000

    chunks = chunk_text(text, max_tokens=500)

    assert all(c for c in chunks), "chunk_text emitted a zero-length chunk"
    assert all(len(c) <= 2000 for c in chunks), (
        f"chunk_text emitted an oversize chunk: {[len(c) for c in chunks]}"
    )
    assert "".join(chunks) == text, "hard-splitting a long token must lose nothing"


def test_an_oversize_token_mid_paragraph_does_not_corrupt_its_neighbours():
    """The blob must not swallow the prose around it, and the prose must not be
    dropped to make room for the blob."""
    text = "before " + ("y" * 3000) + " after"

    chunks = chunk_text(text, max_tokens=500)

    assert all(len(c) <= 2000 for c in chunks)
    joined = " ".join(chunks)
    assert "before" in joined and "after" in joined


def test_has_content_rejects_punctuation_only_text():
    """B1's 66,653 content-free chunks (37% of the live store) are ~2,000-char
    strings of '| | | | |' from empty spreadsheet cells — all embedded, none
    matchable. has_content is the generic guard against ever writing one."""
    from mcpbrain.chunking import has_content

    assert has_content("Budget 2026") is True
    assert has_content("| 42 |") is True
    assert has_content("|  |  |  |") is False
    assert has_content("| --- | --- |") is False
    assert has_content("") is False
    assert has_content("   \n\t ") is False


def test_has_content_accepts_non_ascii_alphanumerics():
    """str.isalnum is used rather than [A-Za-z0-9] precisely so a sheet of
    Chinese or accented names is not discarded as content-free."""
    from mcpbrain.chunking import has_content

    assert has_content("| 会議 |") is True
    assert has_content("| Åsa |") is True
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_chunking.py -q -p no:randomly`
Expected: `test_has_content_*` FAIL with `ImportError: cannot import name 'has_content'`; the two `chunk_text` tests FAIL on the zero-length/oversize assertions.

- [ ] **Step 3: Implement**

In `mcpbrain/chunking.py`, replace `chunk_text` (lines 154-179) with:

```python
def has_content(text: str) -> bool:
    """True when `text` carries at least one alphanumeric character.

    The generic no-content guard. B1's empty-spreadsheet chunks are ~2,000-char
    strings of '| | | | |' — 66,653 of them, 37% of the live store, every one
    embedded and none of them matchable by any query. They also dominate the
    duplicate count (65,770 copies of a single content_hash).

    `str.isalnum()` per character, not a `[A-Za-z0-9]` regex: a sheet of Chinese
    or accented names is content, and must not be discarded as noise.
    """
    return any(ch.isalnum() for ch in text)


def _hard_split(word: str, max_chars: int) -> list[str]:
    """Split a single whitespace-free token that is itself longer than the whole
    chunk budget (a base64 blob, a minified line, a long URL). Without this the
    word-split path below has no way to make progress and emits the token whole,
    exceeding max_chars."""
    if len(word) <= max_chars:
        return [word]
    return [word[i:i + max_chars] for i in range(0, len(word), max_chars)]


def _split_paragraph(para: str, max_chars: int, overlap: int) -> list[str]:
    """Split one over-long paragraph on word boundaries.

    Guarantees, both of which the previous implementation broke (B6): no emitted
    chunk is empty, and none exceeds max_chars. The old code appended `current`
    unconditionally on overflow — including on the first iteration when it was
    still "" — and then seeded the next chunk with `overlap` words PLUS the
    oversize word without re-checking the budget.

    The overlap seed is kept whenever it still leaves room for the next piece,
    preserving the contract pinned by
    test_word_split_chunks_overlap_and_lose_nothing; it is dropped only in the
    hard-split case, where by construction no overlap can fit beside a piece
    that already fills the whole budget.
    """
    out: list[str] = []
    current = ""
    for word in para.split():
        for piece in _hard_split(word, max_chars):
            if not current:
                current = piece
            elif len(current) + 1 + len(piece) <= max_chars:
                current += " " + piece
            else:
                out.append(current)
                tail = " ".join(current.split()[-overlap:])
                current = (f"{tail} {piece}"
                           if len(tail) + 1 + len(piece) <= max_chars else piece)
    if current:
        out.append(current)
    return out


def chunk_text(text: str, max_tokens: int = 500, overlap: int = 50) -> list[str]:
    """Split text into embeddable chunks on paragraph boundaries.

    Every returned chunk is non-empty and at most `max_tokens * 4` characters
    (the BGE window is 512 tokens; anything longer is silently truncated at
    embed time — 15,576 such chunks exist in the live store, B3).
    """
    max_chars = max_tokens * 4
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        if len(current) + len(para) + 2 <= max_chars:
            current += ("\n\n" + para) if current else para
        elif len(para) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            pieces = _split_paragraph(para, max_chars, overlap)
            chunks.extend(pieces[:-1])
            current = pieces[-1] if pieces else ""
        else:
            if current:
                chunks.append(current)
            current = para
    if current:
        chunks.append(current)
    return [c for c in chunks if c] or ([text[:max_chars]] if text.strip() else [])
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_chunking.py tests/test_normalise.py tests/test_drive_extraction.py tests/test_chunk_metadata.py -q -p no:randomly`
Expected: PASS. `test_word_split_chunks_overlap_and_lose_nothing` must still pass unchanged — it pins the overlap contract and this refactor deliberately preserves it.

- [ ] **Step 5: Verify the tests discriminate**

Temporarily restore the old `chunk_text` body from git (`git show HEAD:mcpbrain/chunking.py`), re-run, confirm the two new `chunk_text` tests fail, then restore your implementation. A test that passes against the defect is not a test.

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/chunking.py tests/test_chunking.py
git commit -m "fix(chunking): never emit empty or oversize chunks; add has_content guard"
```

---

### Task 2: Tabular ingestion — header-repeating row groups

The largest quality win in the plan, and the one that makes spec 3's re-extraction worth running.

**Files:**
- Create: `mcpbrain/sync/tabular.py`
- Modify: `mcpbrain/sync/extractors.py:181-231` (`_rows_to_markdown`, `extract_text_from_xlsx`)
- Modify: `mcpbrain/sync/drive.py:122-168` (`normalise_drive`), `:47` (`_DOWNLOAD_TEXT`)
- Modify: `mcpbrain/config.py`
- Test: `tests/test_tabular.py` (create), `tests/test_extractors.py` (extend), `tests/test_drive_extraction.py` (extend)

**Interfaces:**
- Consumes: `chunking.has_content`, `chunking.content_hash` (Task 1).
- Produces:
  - `tabular.TABLE_MIMES: frozenset[str]`
  - `tabular.is_tabular(mime: str) -> bool`
  - `tabular.serialise_sheets(sheets: list[tuple[str, list[list[str]]]], *, char_budget: int) -> str` — the canonical intermediate form, written by `extract_text_from_xlsx`.
  - `tabular.render_chunks(text: str, *, file_name: str, max_chars: int) -> list[tuple[str, dict]]` — `(chunk_text, metadata_extras)` pairs, consumed by `normalise_drive`.
  - `config.sheet_char_budget(home) -> int`

**Why an intermediate text form rather than passing structured tables around:** `_fetch_text` returns `str | None`, and three call sites (`_cache_first_extract_one`, `sync_drive`, `backfill_drive`) plus the entire shared-drive ingest-cache artifact format depend on that contract. Google Sheets already arrive as CSV (`drive.py:43` exports them that way) and CSV downloads arrive as CSV, so serialising XLSX to the same shape means **one** tabular parser serves all three sources instead of three, with zero churn in the cache path.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_tabular.py`:

```python
"""Tables are not prose.

Splitting a table on character count produces headerless fragments. A real
mid-table chunk from the live 2026 Budget read:

    ,,Internet & IT support,"7,000",583,,"7,000",583,,,0,,,0,

The column names live in chunk 0 and every subsequent chunk is orphaned
numbers — uninterpretable by the embedder and by a model reading it.
"""
from mcpbrain.sync import tabular

_SHEET = [
    ["Date", "Account", "Description", "Amount"],
    ["2024-03-01", "4521", "Office supplies", "42.00"],
    ["2024-03-02", "4521", "Printer toner", "88.50"],
    ["2024-03-03", "6100", "Venue hire", "1200.00"],
]


def _serialised(rows=None, *, name="GL Detail", budget=1_000_000):
    return tabular.serialise_sheets([(name, rows or _SHEET)], char_budget=budget)


def test_every_row_group_chunk_repeats_the_header():
    chunks = tabular.render_chunks(_serialised(), file_name="Ledger.xlsx",
                                   max_chars=120)
    rows = [(t, m) for t, m in chunks if m["table_role"] == "rows"]

    assert len(rows) >= 2, "max_chars=120 should force more than one row group"
    for text, _meta in rows:
        assert "| Date | Account | Description | Amount |" in text, (
            "a row-group chunk without its header is the B2 defect: orphaned "
            f"numbers with no column names. Got:\n{text}"
        )


def test_each_row_group_names_its_sheet_and_row_range():
    chunks = tabular.render_chunks(_serialised(), file_name="Ledger.xlsx",
                                   max_chars=120)
    rows = [(t, m) for t, m in chunks if m["table_role"] == "rows"]

    first_text, first_meta = rows[0]
    assert "### Sheet: GL Detail" in first_text
    assert first_meta["sheet"] == "GL Detail"
    assert first_meta["row_start"] == 1
    assert first_meta["row_end"] >= 1
    assert first_meta["rows_total"] == 3, "3 data rows, header excluded"


def test_a_summary_chunk_is_emitted_for_each_sheet():
    """Broad questions ('how big is the Harvestnet budget?') have nothing good
    to match without one; specific lookups match a row group instead."""
    chunks = tabular.render_chunks(_serialised(), file_name="Ledger.xlsx",
                                   max_chars=2000)
    summaries = [(t, m) for t, m in chunks if m["table_role"] == "summary"]

    assert len(summaries) == 1
    text, meta = summaries[0]
    assert "Ledger.xlsx" in text
    assert "GL Detail" in text
    assert "Date, Account, Description, Amount" in text
    assert "1330.50" in text, "numeric columns should be totalled in the summary"
    assert meta["sheet"] == "GL Detail"


def test_entirely_empty_rows_are_dropped_before_chunking():
    """B1: the old extractor appended empty rows verbatim. One file — Fixed
    Assett Register 2023 onwards.xlsx — produced 17,281 chunks of ~2,000 chars
    each, 300-500 pipes per chunk, zero alphanumerics."""
    rows = [_SHEET[0], ["", "", "", ""], _SHEET[1], ["", "", "", ""]]

    chunks = tabular.render_chunks(_serialised(rows), file_name="X.xlsx",
                                   max_chars=2000)
    body = "\n".join(t for t, m in chunks if m["table_role"] == "rows")

    assert "Office supplies" in body
    assert "|  |  |  |  |" not in body
    for text, _ in chunks:
        from mcpbrain.chunking import has_content
        assert has_content(text), f"content-free chunk emitted:\n{text!r}"


def test_trailing_empty_columns_are_trimmed():
    rows = [["Name", "Amount", "", ""], ["Rent", "500", "", ""]]

    chunks = tabular.render_chunks(_serialised(rows), file_name="X.xlsx",
                                   max_chars=2000)
    rowtext = next(t for t, m in chunks if m["table_role"] == "rows")

    assert "| Name | Amount |" in rowtext
    assert "| Name | Amount |  |  |" not in rowtext


def test_the_char_budget_replaces_the_200_row_cap():
    """338 live files hit the old 200-row cap — budgets, general ledgers and
    risk assessments, every row past 200 per sheet discarded. The bound is now
    characters, counting only non-empty rows, so shape no longer decides how
    much real content survives."""
    big = [_SHEET[0]] + [[f"2024-03-{i:02d}", "4521", f"Item {i}", str(i)]
                         for i in range(1, 400)]

    serialised = tabular.serialise_sheets([("Big", big)], char_budget=1_000_000)

    assert "Item 250" in serialised, "row 250 must survive; the old cap cut at 200"
    assert "Item 399" in serialised


def test_truncation_is_recorded_in_metadata_not_only_in_the_text():
    """So doctor and the dashboard can report 'N sheets clipped' instead of it
    being invisible for months."""
    big = [_SHEET[0]] + [["2024-03-01", "4521", f"Item {i}", str(i)]
                         for i in range(1, 500)]

    serialised = tabular.serialise_sheets([("Big", big)], char_budget=2_000)
    chunks = tabular.render_chunks(serialised, file_name="X.xlsx", max_chars=2000)

    meta = chunks[0][1]
    assert meta["truncated"] is True
    assert meta["rows_total"] == 499
    assert 0 < meta["rows_captured"] < 499


def test_a_plain_csv_with_no_sheet_directive_still_renders_with_headers():
    """Google Sheets export as text/csv (drive.py:43) and CSV downloads pass
    through verbatim, so render_chunks must handle bare CSV, not only the
    XLSX-serialised form."""
    csv_text = "Name,Amount\nRent,500\nPower,120\n"

    chunks = tabular.render_chunks(csv_text, file_name="Budget.csv",
                                   max_chars=2000)
    rowtext = next(t for t, m in chunks if m["table_role"] == "rows")

    assert "| Name | Amount |" in rowtext
    assert "| Rent | 500 |" in rowtext


def test_a_quoted_cell_containing_the_sheet_directive_is_not_a_sheet_break():
    """serialise_sheets writes CSV with quoting, so a cell whose text happens to
    be '### Sheet: x' arrives quoted and cannot be mistaken for a block header."""
    rows = [["Note"], ["### Sheet: not a real sheet"]]

    chunks = tabular.render_chunks(_serialised(rows, name="Real"),
                                   file_name="X.xlsx", max_chars=2000)
    sheets = {m["sheet"] for _t, m in chunks}

    assert sheets == {"Real"}


def test_table_mimes_agrees_with_the_drive_extraction_meta_table():
    """tabular.TABLE_MIMES and drive._MIME_EXTRACTION_META's 'table' subtype are
    two lists of the same thing in two modules (tabular cannot import drive —
    drive imports tabular). This guard is what keeps them honest."""
    from mcpbrain.sync import drive

    from_drive = {m for m, (_meth, sub, _c) in drive._MIME_EXTRACTION_META.items()
                  if sub == "table"}

    assert from_drive == set(tabular.TABLE_MIMES)
```

Add to `tests/test_extractors.py`:

```python
def test_xlsx_serialises_to_the_canonical_tabular_form():
    """extract_text_from_xlsx no longer returns markdown — it returns the
    canonical CSV-with-directives form that tabular.render_chunks parses, so one
    parser serves XLSX, CSV downloads and Google Sheets exports alike."""
    import io

    import openpyxl

    from mcpbrain.sync.extractors import extract_text_from_xlsx

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Budget"
    ws.append(["Item", "Amount"])
    ws.append(["Rent", 500])
    buf = io.BytesIO()
    wb.save(buf)

    text = extract_text_from_xlsx(buf.getvalue())

    assert text.startswith("### Sheet: Budget")
    assert "#! rows_total=1 rows_captured=1 truncated=0" in text
    assert "Item,Amount" in text
    assert "Rent,500" in text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_tabular.py tests/test_extractors.py -q -p no:randomly`
Expected: `ModuleNotFoundError: No module named 'mcpbrain.sync.tabular'`, and the extractor test fails because `extract_text_from_xlsx` still returns markdown.

- [ ] **Step 3: Create `mcpbrain/sync/tabular.py`**

```python
"""Row-group chunking for tabular sources (XLSX, CSV, Google Sheets).

A table is not prose, and chunking it like prose destroys it. Character-split
CSV produces headerless fragments — a real mid-table chunk from the live 2026
Budget read `,,Internet & IT support,"7,000",583,,"7,000",583,,,0,` with the
column names stranded in chunk 0. Neither the embedding nor a model reading
that chunk can tell whether 583 is a monthly figure, an actual or a variance.

This module emits, per sheet:

  1. one SUMMARY chunk — file, sheet, dimensions, column names and per-column
     numeric totals, so a broad question has something to match; and
  2. N ROW-GROUP chunks, each repeating the sheet name, its row range and the
     header row, so every chunk is independently interpretable.

The canonical intermediate form
-------------------------------
`serialise_sheets` writes, and `render_chunks` reads, blocks of:

    ### Sheet: <name>
    #! rows_total=<n> rows_captured=<n> truncated=<0|1>
    <header row, CSV>
    <data rows, CSV>

CSV rather than a structured hand-off because Google Sheets already arrive as
CSV (drive.py exports them that way) and CSV downloads arrive as CSV — so ONE
parser serves all three sources, and `_fetch_text`'s `str | None` contract (and
with it the whole shared-drive ingest-cache artifact format) is untouched.

Rows are written through `csv.writer`, so a cell whose own text is `### Sheet:`
or `#!` arrives quoted and cannot be misread as a directive. `render_chunks`
additionally only accepts `#!` on the line directly after a `### Sheet:` line.
"""

import csv
import io
import logging

from mcpbrain.chunking import has_content

log = logging.getLogger(__name__)

# MIME types whose content is a table. Must stay in step with the entries in
# drive._MIME_EXTRACTION_META whose content_subtype is "table" — tabular cannot
# import drive (drive imports tabular), so
# test_table_mimes_agrees_with_the_drive_extraction_meta_table is the guard.
TABLE_MIMES = frozenset({
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.google-apps.spreadsheet",
    "text/csv",
    "application/csv",
    "text/tab-separated-values",
})

_SHEET_PREFIX = "### Sheet: "
_DIRECTIVE_PREFIX = "#! "
_DEFAULT_SHEET = "Sheet1"

# Target size of one rendered row-group chunk. Matches chunk_text's default
# budget (max_tokens=500 * 4) so a table chunk and a prose chunk fit the same
# 512-token embedder window. Public because both Drive files and email
# attachments render tables and must agree on it.
CHUNK_CHARS = 2000

# Longest a single rendered cell may be before it is elided. One runaway cell
# (a pasted paragraph in a spreadsheet) must not blow a whole row group past
# the embedder window on its own.
_MAX_CELL_CHARS = 300


def is_tabular(mime: str) -> bool:
    return mime in TABLE_MIMES


# --- serialisation (written by extractors.extract_text_from_xlsx) -----------

def _drop_empty_rows(rows: list[list[str]]) -> list[list[str]]:
    return [r for r in rows if any((c or "").strip() for c in r)]


def _trim_empty_columns(rows: list[list[str]]) -> list[list[str]]:
    """Drop trailing columns that are empty in EVERY row.

    Only trailing ones: an interior blank column can be meaningful (a spacer
    between a budget's actuals and its variance), and dropping it would
    misalign the header against its values.
    """
    width = 0
    for r in rows:
        for i, c in enumerate(r):
            if (c or "").strip():
                width = max(width, i + 1)
    return [r[:width] for r in rows]


def serialise_sheets(sheets, *, char_budget: int) -> str:
    """Render `[(sheet_name, rows)]` to the canonical form.

    `char_budget` bounds each sheet independently, counted over NON-EMPTY
    rendered rows only. This replaces the old 200-row-per-sheet cap: row count
    is the wrong bound, because 200 rows of a bloated grid is megabytes of empty
    pipes while 200 rows of a general ledger is a fraction of the real content.
    338 live files hit the old cap — budgets, ledgers and risk assessments.
    """
    blocks: list[str] = []
    for name, raw_rows in sheets:
        rows = _trim_empty_columns(_drop_empty_rows([
            [str(c) if c is not None else "" for c in row] for row in raw_rows
        ]))
        if not rows:
            continue
        header, data = rows[0], rows[1:]
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(header)
        captured = 0
        for row in data:
            if buf.tell() >= char_budget:
                break
            writer.writerow(row)
            captured += 1
        truncated = captured < len(data)
        if truncated:
            log.warning("sheet %r truncated at %d of %d rows (char budget %d)",
                        name, captured, len(data), char_budget)
        directive = (f"{_DIRECTIVE_PREFIX}rows_total={len(data)} "
                     f"rows_captured={captured} truncated={int(truncated)}")
        blocks.append(f"{_SHEET_PREFIX}{name}\n{directive}\n{buf.getvalue().rstrip()}")
    return "\n\n".join(blocks)


# --- parsing + rendering (read by drive.normalise_drive) --------------------

def _parse_blocks(text: str):
    """Yield (sheet_name, directive_dict, csv_body) for each block.

    Bare CSV (no directive line at all) yields one block named _DEFAULT_SHEET,
    which is the Google-Sheets-export and CSV-download path.
    """
    lines = text.splitlines()
    blocks: list[tuple[str, dict, list[str]]] = []
    name, directive, body = _DEFAULT_SHEET, {}, []
    seen_header = False
    for line in lines:
        if line.startswith(_SHEET_PREFIX):
            if body:
                blocks.append((name, directive, body))
            name = line[len(_SHEET_PREFIX):].strip() or _DEFAULT_SHEET
            directive, body, seen_header = {}, [], False
            continue
        if line.startswith(_DIRECTIVE_PREFIX) and not seen_header:
            for pair in line[len(_DIRECTIVE_PREFIX):].split():
                k, _, v = pair.partition("=")
                directive[k] = v
            continue
        if line.strip():
            seen_header = True
            body.append(line)
    if body:
        blocks.append((name, directive, body))
    return blocks


def _cell(value: str) -> str:
    out = (value or "").replace("|", "\\|").replace("\n", " ").strip()
    return out[:_MAX_CELL_CHARS] + "…" if len(out) > _MAX_CELL_CHARS else out


def _md_row(row: list[str], width: int) -> str:
    cells = [_cell(c) for c in row] + [""] * (width - len(row))
    return "| " + " | ".join(cells[:width]) + " |"


def _summary(file_name: str, sheet: str, header: list[str],
             rows: list[list[str]], directive: dict) -> str:
    """One chunk describing the sheet as a whole."""
    lines = [f"### Sheet summary: {sheet} ({file_name})",
             f"Rows: {directive.get('rows_total', len(rows))} · "
             f"Columns: {len(header)}",
             f"Columns: {', '.join(h for h in header if h.strip())}"]
    totals = []
    for i, name in enumerate(header):
        values = []
        for r in rows:
            raw = (r[i] if i < len(r) else "").replace(",", "").replace("$", "").strip()
            try:
                values.append(float(raw))
            except ValueError:
                continue
        if values and len(values) >= max(1, len(rows) // 2):
            total = sum(values)
            totals.append(f"{name or f'col{i}'} {total:.2f}")
    if totals:
        lines.append("Totals: " + ", ".join(totals))
    if directive.get("truncated") == "1":
        lines.append(f"NOTE: only {directive.get('rows_captured')} of "
                     f"{directive.get('rows_total')} rows were captured.")
    return "\n".join(lines)


def render_chunks(text: str, *, file_name: str,
                  max_chars: int) -> list[tuple[str, dict]]:
    """Parse the canonical form (or bare CSV) into (chunk_text, meta) pairs.

    Every row-group chunk repeats the sheet name, its row range and the header,
    so it is independently interpretable. Content-free chunks are never emitted.
    """
    out: list[tuple[str, dict]] = []
    for sheet, directive, body in _parse_blocks(text):
        rows = [r for r in csv.reader(body) if any((c or "").strip() for c in r)]
        if not rows:
            continue
        rows = _trim_empty_columns(rows)
        header, data = rows[0], rows[1:]
        width = max(len(r) for r in rows)
        rows_total = int(directive.get("rows_total") or len(data))
        rows_captured = int(directive.get("rows_captured") or len(data))
        truncated = directive.get("truncated") == "1"
        base = {"sheet": sheet, "rows_total": rows_total,
                "rows_captured": rows_captured, "truncated": truncated}

        summary = _summary(file_name, sheet, header, data, directive)
        if has_content(summary):
            out.append((summary, {**base, "table_role": "summary"}))

        head = (f"{_SHEET_PREFIX}{sheet}",
                _md_row(header, width),
                "| " + " | ".join(["---"] * width) + " |")
        group: list[str] = []
        start = 1
        for n, row in enumerate(data, start=1):
            line = _md_row(row, width)
            head_len = sum(len(h) + 1 for h in head) + 40  # +range annotation
            if group and head_len + sum(len(g) + 1 for g in group) + len(line) > max_chars:
                out.append(_emit(head, group, base, start, start + len(group) - 1))
                start, group = n, []
            group.append(line)
        if group:
            out.append(_emit(head, group, base, start, start + len(group) - 1))
    return [(t, m) for t, m in out if has_content(t)]


def _emit(head, group, base, row_start, row_end) -> tuple[str, dict]:
    title = (f"{head[0]} — rows {row_start}–{row_end} "
             f"of {base['rows_total']}")
    text = "\n".join([title, head[1], head[2], *group])
    return text, {**base, "table_role": "rows",
                  "row_start": row_start, "row_end": row_end}
```

- [ ] **Step 4: Rewrite the XLSX extractor**

In `mcpbrain/sync/extractors.py`, delete `_rows_to_markdown` (lines 181-202 — its only caller is `extract_text_from_xlsx`; DOCX tables keep their own `" | ".join` at line 171) and replace `extract_text_from_xlsx`:

```python
def extract_text_from_xlsx(content_bytes: bytes, *, char_budget: int = 2_000_000) -> str:
    """Serialise every sheet to the canonical tabular form (see sync/tabular.py).

    Deliberately NOT markdown any more. Rendering to markdown row groups happens
    later, in normalise_drive, where the chunk boundaries are decided — so that
    each chunk can repeat the header rather than orphaning it in chunk 0 (B2).

    `char_budget` bounds each sheet, counting only non-empty rows, and replaces
    the old flat 200-rows-per-sheet cap (B1): 338 live files hit that cap,
    including several budgets and a general ledger, losing everything past row
    200 per sheet while the same files bloated the store with empty cells.
    """
    from mcpbrain.sync.tabular import serialise_sheets

    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(content_bytes), read_only=True,
                                    data_only=True)
    except Exception as exc:
        log.warning("xlsx: workbook open failed: %s", exc)
        return ""
    try:
        sheets = [(name, list(wb[name].iter_rows(values_only=True)))
                  for name in wb.sheetnames]
        return serialise_sheets(sheets, char_budget=char_budget)
    except Exception as exc:
        log.warning("xlsx: extraction failed: %s", exc)
        return ""
    finally:
        wb.close()
```

Add `import logging` and `log = logging.getLogger(__name__)` at the top of `extractors.py` if not present.

- [ ] **Step 5: Route tabular content in `normalise_drive`**

In `mcpbrain/sync/drive.py`, add `"application/csv"` and `"text/tab-separated-values"` to `_DOWNLOAD_TEXT` (line 47) — they are already advertised in `_MIME_EXTRACTION_META` but reachable by no fetcher, so `_fetch_text` returns `None` and the file is dropped with no chunk, no stub and no log line (A2):

```python
_DOWNLOAD_TEXT = {"text/plain", "text/markdown", "text/csv",
                  "application/csv", "text/tab-separated-values"}
```

Then replace the chunk-building loop at the end of `normalise_drive` (lines 159-168):

```python
    from mcpbrain.chunking import has_content
    from mcpbrain.sync import tabular

    if tabular.is_tabular(mime):
        rendered = tabular.render_chunks(
            text, file_name=base_meta["file_name"], max_chars=tabular.CHUNK_CHARS)
    else:
        rendered = [(t, {}) for t in chunk_text(text)]

    kept = [(t, extra) for t, extra in rendered if has_content(t)]
    out = []
    for i, (t, extra) in enumerate(kept):
        meta = {**base_meta, **extra, "chunk_index": i, "chunk_total": len(kept)}
        out.append(Chunk(doc_id=f"gdrive-{fid}-{i}", text=t,
                         content_hash=content_hash(t), metadata=meta))
    return out
```

`chunk_total` here is C1, landing for free at the one call site that has the count.

- [ ] **Step 6: Add the config flag**

In `mcpbrain/config.py`:

```python
def sheet_char_budget(home) -> int:
    """Per-sheet character budget for spreadsheet extraction.

    Replaces the old 200-rows-per-sheet cap. Counted over non-empty rendered
    rows only, so the pathological case that motivated a cap in the first place
    (a grid of empty cells — one live file produced 17,281 chunks of pipes) is
    already handled by dropping empty rows, and this bound only bites on
    genuinely enormous REAL content. Default 2,000,000 chars ≈ 16,000 typical
    rows, roughly 80x the old cap.
    """
    raw = read_config(home).get("sheet_char_budget", 2_000_000)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 2_000_000
    return value if value > 0 else 2_000_000
```

Wire it at the `_DOWNLOAD_BINARY` call site in `_fetch_text` (`drive.py:112`):

```python
        if mime == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
            return extract_text_from_xlsx(
                data, char_budget=config.sheet_char_budget(str(config.app_dir())))
        return _DOWNLOAD_BINARY[mime](data)
```

adding `from mcpbrain import config` to `drive.py`'s imports.

- [ ] **Step 7: Run the tests**

Run: `uv run pytest tests/test_tabular.py tests/test_extractors.py tests/test_drive_extraction.py tests/test_drive_sync.py tests/test_drive_changes.py tests/test_drive_shared.py tests/test_chunk_metadata.py tests/test_ingest_cache_lifecycle.py -q -p no:randomly`
Expected: PASS. Existing tests asserting the old markdown output of `extract_text_from_xlsx` will fail — update each one to the new canonical form and add a comment saying why the expectation changed. Do **not** loosen an assertion to make it pass.

- [ ] **Step 8: Commit**

```bash
git add mcpbrain/sync/tabular.py mcpbrain/sync/extractors.py mcpbrain/sync/drive.py \
        mcpbrain/config.py tests/test_tabular.py tests/test_extractors.py \
        tests/test_drive_extraction.py
git commit -m "feat(ingest): header-repeating row-group chunks for tabular sources

Replaces the 200-row-per-sheet cap with a character budget over non-empty rows
and stops character-splitting tables. 338 live files were clipped at row 200 —
budgets, a general ledger, risk assessments — while the same files produced
66,653 content-free chunks of empty cells. Every row group now repeats its
sheet name, row range and header, and each sheet gains a summary chunk."
```

---

### Task 3: Drive type coverage and extraction-failure visibility

**Files:**
- Create: `mcpbrain/sync/ingest_report.py`
- Modify: `mcpbrain/sync/extractors.py` (PPTX extractor; failure logging), `mcpbrain/sync/drive.py` (`_MIME_EXTRACTION_META`, `_DOWNLOAD_BINARY`, `_fetch_text`, `sync_drive`/`_cache_first_extract_one` skip reporting)
- Modify: `pyproject.toml`
- Test: `tests/test_ingest_visibility.py` (create), `tests/test_extractors.py` (extend)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `ingest_report.record_skip(store, kind: str, ref_id: str, detail: str) -> None`; `extractors.extract_text_from_pptx(content_bytes: bytes) -> str`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ingest_visibility.py`:

```python
"""Every drop in the findings register must be countable.

The register's recurring theme is not that content is dropped — some of it
should be — but that it is dropped INVISIBLY. `_fetch_text` returns None for an
unsupported type with no log line; the `processed` counters never see the file,
so the dashboard reports a clean sync while content is discarded.
"""
from mcpbrain.sync import ingest_report


class _RecordingStore:
    def __init__(self):
        self.changes: list = []

    def record_change(self, kind, ref_id="", summary=""):
        self.changes.append((kind, ref_id, summary))


def test_record_skip_is_durable_and_carries_the_reason():
    store = _RecordingStore()

    ingest_report.record_skip(store, "unsupported_mime", "file-1", "image/png")

    assert store.changes == [
        ("ingest_skip", "file-1", "unsupported_mime: image/png")
    ]


def test_record_skip_never_raises_on_a_broken_store():
    """Reporting a skip must not be able to break a sync — it is bookkeeping."""
    class _Boom:
        def record_change(self, *a, **kw):
            raise RuntimeError("db is gone")

    ingest_report.record_skip(_Boom(), "unsupported_mime", "f", "x")  # no raise


def test_an_unsupported_drive_type_is_recorded_rather_than_silently_dropped(monkeypatch):
    """A2: .pptx, .doc, .pages, images and .zip all return None from _fetch_text
    with no chunk, no stub and no log line. Verified live: 0 chunks for .pptx."""
    from mcpbrain.sync import drive

    store = _RecordingStore()
    fmeta = {"id": "f-1", "name": "Deck.key", "mimeType": "application/x-iwork-keynote-sffkey"}

    text = drive.fetch_text_reporting(object(), fmeta, store=store)

    assert text is None
    assert store.changes and store.changes[0][0] == "ingest_skip"
    assert "application/x-iwork-keynote-sffkey" in store.changes[0][2]


def test_a_supported_type_that_extracts_to_nothing_is_recorded_distinctly(monkeypatch):
    """B7: eight `except Exception: return ""` sites make a corrupt DOCX
    indistinguishable from an unsupported type. They must not share a bucket."""
    from mcpbrain.sync import drive

    store = _RecordingStore()
    fmeta = {"id": "f-2", "name": "Broken.docx",
             "mimeType": "application/vnd.openxmlformats-officedocument."
                         "wordprocessingml.document"}
    monkeypatch.setattr(drive, "_fetch_text", lambda service, meta: "")

    text = drive.fetch_text_reporting(object(), fmeta, store=store)

    assert text == ""
    kinds = [summary.split(":")[0] for _k, _r, summary in store.changes]
    assert kinds == ["extraction_empty"], (
        "a supported type that yielded no text must not be filed as "
        f"'unsupported' — got {store.changes}"
    )
```

Add to `tests/test_extractors.py`:

```python
def test_pptx_text_is_extracted():
    """A2: presentationml.presentation is advertised in _MIME_EXTRACTION_META but
    reachable by no fetcher, so every .pptx was dropped. Verified live: 0 chunks
    for .pptx against 28 for native Google Slides."""
    import io

    from pptx import Presentation

    from mcpbrain.sync.extractors import extract_text_from_pptx

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = "Q3 Ministry Review"
    buf = io.BytesIO()
    prs.save(buf)

    text = extract_text_from_pptx(buf.getvalue())

    assert "Q3 Ministry Review" in text


def test_pptx_extraction_failure_returns_empty_and_logs(caplog):
    from mcpbrain.sync.extractors import extract_text_from_pptx

    with caplog.at_level("WARNING"):
        assert extract_text_from_pptx(b"not a pptx") == ""

    assert any("pptx" in r.message for r in caplog.records), (
        "a failed extraction must leave a trace; eight sites in this module "
        "used to return '' with no log line at all (B7)"
    )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_ingest_visibility.py tests/test_extractors.py -q -p no:randomly`
Expected: `ModuleNotFoundError` for `ingest_report`, `AttributeError` for `drive.fetch_text_reporting`, `ImportError` for `pptx`.

- [ ] **Step 3: Add the dependency**

In `pyproject.toml`, after the `openpyxl` line:

```toml
  "python-pptx>=1.0",      # PPTX extraction (A2 — .pptx was silently dropped)
```

Then: `uv sync`

- [ ] **Step 4: Create `mcpbrain/sync/ingest_report.py`**

```python
"""One durable seam for 'ingestion dropped something'.

The findings register's recurring failure mode is invisibility, not loss:
`_fetch_text` returns None for an unsupported type, `normalise_gmail` returns []
for bulk mail, and eight `except Exception: return ""` sites in extractors.py
all produce the same nothing — while the `processed` counters keep incrementing
and the dashboard reports a clean sync.

`record_change` is used rather than a bespoke table because it is already
durable, already queryable, and already surfaced in the change log, so a skip
becomes auditable with no schema change. Reporting is strictly best-effort:
bookkeeping must never be able to break a sync.
"""

import logging

log = logging.getLogger(__name__)


def record_skip(store, kind: str, ref_id: str, detail: str = "") -> None:
    """Record that one item was not ingested, and why.

    `kind` is the reason class, and the classes must stay distinguishable —
    'unsupported_mime' (we never could) and 'extraction_empty' (we should have
    and did not) demand different responses, and B7 exists precisely because
    they were indistinguishable.
    """
    summary = f"{kind}: {detail}" if detail else kind
    log.info("ingest skip [%s] %s %s", kind, ref_id, detail)
    try:
        store.record_change("ingest_skip", ref_id=ref_id, summary=summary)
    except Exception as exc:  # noqa: BLE001 — bookkeeping must not break a sync
        log.debug("ingest_report: could not record skip: %s", exc)
```

- [ ] **Step 5: Add the PPTX extractor**

In `mcpbrain/sync/extractors.py`:

```python
def extract_text_from_pptx(content_bytes: bytes) -> str:
    """Extract slide text from PPTX bytes: titles, body frames and table cells.

    Slides are separated by a labelled heading so chunk_text's paragraph split
    keeps slide boundaries, and so a recalled chunk says which slide it came
    from.
    """
    try:
        from pptx import Presentation
        prs = Presentation(io.BytesIO(content_bytes))
    except Exception as exc:
        log.warning("pptx: presentation open failed: %s", exc)
        return ""
    parts: list[str] = []
    try:
        for n, slide in enumerate(prs.slides, start=1):
            lines: list[str] = []
            for shape in slide.shapes:
                if shape.has_text_frame:
                    for para in shape.text_frame.paragraphs:
                        text = "".join(run.text for run in para.runs).strip()
                        if text:
                            lines.append(text)
                if getattr(shape, "has_table", False):
                    for row in shape.table.rows:
                        cells = [c.text.strip() for c in row.cells if c.text.strip()]
                        if cells:
                            lines.append(" | ".join(cells))
            if lines:
                parts.append(f"Slide {n}\n" + "\n".join(lines))
    except Exception as exc:
        log.warning("pptx: extraction failed after %d slides: %s", len(parts), exc)
    return "\n\n".join(parts)
```

Then add logging to the other silent handlers. For each of `extract_text_from_pdf` (`:65`, `:83`), `extract_text_from_docx` (`:173`), `_ocr_page` (`:100`, `:111`) and `is_scanned_pdf` (`:40`, `:47`), change `except Exception:` to `except Exception as exc:` and add a `log.warning(...)`/`log.debug(...)` naming the extractor before the `return ""`. Keep the return values exactly as they are — this step changes visibility, not behaviour.

- [ ] **Step 6: Wire the reporting fetch in `drive.py`**

Add `extract_text_from_pptx` to `_DOWNLOAD_BINARY`:

```python
_DOWNLOAD_BINARY = {
    "application/pdf": extract_text_from_pdf,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": extract_text_from_docx,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": extract_text_from_xlsx,
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": extract_text_from_pptx,
}
```

Add these entries to `_MIME_EXTRACTION_META` so the new text types are labelled rather than falling back to the `("text", "prose", 1.0)` default:

```python
    "application/rtf": ("text", "prose", 0.9),
    "application/json": ("text", "prose", 1.0),
    "text/html": ("text", "prose", 0.9),
```

and add those three to `_DOWNLOAD_TEXT`.

Then add the reporting wrapper immediately after `_fetch_text`:

```python
def fetch_text_reporting(service, file_meta: dict, *, store=None) -> str | None:
    """`_fetch_text`, but a drop leaves a durable trace.

    Three outcomes, and they must stay distinguishable (B7 exists because they
    were not): text (ingest it), `""` — a SUPPORTED type that extracted to
    nothing, i.e. a corrupt or image-only file worth investigating — and `None`,
    a type we never claimed to handle.

    Types deliberately still unsupported, and now recorded rather than silently
    skipped: legacy .doc/.ppt/.xls, Apple .pages/.numbers/.keynote, .zip, .eml,
    and every image format. Each needs either a new dependency or a different
    pipeline; what this plan fixes is that a sync no longer reports success
    while discarding them.
    """
    text = _fetch_text(service, file_meta)
    if store is None:
        return text
    fid = file_meta.get("id", "")
    mime = file_meta.get("mimeType", "")
    name = file_meta.get("name", "")
    if text is None:
        ingest_report.record_skip(store, "unsupported_mime", fid, f"{mime} ({name})")
    elif not text.strip():
        ingest_report.record_skip(store, "extraction_empty", fid, f"{mime} ({name})")
    return text
```

with `from mcpbrain.sync import ingest_report` at the top of `drive.py`.

Then replace the three `_fetch_text(service, fmeta)` call sites — in `_cache_first_extract_one` (`drive.py:~306`), `sync_drive`, and `backfill_drive`/`sync_shared_drive` — with `fetch_text_reporting(service, fmeta, store=store)`. Each of those functions already has `store` in scope. Do **not** change the surrounding control flow: `if not text: return False, None` still means "not processed", exactly as before.

- [ ] **Step 7: Run the tests**

Run: `uv run pytest tests/test_ingest_visibility.py tests/test_extractors.py tests/test_drive_sync.py tests/test_drive_changes.py tests/test_drive_shared.py tests/test_drive_extraction.py -q -p no:randomly`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml uv.lock mcpbrain/sync/ingest_report.py mcpbrain/sync/extractors.py \
        mcpbrain/sync/drive.py tests/test_ingest_visibility.py tests/test_extractors.py
git commit -m "feat(ingest): extract .pptx, cover advertised text types, record every skip

_MIME_EXTRACTION_META advertised .pptx, application/csv and TSV that no fetcher
handled, so _fetch_text returned None and the file vanished with no chunk, no
stub and no log line — 0 .pptx chunks live. Unsupported types and
extracted-to-nothing files are now recorded distinctly via record_change."
```

---

### Task 4: Email body coverage

**Files:**
- Modify: `mcpbrain/sync/normalise.py:84-92` (`strip_reply_chains`), `:113-124` (`extract_body_with_signature`), `:131-146` (`_is_bulk_or_auto`), `:153-182` (`normalise_gmail`)
- Modify: `mcpbrain/sync/gmail.py` (skip reporting in both `sync_gmail` and `backfill_gmail`)
- Modify: `mcpbrain/config.py`
- Test: `tests/test_normalise.py` (extend), `tests/test_gmail_sync.py` (extend)

**Interfaces:**
- Consumes: `ingest_report.record_skip` (Task 3).
- Produces: `normalise_gmail(raw: dict, *, report: dict | None = None) -> list[Chunk]` — `report`, when passed, is mutated in place to `{reason: count}`; `config.gmail_ingest_bulk(home) -> bool`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_normalise.py`:

```python
def test_a_reply_written_below_the_quote_survives():
    """A3: strip_reply_chains kept only text[:earliest], so a bottom-posted
    reply was thrown away along with the quote it sat under."""
    from mcpbrain.sync.normalise import strip_reply_chains

    text = (
        "On Mon, 2 Jun 2026 at 09:14, Sam <sam@example.com> wrote:\n"
        "> Can you confirm the Hall B booking for Sunday?\n"
        "> Sam\n"
        "\n"
        "Yes — Hall B is confirmed for Sunday the 8th, 9am to 1pm. "
        "I have put Priya down as the contact on the day.\n"
    )

    out = strip_reply_chains(text)

    assert "Hall B is confirmed" in out, "the bottom-posted reply was discarded"
    assert "Can you confirm" not in out, "the quote itself must still be stripped"


def test_a_short_sign_off_below_a_quote_is_not_treated_as_a_reply():
    """Err toward dropping: 'Sent from my iPhone' under a quote is not content,
    and rescuing it would re-introduce boilerplate at the bottom of every reply."""
    from mcpbrain.sync.normalise import strip_reply_chains

    text = ("Thanks!\n"
            "On Mon, 2 Jun 2026 at 09:14, Sam <sam@example.com> wrote:\n"
            "> long quoted thing\n"
            "\nSent from my iPhone\n")

    out = strip_reply_chains(text)

    assert out.strip() == "Thanks!"


def test_html_mail_does_not_get_the_bottom_post_rescue():
    """The rescue is only sound where '>' quoting was stripped first. In HTML
    mail the quote is markup, so a tail-rescue would re-ingest the entire quoted
    history as if it were new prose."""
    from mcpbrain.sync.normalise import extract_body_with_signature

    html = ("<p>Short answer: yes.</p>"
            "<div>On Mon, 2 Jun 2026 at 09:14, Sam wrote:</div>"
            "<blockquote>The whole previous thread, at length, "
            "repeated verbatim for many lines.</blockquote>")
    payload = _html_payload(html)  # existing helper in this file

    body, _sig = extract_body_with_signature(payload)

    assert "Short answer: yes." in body
    assert "repeated verbatim" not in body


def test_bulk_mail_drop_is_reported():
    """A4: _is_bulk_or_auto returns [] for anything with List-Id /
    List-Unsubscribe / Precedence: bulk — most vendor and ministry-platform mail
    — and sync_gmail counted it as processed anyway, so the drop was invisible."""
    from mcpbrain.sync.normalise import normalise_gmail

    raw = _message(headers=[("Subject", "Weekly digest"),
                            ("List-Unsubscribe", "<mailto:x@y.z>")],
                   body="Some newsletter body text.")
    report: dict = {}

    chunks = normalise_gmail(raw, report=report)

    assert chunks == []
    assert report == {"bulk": 1}


def test_bulk_mail_is_ingested_cold_when_the_flag_is_on(tmp_path, monkeypatch):
    """The complete fix is not 'drop it louder'. The salience gate (0.7.65)
    already exists to cold-mark promotional email — embedded and searchable, not
    graph-extracted — so the coherent behaviour is to ingest bulk mail as cold
    rather than discard it at the door. Defaults OFF only because it grows an
    11.9 GB store by an as-yet-unmeasured amount; the counter above is how that
    amount becomes knowable."""
    from mcpbrain.sync.normalise import normalise_gmail

    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text('{"gmail_ingest_bulk": true}')
    raw = _message(headers=[("Subject", "Weekly digest"),
                            ("List-Unsubscribe", "<mailto:x@y.z>")],
                   body="Some newsletter body text worth keeping.")

    chunks = normalise_gmail(raw)

    assert chunks, "with the flag on, bulk mail must be ingested, not dropped"
    assert chunks[0].metadata["bulk"] is True, (
        "and marked, so the salience gate can cold-mark it rather than "
        "graph-extracting a newsletter"
    )


def test_recipient_lists_are_not_clipped_at_300_chars():
    """C6: to[:300]/cc[:300] loses most recipients of an all-staff email."""
    from mcpbrain.sync.normalise import normalise_gmail

    recipients = ", ".join(f"person{i}@centrepoint.church" for i in range(60))
    raw = _message(headers=[("Subject", "All staff"), ("To", recipients)],
                   body="Team update.")

    meta = normalise_gmail(raw)[0].metadata

    assert meta["to"].count("@") >= 50
    assert meta["to_count"] == 60
```

If `_message` / `_html_payload` helpers do not already exist in `tests/test_normalise.py`, write them at the top of the file:

```python
import base64


def _b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode()


def _message(*, headers, body, mime="text/plain", msg_id="m1", thread_id="t1"):
    return {"id": msg_id, "threadId": thread_id,
            "labelIds": ["INBOX"],
            "payload": {"mimeType": mime,
                        "headers": [{"name": n, "value": v} for n, v in headers],
                        "body": {"data": _b64(body)}}}


def _html_payload(html):
    return {"mimeType": "text/html", "headers": [],
            "body": {"data": _b64(html)}}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_normalise.py -q -p no:randomly`
Expected: the bottom-post test fails (reply discarded), the report test fails with `TypeError: normalise_gmail() got an unexpected keyword argument 'report'`, the recipient test fails on `to_count`.

- [ ] **Step 3: Implement the reply rescue**

In `mcpbrain/sync/normalise.py`:

```python
# Lines that belong to a quote's attribution block rather than to a reply
# written under it. Matched against the TAIL only (everything after the reply
# marker), where a genuine bottom-post is the only thing that should survive.
_QUOTE_HEADER_LINE = re.compile(
    r'^\s*(from|sent|to|cc|bcc|subject|date|on|reply-to)\b.*$', re.IGNORECASE)

# A bottom-post shorter than this is boilerplate ("Sent from my iPhone",
# "[Quoted text hidden]"), not content. Err toward dropping: a false rescue
# re-introduces the same noise on every reply in the corpus, while a false drop
# costs one short line.
_MIN_BOTTOM_POST_CHARS = 40


def _bottom_posted_reply(tail: str) -> str:
    """Prose written BELOW a quoted chain.

    Only sound after '>' quoting has been stripped: what remains in the tail is
    then the quote's attribution lines plus, if the sender bottom-posted, their
    actual message. Callers must not apply this to HTML-derived text, where the
    quote is markup rather than '>' prefixes and the whole quoted history would
    survive as if it were new prose (see extract_body_with_signature).
    """
    kept = [ln for ln in tail.splitlines()
            if ln.strip() and not _QUOTE_HEADER_LINE.match(ln)]
    joined = "\n".join(kept).strip()
    return joined if len(joined) >= _MIN_BOTTOM_POST_CHARS else ""


def strip_reply_chains(text: str, *, rescue_bottom_post: bool = True) -> str:
    """Remove quoted history, keeping BOTH the text above the quote and any
    reply written below it (A3).

    The old implementation returned `text[:earliest]`, which is correct for
    top-posting — the overwhelmingly common case — and silently discarded every
    bottom-posted reply along with the quote it sat under.
    """
    text = re.sub(r'(?m)^>.*$', '', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    earliest = len(text)
    for pattern in _REPLY_CHAIN_PATTERNS:
        m = pattern.search(text)
        if m and m.start() < earliest:
            earliest = m.start()
    head = text[:earliest].strip()
    if not rescue_bottom_post or earliest == len(text):
        return head
    tail = _bottom_posted_reply(text[earliest:])
    return f"{head}\n\n{tail}".strip() if tail else head
```

and in `extract_body_with_signature`, pass the flag:

```python
def extract_body_with_signature(payload: dict) -> tuple[str, str]:
    """Return (stripped_body, signature_block). Plain text first, HTML fallback.
    Runs reply-chain stripping before signature extraction.

    The bottom-post rescue is enabled for the plain-text branch only: it relies
    on '>' quoting having been stripped first, which is meaningless for HTML.
    """
    text = _find_part_text(payload, "text/plain")
    if text and len(text.strip()) > 10:
        text = strip_reply_chains(text)
        return extract_signature_block(text)
    html = _find_part_text(payload, "text/html")
    if html:
        text = strip_reply_chains(strip_html(html), rescue_bottom_post=False)
        return extract_signature_block(text)
    return "", ""
```

- [ ] **Step 4: Implement the report and the bulk flag**

Replace `normalise_gmail`:

```python
def _note(report: dict | None, reason: str) -> None:
    if report is not None:
        report[reason] = report.get(reason, 0) + 1


def normalise_gmail(raw: dict, *, report: dict | None = None) -> list[Chunk]:
    """Raw Gmail message (messages.get format=full) -> list[Chunk].
    doc_id = gmail-<id>-body-<i>. Empty body -> [].

    `report`, when passed, is mutated in place to {reason: count} for every
    message that produced no chunks. Without it a drop is invisible: sync_gmail
    counts a bulk-filtered message as processed either way (A4).
    """
    msg_id = raw["id"]
    payload = raw.get("payload", {})
    headers = payload.get("headers", [])
    subject = get_header(headers, "subject")
    bulk = _is_bulk_or_auto(headers, subject)
    if bulk and not config.gmail_ingest_bulk(str(config.app_dir())):
        _note(report, "bulk")
        return []
    body, signature_block = extract_body_with_signature(payload)
    if not body:
        _note(report, "empty_body")
        return []
    to = get_header(headers, "to")
    cc = get_header(headers, "cc")
    base_metadata = {
        "source_type": "gmail",
        "message_id": msg_id,
        "thread_id": raw.get("threadId", ""),
        "subject": subject[:200],
        "sender": get_header(headers, "from")[:200],
        # C6: was to[:300]/cc[:300], which loses most recipients of an all-staff
        # email. The counts are kept separately so a truncation that does happen
        # is still visible rather than inferred from a clipped string.
        "to": to[:2000],
        "cc": cc[:2000],
        "to_count": to.count("@"),
        "cc_count": cc.count("@"),
        "date": get_header(headers, "date")[:80],
        "labels": ",".join(raw.get("labelIds", []))[:200],
        "signature_block": signature_block[:500],
    }
    if bulk:
        # Ingested because gmail_ingest_bulk is on. Marked so the salience gate
        # cold-marks it (embedded + searchable, never graph-extracted) instead
        # of spending Haiku on a newsletter.
        base_metadata["bulk"] = True
    pieces = [c for c in chunk_text(body) if has_content(c)]
    out = []
    for i, chunk in enumerate(pieces):
        meta = {**base_metadata, "content_type": "email_body",
                "chunk_index": i, "chunk_total": len(pieces)}
        out.append(Chunk(doc_id=f"gmail-{msg_id}-body-{i}", text=chunk,
                         content_hash=content_hash(chunk), metadata=meta))
    return out
```

Add to the imports at the top of `normalise.py`: `from mcpbrain import config` and `from mcpbrain.chunking import chunk_text, content_hash, has_content`.

In `mcpbrain/config.py`:

```python
def gmail_ingest_bulk(home) -> bool:
    """Whether list/bulk/auto-submitted mail is INGESTED (cold) rather than
    dropped at the door.

    `_is_bulk_or_auto` drops anything carrying List-Id, List-Unsubscribe or
    Precedence: bulk — which includes most vendor and ministry-platform mail,
    some of it genuinely wanted. The salience gate already exists to cold-mark
    promotional email (embedded and searchable, never graph-extracted), so
    ingesting-as-cold is the coherent behaviour and dropping is the crude one.

    Default: FALSE. Not because dropping is right, but because flipping it grows
    an already-11.9 GB store by an unmeasured amount. The per-sync 'bulk' counter
    ships live in BOTH modes, so the volume can be measured before the flip.
    Fleet-flippable via org-config.json {"flags": {"gmail_ingest_bulk": true}}.
    """
    return bool(fleet_flag(home, "gmail_ingest_bulk", False))
```

- [ ] **Step 5: Surface the report in `gmail.py`**

In `sync_gmail`, replace the normalise call and add a post-loop summary:

```python
    skips: dict = {}
    ...
        with bulk_section():
            for chunk in normalise_gmail(raw, report=skips):
                store.upsert_chunk(chunk.doc_id, chunk.text, chunk.content_hash, chunk.metadata)
```

and immediately before `return messages_processed`:

```python
    for reason, count in sorted(skips.items()):
        ingest_report.record_skip(store, f"gmail_{reason}", source, str(count))
```

Do the same in `backfill_gmail`. Add `from mcpbrain.sync import ingest_report` to `gmail.py`.

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_normalise.py tests/test_gmail_sync.py tests/test_ingest_visibility.py tests/test_chunk_metadata.py tests/test_salience_gate.py -q -p no:randomly`
Expected: PASS. Existing `test_normalise.py` tests asserting `to`/`cc` are clipped to 300 chars must be updated with a comment explaining the C6 change.

- [ ] **Step 7: Commit**

```bash
git add mcpbrain/sync/normalise.py mcpbrain/sync/gmail.py mcpbrain/config.py \
        tests/test_normalise.py tests/test_gmail_sync.py
git commit -m "fix(gmail): keep bottom-posted replies, report bulk drops, stop clipping recipients

strip_reply_chains returned text[:earliest], discarding any reply written below
the quote. Bulk mail was dropped while still counted as processed. to/cc were
clipped at 300 chars, losing most recipients of an all-staff email."
```

---

### Task 5: Email attachments

The largest single content gap in the register: a PDF emailed to Josh is invisible to the brain, while the byte-identical file in Drive is extracted normally.

**Files:**
- Create: `mcpbrain/sync/attachments.py`
- Modify: `mcpbrain/sync/gmail.py` (both `sync_gmail` and `backfill_gmail`)
- Modify: `mcpbrain/config.py`
- Test: `tests/test_attachments.py` (create), `tests/test_gmail_sync.py` (extend)

**Interfaces:**
- Consumes: `chunking.has_content` / `chunk_text` (Task 1); `extractors.extract_text_from_{pdf,docx,xlsx,pptx}` (Tasks 2, 3); `ingest_report.record_skip` (Task 3); `tabular.render_chunks` (Task 2).
- Produces:
  - `attachments.iter_attachment_parts(payload: dict) -> list[dict]` — each `{"filename", "mime", "attachment_id", "size"}`
  - `attachments.normalise_attachment(raw_message: dict, part: dict, data: bytes) -> list[Chunk]`
  - `attachments.fetch_and_normalise(service, raw_message: dict, *, store=None) -> list[Chunk]`
  - `config.gmail_attachments(home) -> bool`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_attachments.py`:

```python
"""A PDF emailed to the user is invisible to the brain, while the byte-identical
file in Drive is extracted normally. `_find_part_text` returns only text/plain
and text/html parts, and there is no attachment-handling code anywhere in the
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
            _part("Budget.pdf", "application/pdf"),
        ]},
    ]}

    found = attachments.iter_attachment_parts(payload)

    assert [p["filename"] for p in found] == ["Budget.pdf"]


def test_a_body_part_is_not_an_attachment():
    """A part with no filename is the message body, already handled by
    _find_part_text; treating it as an attachment would double-ingest it."""
    payload = {"parts": [{"mimeType": "text/plain", "filename": "",
                          "body": {"data": "abc"}}]}

    assert attachments.iter_attachment_parts(payload) == []


def test_an_inline_image_is_not_ingested():
    payload = {"parts": [_part("signature-logo.png", "image/png")]}

    assert attachments.iter_attachment_parts(payload) == []


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
        "the parent message's date must be propagated or the chunk is "
        "date-blind and recency_decay returns its neutral 0.5 fallback"
    )


def test_a_spreadsheet_attachment_uses_the_row_group_chunker(monkeypatch):
    """An emailed budget must not be character-split any more than a Drive one."""
    from mcpbrain.sync import tabular

    serialised = tabular.serialise_sheets(
        [("Budget", [["Item", "Amount"], ["Rent", "500"], ["Power", "120"]])],
        char_budget=100_000)
    monkeypatch.setattr(attachments, "_EXTRACTORS",
                        {"application/vnd.openxmlformats-officedocument."
                         "spreadsheetml.sheet": lambda b: serialised})
    raw = _msg([_part("Budget.xlsx",
                      "application/vnd.openxmlformats-officedocument."
                      "spreadsheetml.sheet")])
    part = attachments.iter_attachment_parts(raw["payload"])[0]

    chunks = attachments.normalise_attachment(raw, part, b"fake")
    rowtext = next(c.text for c in chunks
                   if c.metadata.get("table_role") == "rows")

    assert "| Item | Amount |" in rowtext


def test_an_oversized_attachment_is_skipped_and_recorded():
    raw = _msg([_part("Huge.pdf", "application/pdf", size=80 * 1024 * 1024)])

    found = attachments.iter_attachment_parts(raw["payload"])

    assert found == [], "an 80 MB attachment must not be fetched"


def test_only_the_first_n_attachments_of_one_message_are_taken():
    parts = [_part(f"f{i}.pdf", "application/pdf", attachment_id=f"a{i}")
             for i in range(30)]

    found = attachments.iter_attachment_parts({"parts": parts})

    assert len(found) == attachments._MAX_ATTACHMENTS_PER_MESSAGE


def test_fetch_and_normalise_reports_an_unsupported_attachment_type():
    class _Store:
        def __init__(self):
            self.changes = []

        def record_change(self, kind, ref_id="", summary=""):
            self.changes.append((kind, ref_id, summary))

    store = _Store()
    raw = _msg([_part("Archive.zip", "application/zip")])

    chunks = attachments.fetch_and_normalise(object(), raw, store=store)

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

    raw = _msg([_part("Notes.pdf", "application/pdf")])

    chunks = attachments.fetch_and_normalise(_Service(), raw)

    assert chunks and "extracted words here" in chunks[0].text
```

Add to `tests/test_gmail_sync.py` (this file already provides `Store`, `sync_gmail`, `plain_msg`, `_make_page` and `FakeService`):

```python
def test_sync_gmail_ingests_attachments(tmp_path, monkeypatch):
    """Wiring test: the attachment path must be reached from the real sync loop,
    not merely be callable in isolation. `normalise_gmail` has never called it,
    which is why A1 went unnoticed."""
    from mcpbrain.sync import attachments
    from mcpbrain.sync.normalise import Chunk

    store = Store(tmp_path / "test.sqlite3", dim=4)
    store.init()
    store.set_cursor("gmail", "1000")

    seen: list = []

    def _fake_fetch(service, raw, store=None):
        seen.append(raw["id"])
        return [Chunk(doc_id=f"gmail-{raw['id']}-att-0-0",
                      text="Total due: 4,200.00", content_hash="h1",
                      metadata={"source_type": "gmail",
                                "content_type": "email_attachment",
                                "message_id": raw["id"]})]

    monkeypatch.setattr(attachments, "fetch_and_normalise", _fake_fetch)

    msg = plain_msg("m1", "Invoice", "alice@example.com", "See attached.")
    svc = FakeService(profile_hid="1000",
                      pages=[_make_page(["m1"], history_id="1005")],
                      messages={"m1": msg})

    sync_gmail(svc, store)

    assert seen == ["m1"], "sync_gmail never reached the attachment path"
    assert store.get_chunk("gmail-m1-att-0-0") is not None, (
        "the attachment's chunks were fetched but never upserted"
    )


def test_sync_gmail_skips_attachments_when_the_flag_is_off(tmp_path, monkeypatch):
    from mcpbrain.sync import attachments

    store = Store(tmp_path / "test.sqlite3", dim=4)
    store.init()
    store.set_cursor("gmail", "1000")
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text('{"gmail_attachments": false}')

    called: list = []
    monkeypatch.setattr(attachments, "fetch_and_normalise",
                        lambda *a, **kw: called.append(1) or [])

    svc = FakeService(profile_hid="1000",
                      pages=[_make_page(["m1"], history_id="1005")],
                      messages={"m1": plain_msg("m1", "s", "a@b.com", "body")})
    sync_gmail(svc, store)

    assert called == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_attachments.py -q -p no:randomly`
Expected: `ModuleNotFoundError: No module named 'mcpbrain.sync.attachments'`.

- [ ] **Step 3: Create `mcpbrain/sync/attachments.py`**

```python
"""Email attachment ingestion.

`_find_part_text` returns only text/plain and text/html body parts, and before
this module there was no attachment-handling code in the repo at all: a PDF
emailed to the user was invisible to the brain, while the byte-identical file in
Drive was extracted normally.

Design notes
------------
Attachments keep `source_type: "gmail"` and carry their parent's `message_id`
and `thread_id`, so they join their thread for enrichment, thread expansion and
`doc_ids_for_messages` rather than becoming orphan documents. They are
distinguished by `content_type: "email_attachment"`.

They also inherit the parent message's `date`, without which they would be
date-blind and `importance.recency_decay` would return its neutral 0.5 fallback
for every one of them — the same defect C2 documents for the enriched layer.

Fetching lives here rather than in normalise.py because normalise.py is
declared pure data transformation ("No Google API calls here"), and that
boundary is worth keeping: `normalise_attachment` takes bytes and is testable
with no service at all.
"""

import base64
import logging

from mcpbrain.chunking import chunk_text, content_hash, has_content
from mcpbrain.sync.extractors import (
    extract_text_from_docx,
    extract_text_from_pdf,
    extract_text_from_pptx,
    extract_text_from_xlsx,
)
from mcpbrain.sync.normalise import Chunk, get_header
from mcpbrain.sync import ingest_report, tabular

log = logging.getLogger(__name__)

# Gmail's own attachment ceiling is 25 MB; anything claiming more is not
# something we will get whole anyway.
_MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024

# One message with 40 attachments must not spend a whole cycle's budget. The
# skipped remainder is recorded, not silently dropped.
_MAX_ATTACHMENTS_PER_MESSAGE = 10

_EXTRACTORS = {
    "application/pdf": extract_text_from_pdf,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        extract_text_from_docx,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
        extract_text_from_xlsx,
    "application/vnd.openxmlformats-officedocument.presentationml.presentation":
        extract_text_from_pptx,
    "text/plain": lambda b: b.decode("utf-8", errors="replace"),
    "text/csv": lambda b: b.decode("utf-8", errors="replace"),
    "text/markdown": lambda b: b.decode("utf-8", errors="replace"),
}

# Never worth fetching: no text to extract, and images in particular are almost
# always signature logos rather than content.
_SKIP_PREFIXES = ("image/", "audio/", "video/")


def iter_attachment_parts(payload: dict) -> list[dict]:
    """Every attachment part in a message payload, at any nesting depth.

    A part with an empty `filename` is the message BODY (already handled by
    `_find_part_text`); treating it as an attachment would double-ingest the
    email text.
    """
    found: list[dict] = []

    def walk(part: dict) -> None:
        if len(found) >= _MAX_ATTACHMENTS_PER_MESSAGE:
            return
        filename = (part.get("filename") or "").strip()
        body = part.get("body") or {}
        attachment_id = body.get("attachmentId")
        mime = part.get("mimeType", "")
        if filename and attachment_id and not mime.startswith(_SKIP_PREFIXES):
            size = int(body.get("size") or 0)
            if size <= _MAX_ATTACHMENT_BYTES:
                # `index` is assigned HERE, not by the caller: it is part of the
                # doc_id (gmail-<mid>-att-<index>-<i>), so it must be stable for
                # a given message and must be present for every consumer —
                # including normalise_attachment called directly in tests.
                found.append({"filename": filename, "mime": mime,
                              "attachment_id": attachment_id, "size": size,
                              "index": len(found)})
            else:
                log.info("attachment %r skipped: %d bytes", filename, size)
        for child in part.get("parts", []) or []:
            walk(child)

    walk(payload)
    return found[:_MAX_ATTACHMENTS_PER_MESSAGE]


def normalise_attachment(raw_message: dict, part: dict, data: bytes) -> list[Chunk]:
    """One attachment's bytes -> Chunks. Pure: no API calls.

    doc_id: gmail-<message_id>-att-<attachment_index>-<chunk_index>. The
    attachment index comes from the part's position in
    iter_attachment_parts(), which is stable for a given message.
    """
    extractor = _EXTRACTORS.get(part["mime"])
    if extractor is None:
        return []
    try:
        text = extractor(data)
    except Exception as exc:  # noqa: BLE001 — one bad attachment must not kill a sync
        log.warning("attachment %r extraction failed: %s", part["filename"], exc)
        return []
    if not text or not text.strip():
        return []

    msg_id = raw_message["id"]
    headers = (raw_message.get("payload") or {}).get("headers", [])
    att_index = part["index"]
    base = {
        "source_type": "gmail",
        "content_type": "email_attachment",
        "message_id": msg_id,
        "thread_id": raw_message.get("threadId", ""),
        "subject": get_header(headers, "subject")[:200],
        "sender": get_header(headers, "from")[:200],
        "date": get_header(headers, "date")[:80],
        "attachment_name": part["filename"][:200],
        "attachment_mime": part["mime"][:100],
        "extraction_method": _EXTRACTION_METHOD.get(part["mime"], "text"),
    }

    if tabular.is_tabular(part["mime"]):
        rendered = tabular.render_chunks(text, file_name=part["filename"],
                                         max_chars=tabular.CHUNK_CHARS)
    else:
        rendered = [(t, {}) for t in chunk_text(text)]
    kept = [(t, extra) for t, extra in rendered if has_content(t)]

    out = []
    for i, (t, extra) in enumerate(kept):
        meta = {**base, **extra, "chunk_index": i, "chunk_total": len(kept)}
        out.append(Chunk(doc_id=f"gmail-{msg_id}-att-{att_index}-{i}", text=t,
                         content_hash=content_hash(t), metadata=meta))
    return out


_EXTRACTION_METHOD = {
    "application/pdf": "pdf_layout",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "spreadsheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "slides",
}


def fetch_and_normalise(service, raw_message: dict, *, store=None) -> list[Chunk]:
    """Fetch every attachment of one message and normalise it.

    Best-effort per attachment: a 404 or a failed extraction skips that one and
    is recorded, rather than aborting the message or the sync.
    """
    payload = raw_message.get("payload") or {}
    msg_id = raw_message["id"]
    out: list[Chunk] = []
    for part in iter_attachment_parts(payload):
        if part["mime"] not in _EXTRACTORS:
            if store is not None:
                ingest_report.record_skip(
                    store, "attachment_unsupported", msg_id,
                    f"{part['mime']} ({part['filename']})")
            continue
        try:
            resp = service.users().messages().attachments().get(
                userId="me", messageId=msg_id, id=part["attachment_id"]).execute()
            data = base64.urlsafe_b64decode(resp.get("data") or "")
        except Exception as exc:  # noqa: BLE001 — one attachment must not kill a sync
            log.warning("attachment fetch failed for %s/%s: %s",
                        msg_id, part["filename"], exc)
            if store is not None:
                ingest_report.record_skip(store, "attachment_fetch_failed",
                                          msg_id, part["filename"])
            continue
        chunks = normalise_attachment(raw_message, part, data)
        if not chunks and store is not None:
            ingest_report.record_skip(store, "attachment_empty", msg_id,
                                      f"{part['mime']} ({part['filename']})")
        out.extend(chunks)
    return out
```

- [ ] **Step 4: Wire it into `gmail.py`**

In `sync_gmail`, inside the existing `with bulk_section():` block:

```python
        with bulk_section():
            for chunk in normalise_gmail(raw, report=skips):
                store.upsert_chunk(chunk.doc_id, chunk.text, chunk.content_hash, chunk.metadata)
            if config.gmail_attachments(str(config.app_dir())):
                for chunk in attachments.fetch_and_normalise(service, raw, store=store):
                    store.upsert_chunk(chunk.doc_id, chunk.text,
                                       chunk.content_hash, chunk.metadata)
```

**Note for the implementer:** `fetch_and_normalise` performs network I/O, and the daemon-scheduling work established that network I/O must not run under `_bulk_lock` (see `_cache_first_extract_one`'s docstring in `drive.py`). Hoist the fetch out:

```python
        att_chunks = (attachments.fetch_and_normalise(service, raw, store=store)
                      if config.gmail_attachments(str(config.app_dir())) else [])
        with bulk_section():
            for chunk in normalise_gmail(raw, report=skips):
                store.upsert_chunk(chunk.doc_id, chunk.text, chunk.content_hash, chunk.metadata)
            for chunk in att_chunks:
                store.upsert_chunk(chunk.doc_id, chunk.text, chunk.content_hash, chunk.metadata)
```

Apply the same shape to `backfill_gmail`. Add `from mcpbrain import config` and `from mcpbrain.sync import attachments` to `gmail.py`.

In `mcpbrain/config.py`:

```python
def gmail_attachments(home) -> bool:
    """Whether email attachments are fetched and extracted.

    Default: TRUE. Unlike gmail_ingest_bulk this is pure content gain, not
    volume risk: an emailed PDF is content the user already believes is in their
    brain, and the byte-identical file in Drive is already extracted. It was the
    single largest content gap in the 2026-07-27 ingestion audit.
    """
    return bool(fleet_flag(home, "gmail_attachments", True))
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_attachments.py tests/test_gmail_sync.py tests/test_normalise.py tests/test_tabular.py -q -p no:randomly`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/sync/attachments.py mcpbrain/sync/gmail.py mcpbrain/config.py \
        tests/test_attachments.py tests/test_gmail_sync.py
git commit -m "feat(gmail): ingest email attachments

A PDF emailed to the user was invisible to the brain while the byte-identical
file in Drive extracted normally — there was no attachment-handling code in the
repo at all. Attachments keep source_type gmail and carry their parent's
message_id, thread_id and date, so they join their thread for enrichment and
expansion instead of becoming date-blind orphans."
```

---

### Task 6: Provenance and metadata

**Files:**
- Modify: `mcpbrain/semantic.py:23-100` (`build_semantic_doc`), `mcpbrain/graph_write.py:1516-1531` (call site)
- Modify: `mcpbrain/sync/drive.py` (`folder_path`, `_CHANGES_FIELDS`)
- Modify: `mcpbrain/sync/calendar.py` (`chunk_total`, `chunk_text` for long descriptions)
- Modify: `mcpbrain/config.py`
- Test: `tests/test_semantic.py` (extend), `tests/test_chunk_metadata.py` (extend), `tests/test_drive_sync.py` (extend), `tests/test_calendar_sync.py` (extend)

**Interfaces:**
- Consumes: `chunking.chunk_text` / `has_content` (Task 1).
- Produces: `build_semantic_doc(extraction, thread, owner=None, taxonomy=None, *, date_iso: str = "", message_id: str = "") -> tuple[str, dict]`; `drive.folder_path(service, file_meta, cache: dict) -> str`; `config.drive_folder_path(home) -> bool`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_semantic.py`:

```python
def test_the_enriched_chunk_carries_a_date():
    """C2: 21,162 gmail_enriched_v2 chunks carry no date in any form, so
    importance.recency_decay returns its neutral 0.5 fallback for all of them.
    These are the LLM-digested summaries — the highest-value chunks in the store
    — and the only significant population the recency axis cannot rank. Their
    source chunks DO carry dates; the value was simply not propagated."""
    from mcpbrain.importance import recency_decay
    from mcpbrain.semantic import build_semantic_doc

    thread = {"subject": "Hall B", "sender": "sam@example.com",
              "date": "Tue, 02 Jun 2026 16:30:01 +0800", "labels": "INBOX"}

    _text, meta = build_semantic_doc({"thread_id": "t1", "summary": "x"}, thread)

    assert meta["date"] == "Tue, 02 Jun 2026 16:30:01 +0800"
    assert recency_decay(meta) != 0.5, (
        "the enriched chunk is still date-blind to the ranker"
    )


def test_the_enriched_chunk_keeps_the_message_level_link():
    """C3: enriched chunks retained thread_id but no message_id, so a fact could
    be traced to a thread but not to the message it came from."""
    from mcpbrain.semantic import build_semantic_doc

    _text, meta = build_semantic_doc(
        {"thread_id": "t1"}, {"subject": "s"}, message_id="msg-9")

    assert meta["message_id"] == "msg-9"


def test_a_calendar_derived_enrichment_is_not_labelled_as_gmail():
    """C4: observed live — a chunk with source_type gmail_enriched_v2 whose
    thread_id was cal-e734d9f93c894a5a81e3230300748014."""
    from mcpbrain.semantic import build_semantic_doc

    _text, meta = build_semantic_doc({"thread_id": "cal-e734d9f9"}, {"subject": "s"})

    assert meta["source_type"] == "calendar_enriched_v2"


def test_an_email_derived_enrichment_keeps_its_existing_label():
    from mcpbrain.semantic import build_semantic_doc

    _text, meta = build_semantic_doc({"thread_id": "t-1234"}, {"subject": "s"})

    assert meta["source_type"] == "gmail_enriched_v2"
```

Add to `tests/test_chunk_metadata.py`:

```python
def test_every_chunk_records_how_many_chunks_its_document_has():
    """C1: 154,601 chunks carry chunk_index and ZERO carry a total. Given 'chunk
    7', nothing can tell whether the document has 8 chunks or 17,281 — so there
    is no integrity check for partial ingestion, and no consumer can detect the
    B5 orphaning."""
    from mcpbrain.sync.drive import normalise_drive

    fmeta = {"id": "f1", "name": "Doc.txt", "mimeType": "text/plain"}
    text = "\n\n".join(f"Paragraph {i} " + "word " * 300 for i in range(4))

    chunks = normalise_drive(fmeta, text)

    assert len(chunks) > 1
    for c in chunks:
        assert c.metadata["chunk_total"] == len(chunks)
```

Add to `tests/test_drive_sync.py`:

```python
def test_folder_path_is_stamped_on_drive_chunks(monkeypatch):
    """C5: embed.contextual_prefix reads metadata['folder_path'] and
    normalise_drive never set it, so every Drive contextual prefix lacked folder
    context — dead provenance in a default-ON retrieval feature."""
    from mcpbrain.sync import drive

    cache: dict = {}

    class _Service:
        def files(self):
            return self

        def get(self, fileId, fields, supportsAllDrives=None):
            return self

        def execute(self):
            return {"id": "folder-1", "name": "Finance", "parents": []}

    fmeta = {"id": "f1", "name": "Budget.xlsx", "parents": ["folder-1"]}

    assert drive.folder_path(_Service(), fmeta, cache) == "Finance"
    assert cache["folder-1"] == ("Finance", [])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_semantic.py tests/test_chunk_metadata.py tests/test_drive_sync.py -q -p no:randomly`
Expected: `KeyError: 'date'`, `KeyError: 'message_id'`, `assert 'gmail_enriched_v2' == 'calendar_enriched_v2'`, `KeyError: 'chunk_total'`, `AttributeError: folder_path`.

- [ ] **Step 3: Fix the enriched-chunk metadata**

In `mcpbrain/semantic.py`, change the signature and the metadata dict:

```python
def build_semantic_doc(extraction: dict, thread: dict, owner=None, taxonomy=None,
                       *, date_iso: str = "", message_id: str = "") -> tuple[str, dict]:
```

and replace the metadata block at the end:

```python
    thread_id = extraction.get("thread_id", "") or ""
    metadata = {
        # C4: a calendar-sourced enrichment carries a cal-* thread id and was
        # nonetheless labelled gmail_enriched_v2. Observed live on
        # cal-e734d9f93c894a5a81e3230300748014. No consumer reads these values
        # today beyond tests, so correcting the label is safe.
        "source_type": ("calendar_enriched_v2" if thread_id.startswith("cal-")
                        else "gmail_enriched_v2"),
        "thread_id": thread_id,
        "subject": subject[:200],
        "org": org,
        "content_type": content_type,
        # C2: without a date, importance.recency_decay returns its neutral 0.5
        # fallback for all 21,162 of these — the highest-value chunks in the
        # store and the only significant population the recency axis cannot
        # rank. `date` is the lead's RFC2822 header, which _parse_age_days
        # already handles; date_iso is preferred when the caller has it.
        "date": date[:80],
        # C3: thread-level provenance without message-level provenance means a
        # fact can be traced to a thread but not to the message it came from.
        "message_id": message_id[:200],
    }
    if date_iso:
        metadata["date_iso"] = date_iso[:40]
    return text, metadata
```

In `mcpbrain/graph_write.py`, at the `build_semantic_doc` call (line ~1520), pass what the caller already has in scope:

```python
        semantic_text, semantic_meta = build_semantic_doc(
            extraction, lead, owner=owner, taxonomy=taxonomy,
            date_iso=lead_date_iso or "", message_id=lead_msg_id or "")
```

- [ ] **Step 4: Add `chunk_total` at the remaining normalise sites**

`normalise_drive` and `normalise_gmail` already gained it in Tasks 2 and 4. `normalise_calendar` (`mcpbrain/sync/calendar.py`) currently ends with a single hard-coded chunk:

```python
    return [Chunk(doc_id=f"cal-{eid}", text=text, content_hash=content_hash(text), metadata=meta)]
```

Replace that one line with:

```python
    # Finding E: this emitted exactly one chunk per event with the description
    # inlined, never calling chunk_text, so a long agenda was truncated by the
    # embedder rather than split. Measured impact is small — of 1,149 live
    # calendar chunks, max length 2,977 and only 4 exceed 2,000 chars — so the
    # single-chunk case must stay byte-identical, and only those 4 take the
    # suffixed form.
    pieces = [p for p in chunk_text(text) if has_content(p)]
    if not pieces:
        return []
    if len(pieces) == 1:
        return [Chunk(doc_id=f"cal-{eid}", text=pieces[0],
                      content_hash=content_hash(pieces[0]),
                      metadata={**meta, "chunk_index": 0, "chunk_total": 1})]
    return [
        Chunk(doc_id=f"cal-{eid}-{i}", text=piece,
              content_hash=content_hash(piece),
              metadata={**meta, "chunk_index": i, "chunk_total": len(pieces)})
        for i, piece in enumerate(pieces)
    ]
```

and add `chunk_text, has_content` to the `mcpbrain.chunking` import at the top of `calendar.py`.

**Before making this edit, read `store.delete_calendar_chunks_after`.** The single-chunk doc_id must stay `cal-<eid>` exactly: that sweep and the calendar enrichment path both key on the shape, and changing it for the common case would orphan every existing calendar chunk. Then confirm its `LIKE` pattern also matches the new `cal-<eid>-<i>` form — if it is `LIKE 'cal-%'` it already does; if it is an exact match or `cal-<eid>` with no wildcard, widen it and add a test for the multi-chunk event, because otherwise those 4 events become undeletable.

- [ ] **Step 5: Implement `folder_path`**

In `mcpbrain/sync/drive.py`, extend `_CHANGES_FIELDS` to request parents:

```python
_CHANGES_FIELDS = (
    "nextPageToken,newStartPageToken,"
    "changes(fileId,removed,file(id,name,mimeType,modifiedTime,owners,"
    "md5Checksum,version,size,parents))"
)
```

and add:

```python
def folder_path(service, file_meta: dict, cache: dict) -> str:
    """The file's folder chain, e.g. 'Finance/Budgets/2026'.

    C5: embed.contextual_prefix (a default-ON retrieval feature) reads
    metadata['folder_path'] and normalise_drive never wrote it, so every Drive
    contextual prefix has been missing its folder context.

    `cache` maps folder_id -> (name, parents) and is owned by the CALLER for the
    whole sync round, so a Drive with 5,000 files in 40 folders costs 40 lookups,
    not 5,000. Any failure degrades to '' — provenance is a nice-to-have and must
    never break a sync.
    """
    names: list[str] = []
    parents = file_meta.get("parents") or []
    seen: set[str] = set()
    while parents and len(names) < 8:
        fid = parents[0]
        if fid in seen:
            break
        seen.add(fid)
        if fid not in cache:
            try:
                info = service.files().get(
                    fileId=fid, fields="id,name,parents",
                    supportsAllDrives=True).execute()
                cache[fid] = (info.get("name", ""), info.get("parents") or [])
            except Exception as exc:  # noqa: BLE001 — provenance is best-effort
                log.debug("folder_path: lookup failed for %s: %s", fid, exc)
                cache[fid] = ("", [])
        name, parents = cache[fid]
        if name:
            names.append(name)
    return "/".join(reversed(names))
```

Thread it into `normalise_drive` as a keyword-only optional so the locked signature is preserved:

```python
def normalise_drive(file_meta: dict, text: str, drive_id: str | None = None,
                    *, folder: str = "") -> list[Chunk]:
    ...
    if folder:
        base_meta["folder_path"] = folder[:300]
```

and at the three call sites, when `config.drive_folder_path(...)` is on, pass `folder=folder_path(service, fmeta, folder_cache)` with a `folder_cache: dict = {}` created once per sync call.

In `mcpbrain/config.py`:

```python
def drive_folder_path(home) -> bool:
    """Whether Drive chunks are stamped with their folder chain.

    Costs one cached files().get per unseen folder per sync round (a Drive with
    5,000 files in 40 folders costs 40 calls, not 5,000). Default TRUE: without
    it embed.contextual_prefix's folder slot is permanently empty, which is dead
    provenance in a default-ON retrieval feature.
    """
    return bool(read_config(home).get("drive_folder_path", True))
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_semantic.py tests/test_chunk_metadata.py tests/test_drive_sync.py tests/test_drive_changes.py tests/test_calendar_sync.py tests/test_embed.py tests/test_graph_write.py -q -p no:randomly`
Expected: PASS. `tests/test_semantic.py:94` and `:118` currently assert `source_type == "gmail_enriched_v2"` for what may be a `cal-`-prefixed fixture — check each and update with a comment if the fixture's thread_id makes the new label correct.

- [ ] **Step 7: Commit**

```bash
git add mcpbrain/semantic.py mcpbrain/graph_write.py mcpbrain/sync/drive.py \
        mcpbrain/sync/calendar.py mcpbrain/config.py tests/
git commit -m "fix(provenance): date + message_id on enriched chunks, chunk_total, folder_path

21,162 enriched chunks — the LLM-digested summaries, the highest-value chunks in
the store — carried no date in any form, so recency_decay returned its neutral
0.5 fallback for every one. They also lost the message-level link, and
calendar-derived enrichments were labelled as gmail. No chunk recorded its
document's chunk count, and folder_path was read by contextual_prefix but never
written."
```

---

### Task 7: Retrieval-side correctness — ordering, orphans, partial documents

**Files:**
- Modify: `mcpbrain/retrieval_expand.py:37-38` (`_by_date`), `:56-59` (`expand_parent`)
- Modify: `mcpbrain/store.py:2503-2525` (`thread_chunks` docstring)
- Modify: `mcpbrain/sync/drive.py` (orphan delete after upsert)
- Modify: `mcpbrain/thread_enrich.py:98,148` (gap marker)
- Test: `tests/test_retrieval_expand.py` (extend), `tests/test_drive_sync.py` (extend), `tests/test_thread_enrich.py` (extend)

**Interfaces:**
- Consumes: `chunk_total` (Task 6), `store.doc_ids_for_file` / `store.delete_chunks` (existing).
- Produces: no new public API.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_retrieval_expand.py`:

```python
def test_thread_expansion_orders_chunks_within_a_message():
    """B4: _by_date sorts by date alone, and every chunk of one message shares a
    date, so a stable sort preserves raw SQLite scan order. Any email over 2,000
    chars was injected with its paragraphs scrambled."""
    from mcpbrain import retrieval_expand

    scan_order = [
        {"doc_id": "gmail-m1-body-2", "text": "third",
         "metadata": {"date": "2026-06-02", "message_id": "m1", "chunk_index": 2}},
        {"doc_id": "gmail-m1-body-0", "text": "first",
         "metadata": {"date": "2026-06-02", "message_id": "m1", "chunk_index": 0}},
        {"doc_id": "gmail-m1-body-1", "text": "second",
         "metadata": {"date": "2026-06-02", "message_id": "m1", "chunk_index": 1}},
    ]

    ordered = retrieval_expand._by_date(scan_order)

    assert [c["text"] for c in ordered] == ["first", "second", "third"]


def test_thread_expansion_still_orders_messages_by_date():
    from mcpbrain import retrieval_expand

    chunks = [
        {"doc_id": "b", "text": "later",
         "metadata": {"date": "2026-06-03", "message_id": "m2", "chunk_index": 0}},
        {"doc_id": "a", "text": "earlier",
         "metadata": {"date": "2026-06-01", "message_id": "m1", "chunk_index": 0}},
    ]

    assert [c["text"] for c in retrieval_expand._by_date(chunks)] == ["earlier", "later"]
```

Add to `tests/test_drive_sync.py`:

```python
def test_a_shrinking_document_drops_its_orphaned_chunks(tmp_path):
    """B5: Drive writes gdrive-<fid>-<i> for i in 0..n-1 and only ever upserts.
    Nothing deleted indices n..m left by a previous, longer version, so deleted
    paragraphs stayed searchable indefinitely and were re-fed to expansion as
    current content."""
    from mcpbrain.sync.drive import normalise_drive, upsert_file_chunks
    from mcpbrain.store import Store

    store = Store(tmp_path / "b.sqlite3", dim=4)
    fmeta = {"id": "f1", "name": "Notes.txt", "mimeType": "text/plain"}

    long_text = "\n\n".join(f"Para {i} " + "word " * 400 for i in range(5))
    upsert_file_chunks(store, normalise_drive(fmeta, long_text), file_id="f1")
    before = len(store.doc_ids_for_file("f1"))
    assert before >= 3

    short_text = "Para 0 " + "word " * 100
    upsert_file_chunks(store, normalise_drive(fmeta, short_text), file_id="f1")

    remaining = store.doc_ids_for_file("f1")
    assert len(remaining) == 1, (
        f"stale chunks survived the shrink: {sorted(remaining)}"
    )
    assert remaining == ["gdrive-f1-0"]


def test_upserting_an_unchanged_document_deletes_nothing(tmp_path):
    from mcpbrain.sync.drive import normalise_drive, upsert_file_chunks
    from mcpbrain.store import Store

    store = Store(tmp_path / "b.sqlite3", dim=4)
    fmeta = {"id": "f1", "name": "Notes.txt", "mimeType": "text/plain"}
    text = "\n\n".join(f"Para {i} " + "word " * 400 for i in range(5))

    upsert_file_chunks(store, normalise_drive(fmeta, text), file_id="f1")
    first = sorted(store.doc_ids_for_file("f1"))
    upsert_file_chunks(store, normalise_drive(fmeta, text), file_id="f1")

    assert sorted(store.doc_ids_for_file("f1")) == first
```

Add to `tests/test_thread_enrich.py`:

```python
def test_a_partially_enriched_thread_is_presented_with_a_gap_marker():
    """B8: group_unenriched_threads iterates unenriched_chunks and
    reassemble_thread joins only those. If part of a thread was already enriched
    — or cold-marked, excluded at store.py:1264 — the model received a partial
    document with no indication anything was missing."""
    from mcpbrain.thread_enrich import reassemble_thread

    chunks = [
        {"doc_id": "gmail-m1-body-0", "text": "First half.",
         "metadata": {"message_id": "m1", "chunk_index": 0, "chunk_total": 3,
                      "date": "2026-06-01", "sender": "a@b.com", "subject": "s"}},
        {"doc_id": "gmail-m1-body-2", "text": "Third part.",
         "metadata": {"message_id": "m1", "chunk_index": 2, "chunk_total": 3,
                      "date": "2026-06-01", "sender": "a@b.com", "subject": "s"}},
    ]

    messages = list(reassemble_thread(chunks))

    assert len(messages) == 1
    assert "[…]" in messages[0]["text"], (
        "a gap between chunk 0 and chunk 2 must be visible to the model; "
        "silently concatenating them presents a partial document as whole"
    )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_retrieval_expand.py tests/test_drive_sync.py tests/test_thread_enrich.py -q -p no:randomly`
Expected: ordering test fails (`['third', 'first', 'second']`), `ImportError: upsert_file_chunks`, gap-marker test fails.

- [ ] **Step 3: Fix thread ordering**

In `mcpbrain/retrieval_expand.py`:

```python
def _by_date(chunks: list[dict]) -> list[dict]:
    """Order a thread's chunks for stitching.

    Sorting by date ALONE (the previous implementation) is not an ordering at
    all within a message: every chunk of one message shares that message's date,
    so a stable sort preserved raw SQLite scan order and any email over 2,000
    chars was injected with its paragraphs scrambled (B4). message_id then
    chunk_index restores intra-message order; date still orders messages
    relative to each other.
    """
    def key(c: dict) -> tuple:
        meta = c.get("metadata") or {}
        return (meta.get("date", "") or "",
                meta.get("message_id", "") or "",
                int(meta.get("chunk_index", 0) or 0))

    return sorted(chunks, key=key)
```

In `mcpbrain/store.py`, correct the `thread_chunks` docstring — the "Order is not guaranteed — callers sort by date if needed" line is exactly the instruction that produced B4:

```python
        """Return all chunks whose metadata.thread_id matches thread_id.

        Each result is {doc_id, text, metadata} with metadata parsed from JSON.

        Row order is arbitrary (no ORDER BY). Callers MUST sort by
        (date, message_id, chunk_index) — see retrieval_expand._by_date. Sorting
        by date alone is not an ordering within a message, because every chunk of
        one message shares its date; that was B4.
        """
```

- [ ] **Step 4: Fix orphaning on shrink**

In `mcpbrain/sync/drive.py`, add a single helper and route all three upsert sites through it:

```python
def upsert_file_chunks(store, chunks: list[Chunk], *, file_id: str) -> int:
    """Upsert one Drive file's chunks and delete the ones it no longer has.

    B5: doc_ids are positional (gdrive-<fid>-<i>), and every write path only
    ever upserted. When a document shrank from m chunks to n, indices n..m-1
    survived — deleted paragraphs stayed searchable indefinitely and were re-fed
    to expansion as current content, with nothing able to detect it (no chunk
    recorded its document's total until C1).

    Returns the number of orphans deleted. `store.delete_chunks` also clears the
    matching vec_chunks and fts_chunks rows, so the stale text does not survive
    in either retrieval arm.
    """
    for c in chunks:
        store.upsert_chunk(c.doc_id, c.text, c.content_hash, c.metadata)
    written = {c.doc_id for c in chunks}
    orphans = [d for d in store.doc_ids_for_file(file_id) if d not in written]
    if orphans:
        log.info("drive: %s shrank; deleting %d orphaned chunk(s)",
                 file_id, len(orphans))
        store.delete_chunks(orphans)
    return len(orphans)
```

Then in `_cache_first_extract_one`, `sync_drive` and `backfill_drive`/`sync_shared_drive`, replace

```python
    with bulk_section():
        for c in chunks:
            store.upsert_chunk(c.doc_id, c.text, c.content_hash, c.metadata)
```

with

```python
    with bulk_section():
        upsert_file_chunks(store, chunks, file_id=fid)
```

**Do not** call this on the cache-import path: `ingest_cache.try_import` writes its own chunk set in its own transaction, and an orphan sweep there would race it.

- [ ] **Step 5: Add the gap marker**

`reassemble_thread` (`mcpbrain/thread_enrich.py:143-148`) currently sorts and joins each group like this:

```python
    for mid in order:
        parts = sorted(by_message[mid],
                       key=lambda c: (c.get("metadata") or {}).get("chunk_index", 0))
        meta = parts[0].get("metadata") or {}
        text = _CHUNK_JOIN.join(p.get("text", "") for p in parts)
```

Replace only the `text = ...` line with `text = _join_with_gaps(parts)`, leave the grouping and the rest of the loop untouched, and add above `reassemble_thread`:

```python
_GAP_MARKER = "\n\n[…]\n\n"


def _join_with_gaps(parts: list[dict]) -> str:
    """Join one message's chunks in index order, marking any missing piece.

    B8: this function only ever sees the chunks its CALLER selected, and
    group_unenriched_threads selects UNENRICHED chunks while
    store.unenriched_chunks additionally excludes cold-marked ones
    (store.py:1264). A partially-enriched or partially-cold document therefore
    reached the model as a seamless body with pieces silently absent — and the
    model has no way to know, so it extracts confidently from a fragment.

    Two independent signals, because either can occur alone: a hole in the
    middle (indices 0, 2 — chunk 1 already enriched) and a truncated tail
    (indices 0, 1 of a chunk_total of 5). The tail check needs chunk_total,
    which only exists on chunks written after this plan's C1 change; on older
    chunks it is absent and the check simply does not fire, which is the
    correct degradation.

    `parts` is already sorted by chunk_index by the caller.
    """
    out: list[str] = []
    prev = None
    for p in parts:
        idx = int((p.get("metadata") or {}).get("chunk_index", 0) or 0)
        if prev is not None:
            out.append(_GAP_MARKER if idx != prev + 1 else _CHUNK_JOIN)
        out.append(p.get("text", ""))
        prev = idx
    if parts and prev is not None:
        total = int((parts[-1].get("metadata") or {}).get("chunk_total", 0) or 0)
        if total and prev < total - 1:
            out.append(_GAP_MARKER)
    return "".join(out)
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_retrieval_expand.py tests/test_drive_sync.py tests/test_drive_shared.py tests/test_thread_enrich.py tests/test_prepare.py tests/test_ingest_cache_lifecycle.py tests/test_stale_reextract.py -q -p no:randomly`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add mcpbrain/retrieval_expand.py mcpbrain/store.py mcpbrain/sync/drive.py \
        mcpbrain/thread_enrich.py tests/
git commit -m "fix(retrieval): order thread chunks, delete orphans on shrink, mark gaps

Thread expansion sorted by date alone, and every chunk of one message shares a
date — so any email over 2,000 chars was injected with its paragraphs in raw
SQLite scan order. Drive only ever upserted, so a shrinking document left its
tail chunks searchable forever. Enrichment presented partial documents as whole."
```

---

### Task 8: Scanned PDFs and oversize chunks

**Files:**
- Modify: `mcpbrain/sync/extractors.py:30-118`
- Modify: `mcpbrain/index.py:64` (oversize counter — the one place the contextual prefix is joined to the chunk text before `embed_passages`)
- Modify: `mcpbrain/store.py`, `mcpbrain/doctor.py`
- Test: `tests/test_extractors.py` (extend), `tests/test_ingest_visibility.py` (extend)

**Interfaces:**
- Consumes: `ingest_report.record_skip` (Task 3).
- Produces: no new public API; `is_scanned_pdf` gains a caller or is deleted.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_extractors.py`:

```python
def test_a_scanned_pdf_with_no_ocr_available_is_reported_not_silently_empty(monkeypatch, caplog):
    """A5: with tesseract absent from PATH a fully-scanned PDF returns '' with no
    warning at all — no chunks, no log line, nothing to explain the absence."""
    import fitz

    from mcpbrain.sync import extractors

    monkeypatch.setattr(extractors, "_tesseract_available", lambda: False)
    doc = fitz.open()
    doc.new_page()  # one page, no text layer
    data = doc.tobytes()

    with caplog.at_level("WARNING"):
        text = extractors.extract_text_from_pdf(data)

    assert text.strip() == ""
    assert any("scanned" in r.message.lower() and "tesseract" in r.message.lower()
               for r in caplog.records), (
        f"no warning explained the empty result: {[r.message for r in caplog.records]}"
    )


def test_a_timed_out_ocr_page_is_logged(monkeypatch, caplog):
    """A5: per-page OCR failure/timeout returns '' and falls back to page_text,
    so a timed-out page yields nothing, unlogged."""
    import fitz

    from mcpbrain.sync import extractors

    monkeypatch.setattr(extractors, "_tesseract_available", lambda: True)
    monkeypatch.setattr(extractors, "_ocr_page", lambda page: "")
    doc = fitz.open()
    doc.new_page()
    data = doc.tobytes()

    with caplog.at_level("WARNING"):
        extractors.extract_text_from_pdf(data)

    assert any("ocr" in r.message.lower() for r in caplog.records)


def test_is_scanned_pdf_is_either_used_or_gone():
    """A5: is_scanned_pdf is defined but never called; the real gate is an
    inline char-count heuristic. Two heuristics that can disagree, one of them
    dead, is worse than either alone."""
    import subprocess

    out = subprocess.run(
        ["git", "grep", "-n", "is_scanned_pdf", "--", "mcpbrain/"],
        capture_output=True, text=True).stdout

    call_sites = [ln for ln in out.splitlines() if "def is_scanned_pdf" not in ln]
    assert call_sites, "is_scanned_pdf is still dead code"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_extractors.py -q -p no:randomly`
Expected: all three FAIL — no warnings are emitted and `is_scanned_pdf` has no caller.

- [ ] **Step 3: Implement**

Rewrite `extract_text_from_pdf`'s OCR section to use `is_scanned_pdf` as the gate (deleting the duplicate inline heuristic) and to log every degradation:

```python
def extract_text_from_pdf(content_bytes: bytes) -> str:
    """PDF text via pymupdf; per-page OCR fallback (tesseract CLI) for scanned pages.

    Every path that yields less than the document contains now says so (A5).
    Previously a scanned PDF with tesseract absent returned '' in silence, and a
    per-page OCR timeout (120 s) fell back to an empty page_text unlogged — so
    the file simply had no chunks and nothing recorded why.
    """
    try:
        import fitz  # pymupdf
        doc = fitz.open(stream=content_bytes, filetype="pdf")
    except Exception as exc:
        log.warning("pdf: open failed: %s", exc)
        return ""
    try:
        pages = [page.get_text() for page in doc]
        if not is_scanned_pdf(content_bytes):
            return "\n\n".join(pages)
        if not _tesseract_available():
            log.warning("pdf: looks scanned (%d pages, %d text chars) and "
                        "tesseract is unavailable — returning the text layer only",
                        len(pages), sum(len(p or "") for p in pages))
            return "\n\n".join(pages)
        out, ocr_failures = [], 0
        for i, page in enumerate(doc):
            page_text = (pages[i] if i < len(pages) else page.get_text()).strip()
            if len(page_text) >= _OCR_MIN_PAGE_CHARS:
                out.append(page_text)
                continue
            ocr = _ocr_page(page)
            if not ocr:
                ocr_failures += 1
            out.append(ocr or page_text)
        if ocr_failures:
            log.warning("pdf: OCR produced nothing for %d of %d pages "
                        "(timeout or render failure) — those pages are empty",
                        ocr_failures, len(out))
        return "\n\n".join(out)
    except Exception as exc:
        log.warning("pdf: extraction failed: %s", exc)
        return ""
    finally:
        doc.close()
```

**Behaviour note for the implementer:** `is_scanned_pdf` uses `avg chars/page < 50`; the inline heuristic being replaced used `total < max(20, 20 * pages)`, i.e. `avg < 20`. The new gate is therefore slightly more willing to attempt OCR. That is the intended direction (a page with 30 chars of text is a scan with a caption), but run `tests/test_extractors.py::test_pdf_text_layer` and any PDF fixtures before and after and record the difference in the commit message.

- [ ] **Step 4: Count oversize passages at embed time**

B3's 15,576 over-window chunks are now prevented at write time by Task 1 for everything that goes through `chunk_text` — but the enriched semantic doc (`graph_write.py:1522`) is written whole, with no chunking, and is the population most likely to exceed the window. Splitting it would break `enriched-<thread_id>` identity, which `mark_enriched`, `doc_ids_for_messages` and the stale-reextract sweep all key on, so this task does **not** split it. It makes the truncation visible instead.

`index.py:60-67` builds the passage list. It currently reads:

```python
            texts = [
                (contextual_prefix(c["metadata"]) + c["text"]) if use_prefix else c["text"]
                for c in batch
            ]
            vectors = embedder.embed_passages(texts)
```

Insert the count between those two statements:

```python
            oversize = sum(1 for t in texts if len(t) > EMBED_WINDOW_CHARS)
            if oversize:
                # The BGE window is 512 tokens ≈ 2,000 characters; anything
                # longer is silently truncated by the model and its tail is
                # unsearchable. 15,576 such chunks existed in the live store,
                # uncounted and unlogged (B3). Note this measures the PREFIXED
                # text — contextual_retrieval is default ON and its prefix eats
                # into the same window, which is part of why chunks sized right
                # at 2,000 chars still overflowed.
                log.warning("index: %d of %d passages exceed the %d-char embedder "
                            "window; their tails will not be searchable",
                            oversize, len(texts), EMBED_WINDOW_CHARS)
```

with `EMBED_WINDOW_CHARS = 2000` at module level in `index.py`, and `log` if the module does not already have one.

Add to `mcpbrain/store.py`, beside the other count helpers:

```python
    def count_chunks_longer_than(self, n: int) -> int:
        """Chunks whose stored text exceeds n characters — i.e. whose tail the
        512-token embedder silently discards (B3)."""
        with self._connect() as db:
            return db.execute("SELECT COUNT(*) FROM chunks WHERE length(text) > ?",
                              (int(n),)).fetchone()[0]
```

and a line to `doctor.py` alongside the existing store checks (match the surrounding style — read how the neighbouring checks build their line before writing this one):

```python
    oversize = store.count_chunks_longer_than(2000)
    lines.append(f"{'✅' if not oversize else '⚠️'} chunks over the embedder "
                 f"window: {oversize}")
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_extractors.py tests/test_ingest_visibility.py tests/test_embed.py tests/test_doctor.py tests/test_drive_extraction.py -q -p no:randomly`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/sync/extractors.py mcpbrain/embed.py mcpbrain/doctor.py \
        mcpbrain/store.py tests/
git commit -m "fix(pdf): explain every empty extraction; count over-window chunks

A scanned PDF with tesseract absent returned '' with no warning, and a per-page
OCR timeout fell back to an empty page unlogged — the file simply had no chunks
and nothing said why. is_scanned_pdf is now the single gate instead of dead code
beside a second inline heuristic."
```

---

## Findings index

Every finding in `2026-07-27-ingestion-defects-findings.md` and where this plan addresses it.

| Finding | Issue | Task |
|---|---|---|
| A1 | Email attachments never ingested — CRITICAL | 5 |
| A2 | .pptx / application/csv / TSV advertised but dropped; .doc, images, .zip dropped silently | 2 (CSV/TSV), 3 (.pptx + reporting) |
| A3 | Bottom-posted replies discarded | 4 |
| A4 | Bulk mail dropped but counted as processed | 4 |
| A5 | Scanned PDFs vanish without a trace; `is_scanned_pdf` dead | 8 |
| B1 | Spreadsheets keep empty rows, cap real rows at 200 — CRITICAL | 2 |
| B2 | CSV / Google Sheets split mid-row with no header | 2 |
| B3 | 15,576 chunks exceed the embedder window | 1 (prevented), 8 (counted) |
| B4 | Thread expansion emits scrambled paragraph order | 7 |
| B5 | Stale chunks orphaned when a document shrinks | 7 |
| B6 | `chunk_text` emits empty and oversize chunks | 1 |
| B7 | Eight silent `except Exception: return ""` | 3 |
| B8 | Enrichment presents a partial document as whole | 7 |
| C1 | No chunk records its document's chunk count | 2 (drive), 4 (gmail), 6 (calendar) |
| C2 | Enriched chunks are date-blind — 21,162 chunks | 6 |
| C3 | Enriched chunks lose the message-level link | 6 |
| C4 | Calendar-derived chunks mislabelled as Gmail | 6 |
| C5 | `folder_path` read but never written | 6 |
| C6 | Recipient lists truncated at 300 chars | 4 |
| C7 | 9,353 files ingested by the old extractor | **spec 3** — re-extraction, not a code fix |
| D | 96,335 redundant copies (54% of the store) | **spec 3** — purge; Task 1's `has_content` prevents the dominant cause recurring |
| E | Calendar events never chunked | 6 |

---

## Notes for the implementer

- **Task 1 first, always.** Tasks 2, 4, 5 and 6 all call `has_content` and rely on `chunk_text`'s new guarantees. Tasks 3–8 are otherwise independent of each other and can be reordered if a review sends one back.
- **The tests in this plan are the specification.** Where a step gives test code and implementation code, the test code is authoritative — if they disagree, the implementation is wrong.
- **Every task has a "verify the tests discriminate" habit even where a step does not spell it out.** Revert the fix, confirm the new test fails, restore. A test that passes against the defect it names is worse than no test, because it reads as coverage. The daemon-scheduling work found three of these.
- **Existing test expectations WILL break in Tasks 2, 4 and 6** (markdown XLSX output, `to[:300]`, `gmail_enriched_v2`). Update each one with a comment saying why the expectation changed. Never loosen an assertion to make it pass — if you cannot say why the old expectation was wrong, stop and ask.
- **Nothing in this plan rewrites existing rows.** No sweep, no migration, no re-extraction. If a task looks like it needs one to be useful, that is spec 3 and it is correct to leave the improvement latent until then. The one exception is Task 7's orphan delete, which is a write-time invariant on files being re-synced now.
- **Do not push and do not release.** There are 45 unpushed commits ahead of this work already.
- **Expect the store to grow slightly**, not shrink. This plan adds attachments and recovers clipped spreadsheet rows while preventing new content-free chunks; it does not delete the 66,653 that already exist. The net shrink comes in spec 3.
- **The gold eval is not a gate for this plan** and running it will not tell you much: nothing here re-embeds existing content, so recall@10 / MRR should be unchanged. It becomes the gate in spec 3, where re-extraction changes what is in the index. If you want a sanity check, `uv run python tests/eval/run_eval.py --gold --k 10` should still report 0.750 / 0.556.
