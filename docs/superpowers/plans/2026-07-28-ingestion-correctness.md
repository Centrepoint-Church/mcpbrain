# Ingestion Correctness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan. Tasks are grouped into WAVES — every task in a wave owns a disjoint set of files and runs in PARALLEL. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix every defect in `docs/superpowers/specs/2026-07-27-ingestion-defects-findings.md` sections A, B1–B8 and C1–C6, so that spec 3's repair backfill re-imports correct content instead of re-importing the same defects.

**Architecture:** No new subsystem. Every fix lands in the ingest path that already exists, plus three small focused modules: `sync/tabular.py` (row-group chunking for tables), `sync/attachments.py` (email attachments), `sync/ingest_report.py` (one durable seam for "we dropped something"). The single structural change is that tabular sources stop going through `chunk_text` and get their own header-repeating chunker.

**Tech Stack:** Python 3.11, SQLite (`sqlite-vec` + FTS5), `openpyxl`, `python-docx`, `pymupdf`, `python-pptx` (new), Google API Python client, pytest + pytest-xdist, ruff.

---

## Execution: three sequential gates, six tasks

Tasks are grouped by **file ownership**, not by concern, because subagents share one git working tree — two agents editing `drive.py` at once will clobber each other. Within a wave, no two tasks touch the same source or test file, so they are safe to run concurrently.

```
Gate 1   Task 1  Foundations                                    (solo — blocks everything)
           │
Gate 2   ├─ Task 2  Extraction          extractors.py, tabular.py
         └─ Task 3  Enrichment+retrieval semantic, graph_write, thread_enrich,
           │                              retrieval_expand, store
Gate 3   ├─ Task 4  Drive               drive.py, calendar.py
         ├─ Task 5  Email + attachments normalise.py, gmail.py, attachments.py,
         │                              prepare.py (should_enrich only)
         └─ Task 6  Observability       index.py, doctor.py
```

**Why these boundaries.** `config.py` is touched by four concerns and `pyproject.toml` by one, so both land entirely in Task 1 — otherwise every later wave serialises on them. `store.py` gains both a docstring correction (B4) and a count helper (B3), so both go to Task 3. Wave-3 tasks all depend on Wave-2 output: Tasks 4 and 5 consume Task 2's extractor API, Task 6 consumes Task 3's store method.

Review each task independently as it lands; only the **gate** is a barrier. Six tasks, ~7 steps each — six review gates rather than fifty.

**Test-file ownership** (no two tasks in a wave write the same test file):

| Task | Owns |
|---|---|
| 1 | `test_chunking.py`, `test_ingest_visibility.py` (create), `test_config_tuning.py` |
| 2 | `test_extractors.py`, `test_tabular.py` (create) |
| 3 | `test_semantic.py`, `test_thread_enrich.py`, `test_retrieval_expand.py` |
| 4 | `test_drive_sync.py`, `test_drive_extraction.py`, `test_chunk_metadata.py`, `test_calendar_sync.py` |
| 5 | `test_normalise.py`, `test_gmail_sync.py`, `test_salience_gate.py`, `test_attachments.py` (create) |
| 6 | `test_doctor.py`, `test_embed.py` |

---

## Global Constraints

- **Work on `main`, commit as you go.** No worktrees, no feature branches. Do **not** push and do **not** release — Josh decides both separately. 45 commits are already unpushed ahead of this work.
- **Do not run the full suite.** Josh runs `pytest tests/` himself. Scope every run to the files named in the task's test step.
- **`uv run pytest …` / `uv run ruff check …`** — bare `pytest`/`python` are not on PATH.
- **Ruff must pass on `mcpbrain/` and on every test file you touch.** `tests/` has 86 pre-existing errors; do not fix them and do not add to them.
- **Do not touch the daemon scheduling work** — `daemon.py`, `prepare.py`'s budget/`bulk_section` plumbing, `test_bulk_lock_fairness.py`. Finished and unpushed. If a task needs a `bulk_section`, follow the existing pattern exactly: `bulk_section=None` → `nullcontext`, one section per item, **never around network I/O**.
- **Nothing here rewrites existing rows.** No sweep, no migration, no re-extraction — that is spec 3. The one exception is Task 4's orphan delete, a write-time invariant on files being re-synced now.
- **`normalise_gmail` / `normalise_drive` / `normalise_calendar` are locked interfaces.** Their positional signature and `list[Chunk]` return must not change; new inputs are keyword-only optional.
- Version bumping is not part of this plan.

---

## Design decisions

Agreed with Josh in the session that produced the findings register, and not to be re-litigated:

1. **The 200-row cap is replaced by a character budget, not a bigger row cap.** Row count is the wrong bound: 200 rows of a bloated grid is megabytes of empty pipes, while 200 rows of a general ledger is a fraction of the real content.
2. **Tabular content is chunked by row group with a repeated header.** The biggest quality win here: it fixes all 338 truncated files *and* every table already in the store.
3. **Each sheet also gets one summary chunk** (dimensions, columns, numeric totals) so a broad question has something to match.
4. **Text with no alphanumeric character is never a chunk.** Generic, not spreadsheet-specific.
5. **Truncation is recorded in metadata**, not buried in the text, so `doctor` can report "N sheets clipped".
6. **Accepted risk:** a 50k-row ledger becomes ~4,000 semantically-similar row chunks that could crowd recall. CLAUDE.md records the 0.7.101 decision *not* to exclude tabular from recall, and headers make these chunks interpretable rather than noise. Spec 3's gold gate measures it; this plan re-embeds nothing, so it cannot regress the existing number.

### Nothing is deferred

Everything in the findings register is either fixed by this plan or tracked in spec 3 (**C7** legacy re-extraction, **D** duplicate purge — both are backfills that must not run before the extractor is correct). There is no third category. Four things that earlier drafts left open, and how each is now closed:

**Tables are passed as structured data, not serialised text.** A first draft had `extract_text_from_xlsx` serialise to a CSV-with-directive-lines intermediate form that `normalise_drive` re-parsed, purely to keep `_fetch_text`'s `str | None` return type. The stated justification — that the shared-drive ingest-cache artifact format depended on that contract — was **wrong**: `ingest_cache` stores `CacheChunk(idx, text, embedding)`, i.e. chunks, and never sees `_fetch_text`'s output. There are exactly three call sites. That design bought a stringly-typed round-trip, a parser state machine and a real failure mode (a cell whose text is `### Sheet:`) in exchange for nothing.

**B3 is fully closed: the enriched semantic doc is bounded at build time.** An earlier draft only *counted* over-window enriched chunks, on the grounds that splitting them would break the `enriched-<thread_id>` doc_id that `mark_enriched`, `doc_ids_for_messages` and the stale-reextract sweep key on. But that framed the wrong fix. The semantic doc is **synthesised** — `build_semantic_doc` assembles it line by line, so its length is ours to choose. Task 3 bounds the assembly (dropping the lowest-value sections first, keeping subject and summary), which fixes B3 completely with no doc_id change and no split. Task 6's counter stays as the regression detector, not as the fix.

**The bulk-mail drop is removed, not flagged.** `_is_bulk_or_auto` discards anything with `List-Id` / `List-Unsubscribe` / `Precedence: bulk` — most vendor and ministry-platform mail. An earlier draft made this a config flag defaulting OFF, i.e. a deferred decision dressed as a feature. It is not a decision worth deferring, because a second gate already does this job properly: `prepare.should_enrich()` cold-marks promotional email (embedded and searchable, never graph-extracted, fully reversible), and it has been shipping since 0.7.65 with ~40% of the corpus gated and **no recall impact**. The ingest-time drop is the same idea done worse — irreversibly, before anything can see the content. So Task 5 **deletes the drop** and moves its (stronger, header-based) signal into `should_enrich` as an additional cold trigger. One gate instead of two, no flag, nothing lost at the door, no Haiku spent on newsletters. This is a net *removal* of machinery.

**Unsupported file types are decided, not monitored.** An earlier draft recorded them and said the counts would be "the evidence for adding it" — a deferral. Each is now decided:

| Type | Decision |
|---|---|
| `.xls` (legacy Excel) | **Add** (Task 2). `xlrd>=2` is small and pure-Python, and 2.0 exists specifically to read `.xls`. These are spreadsheets — B1 established that budgets and ledgers are the highest-value tabular content, so declining a spreadsheet format would be indefensible. Routes into the same `Table` path. |
| `.eml` | **Add** (Task 2). Stdlib `email` parses it; extracted as a prose document with its headers as a preamble. Zero new dependencies. |
| `.doc`, `.ppt` (legacy OLE) | **Decline, permanently.** Needs LibreOffice or antiword — a heavyweight external binary shipped to every install, for a format Google Drive itself converts on open. |
| `.pages`, `.numbers`, `.keynote` | **Decline, permanently.** Proprietary zip containers that Drive cannot export either; the only reliable path is the embedded preview PDF, which is a lossy render, not the document. |
| `.zip` | **Decline, permanently.** Recursive container extraction is a zip-bomb surface, and Drive syncs the contents separately when they are unzipped. |
| Images | **Decline, permanently.** OCR of every logo, screenshot and signature graphic for speculative value. Scanned *documents* — where scanned content actually lives — are already OCR'd through the PDF path (A5). |

The declined types are still recorded via `ingest_report` — but as **monitoring of a settled decision**, so a sync stops reporting success while discarding them. Not as pending work.

---

## New config flags (all in Task 1)

| Flag | Default | Rationale |
|---|---|---|
| `gmail_attachments` | **ON** | Pure content gain, largest gap in the register. |
| `sheet_char_budget` | `2_000_000` | Per-sheet backstop ≈ 16k typical rows. Only bites on genuinely enormous real content once empty rows are dropped. |

That is the whole list. Two other flags appeared in earlier drafts and are gone:

- **`drive_folder_path`** — a kill switch nobody would ever flip. `folder_path` (C5) ships unconditionally. Cost is one cached `files().get` per unseen folder per sync round (40 calls for a Drive with 5,000 files in 40 folders), degrading to `""` on any error.
- **`gmail_ingest_bulk`** — see above; the behaviour is now unconditional and the flag would only have preserved the defect.

`gmail_attachments` survives on different grounds from those two: it is not hedging a decision, it is an operational kill switch on a path that makes an extra Gmail API call per attachment and could matter during a large backfill. If the API cost never bites, it can be removed later — but it is not gating a behaviour anyone is unsure about.

---

## Gate 1 — Task 1: Foundations

Everything downstream imports from here. Solo.

**Files:**
- Modify: `mcpbrain/chunking.py:154-179`, `mcpbrain/config.py`, `pyproject.toml`
- Create: `mcpbrain/sync/ingest_report.py`
- Test: `tests/test_chunking.py` (extend), `tests/test_ingest_visibility.py` (create), `tests/test_config_tuning.py` (extend)

**Interfaces produced:**
- `chunking.has_content(text: str) -> bool`
- `chunking.chunk_text(text, max_tokens=500, overlap=50) -> list[str]` — unchanged signature, new guarantees: no chunk empty, none over `max_tokens * 4` chars
- `ingest_report.record_skip(store, kind: str, ref_id: str, detail: str = "") -> None`
- `config.gmail_attachments(home) -> bool`, `config.sheet_char_budget(home) -> int`

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
    text = "before " + ("y" * 3000) + " after"

    chunks = chunk_text(text, max_tokens=500)

    assert all(len(c) <= 2000 for c in chunks)
    joined = " ".join(chunks)
    assert "before" in joined and "after" in joined


def test_has_content_rejects_punctuation_only_text():
    """B1's 66,653 content-free chunks (37% of the live store) are ~2,000-char
    strings of '| | | | |' from empty spreadsheet cells — all embedded, none
    matchable, and 65,770 of them share a single content_hash."""
    from mcpbrain.chunking import has_content

    assert has_content("Budget 2026") is True
    assert has_content("| 42 |") is True
    assert has_content("|  |  |  |") is False
    assert has_content("| --- | --- |") is False
    assert has_content("") is False
    assert has_content("   \n\t ") is False


def test_has_content_accepts_non_ascii_alphanumerics():
    """str.isalnum rather than [A-Za-z0-9] precisely so a sheet of Chinese or
    accented names is not discarded as content-free."""
    from mcpbrain.chunking import has_content

    assert has_content("| 会議 |") is True
    assert has_content("| Åsa |") is True
```

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

    assert store.changes == [("ingest_skip", "file-1", "unsupported_mime: image/png")]


def test_record_skip_never_raises_on_a_broken_store():
    """Reporting a skip must never be able to break a sync — it is bookkeeping."""
    class _Boom:
        def record_change(self, *a, **kw):
            raise RuntimeError("db is gone")

    ingest_report.record_skip(_Boom(), "unsupported_mime", "f", "x")  # no raise


def test_record_skip_tolerates_no_store():
    ingest_report.record_skip(None, "unsupported_mime", "f", "x")  # no raise
```

Add to `tests/test_config_tuning.py`:

```python
def test_sheet_char_budget_defaults_and_rejects_nonsense(tmp_path, monkeypatch):
    from mcpbrain import config

    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    assert config.sheet_char_budget(str(tmp_path)) == 2_000_000

    (tmp_path / "config.json").write_text('{"sheet_char_budget": 50000}')
    assert config.sheet_char_budget(str(tmp_path)) == 50_000

    (tmp_path / "config.json").write_text('{"sheet_char_budget": "lots"}')
    assert config.sheet_char_budget(str(tmp_path)) == 2_000_000

    (tmp_path / "config.json").write_text('{"sheet_char_budget": -1}')
    assert config.sheet_char_budget(str(tmp_path)) == 2_000_000
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_chunking.py tests/test_ingest_visibility.py tests/test_config_tuning.py -q -p no:randomly`
Expected: `ImportError: cannot import name 'has_content'`, `ModuleNotFoundError: mcpbrain.sync.ingest_report`, `AttributeError: sheet_char_budget`, plus the two `chunk_text` assertions.

- [ ] **Step 3: Fix `chunking.py`**

Replace `chunk_text` (lines 154-179) with:

```python
def has_content(text: str) -> bool:
    """True when `text` carries at least one alphanumeric character.

    The generic no-content guard. B1's empty-spreadsheet chunks are ~2,000-char
    strings of '| | | | |' — 66,653 of them, 37% of the live store, every one
    embedded and none matchable by any query.

    `str.isalnum()` per character, not a `[A-Za-z0-9]` regex: a sheet of Chinese
    or accented names is content and must not be discarded as noise.
    """
    return any(ch.isalnum() for ch in text)


def _hard_split(word: str, max_chars: int) -> list[str]:
    """Split a single whitespace-free token that is itself longer than the whole
    budget (a base64 blob, a minified line, a long URL). Without this the
    word-split path has no way to make progress and emits the token whole."""
    if len(word) <= max_chars:
        return [word]
    return [word[i:i + max_chars] for i in range(0, len(word), max_chars)]


def _split_paragraph(para: str, max_chars: int, overlap: int) -> list[str]:
    """Split one over-long paragraph on word boundaries.

    Two guarantees the previous implementation broke (B6): no emitted chunk is
    empty, and none exceeds max_chars. The old code appended `current`
    unconditionally on overflow — including on the first iteration when it was
    still "" — then seeded the next chunk with `overlap` words PLUS the oversize
    word without re-checking the budget.

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

- [ ] **Step 4: Create `mcpbrain/sync/ingest_report.py`**

```python
"""One durable seam for 'ingestion dropped something'.

The findings register's recurring failure mode is invisibility, not loss:
`_fetch_text` returns None for an unsupported type, `normalise_gmail` returns []
for bulk mail, and eight `except Exception: return ""` sites in extractors.py
all produce the same nothing — while the `processed` counters keep incrementing
and the dashboard reports a clean sync.

`record_change` is used rather than a bespoke table because it is already
durable, already queryable and already surfaced in the change log, so a skip
becomes auditable with no schema change.
"""

import logging

log = logging.getLogger(__name__)


def record_skip(store, kind: str, ref_id: str, detail: str = "") -> None:
    """Record that one item was not ingested, and why.

    `kind` is the reason class, and the classes must stay distinguishable —
    'unsupported_mime' (we never could) and 'extraction_empty' (we should have
    and did not) demand different responses, and B7 exists precisely because
    they were indistinguishable. Strictly best-effort in both directions: a
    missing store and a raising store are both fine.
    """
    summary = f"{kind}: {detail}" if detail else kind
    log.info("ingest skip [%s] %s %s", kind, ref_id, detail)
    if store is None:
        return
    try:
        store.record_change("ingest_skip", ref_id=ref_id, summary=summary)
    except Exception as exc:  # noqa: BLE001 — bookkeeping must not break a sync
        log.debug("ingest_report: could not record skip: %s", exc)
```

- [ ] **Step 5: Add the config flags and the dependency**

In `mcpbrain/config.py`:

```python
def gmail_attachments(home) -> bool:
    """Whether email attachments are fetched and extracted.

    Default TRUE. This is not hedging a decision — an emailed PDF is content
    the user already believes is in their brain, and the byte-identical file in
    Drive is already extracted. It is an operational kill switch on a path that
    makes an extra Gmail API call per attachment, which could matter during a
    large backfill. Attachments were the single largest content gap in the
    2026-07-27 ingestion audit.
    """
    return bool(fleet_flag(home, "gmail_attachments", True))


def sheet_char_budget(home) -> int:
    """Per-sheet character budget for spreadsheet extraction.

    Replaces the 200-rows-per-sheet cap. Counted over non-empty rendered rows
    only, so the pathological case that motivated a cap (a grid of empty cells —
    one live file produced 17,281 chunks of pipes) is already handled by
    dropping empty rows, and this bound only bites on genuinely enormous REAL
    content. Default 2,000,000 chars ≈ 16,000 typical rows, ~80x the old cap.
    """
    raw = read_config(home).get("sheet_char_budget", 2_000_000)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 2_000_000
    return value if value > 0 else 2_000_000
```

In `pyproject.toml`, after the `openpyxl` line:

```toml
  "python-pptx>=1.0",      # PPTX extraction (A2 — .pptx was silently dropped)
  "xlrd>=2",               # legacy .xls extraction (A2). xlrd 2.0 exists purely
                           # to read .xls — it deliberately dropped .xlsx, which
                           # openpyxl above already handles.
```

Then: `uv sync`

- [ ] **Step 6: Run the tests and verify they discriminate**

Run: `uv run pytest tests/test_chunking.py tests/test_ingest_visibility.py tests/test_config_tuning.py tests/test_normalise.py tests/test_drive_extraction.py -q -p no:randomly`
Expected: PASS. `test_word_split_chunks_overlap_and_lose_nothing` must still pass unchanged — it pins the overlap contract this refactor deliberately preserves.

Then restore the old `chunk_text` from `git show HEAD:mcpbrain/chunking.py`, re-run, confirm the two new `chunk_text` tests fail, restore your implementation. Report the result.

- [ ] **Step 7: Commit**

```bash
git add mcpbrain/chunking.py mcpbrain/config.py mcpbrain/sync/ingest_report.py \
        pyproject.toml uv.lock tests/test_chunking.py tests/test_ingest_visibility.py \
        tests/test_config_tuning.py
git commit -m "feat(ingest): chunking guarantees, no-content guard, skip reporting seam

chunk_text emitted zero-length chunks and oversize ones whenever a single token
exceeded the budget (6 zero-length and 36 sub-5-char chunks exist live).
has_content is the generic guard against the 66,653 content-free chunks that are
37% of the store. ingest_report gives the eight silent drop sites one durable,
auditable home."
```

---

## Gate 2 — Task 2: Extraction

Runs in parallel with Task 3. Owns `sync/extractors.py` and the new `sync/tabular.py`.

**Files:**
- Create: `mcpbrain/sync/tabular.py`
- Modify: `mcpbrain/sync/extractors.py` (whole module)
- Test: `tests/test_tabular.py` (create), `tests/test_extractors.py` (extend)

**Interfaces:**
- Consumes: `chunking.has_content` (Task 1).
- Produces:
  - `tabular.Table` — dataclass `(sheet: str, header: list[str], rows: list[list[str]], rows_total: int, truncated: bool)`
  - `tabular.TABLE_MIMES: frozenset[str]`, `tabular.is_tabular(mime: str) -> bool`, `tabular.CHUNK_CHARS: int`
  - `tabular.normalise_rows(rows: list[list[str]]) -> list[list[str]]`
  - `tabular.tables_from_csv(text: str, *, sheet: str = "Sheet1", char_budget: int) -> list[Table]`
  - `tabular.render_chunks(tables: list[Table], *, file_name: str, max_chars: int) -> list[tuple[str, dict]]`
  - `extractors.extract_tables_from_xlsx(content_bytes: bytes, *, char_budget: int) -> list[Table]`
  - `extractors.extract_tables_from_xls(content_bytes: bytes, *, char_budget: int) -> list[Table]`
  - `extractors.extract_text_from_pptx(content_bytes: bytes) -> str`
  - `extractors.extract_text_from_eml(content_bytes: bytes) -> str`

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
def test_xlsx_yields_structured_tables():
    """extract_text_from_xlsx is replaced by extract_tables_from_xlsx: chunk
    boundaries are decided later, by the renderer, so each chunk can repeat the
    header instead of orphaning it in chunk 0 (B2)."""
    import io

    import openpyxl

    from mcpbrain.sync.extractors import extract_tables_from_xlsx

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Budget"
    ws.append(["Item", "Amount"])
    ws.append(["Rent", 500])
    ws.append([None, None])          # empty row — must be dropped
    ws.append(["Power", 120])
    buf = io.BytesIO()
    wb.save(buf)

    tables = extract_tables_from_xlsx(buf.getvalue(), char_budget=1_000_000)

    assert len(tables) == 1
    assert tables[0].sheet == "Budget"
    assert tables[0].header == ["Item", "Amount"]
    assert tables[0].rows == [["Rent", "500"], ["Power", "120"]]
    assert tables[0].truncated is False


def test_xlsx_keeps_rows_past_the_old_200_row_cap():
    """338 live files hit the old cap — budgets, a general ledger, risk
    assessments — losing every row past 200 per sheet."""
    import io

    import openpyxl

    from mcpbrain.sync.extractors import extract_tables_from_xlsx

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Item", "Amount"])
    for i in range(400):
        ws.append([f"Item {i}", i])
    buf = io.BytesIO()
    wb.save(buf)

    tables = extract_tables_from_xlsx(buf.getvalue(), char_budget=1_000_000)

    assert len(tables[0].rows) == 400
    assert tables[0].truncated is False


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

    assert "Q3 Ministry Review" in extract_text_from_pptx(buf.getvalue())


def test_legacy_xls_yields_the_same_table_shape_as_xlsx():
    """A2: .xls was dropped entirely. Declining a SPREADSHEET format would be
    indefensible after B1 established that budgets and ledgers are the
    highest-value tabular content, and xlrd 2.0 exists purely to read .xls."""
    import io

    import xlwt  # dev-only helper; if unavailable, build the fixture with xlrd's
                 # own test assets or check in a small .xls under tests/fixtures/

    from mcpbrain.sync.extractors import extract_tables_from_xls

    wb = xlwt.Workbook()
    ws = wb.add_sheet("Budget")
    for c, v in enumerate(["Item", "Amount"]):
        ws.write(0, c, v)
    for c, v in enumerate(["Rent", "500"]):
        ws.write(1, c, v)
    buf = io.BytesIO()
    wb.save(buf)

    tables = extract_tables_from_xls(buf.getvalue(), char_budget=1_000_000)

    assert len(tables) == 1
    assert tables[0].sheet == "Budget"
    assert tables[0].header == ["Item", "Amount"]
    assert tables[0].rows == [["Rent", "500"]]


def test_eml_is_extracted_as_prose_with_its_headers():
    """A2: .eml files in Drive were dropped. Stdlib `email` parses them — zero
    new dependencies — and they become prose documents, NOT synthetic Gmail
    threads (that would be scope creep into the sync layer's identity model)."""
    from mcpbrain.sync.extractors import extract_text_from_eml

    raw = (b"From: sam@example.com\r\n"
           b"To: josh@centrepoint.church\r\n"
           b"Subject: Hall B booking\r\n"
           b"Date: Tue, 02 Jun 2026 16:30:01 +0800\r\n"
           b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
           b"Confirmed for Sunday the 8th.\r\n")

    text = extract_text_from_eml(raw)

    assert "Subject: Hall B booking" in text
    assert "sam@example.com" in text
    assert "Confirmed for Sunday the 8th." in text


def test_a_multipart_eml_prefers_the_plain_text_part():
    from mcpbrain.sync.extractors import extract_text_from_eml

    raw = (b"Subject: Multi\r\n"
           b'Content-Type: multipart/alternative; boundary="b"\r\n\r\n'
           b"--b\r\nContent-Type: text/plain\r\n\r\nplain body here\r\n"
           b"--b\r\nContent-Type: text/html\r\n\r\n<p>html body</p>\r\n--b--\r\n")

    text = extract_text_from_eml(raw)

    assert "plain body here" in text
    assert "<p>" not in text


def test_pptx_extraction_failure_returns_empty_and_logs(caplog):
    from mcpbrain.sync.extractors import extract_text_from_pptx

    with caplog.at_level("WARNING"):
        assert extract_text_from_pptx(b"not a pptx") == ""

    assert any("pptx" in r.message for r in caplog.records), (
        "a failed extraction must leave a trace; eight sites in this module "
        "returned '' with no log line at all (B7)"
    )


def test_a_scanned_pdf_with_no_ocr_available_is_reported_not_silently_empty(monkeypatch, caplog):
    """A5: with tesseract absent a fully-scanned PDF returns '' with no warning
    at all — no chunks, no log line, nothing to explain the absence."""
    import fitz

    from mcpbrain.sync import extractors

    monkeypatch.setattr(extractors, "_tesseract_available", lambda: False)
    doc = fitz.open()
    doc.new_page()
    data = doc.tobytes()

    with caplog.at_level("WARNING"):
        text = extractors.extract_text_from_pdf(data)

    assert text.strip() == ""
    assert any("scanned" in r.message.lower() and "tesseract" in r.message.lower()
               for r in caplog.records), (
        f"no warning explained the empty result: {[r.message for r in caplog.records]}"
    )


def test_a_failed_ocr_page_is_logged(monkeypatch, caplog):
    """A5: per-page OCR failure/timeout returns '' and falls back to page_text,
    so a timed-out page yields nothing, unlogged."""
    import fitz

    from mcpbrain.sync import extractors

    monkeypatch.setattr(extractors, "_tesseract_available", lambda: True)
    monkeypatch.setattr(extractors, "_ocr_page", lambda page: "")
    doc = fitz.open()
    doc.new_page()

    with caplog.at_level("WARNING"):
        extractors.extract_text_from_pdf(doc.tobytes())

    assert any("ocr" in r.message.lower() for r in caplog.records)


def test_is_scanned_pdf_is_either_used_or_gone():
    """A5: is_scanned_pdf is defined but never called; the real gate is an
    inline char-count heuristic. Two heuristics that can disagree, one of them
    dead, is worse than either alone."""
    import subprocess

    out = subprocess.run(["git", "grep", "-n", "is_scanned_pdf", "--", "mcpbrain/"],
                         capture_output=True, text=True).stdout
    call_sites = [ln for ln in out.splitlines() if "def is_scanned_pdf" not in ln]

    assert call_sites, "is_scanned_pdf is still dead code"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_tabular.py tests/test_extractors.py -q -p no:randomly`
Expected: `ModuleNotFoundError: mcpbrain.sync.tabular`, `ImportError: extract_tables_from_xlsx`, `ImportError: pptx`, and the three A5 tests failing with no warnings emitted.

- [ ] **Step 3: Create `mcpbrain/sync/tabular.py`**

```python
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
```

- [ ] **Step 4: Rewrite the XLSX extractor and add PPTX**

In `mcpbrain/sync/extractors.py`, add `import logging` / `log = logging.getLogger(__name__)` at the top if absent. Delete `_rows_to_markdown` (lines 181-202 — its only caller is `extract_text_from_xlsx`; DOCX tables keep their own `" | ".join` at line 171) and replace `extract_text_from_xlsx` with:

```python
def _tables_from_grid(name: str, raw: list[list[str]],
                      char_budget: int) -> list[Table]:
    """Normalise one sheet's raw grid into at most one `Table`.

    Shared by the .xlsx and .xls readers so the two formats cannot drift on
    empty-row handling, column trimming or the budget rule.

    `char_budget` bounds the sheet over NON-EMPTY rows and replaces the flat
    200-rows-per-sheet cap (B1): 338 live files hit that cap, including several
    budgets and a general ledger, losing everything past row 200 per sheet while
    the same files bloated the store with empty cells.
    """
    rows = normalise_rows(raw)
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
    truncated = len(kept) < len(data)
    if truncated:
        log.warning("sheet %r truncated at %d of %d rows (char budget %d)",
                    name, len(kept), len(data), char_budget)
    return [Table(sheet=name, header=header, rows=kept,
                  rows_total=len(data), truncated=truncated)]


def extract_tables_from_xlsx(content_bytes: bytes, *, char_budget: int) -> list[Table]:
    """Read every sheet as a `Table`.

    Replaces extract_text_from_xlsx. Deliberately does NOT render: chunk
    boundaries are decided by tabular.render_chunks, so each chunk can repeat
    the header rather than orphaning it in chunk 0 (B2).
    """
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(content_bytes), read_only=True,
                                    data_only=True)
    except Exception as exc:
        log.warning("xlsx: workbook open failed: %s", exc)
        return []
    tables: list[Table] = []
    try:
        for name in wb.sheetnames:
            raw = [[str(c) if c is not None else "" for c in row]
                   for row in wb[name].iter_rows(values_only=True)]
            tables.extend(_tables_from_grid(name, raw, char_budget))
    except Exception as exc:
        log.warning("xlsx: extraction failed after %d sheets: %s", len(tables), exc)
    finally:
        wb.close()
    return tables


def extract_tables_from_xls(content_bytes: bytes, *, char_budget: int) -> list[Table]:
    """Legacy .xls via xlrd, yielding the same `Table` shape as .xlsx.

    A2: .xls was dropped entirely. Declining a spreadsheet format is not
    defensible after B1 — budgets, ledgers and risk assessments are the
    highest-value tabular content in the corpus — and xlrd 2.0 exists precisely
    for this format (it dropped .xlsx, which openpyxl handles).
    """
    try:
        import xlrd
        book = xlrd.open_workbook(file_contents=content_bytes)
    except Exception as exc:
        log.warning("xls: workbook open failed: %s", exc)
        return []
    tables: list[Table] = []
    try:
        for sheet in book.sheets():
            raw = [[("" if c is None else str(c)) for c in sheet.row_values(r)]
                   for r in range(sheet.nrows)]
            tables.extend(_tables_from_grid(sheet.name, raw, char_budget))
    except Exception as exc:
        log.warning("xls: extraction failed after %d sheets: %s", len(tables), exc)
    return tables


def extract_text_from_eml(content_bytes: bytes) -> str:
    """A .eml file's headers and body as prose.

    A2: .eml files in Drive were dropped. Stdlib `email` parses them with no new
    dependency. Deliberately extracted as a PROSE DOCUMENT rather than turned
    into a synthetic Gmail thread: a Drive file is not a mailbox message, and
    minting message_id/thread_id for it would put a second, unauthoritative
    identity into the same namespace the real Gmail sync owns.
    """
    try:
        from email import policy
        from email.parser import BytesParser
        msg = BytesParser(policy=policy.default).parsebytes(content_bytes)
    except Exception as exc:
        log.warning("eml: parse failed: %s", exc)
        return ""
    try:
        head = [f"{h}: {msg.get(h, '')}" for h in ("From", "To", "Cc", "Subject", "Date")
                if msg.get(h)]
        body = msg.get_body(preferencelist=("plain", "html"))
        text = body.get_content() if body is not None else ""
        if body is not None and body.get_content_type() == "text/html":
            from mcpbrain.sync.normalise import strip_html
            text = strip_html(text)
        return "\n".join(head) + "\n\n" + (text or "").strip()
    except Exception as exc:
        log.warning("eml: extraction failed: %s", exc)
        return ""


def extract_text_from_pptx(content_bytes: bytes) -> str:
    """Extract slide text from PPTX bytes: titles, body frames and table cells.

    Slides are separated by a labelled heading so chunk_text's paragraph split
    keeps slide boundaries, and so a recalled chunk says which slide it is from.
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

with `from mcpbrain.sync.tabular import Table, normalise_rows` at the top.

- [ ] **Step 5: Make every extraction failure visible (B7) and fix the PDF gate (A5)**

Replace `extract_text_from_pdf`:

```python
def extract_text_from_pdf(content_bytes: bytes) -> str:
    """PDF text via pymupdf; per-page OCR fallback (tesseract CLI) for scanned pages.

    Every path that yields less than the document contains now says so (A5).
    Previously a scanned PDF with tesseract absent returned '' in silence, and a
    per-page OCR timeout (120 s) fell back to an empty page_text unlogged — so
    the file simply had no chunks and nothing recorded why.

    `is_scanned_pdf` is now the single gate. It was dead code sitting beside a
    second, DIFFERENT inline heuristic (avg < 50 chars/page here, avg < 20
    inline), and two heuristics that can disagree — one of them unreachable —
    is worse than either alone. The new gate is slightly more willing to attempt
    OCR, which is the intended direction: a page with 30 characters is a scan
    with a caption.
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

Then for each remaining silent handler — `extract_text_from_docx` (`:173`), `_ocr_page` (`:100`, `:111`), `is_scanned_pdf` (`:40`, `:47`) — change `except Exception:` to `except Exception as exc:` and add a `log.warning`/`log.debug` naming the extractor before the existing return. **Return values must not change**: this step changes visibility, not behaviour.

- [ ] **Step 6: Run the tests and verify they discriminate**

Run: `uv run pytest tests/test_tabular.py tests/test_extractors.py -q -p no:randomly`
Expected: PASS. Existing tests asserting the old markdown output of `extract_text_from_xlsx` will fail — rewrite each against `extract_tables_from_xlsx` with a comment saying why the expectation changed. Do **not** loosen an assertion to make it pass.

Record in the commit message whether `test_pdf_text_layer` behaves identically under the new `is_scanned_pdf` gate.

- [ ] **Step 7: Commit**

```bash
git add mcpbrain/sync/tabular.py mcpbrain/sync/extractors.py \
        tests/test_tabular.py tests/test_extractors.py
git commit -m "feat(extract): structured tables with header-repeating row groups; .pptx; visible failures

Replaces the 200-row-per-sheet cap with a character budget over non-empty rows,
and stops rendering tables to text before chunking — every row group now
repeats its sheet name, row range and header, and each sheet gains a summary
chunk. .pptx was advertised in the MIME table but reachable by no fetcher (0
live chunks). is_scanned_pdf becomes the single PDF gate instead of dead code
beside a second inline heuristic."
```

---

## Gate 2 — Task 3: Enrichment and retrieval

Runs in parallel with Task 2. Owns `semantic.py`, `graph_write.py`, `thread_enrich.py`, `retrieval_expand.py`, `store.py`.

**Files:**
- Modify: `mcpbrain/semantic.py:23-100`, `mcpbrain/graph_write.py:1516-1531`, `mcpbrain/thread_enrich.py:143-148`, `mcpbrain/retrieval_expand.py:37-38`, `mcpbrain/store.py:2503-2525`
- Test: `tests/test_semantic.py`, `tests/test_thread_enrich.py`, `tests/test_retrieval_expand.py`

**Interfaces:**
- Consumes: nothing from Task 1 or 2 — fully independent, which is why it can run in parallel with Task 2.
- Produces: `build_semantic_doc(extraction, thread, owner=None, taxonomy=None, *, date_iso: str = "", message_id: str = "") -> tuple[str, dict]`; `store.count_chunks_longer_than(n: int) -> int` (consumed by Task 6).

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
    assert recency_decay(meta) != 0.5, "still date-blind to the ranker"


def test_the_enriched_chunk_keeps_the_message_level_link():
    """C3: enriched chunks retained thread_id but no message_id, so a fact could
    be traced to a thread but not to the message it came from."""
    from mcpbrain.semantic import build_semantic_doc

    _text, meta = build_semantic_doc({"thread_id": "t1"}, {"subject": "s"},
                                     message_id="msg-9")

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


def test_the_semantic_doc_never_exceeds_the_embedder_window():
    """B3, closed at the source. The semantic doc is SYNTHESISED — its length is
    ours to choose — and it is the one population chunk_text cannot bound,
    because it is written whole to keep its enriched-<thread_id> doc_id. A
    100-person thread with 60 actions used to produce a doc whose tail the
    512-token BGE model silently discarded."""
    from mcpbrain.semantic import SEMANTIC_MAX_CHARS, build_semantic_doc

    extraction = {
        "thread_id": "t1",
        "summary": "Quarterly review. " * 40,
        "entities": [{"type": "person", "name": f"Person Number {i}"}
                     for i in range(100)],
        "actions": [{"description": f"Do the thing number {i}", "due_date": "2026-07-01"}
                    for i in range(60)],
        "topics": [f"topic-{i}" for i in range(80)],
    }

    text, _meta = build_semantic_doc(extraction, {"subject": "Review",
                                                  "sender": "a@b.com",
                                                  "date": "Tue, 02 Jun 2026 16:30:01 +0800"})

    assert len(text) <= SEMANTIC_MAX_CHARS, f"semantic doc is {len(text)} chars"


def test_bounding_keeps_the_highest_value_content_first():
    """Truncation order matters: subject and summary are what a query matches.
    Dropping them to keep a Labels line would be worse than not bounding at all."""
    from mcpbrain.semantic import build_semantic_doc

    extraction = {
        "thread_id": "t1",
        "summary": "The Hall B booking is confirmed for Sunday.",
        "entities": [{"type": "person", "name": f"Person Number {i}"}
                     for i in range(400)],
        "topics": [f"topic-{i}" for i in range(400)],
    }

    text, _meta = build_semantic_doc(extraction, {"subject": "Hall B",
                                                  "sender": "a@b.com", "date": "x"})

    assert "Hall B" in text
    assert "The Hall B booking is confirmed for Sunday." in text
    assert "…" in text or "Topics:" not in text, (
        "over-budget sections must be visibly elided, not silently absent"
    )


def test_a_normal_thread_is_not_truncated():
    """The discriminator: bounding must be invisible for the ordinary case."""
    from mcpbrain.semantic import build_semantic_doc

    text, _meta = build_semantic_doc(
        {"thread_id": "t1", "summary": "Short summary.",
         "entities": [{"type": "person", "name": "Sam Taylor"}],
         "topics": ["booking"]},
        {"subject": "Hall B", "sender": "a@b.com", "date": "x"})

    assert "…" not in text
    assert "Sam Taylor" in text and "booking" in text
```

Add to `tests/test_retrieval_expand.py`:

```python
def test_thread_expansion_orders_chunks_within_a_message():
    """B4: _by_date sorts by date alone, and every chunk of one message shares a
    date, so a stable sort preserved raw SQLite scan order. Any email over 2,000
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

Add to `tests/test_thread_enrich.py`:

```python
def test_a_hole_in_a_message_is_marked():
    """B8: group_unenriched_threads iterates unenriched_chunks and
    reassemble_thread joins only those. If part of a document was already
    enriched — or cold-marked, excluded at store.py:1264 — the model received a
    partial document with no indication anything was missing."""
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


def test_a_truncated_tail_is_marked():
    """The other half of B8, which can occur alone: indices 0 and 1 present, but
    chunk_total says 5."""
    from mcpbrain.thread_enrich import reassemble_thread

    chunks = [
        {"doc_id": f"gmail-m1-body-{i}", "text": f"part {i}",
         "metadata": {"message_id": "m1", "chunk_index": i, "chunk_total": 5,
                      "date": "2026-06-01", "sender": "a@b.com", "subject": "s"}}
        for i in (0, 1)
    ]

    assert "[…]" in list(reassemble_thread(chunks))[0]["text"]


def test_a_complete_message_gets_no_gap_marker():
    """The discriminator: a marker on every message would train the model to
    ignore it."""
    from mcpbrain.thread_enrich import reassemble_thread

    chunks = [
        {"doc_id": f"gmail-m1-body-{i}", "text": f"part {i}",
         "metadata": {"message_id": "m1", "chunk_index": i, "chunk_total": 2,
                      "date": "2026-06-01", "sender": "a@b.com", "subject": "s"}}
        for i in (0, 1)
    ]

    assert "[…]" not in list(reassemble_thread(chunks))[0]["text"]


def test_a_message_with_no_chunk_total_gets_no_tail_marker():
    """chunk_total only exists on chunks written after this plan's C1 change.
    On older chunks the tail check must simply not fire."""
    from mcpbrain.thread_enrich import reassemble_thread

    chunks = [{"doc_id": "gmail-m1-body-0", "text": "only part",
               "metadata": {"message_id": "m1", "chunk_index": 0,
                            "date": "2026-06-01", "sender": "a@b.com",
                            "subject": "s"}}]

    assert "[…]" not in list(reassemble_thread(chunks))[0]["text"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_semantic.py tests/test_retrieval_expand.py tests/test_thread_enrich.py -q -p no:randomly`
Expected: `KeyError: 'date'`, `KeyError: 'message_id'`, `assert 'gmail_enriched_v2' == 'calendar_enriched_v2'`, ordering `['third', 'first', 'second']`, and the two gap-marker tests.

- [ ] **Step 3: Bound the semantic doc (B3)**

In `mcpbrain/semantic.py`, add at module level:

```python
# The BGE window is 512 tokens ≈ 2,000 characters, and embed.contextual_prefix
# (default ON) eats into the same budget — hence the headroom. Anything past it
# is silently truncated by the model and its tail is unsearchable (B3).
#
# This is the ONE population chunk_text cannot bound, because the semantic doc
# is written whole to keep its `enriched-<thread_id>` doc_id (mark_enriched,
# doc_ids_for_messages and the stale-reextract sweep all key on it). Splitting
# it is therefore off the table — but it does not need splitting, because the
# doc is SYNTHESISED here, line by line, so its length is ours to choose.
SEMANTIC_MAX_CHARS = 1800


def _fit(lines: list[str], budget: int) -> list[str]:
    """Keep whole lines while they fit, then stop and mark the elision.

    Callers pass lines in DESCENDING value order — subject, From/Date, summary,
    then People, Actions, Topics, Labels — because what gets dropped under
    pressure must be the least query-relevant content. Dropping the summary to
    keep a Labels line would be worse than not bounding at all.
    """
    out: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > budget:
            out.append("…")
            break
        out.append(line)
        used += len(line) + 1
    return out
```

and wrap the existing assembly's final join. The function currently ends its text build with `text = "\n".join(lines)`; replace that with:

```python
    text = "\n".join(_fit(lines, SEMANTIC_MAX_CHARS))
```

The existing `lines` list is already built in the right order (email line, From, Date, Type, blank, summary, People, Actions, Topics, Labels), so no reordering is needed — verify that when you make the edit, and reorder only if it has drifted.

- [ ] **Step 4: Fix the enriched-chunk metadata**

In `mcpbrain/semantic.py`, change the signature to

```python
def build_semantic_doc(extraction: dict, thread: dict, owner=None, taxonomy=None,
                       *, date_iso: str = "", message_id: str = "") -> tuple[str, dict]:
```

and replace the metadata block at the end:

```python
    thread_id = extraction.get("thread_id", "") or ""
    metadata = {
        # C4: a calendar-sourced enrichment carries a cal-* thread id and was
        # nonetheless labelled gmail_enriched_v2 — observed live on
        # cal-e734d9f93c894a5a81e3230300748014. No consumer reads these values
        # today beyond tests (grep: semantic.py is the only writer, and nothing
        # in importance.py or retrieval.py branches on them), so correcting the
        # label is safe.
        "source_type": ("calendar_enriched_v2" if thread_id.startswith("cal-")
                        else "gmail_enriched_v2"),
        "thread_id": thread_id,
        "subject": subject[:200],
        "org": org,
        "content_type": content_type,
        # C2: without a date, importance.recency_decay returns its neutral 0.5
        # fallback for all 21,162 of these. `date` is the lead's RFC2822 header,
        # which importance._parse_age_days already handles.
        "date": date[:80],
        # C3: thread-level provenance without message-level provenance means a
        # fact can be traced to a thread but not to the message it came from.
        "message_id": message_id[:200],
    }
    if date_iso:
        metadata["date_iso"] = date_iso[:40]
    return text, metadata
```

In `mcpbrain/graph_write.py` at the `build_semantic_doc` call (~line 1520), pass what is already in scope:

```python
        semantic_text, semantic_meta = build_semantic_doc(
            extraction, lead, owner=owner, taxonomy=taxonomy,
            date_iso=lead_date_iso or "", message_id=lead_msg_id or "")
```

- [ ] **Step 5: Fix thread ordering**

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
        by date alone is NOT an ordering within a message, because every chunk
        of one message shares its date; that was B4.
        """
```

- [ ] **Step 6: Add the gap marker**

`reassemble_thread` (`mcpbrain/thread_enrich.py:143-148`) currently reads:

```python
    for mid in order:
        parts = sorted(by_message[mid],
                       key=lambda c: (c.get("metadata") or {}).get("chunk_index", 0))
        meta = parts[0].get("metadata") or {}
        text = _CHUNK_JOIN.join(p.get("text", "") for p in parts)
```

Replace only the `text = ...` line with `text = _join_with_gaps(parts)`, leave the grouping untouched, and add above `reassemble_thread`:

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
    chunks it is absent and the check simply does not fire, which is the correct
    degradation. `parts` is already sorted by chunk_index by the caller.
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

- [ ] **Step 7: Add the store count helper (consumed by Task 6)**

In `mcpbrain/store.py`, beside the other count helpers:

```python
    def count_chunks_longer_than(self, n: int) -> int:
        """Chunks whose stored text exceeds n characters — i.e. whose tail the
        512-token embedder silently discards (B3). 15,576 in the live store."""
        with self._connect() as db:
            return db.execute("SELECT COUNT(*) FROM chunks WHERE length(text) > ?",
                              (int(n),)).fetchone()[0]
```

- [ ] **Step 8: Run the tests and verify they discriminate**

Run: `uv run pytest tests/test_semantic.py tests/test_retrieval_expand.py tests/test_thread_enrich.py tests/test_graph_write.py tests/test_prepare.py tests/test_stale_reextract.py -q -p no:randomly`
Expected: PASS. `tests/test_semantic.py:94` and `:118` assert `source_type == "gmail_enriched_v2"` — check each fixture's `thread_id` and update with a comment only if it is `cal-`-prefixed.

- [ ] **Step 9: Commit**

```bash
git add mcpbrain/semantic.py mcpbrain/graph_write.py mcpbrain/retrieval_expand.py \
        mcpbrain/thread_enrich.py mcpbrain/store.py tests/test_semantic.py \
        tests/test_retrieval_expand.py tests/test_thread_enrich.py
git commit -m "fix(enrich): date + message_id on enriched chunks, ordered expansion, gap markers

21,162 enriched chunks — the LLM-digested summaries, the highest-value chunks in
the store — carried no date in any form, so recency_decay returned its neutral
0.5 fallback for every one; they also lost the message-level link, and
calendar-derived enrichments were labelled as gmail. Thread expansion sorted by
date alone, and every chunk of one message shares a date, so any email over
2,000 chars was injected in raw SQLite scan order."
```

---

## Gate 3 — Task 4: Drive

Runs in parallel with Tasks 5 and 6. Owns `sync/drive.py` and `sync/calendar.py`.

**Files:**
- Modify: `mcpbrain/sync/drive.py` (MIME tables, `_fetch_text` → `fetch_content`, `normalise_drive`, `folder_path`, `upsert_file_chunks`, three call sites), `mcpbrain/sync/calendar.py`
- Test: `tests/test_drive_sync.py`, `tests/test_drive_extraction.py`, `tests/test_chunk_metadata.py`, `tests/test_calendar_sync.py`

**Interfaces:**
- Consumes: `tabular.{Table, is_tabular, tables_from_csv, render_chunks, CHUNK_CHARS}`, `extractors.{extract_tables_from_xlsx, extract_text_from_pptx}` (Task 2); `ingest_report.record_skip`, `config.sheet_char_budget`, `chunking.has_content` (Task 1).
- Produces: `drive.Content` dataclass, `drive.fetch_content(...)`, `drive.folder_path(...)`, `drive.upsert_file_chunks(...)`; `normalise_drive(file_meta, text, drive_id=None, *, tables=None, folder="")`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_drive_extraction.py`:

```python
def test_a_spreadsheet_is_chunked_by_row_group_with_headers():
    """B2: a Google Sheet exports as CSV, chunk_text splits on \\n\\n, a CSV has
    no blank lines, so the whole sheet became one 'paragraph' and fell to the
    word-split branch — cut at 2,000 chars mid-row, mid-cell, with the header
    surviving only in chunk 0."""
    from mcpbrain.sync.drive import normalise_drive
    from mcpbrain.sync.tabular import Table

    fmeta = {"id": "f1", "name": "Budget.xlsx",
             "mimeType": "application/vnd.openxmlformats-officedocument."
                         "spreadsheetml.sheet"}
    tables = [Table(sheet="GL", header=["Item", "Amount"],
                    rows=[[f"Item {i}", str(i)] for i in range(200)],
                    rows_total=200, truncated=False)]

    chunks = normalise_drive(fmeta, "", tables=tables)
    row_chunks = [c for c in chunks if c.metadata.get("table_role") == "rows"]

    assert len(row_chunks) > 1, "200 rows should not fit in one chunk"
    for c in row_chunks:
        assert "| Item | Amount |" in c.text, "row group lost its header"
    assert any(c.metadata.get("table_role") == "summary" for c in chunks)


def test_a_content_free_document_produces_no_chunks():
    """The 66,653 empty-pipe chunks must not be creatable any more."""
    from mcpbrain.sync.drive import normalise_drive

    fmeta = {"id": "f1", "name": "Empty.txt", "mimeType": "text/plain"}

    assert normalise_drive(fmeta, "|  |  |  |\n\n|  |  |  |") == []
```

Add to `tests/test_chunk_metadata.py`:

```python
def test_every_drive_chunk_records_how_many_chunks_its_document_has():
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
def test_an_unsupported_drive_type_is_recorded_rather_than_silently_dropped():
    """A2: .pptx, .doc, .pages, images and .zip all returned None from
    _fetch_text with no chunk, no stub and no log line."""
    from mcpbrain.sync import drive

    class _Store:
        def __init__(self):
            self.changes = []

        def record_change(self, kind, ref_id="", summary=""):
            self.changes.append((kind, ref_id, summary))

    store = _Store()
    fmeta = {"id": "f-1", "name": "Deck.key",
             "mimeType": "application/x-iwork-keynote-sffkey"}

    assert drive.fetch_content(object(), fmeta, store=store) is None
    assert store.changes and store.changes[0][0] == "ingest_skip"
    assert "unsupported_mime" in store.changes[0][2]


def test_a_supported_type_that_extracts_to_nothing_is_recorded_distinctly(monkeypatch):
    """B7: eight `except Exception: return ""` sites make a corrupt DOCX
    indistinguishable from an unsupported type. They must not share a bucket."""
    from mcpbrain.sync import drive

    class _Store:
        def __init__(self):
            self.changes = []

        def record_change(self, kind, ref_id="", summary=""):
            self.changes.append((kind, ref_id, summary))

    store = _Store()
    monkeypatch.setattr(drive, "_fetch_text", lambda service, meta: "")
    fmeta = {"id": "f-2", "name": "Broken.docx",
             "mimeType": "application/vnd.openxmlformats-officedocument."
                         "wordprocessingml.document"}

    drive.fetch_content(object(), fmeta, store=store)

    assert [s.split(":")[0] for _k, _r, s in store.changes] == ["extraction_empty"]


def test_folder_path_is_resolved_and_cached():
    """C5: embed.contextual_prefix reads metadata['folder_path'] and
    normalise_drive never wrote it, so every Drive contextual prefix has been
    missing its folder context — dead provenance in a default-ON feature."""
    from mcpbrain.sync import drive

    calls: list = []

    class _Service:
        def files(self):
            return self

        def get(self, fileId, fields, supportsAllDrives=None):
            calls.append(fileId)
            self._fid = fileId
            return self

        def execute(self):
            return {"folder-1": {"id": "folder-1", "name": "Budgets",
                                 "parents": ["folder-0"]},
                    "folder-0": {"id": "folder-0", "name": "Finance",
                                 "parents": []}}[self._fid]

    cache: dict = {}
    fmeta = {"id": "f1", "name": "Budget.xlsx", "parents": ["folder-1"]}

    assert drive.folder_path(_Service(), fmeta, cache) == "Finance/Budgets"
    drive.folder_path(_Service(), fmeta, cache)
    assert calls == ["folder-1", "folder-0"], "the second call must hit the cache"


def test_a_shrinking_document_drops_its_orphaned_chunks(tmp_path):
    """B5: Drive writes gdrive-<fid>-<i> for i in 0..n-1 and only ever upserts.
    Nothing deleted indices n..m left by a previous, longer version, so deleted
    paragraphs stayed searchable indefinitely and were re-fed to expansion as
    current content."""
    from mcpbrain.store import Store
    from mcpbrain.sync.drive import normalise_drive, upsert_file_chunks

    store = Store(tmp_path / "b.sqlite3", dim=4)
    store.init()
    fmeta = {"id": "f1", "name": "Notes.txt", "mimeType": "text/plain"}

    long_text = "\n\n".join(f"Para {i} " + "word " * 400 for i in range(5))
    upsert_file_chunks(store, normalise_drive(fmeta, long_text), file_id="f1")
    assert len(store.doc_ids_for_file("f1")) >= 3

    upsert_file_chunks(store, normalise_drive(fmeta, "Para 0 " + "word " * 100),
                       file_id="f1")

    assert store.doc_ids_for_file("f1") == ["gdrive-f1-0"], "stale chunks survived"


def test_upserting_an_unchanged_document_deletes_nothing(tmp_path):
    from mcpbrain.store import Store
    from mcpbrain.sync.drive import normalise_drive, upsert_file_chunks

    store = Store(tmp_path / "b.sqlite3", dim=4)
    store.init()
    fmeta = {"id": "f1", "name": "Notes.txt", "mimeType": "text/plain"}
    text = "\n\n".join(f"Para {i} " + "word " * 400 for i in range(5))

    upsert_file_chunks(store, normalise_drive(fmeta, text), file_id="f1")
    first = sorted(store.doc_ids_for_file("f1"))
    upsert_file_chunks(store, normalise_drive(fmeta, text), file_id="f1")

    assert sorted(store.doc_ids_for_file("f1")) == first
```

Add to `tests/test_calendar_sync.py`:

```python
def test_a_short_event_keeps_its_exact_doc_id():
    """Finding E's fix must not change the common case: delete_calendar_chunks_
    after and the calendar enrichment path both key on cal-<event_id>, so a
    suffix here would orphan every existing calendar chunk."""
    from mcpbrain.sync.calendar import normalise_calendar

    chunks = normalise_calendar({"id": "e1", "summary": "Standup",
                                 "start": {"dateTime": "2026-06-02T09:00:00Z"}})

    assert [c.doc_id for c in chunks] == ["cal-e1"]
    assert chunks[0].metadata["chunk_total"] == 1


def test_a_very_long_agenda_is_split():
    """Finding E: normalise_calendar emitted exactly one chunk per event with the
    description inlined, never calling chunk_text, so a long agenda was truncated
    by the embedder rather than split. Only 4 of 1,149 live chunks are affected."""
    from mcpbrain.sync.calendar import normalise_calendar

    chunks = normalise_calendar({"id": "e2", "summary": "Board",
                                 "start": {"dateTime": "2026-06-02T09:00:00Z"},
                                 "description": "agenda item. " * 500})

    assert len(chunks) > 1
    assert [c.doc_id for c in chunks] == [f"cal-e2-{i}" for i in range(len(chunks))]
    assert all(c.metadata["chunk_total"] == len(chunks) for c in chunks)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_drive_sync.py tests/test_drive_extraction.py tests/test_chunk_metadata.py tests/test_calendar_sync.py -q -p no:randomly`
Expected: `AttributeError: fetch_content`, `AttributeError: folder_path`, `ImportError: upsert_file_chunks`, `TypeError: unexpected keyword 'tables'`, `KeyError: 'chunk_total'`.

- [ ] **Step 3: Extend the MIME tables**

`_DOWNLOAD_TEXT` gains the types already advertised in `_MIME_EXTRACTION_META` but reachable by no fetcher (A2), plus three new prose types:

```python
_DOWNLOAD_TEXT = {"text/plain", "text/markdown", "text/csv",
                  "application/csv", "text/tab-separated-values",
                  "application/rtf", "application/json", "text/html"}
```

`_DOWNLOAD_BINARY` gains PPTX and EML (legacy `.xls` is tabular and routed in Step 4 alongside `.xlsx`, not here):

```python
    "application/vnd.openxmlformats-officedocument.presentationml.presentation":
        extract_text_from_pptx,
    "message/rfc822": extract_text_from_eml,
```

`_MIME_EXTRACTION_META` gains labels for the new types so they do not fall back to the default:

```python
    "application/rtf": ("text", "prose", 0.9),
    "application/json": ("text", "prose", 1.0),
    "text/html": ("text", "prose", 0.9),
    "message/rfc822": ("eml", "prose", 0.95),
    "application/vnd.ms-excel": ("spreadsheet", "table", 1.0),   # legacy .xls
```

**Note the `.xls` entry makes `test_table_mimes_agrees_with_the_drive_extraction_meta_table` (Task 2) fail unless `tabular.TABLE_MIMES` also gains `application/vnd.ms-excel`.** That is the guard working as designed — the two lists exist in modules that cannot import each other. Task 2 owns `tabular.py` and adds it there; if you are running Wave 3 and Task 2 did not, stop and raise it rather than editing `tabular.py` from this task.

- [ ] **Step 4: Add `fetch_content` and structured tabular routing**

Keep `_fetch_text` exactly as it is for non-tabular types. Add above it:

```python
@dataclass
class Content:
    """What one Drive file yielded. `tables` is set only for tabular MIME types,
    where the chunker needs structure rather than text (see sync/tabular.py)."""
    text: str = ""
    tables: list[Table] | None = None
```

and below it:

```python
def fetch_content(service, file_meta: dict, *, store=None) -> Content | None:
    """Fetch one Drive file, and leave a durable trace when it yields nothing.

    Three outcomes, and they must stay distinguishable (B7 exists because they
    were not):
      - a Content with text or tables — ingest it;
      - a Content that is empty — a SUPPORTED type that extracted to nothing,
        i.e. a corrupt or image-only file worth investigating;
      - None — a type we never claimed to handle.

    Types deliberately still unsupported, and now RECORDED rather than silently
    skipped: legacy .doc/.ppt/.xls, Apple .pages/.numbers/.keynote, .zip, .eml
    and every image format. Each needs a new dependency of doubtful value or a
    different pipeline; what changes here is that a sync no longer reports
    success while discarding them.
    """
    mime = file_meta.get("mimeType", "")
    fid = file_meta.get("id", "")
    name = file_meta.get("name", "")
    budget = config.sheet_char_budget(str(config.app_dir()))

    binary_tables = {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
            extract_tables_from_xlsx,
        "application/vnd.ms-excel": extract_tables_from_xls,   # legacy .xls
    }
    if mime in binary_tables:
        raw = service.files().get_media(fileId=fid, supportsAllDrives=True).execute()
        data = raw if isinstance(raw, bytes) else str(raw).encode("utf-8", "replace")
        tables = binary_tables[mime](data, char_budget=budget)
        if not tables:
            ingest_report.record_skip(store, "extraction_empty", fid, f"{mime} ({name})")
        return Content(tables=tables)

    text = _fetch_text(service, file_meta)
    if text is None:
        ingest_report.record_skip(store, "unsupported_mime", fid, f"{mime} ({name})")
        return None
    if not text.strip():
        ingest_report.record_skip(store, "extraction_empty", fid, f"{mime} ({name})")
        return Content()
    if tabular.is_tabular(mime):
        # Google Sheets export as text/csv, and CSV/TSV download verbatim — both
        # converge on the same Table shape as XLSX so there is ONE renderer.
        return Content(tables=tabular.tables_from_csv(
            text, sheet=name or "Sheet1", char_budget=budget))
    return Content(text=text)
```

with `from dataclasses import dataclass`, `from mcpbrain import config`, `from mcpbrain.sync import ingest_report, tabular`, `from mcpbrain.sync.tabular import Table` and `extract_tables_from_xls, extract_tables_from_xlsx, extract_text_from_eml, extract_text_from_pptx` added to the imports.

- [ ] **Step 5: Rewrite the tail of `normalise_drive`**

Change the signature to `def normalise_drive(file_meta, text, drive_id=None, *, tables=None, folder="")` — positional part unchanged, so the locked interface holds — and replace the guard at the top and the chunk loop at the end:

```python
    if not tables and (not text or not text.strip()):
        return []
    ...
    if folder:
        base_meta["folder_path"] = folder[:300]

    if tables:
        rendered = tabular.render_chunks(tables, file_name=base_meta["file_name"],
                                         max_chars=tabular.CHUNK_CHARS)
    else:
        rendered = [(t, {}) for t in chunk_text(text)]

    kept = [(t, extra) for t, extra in rendered if has_content(t)]
    out = []
    for i, (chunk, extra) in enumerate(kept):
        meta = {**base_meta, **extra, "chunk_index": i, "chunk_total": len(kept)}
        out.append(Chunk(doc_id=f"gdrive-{fid}-{i}", text=chunk,
                         content_hash=content_hash(chunk), metadata=meta))
    return out
```

- [ ] **Step 6: Add `folder_path` and `upsert_file_chunks`**

Extend `_CHANGES_FIELDS` to request parents:

```python
_CHANGES_FIELDS = (
    "nextPageToken,newStartPageToken,"
    "changes(fileId,removed,file(id,name,mimeType,modifiedTime,owners,"
    "md5Checksum,version,size,parents))"
)
```

```python
def folder_path(service, file_meta: dict, cache: dict) -> str:
    """The file's folder chain, e.g. 'Finance/Budgets'.

    C5: embed.contextual_prefix (default ON) reads metadata['folder_path'] and
    normalise_drive never wrote it. `cache` maps folder_id -> (name, parents) and
    is owned by the CALLER for a whole sync round, so a Drive with 5,000 files in
    40 folders costs 40 lookups, not 5,000. Any failure degrades to '' —
    provenance must never break a sync. Depth-capped at 8 and cycle-guarded,
    because Drive shortcuts can produce a loop.
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
                info = service.files().get(fileId=fid, fields="id,name,parents",
                                           supportsAllDrives=True).execute()
                cache[fid] = (info.get("name", ""), info.get("parents") or [])
            except Exception as exc:  # noqa: BLE001 — provenance is best-effort
                log.debug("folder_path: lookup failed for %s: %s", fid, exc)
                cache[fid] = ("", [])
        name, parents = cache[fid]
        if name:
            names.append(name)
    return "/".join(reversed(names))


def upsert_file_chunks(store, chunks: list[Chunk], *, file_id: str) -> int:
    """Upsert one Drive file's chunks and delete the ones it no longer has.

    B5: doc_ids are positional (gdrive-<fid>-<i>) and every write path only ever
    upserted. When a document shrank from m chunks to n, indices n..m-1 survived
    — deleted paragraphs stayed searchable indefinitely and were re-fed to
    expansion as current content, with nothing able to detect it (no chunk
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

- [ ] **Step 7: Update the three call sites**

In `_cache_first_extract_one` (`:307`), `sync_drive` (`:434`) and `backfill_drive`/`sync_shared_drive` (`:835`), each currently reads `text = _fetch_text(service, fmeta)` followed by `if not text: …` and a `normalise_drive(fmeta, text, …)`. Replace each with:

```python
    content = fetch_content(service, fmeta, store=store)
    if content is None or (not content.text and not content.tables):
        return False, None          # or `continue` — match the existing control flow
    chunks = normalise_drive(fmeta, content.text, drive_id=drive_id,
                             tables=content.tables,
                             folder=folder_path(service, fmeta, folder_cache))
```

Create `folder_cache: dict = {}` once per sync call (not per file) in each of `sync_drive`, `backfill_drive`, `sync_shared_drive`; `_cache_first_extract_one` takes it as a new keyword argument from its two callers.

Then replace each upsert loop with `upsert_file_chunks(store, chunks, file_id=fid)` inside the existing `with bulk_section():`. **Do not** call it on the cache-import path: `ingest_cache.try_import` writes its own chunk set in its own transaction, and an orphan sweep there would race it.

- [ ] **Step 8: Fix calendar chunking (E) and `chunk_total`**

`normalise_calendar` ends with a single hard-coded chunk:

```python
    return [Chunk(doc_id=f"cal-{eid}", text=text, content_hash=content_hash(text), metadata=meta)]
```

Replace that one line with:

```python
    # Finding E: this emitted exactly one chunk per event with the description
    # inlined, never calling chunk_text, so a long agenda was truncated by the
    # embedder rather than split. Impact is small — of 1,149 live calendar
    # chunks, max length 2,977 and only 4 exceed 2,000 chars — so the
    # single-chunk case stays byte-identical and only those 4 take a suffix.
    pieces = [p for p in chunk_text(text) if has_content(p)]
    if not pieces:
        return []
    if len(pieces) == 1:
        return [Chunk(doc_id=f"cal-{eid}", text=pieces[0],
                      content_hash=content_hash(pieces[0]),
                      metadata={**meta, "chunk_index": 0, "chunk_total": 1})]
    return [Chunk(doc_id=f"cal-{eid}-{i}", text=p, content_hash=content_hash(p),
                  metadata={**meta, "chunk_index": i, "chunk_total": len(pieces)})
            for i, p in enumerate(pieces)]
```

adding `chunk_text, has_content` to the `mcpbrain.chunking` import.

**Before this edit, read `store.delete_calendar_chunks_after`.** The single-chunk doc_id must stay `cal-<eid>` exactly — that sweep and the calendar enrichment path key on the shape. Then confirm its `LIKE` pattern also matches `cal-<eid>-<i>`: if it is `LIKE 'cal-%'` it already does; if it is an exact match, widen it and add a test, because otherwise those 4 events become undeletable.

- [ ] **Step 9: Run the tests and verify they discriminate**

Run: `uv run pytest tests/test_drive_sync.py tests/test_drive_extraction.py tests/test_drive_changes.py tests/test_drive_shared.py tests/test_chunk_metadata.py tests/test_calendar_sync.py tests/test_ingest_cache_lifecycle.py -q -p no:randomly`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add mcpbrain/sync/drive.py mcpbrain/sync/calendar.py tests/test_drive_sync.py \
        tests/test_drive_extraction.py tests/test_chunk_metadata.py \
        tests/test_calendar_sync.py
git commit -m "fix(drive): row-group tabular chunks, folder_path, orphan delete, type coverage

Spreadsheets now route through the structured chunker instead of being
character-split into headerless fragments. folder_path was read by
contextual_prefix and never written. A shrinking document left its tail chunks
searchable forever. .pptx, application/csv and TSV were advertised in the MIME
table but reachable by no fetcher; unsupported types are now recorded."
```

---

## Gate 3 — Task 5: Email and attachments

Runs in parallel with Tasks 4 and 6. Owns `sync/normalise.py`, `sync/gmail.py`, the new `sync/attachments.py`.

**Files:**
- Create: `mcpbrain/sync/attachments.py`
- Modify: `mcpbrain/sync/normalise.py`, `mcpbrain/sync/gmail.py`, `mcpbrain/prepare.py` (`should_enrich` only)
- Test: `tests/test_normalise.py`, `tests/test_gmail_sync.py`, `tests/test_salience_gate.py`, `tests/test_attachments.py` (create)

**Interfaces:**
- Consumes: `chunking.{chunk_text, content_hash, has_content}`, `config.gmail_attachments`, `ingest_report.record_skip` (Task 1); `extractors.*` and `tabular.*` (Task 2).
- Produces: `normalise_gmail(raw, *, report: dict | None = None)`; `attachments.{iter_attachment_parts, normalise_attachment, fetch_and_normalise}`.

- [ ] **Step 1: Write the failing tests**

If `tests/test_normalise.py` lacks these helpers, add them at the top:

```python
import base64


def _b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode()


def _message(*, headers, body, mime="text/plain", msg_id="m1", thread_id="t1"):
    return {"id": msg_id, "threadId": thread_id, "labelIds": ["INBOX"],
            "payload": {"mimeType": mime,
                        "headers": [{"name": n, "value": v} for n, v in headers],
                        "body": {"data": _b64(body)}}}


def _html_payload(html):
    return {"mimeType": "text/html", "headers": [], "body": {"data": _b64(html)}}
```

Then add:

```python
def test_a_reply_written_below_the_quote_survives():
    """A3: strip_reply_chains kept only text[:earliest], so a bottom-posted
    reply was thrown away along with the quote it sat under."""
    from mcpbrain.sync.normalise import strip_reply_chains

    text = ("On Mon, 2 Jun 2026 at 09:14, Sam <sam@example.com> wrote:\n"
            "> Can you confirm the Hall B booking for Sunday?\n"
            "> Sam\n\n"
            "Yes — Hall B is confirmed for Sunday the 8th, 9am to 1pm. "
            "I have put Priya down as the contact on the day.\n")

    out = strip_reply_chains(text)

    assert "Hall B is confirmed" in out, "the bottom-posted reply was discarded"
    assert "Can you confirm" not in out, "the quote itself must still be stripped"


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
```

Create `tests/test_attachments.py`:

```python
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
```

Add to `tests/test_gmail_sync.py` (this file already provides `Store`, `sync_gmail`, `plain_msg`, `_make_page`, `FakeService`):

```python
def test_sync_gmail_ingests_attachments(tmp_path, monkeypatch):
    """Wiring test: the attachment path must be reached from the real sync loop,
    not merely be callable in isolation. normalise_gmail has never called it,
    which is why A1 went unnoticed."""
    from mcpbrain.sync import attachments
    from mcpbrain.sync.normalise import Chunk

    store = Store(tmp_path / "test.sqlite3", dim=4)
    store.init()
    store.set_cursor("gmail", "1000")
    seen: list = []

    def _fake_fetch(service, raw, store=None):
        seen.append(raw["id"])
        return [Chunk(doc_id=f"gmail-{raw['id']}-att-0-0", text="Total due: 4,200.00",
                      content_hash="h1",
                      metadata={"source_type": "gmail",
                                "content_type": "email_attachment",
                                "message_id": raw["id"]})]

    monkeypatch.setattr(attachments, "fetch_and_normalise", _fake_fetch)
    svc = FakeService(profile_hid="1000",
                      pages=[_make_page(["m1"], history_id="1005")],
                      messages={"m1": plain_msg("m1", "Invoice", "a@b.com",
                                                "See attached.")})

    sync_gmail(svc, store)

    assert seen == ["m1"], "sync_gmail never reached the attachment path"
    assert store.get_chunk("gmail-m1-att-0-0") is not None


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

    sync_gmail(FakeService(profile_hid="1000",
                           pages=[_make_page(["m1"], history_id="1005")],
                           messages={"m1": plain_msg("m1", "s", "a@b.com", "body")}),
               store)

    assert called == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_normalise.py tests/test_attachments.py tests/test_gmail_sync.py -q -p no:randomly`
Expected: `ModuleNotFoundError: mcpbrain.sync.attachments`, `TypeError: unexpected keyword 'report'`, plus the bottom-post and `to_count` assertions.

- [ ] **Step 3: Fix the reply rescue**

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
    """Remove quoted history, keeping BOTH the text above the quote and any reply
    written below it (A3).

    The old implementation returned `text[:earliest]`, correct for top-posting —
    the overwhelmingly common case — and silently discarding every bottom-posted
    reply along with the quote it sat under.
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

and in `extract_body_with_signature`, disable the rescue on the HTML branch:

```python
    html = _find_part_text(payload, "text/html")
    if html:
        text = strip_reply_chains(strip_html(html), rescue_bottom_post=False)
        return extract_signature_block(text)
```

adding to that function's docstring: *"The bottom-post rescue is enabled for the plain-text branch only: it relies on '>' quoting having been stripped first, which is meaningless for HTML."*

- [ ] **Step 4: Rewrite `normalise_gmail`**

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
    # A4: this used to `return []` for bulk mail. The drop is gone — see the
    # `bulk` stamp below and prepare.should_enrich, which is the gate that
    # actually belongs in this role.
    bulk = _is_bulk_or_auto(headers, subject)
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
        # email. The counts are kept separately so a truncation that DOES happen
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
        # Marked, not dropped. prepare.should_enrich reads this and cold-marks
        # the chunk: embedded and searchable, never graph-extracted, reversible.
        # A header-based signal (List-Id / List-Unsubscribe / Precedence) is
        # strictly stronger than the Gmail CATEGORY_* labels should_enrich
        # already checks, so this improves that gate as well as replacing this
        # one.
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

adding `has_content` to the `mcpbrain.chunking` import. `normalise_gmail` no longer needs `config` at all — if nothing else in the module uses it, do not add the import.

- [ ] **Step 5: Make the salience gate act on the bulk signal**

Removing the drop without teaching `should_enrich` about it would send every newsletter to Haiku — strictly worse than before. In `mcpbrain/prepare.py`'s `should_enrich`, in the email branch (currently just above the `_PROMOTIONAL_LABELS` check at `:296`):

```python
    if source == "gmail" or meta.get("thread_id"):
        # Header-based bulk signal (List-Id / List-Unsubscribe / Precedence),
        # stamped at ingest by normalise_gmail. Checked BEFORE the Gmail
        # CATEGORY_* labels because it is strictly stronger: Gmail's categoriser
        # misses plenty of list mail that carries these headers, and this is the
        # signal that used to DROP the message outright at ingest (A4). Now it
        # cold-marks instead — embedded, searchable, never graph-extracted,
        # and reversible.
        if meta.get("bulk"):
            return False
        labels_raw = meta.get("labels") or ""
        ...
```

**Scope note:** this is the only edit this plan makes to `prepare.py`, and it is in `should_enrich` — nowhere near the budget / `bulk_section` plumbing the global constraints protect. No other task in this wave owns `prepare.py`.

- [ ] **Step 6: Create `mcpbrain/sync/attachments.py`**

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

They also inherit the parent's `date`, without which they would be date-blind
and `importance.recency_decay` would return its neutral 0.5 fallback for every
one — the same defect C2 documents for the enriched layer.

Fetching lives here rather than in normalise.py because that module is declared
pure data transformation ("No Google API calls here"), and the boundary is worth
keeping: `normalise_attachment` takes bytes and is testable with no service.
"""

import base64
import logging

from mcpbrain import config
from mcpbrain.chunking import chunk_text, content_hash, has_content
from mcpbrain.sync import ingest_report, tabular
from mcpbrain.sync.extractors import (
    extract_tables_from_xlsx,
    extract_text_from_docx,
    extract_text_from_pdf,
    extract_text_from_pptx,
)
from mcpbrain.sync.normalise import Chunk, get_header

log = logging.getLogger(__name__)

# Gmail's own attachment ceiling is 25 MB; anything claiming more is not
# something we will get whole anyway.
_MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024

# One message with 40 attachments must not spend a whole cycle's budget.
_MAX_ATTACHMENTS_PER_MESSAGE = 10

_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

_EXTRACTORS = {
    "application/pdf": extract_text_from_pdf,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        extract_text_from_docx,
    "application/vnd.openxmlformats-officedocument.presentationml.presentation":
        extract_text_from_pptx,
    "text/plain": lambda b: b.decode("utf-8", errors="replace"),
    "text/markdown": lambda b: b.decode("utf-8", errors="replace"),
}

# Tabular attachments yield Tables, not text, so they get the row-group chunker
# rather than chunk_text — an emailed budget must not be character-split any
# more than a Drive one.
_TABLE_EXTRACTORS = {_XLSX: extract_tables_from_xlsx}

_CSV_MIMES = ("text/csv", "application/csv", "text/tab-separated-values")

_EXTRACTION_METHOD = {
    "application/pdf": "pdf_layout",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    _XLSX: "spreadsheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "slides",
}

# Never worth fetching: no text to extract, and images in particular are almost
# always signature logos rather than content.
_SKIP_PREFIXES = ("image/", "audio/", "video/")


def _supported(mime: str) -> bool:
    return mime in _EXTRACTORS or mime in _TABLE_EXTRACTORS or mime in _CSV_MIMES


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
                # a given message and present for every consumer, including a
                # direct normalise_attachment call.
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

    doc_id: gmail-<message_id>-att-<attachment_index>-<chunk_index>.
    """
    mime = part["mime"]
    budget = config.sheet_char_budget(str(config.app_dir()))
    tables = None
    text = ""
    try:
        if mime in _TABLE_EXTRACTORS:
            tables = _TABLE_EXTRACTORS[mime](data, char_budget=budget)
        elif mime in _CSV_MIMES:
            tables = tabular.tables_from_csv(
                data.decode("utf-8", errors="replace"),
                sheet=part["filename"], char_budget=budget)
        elif mime in _EXTRACTORS:
            text = _EXTRACTORS[mime](data)
        else:
            return []
    except Exception as exc:  # noqa: BLE001 — one bad attachment must not kill a sync
        log.warning("attachment %r extraction failed: %s", part["filename"], exc)
        return []
    if not tables and not (text or "").strip():
        return []

    msg_id = raw_message["id"]
    headers = (raw_message.get("payload") or {}).get("headers", [])
    base = {
        "source_type": "gmail",
        "content_type": "email_attachment",
        "message_id": msg_id,
        "thread_id": raw_message.get("threadId", ""),
        "subject": get_header(headers, "subject")[:200],
        "sender": get_header(headers, "from")[:200],
        "date": get_header(headers, "date")[:80],
        "attachment_name": part["filename"][:200],
        "attachment_mime": mime[:100],
        "extraction_method": _EXTRACTION_METHOD.get(mime, "text"),
    }

    if tables:
        rendered = tabular.render_chunks(tables, file_name=part["filename"],
                                         max_chars=tabular.CHUNK_CHARS)
    else:
        rendered = [(t, {}) for t in chunk_text(text)]
    kept = [(t, extra) for t, extra in rendered if has_content(t)]

    return [
        Chunk(doc_id=f"gmail-{msg_id}-att-{part['index']}-{i}", text=t,
              content_hash=content_hash(t),
              metadata={**base, **extra, "chunk_index": i, "chunk_total": len(kept)})
        for i, (t, extra) in enumerate(kept)
    ]


def fetch_and_normalise(service, raw_message: dict, *, store=None) -> list[Chunk]:
    """Fetch every attachment of one message and normalise it.

    Best-effort per attachment: a 404 or a failed extraction skips that one and
    is recorded, rather than aborting the message or the sync.
    """
    payload = raw_message.get("payload") or {}
    msg_id = raw_message["id"]
    out: list[Chunk] = []
    for part in iter_attachment_parts(payload):
        if not _supported(part["mime"]):
            ingest_report.record_skip(store, "attachment_unsupported", msg_id,
                                      f"{part['mime']} ({part['filename']})")
            continue
        try:
            resp = service.users().messages().attachments().get(
                userId="me", messageId=msg_id, id=part["attachment_id"]).execute()
            data = base64.urlsafe_b64decode(resp.get("data") or "")
        except Exception as exc:  # noqa: BLE001 — one attachment must not kill a sync
            log.warning("attachment fetch failed for %s/%s: %s",
                        msg_id, part["filename"], exc)
            ingest_report.record_skip(store, "attachment_fetch_failed", msg_id,
                                      part["filename"])
            continue
        chunks = normalise_attachment(raw_message, part, data)
        if not chunks:
            ingest_report.record_skip(store, "attachment_empty", msg_id,
                                      f"{part['mime']} ({part['filename']})")
        out.extend(chunks)
    return out
```

- [ ] **Step 7: Wire it into `gmail.py`**

In `sync_gmail`, add `skips: dict = {}` before the fetch loop, and replace the message body of the loop:

```python
        # Attachment fetch is NETWORK I/O and must be hoisted OUT of the bulk
        # section — the daemon-scheduling work established that _bulk_lock must
        # never be held across network calls (see _cache_first_extract_one).
        att_chunks = (attachments.fetch_and_normalise(service, raw, store=store)
                      if config.gmail_attachments(str(config.app_dir())) else [])
        with bulk_section():
            for chunk in normalise_gmail(raw, report=skips):
                store.upsert_chunk(chunk.doc_id, chunk.text, chunk.content_hash,
                                   chunk.metadata)
            for chunk in att_chunks:
                store.upsert_chunk(chunk.doc_id, chunk.text, chunk.content_hash,
                                   chunk.metadata)
```

and immediately before `return messages_processed`:

```python
    for reason, count in sorted(skips.items()):
        ingest_report.record_skip(store, f"gmail_{reason}", source, str(count))
```

Apply the same shape to `backfill_gmail`. Add `from mcpbrain import config` and `from mcpbrain.sync import attachments, ingest_report` to `gmail.py`.

- [ ] **Step 8: Run the tests and verify they discriminate**

Run: `uv run pytest tests/test_normalise.py tests/test_attachments.py tests/test_gmail_sync.py tests/test_salience_gate.py -q -p no:randomly`
Expected: PASS. Existing `test_normalise.py` tests asserting `to`/`cc` clip at 300 chars must be updated with a comment explaining C6.

- [ ] **Step 9: Commit**

```bash
git add mcpbrain/sync/attachments.py mcpbrain/sync/normalise.py mcpbrain/sync/gmail.py \
        tests/test_attachments.py tests/test_normalise.py tests/test_gmail_sync.py
git commit -m "feat(gmail): ingest attachments; keep bottom-posted replies; report bulk drops

A PDF emailed to the user was invisible to the brain while the byte-identical
file in Drive extracted normally — there was no attachment-handling code in the
repo at all. strip_reply_chains returned text[:earliest], discarding any reply
written below the quote. Bulk mail was dropped while still counted as processed,
and to/cc were clipped at 300 chars."
```

---

## Gate 3 — Task 6: Observability

Runs in parallel with Tasks 4 and 5. Owns `index.py` and `doctor.py`. Small; can be given to the fastest agent.

**Files:**
- Modify: `mcpbrain/index.py:60-67`, `mcpbrain/doctor.py`
- Test: `tests/test_embed.py`, `tests/test_doctor.py`

**Interfaces:**
- Consumes: `store.count_chunks_longer_than` (Task 3).
- Produces: `index.EMBED_WINDOW_CHARS`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_embed.py`:

```python
def test_an_over_window_passage_is_counted_and_logged(caplog, tmp_path, monkeypatch):
    """B3: 15,576 chunks exceed the 512-token BGE window and are silently
    truncated at embed time, so their tails are unsearchable — not logged, not
    counted. chunk_text now bounds everything that goes through it; what remains
    is the enriched semantic doc, written whole because splitting it would break
    the enriched-<thread_id> doc_id."""
    from mcpbrain.index import EMBED_WINDOW_CHARS, index_pending
    from mcpbrain.store import Store

    class _Embedder:
        dim = 4

        def embed_passages(self, texts):
            return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    store = Store(tmp_path / "b.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("enriched-t1", "x" * (EMBED_WINDOW_CHARS + 500), "h1",
                       {"source_type": "gmail_enriched_v2"})

    with caplog.at_level("WARNING"):
        index_pending(store, _Embedder())

    assert any("window" in r.message for r in caplog.records), (
        f"the truncation was silent: {[r.message for r in caplog.records]}"
    )


def test_a_within_window_passage_logs_nothing(caplog, tmp_path, monkeypatch):
    """The discriminator: a warning on every batch would be noise nobody reads."""
    from mcpbrain.index import index_pending
    from mcpbrain.store import Store

    class _Embedder:
        dim = 4

        def embed_passages(self, texts):
            return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    store = Store(tmp_path / "b.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("d1", "a short passage", "h1", {"source_type": "gmail"})

    with caplog.at_level("WARNING"):
        index_pending(store, _Embedder())

    assert not any("window" in r.message for r in caplog.records)
```

Add to `tests/test_doctor.py`:

```python
def test_doctor_reports_over_window_chunks(tmp_path, monkeypatch):
    from mcpbrain.store import Store

    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    store = Store(tmp_path / "b.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("d1", "y" * 3000, "h1", {"source_type": "gdrive"})

    assert store.count_chunks_longer_than(2000) == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_embed.py tests/test_doctor.py -q -p no:randomly`
Expected: `ImportError: cannot import name 'EMBED_WINDOW_CHARS'`.

- [ ] **Step 3: Implement**

`index.py:60-67` currently reads:

```python
            texts = [
                (contextual_prefix(c["metadata"]) + c["text"]) if use_prefix else c["text"]
                for c in batch
            ]
            vectors = embedder.embed_passages(texts)
```

Add `EMBED_WINDOW_CHARS = 2000` at module level (plus `log = logging.getLogger(__name__)` if absent), and insert between those two statements:

```python
            oversize = sum(1 for t in texts if len(t) > EMBED_WINDOW_CHARS)
            if oversize:
                # The BGE window is 512 tokens ≈ 2,000 characters; anything
                # longer is silently truncated by the model and its tail is
                # unsearchable. 15,576 such chunks existed in the live store,
                # uncounted and unlogged (B3). This measures the PREFIXED text:
                # contextual_retrieval is default ON and its prefix eats into
                # the same window, which is part of why chunks sized right at
                # 2,000 chars still overflowed.
                log.warning("index: %d of %d passages exceed the %d-char embedder "
                            "window; their tails will not be searchable",
                            oversize, len(texts), EMBED_WINDOW_CHARS)
```

In `doctor.py`, add a line alongside the existing store checks — read how the neighbouring checks build their line and match that style:

```python
    oversize = store.count_chunks_longer_than(2000)
    lines.append(f"{'✅' if not oversize else '⚠️'} chunks over the embedder "
                 f"window: {oversize}")
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_embed.py tests/test_doctor.py tests/test_index_bounded.py tests/test_fts_reembed.py -q -p no:randomly`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/index.py mcpbrain/doctor.py tests/test_embed.py tests/test_doctor.py
git commit -m "feat(index): count and report passages that exceed the embedder window

15,576 live chunks are silently truncated by the 512-token BGE model, tails
unsearchable, neither logged nor counted. chunk_text now prevents new ones; the
remainder is the enriched semantic doc, which stays whole because splitting it
would break the enriched-<thread_id> doc_id."
```

---

## Findings index

| Finding | Issue | Task |
|---|---|---|
| A1 | Email attachments never ingested — CRITICAL | 5 |
| A2 | .pptx / CSV / TSV advertised but dropped; .doc, .xls, .eml, images, .zip dropped silently | 2 (.pptx, .xls, .eml extractors), 4 (MIME routing + reporting). `.doc`/`.ppt`/iWork/`.zip`/images **declined permanently** — see "Nothing is deferred". |
| A3 | Bottom-posted replies discarded | 5 |
| A4 | Bulk mail dropped but counted as processed | 5 — the drop is **deleted**, the signal moves into `should_enrich` |
| A5 | Scanned PDFs vanish without a trace; `is_scanned_pdf` dead | 2 |
| B1 | Spreadsheets keep empty rows, cap real rows at 200 — CRITICAL | 2 |
| B2 | CSV / Google Sheets split mid-row with no header | 2 (renderer), 4 (routing) |
| B3 | 15,576 chunks exceed the embedder window | 1 (`chunk_text` bounded), 3 (semantic doc bounded at build time — the one population chunk_text cannot reach), 6 (counter, as regression detector) |
| B4 | Thread expansion emits scrambled paragraph order | 3 |
| B5 | Stale chunks orphaned when a document shrinks | 4 |
| B6 | `chunk_text` emits empty and oversize chunks | 1 |
| B7 | Eight silent `except Exception: return ""` | 1 (seam), 2 (logging), 4 (classification) |
| B8 | Enrichment presents a partial document as whole | 3 |
| C1 | No chunk records its document's chunk count | 4 (drive, calendar), 5 (gmail) |
| C2 | Enriched chunks date-blind — 21,162 chunks | 3 |
| C3 | Enriched chunks lose the message-level link | 3 |
| C4 | Calendar-derived chunks mislabelled as Gmail | 3 |
| C5 | `folder_path` read but never written | 4 |
| C6 | Recipient lists truncated at 300 chars | 5 |
| C7 | 9,353 files ingested by the old extractor | **spec 3** — re-extraction, not a code fix |
| D | 96,335 redundant copies (54% of the store) | **spec 3** — purge; Task 1's `has_content` prevents the dominant cause recurring |
| E | Calendar events never chunked | 4 |

---

## Notes for the implementer

- **Gates are barriers; tasks within a gate are not.** Launch all tasks in a wave at once. They own disjoint files by construction — if you find yourself needing to edit a file another task in your wave owns, stop and raise it rather than editing it.
- **The tests in this plan are the specification.** Where a step gives both test and implementation code, the test is authoritative.
- **Revert-and-confirm on every new test.** Undo the fix, confirm the test fails, restore. Report it in the task summary. The daemon-scheduling work shipped three tests that passed against the defects they named; that is worse than no test, because it reads as coverage.
- **Existing expectations WILL break in Tasks 2, 4 and 5** (markdown XLSX output, `to[:300]`, possibly `gmail_enriched_v2` in Task 3). Update each with a comment saying why the old expectation was wrong. Never loosen an assertion — if you cannot say why the old expectation was wrong, stop and ask.
- **Expect the store to grow slightly**, not shrink: this adds attachments and recovers clipped spreadsheet rows while preventing new content-free chunks. It does not delete the 66,653 that already exist. The net shrink is spec 3's.
- **The gold eval is not a gate here.** Nothing re-embeds, so recall@10 / MRR should be unchanged; `uv run python tests/eval/run_eval.py --gold --k 10` should still report 0.750 / 0.556. It becomes the gate in spec 3.
- **Do not push and do not release.**
