# Extraction Fidelity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract PDF/DOCX/PPTX/RTF/Google Docs/Slides with their structure intact, stop the chunker collapsing line breaks, and re-chunk the existing corpus in the background without discarding enrichment.

**Architecture:** Extractors return an ordered list of `Block`s (heading / paragraph / table) that one renderer (`sync/blocks.py`) turns into chunks carrying a heading trail and their source spans. `chunk_text` gains a line → sentence → word fallback that is byte-identical when no paragraph overflows. A `reflow` queue source in the existing `sync_queue` re-fetches owners whose chunks predate the new versions, proves the source unchanged, and applies a single-transaction carry-over (`reflow.plan` + `Store.apply_reflow`) that inherits enrichment where the new text is provably covered and remaps every doc_id reference in place.

**Tech Stack:** Python 3.12, SQLite (+ sqlite-vec, FTS5), pymupdf 1.27, python-docx, python-pptx, pytest (xdist `-n auto`).

**Spec:** `docs/superpowers/specs/2026-09-24-extraction-fidelity-design.md`

## Global Constraints

- `CHUNKER_VERSION` stays **3**. Do not bump it (it gates the table pipeline and `bin/repair.py`).
- New constants: `chunking.SPLIT_VERSION = 1`; `sync/blocks.EXTRACTION_VERSIONS` maps these MIME types to `1`: `application/pdf`, `application/vnd.openxmlformats-officedocument.wordprocessingml.document`, `application/vnd.openxmlformats-officedocument.presentationml.presentation`, `application/rtf`, `application/vnd.google-apps.document`, `application/vnd.google-apps.presentation`.
- Chunk metadata keys stamped: `split_version` (every source), `extraction_version` (block-extracted MIMEs), `heading_trail` (block-rendered chunks, `" › "`-joined, omitted when empty).
- No new runtime dependency. RTF decoding is in-tree.
- Chunk budget for prose is `chunking.prose_max_chars()` = 1800 (what `chunk_text()` computes by default).
- Reflow per-cycle cap: at most **10 items** and **15 s** per `run_sync_cycle`. Seed window: **200** queued reflow items. Backup gate: last successful backup within **86400 s**.
- Reflow rows use `modified_at = "1970-01-01T00:00:00"` so real sync work always sorts first.
- Kill switch: `config.reflow_enabled(home)` = `fleet_flag(home, "reflow_enabled", True)`.
- No real people's names or name-derived ids in any committed file, test or commit message. Test fixtures use the neutral cast (Dana Okafor, Marcus Reyes, Priya Anand; Northgate Trust).
- Tests use a REAL `Store` (`Store(tmp_path / "a.sqlite3", dim=4); s.init()`), not fakes, for anything touching the store.
- Run scoped tests only: `uv run pytest <files> -q -n0`. Josh runs the full suite.
- Commit after every task on `main` (`git add <files>; git commit`), with the session attribution lines. Do not push or release; release is a separate, explicit step.

## Review Focus

1. **Repeated boilerplate text** (the same paragraph appearing in several old chunks, e.g. a per-page disclaimer). A remap must not send every old id to the first occurrence. Pinned by `test_plan_repeated_text_maps_monotonically` in Task 9.
2. **An owner whose old chunk sequence has a hole** (index 3 missing). Stitching must not crash; text past the hole is uncovered, so it is re-enriched rather than wrongly inherited. Pinned by `test_plan_gap_in_old_chunks_is_safe` in Task 9.
3. **An owner whose old chunks were all cold** (`enrich_state='cold'`). They must stay cold after reflow, never flip hot and flood enrichment. Pinned by `test_apply_reflow_preserves_cold` in Task 10.
4. **A Gmail message that now 404s during reflow.** The item completes, the existing chunks are kept and stamped so the selector stops choosing them. Pinned by `test_reflow_gmail_404_stamps_and_completes` in Task 11.
5. **A Drive file edited between seeding and reflow** (`modifiedTime` moved). It must take the ordinary change path, never the carry-over. Pinned by `test_reflow_drive_changed_file_takes_normal_path` in Task 11.
6. **A re-extraction that now yields nothing** (a transient extractor failure) for an owner that has chunks. Nothing may be deleted; the item fails and retries. Pinned by `test_reflow_empty_extraction_fails_without_deleting` in Task 11.

---

### Task 1: Line-aware `chunk_text` fallback

**Files:**
- Modify: `mcpbrain/chunking.py` (add `SPLIT_VERSION`, `prose_max_chars`, `split_long_paragraph`; change one call in `chunk_text`)
- Create: `tests/oracles/__init__.py` (empty), `tests/oracles/chunking_v0.py`
- Test: `tests/test_chunking_split.py`

**Interfaces:**
- Produces: `chunking.SPLIT_VERSION: int = 1`; `chunking.prose_max_chars(max_tokens: int = 500) -> int`; `chunking.split_long_paragraph(para: str, max_chars: int, overlap: int = 50) -> list[str]`. `chunk_text` signature unchanged.

- [ ] **Step 1: Freeze the current implementation as a test oracle**

Copy `_hard_split`, `_split_paragraph` and `chunk_text` from `mcpbrain/chunking.py` **verbatim** (current `main`) into `tests/oracles/chunking_v0.py`, with these imports at the top and `_PREFIX_HEADROOM_CHARS` taken from the live module:

```python
"""FROZEN copy of chunking.chunk_text as of 0.7.131 — the oracle for the
byte-identity property in tests/test_chunking_split.py. Never edit."""
from mcpbrain.chunking import _PREFIX_HEADROOM_CHARS  # noqa: F401  (used by chunk_text)
```

Rename the copied `chunk_text` to `chunk_text_v0`. Leave the other two names unchanged.

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_chunking_split.py
import random
import string

from mcpbrain import chunking
from mcpbrain.chunking import chunk_text, split_long_paragraph, prose_max_chars
from tests.oracles.chunking_v0 import chunk_text_v0


def _para(rng, max_len):
    words = []
    while True:
        w = "".join(rng.choice(string.ascii_lowercase) for _ in range(rng.randint(1, 9)))
        if sum(len(x) + 1 for x in words) + len(w) >= max_len:
            break
        words.append(w)
    return " ".join(words) or "x"


def test_byte_identical_when_no_paragraph_overflows():
    rng = random.Random(1234)
    cap = prose_max_chars()
    for _ in range(300):
        paras = [_para(rng, rng.randint(5, cap - 1)) for _ in range(rng.randint(1, 8))]
        text = "\n\n".join(paras)
        assert chunk_text(text) == chunk_text_v0(text)


def test_prose_max_chars_is_1800():
    assert prose_max_chars() == 1800


def test_long_paragraph_splits_on_lines_and_keeps_newlines():
    lines = [f"Line {i}: " + "word " * 30 for i in range(40)]   # ~160 chars each
    para = "\n".join(lines)
    pieces = split_long_paragraph(para, 1800, overlap=0)
    assert len(pieces) > 1
    for p in pieces:
        assert len(p) <= 1800
        for line in p.split("\n"):
            assert line.strip() in {l.strip() for l in lines}
    # nothing lost, nothing reordered
    rebuilt = [l for p in pieces for l in p.split("\n")]
    assert rebuilt == lines


def test_overlap_carries_whole_trailing_lines():
    lines = [f"L{i} " + "w " * 20 for i in range(100)]
    pieces = split_long_paragraph("\n".join(lines), 600, overlap=50)
    for a, b in zip(pieces, pieces[1:]):
        assert b.split("\n")[0] in a.split("\n")   # seed line came from previous piece


def test_single_huge_line_falls_back_to_sentences_then_words():
    sent = "This is a sentence about the budget. "
    line = sent * 200                               # one line, ~7,400 chars
    pieces = split_long_paragraph(line, 1800, overlap=0)
    assert all(len(p) <= 1800 for p in pieces)
    assert all(p.rstrip().endswith(".") for p in pieces[:-1])   # sentence seams
    blob = "x" * 5000                               # no separators at all
    pieces = split_long_paragraph(blob, 1800, overlap=0)
    assert "".join(pieces) == blob


def test_chunk_text_no_longer_collapses_newlines_in_long_paragraph():
    para = "\n".join(f"Row {i} | value {i}" for i in range(300))   # > budget, no blank lines
    out = chunk_text(para)
    assert len(out) > 1
    assert all("\n" in c for c in out)


def test_split_version_constant():
    assert chunking.SPLIT_VERSION == 1
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_chunking_split.py -q -n0`
Expected: FAIL with `ImportError: cannot import name 'split_long_paragraph'`.

- [ ] **Step 4: Implement**

In `mcpbrain/chunking.py`, next to `CHUNKER_VERSION`:

```python
# Version of chunk_text's over-budget paragraph splitter. 0 (absent) = the
# word-split that collapsed every newline via para.split(); 1 = the
# line -> sentence -> word fallback (2026-09-24 extraction-fidelity spec).
# Stamped as metadata['split_version']; the reflow selector reads it. It is
# deliberately separate from CHUNKER_VERSION, which gates the table pipeline.
SPLIT_VERSION = 1

_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+")
```

After `_split_paragraph`, add:

```python
def prose_max_chars(max_tokens: int = 500) -> int:
    """The per-chunk character budget chunk_text() uses for `max_tokens`."""
    max_chars = max_tokens * 4
    if max_chars >= _PREFIX_HEADROOM_CHARS * 4:
        max_chars -= _PREFIX_HEADROOM_CHARS
    return max_chars


def _units(para: str, max_chars: int) -> list[tuple[str, str]]:
    """(unit, separator-before-next) pairs, each unit <= max_chars: lines first,
    then sentences inside an over-long line, then words inside an over-long
    sentence. The separator is what joins this unit to the following one."""
    out: list[tuple[str, str]] = []
    for line in para.split("\n"):
        if len(line) <= max_chars:
            out.append((line, "\n"))
            continue
        sentences = _SENTENCE_BREAK.split(line)
        for s in sentences:
            if len(s) <= max_chars:
                out.append((s, " "))
            else:
                out.extend((w, " ") for w in _split_paragraph(s, max_chars, 0))
        if out:
            out[-1] = (out[-1][0], "\n")
    return [(u, sep) for u, sep in out if u != ""] or [("", "\n")]


def split_long_paragraph(para: str, max_chars: int, overlap: int = 50) -> list[str]:
    """Split ONE paragraph larger than max_chars without collapsing newlines.

    Packs line/sentence/word units (see _units) greedily. When a piece is
    flushed, the next one is seeded with whole trailing units of the flushed
    piece totalling at most `overlap` words — whole units only, so a seed
    never starts mid-line — and only when seed + next unit still fit.
    """
    units = _units(para, max_chars)
    pieces: list[str] = []
    cur: list[tuple[str, str]] = []

    def text_of(us):
        return "".join(u + (sep if i < len(us) - 1 else "")
                       for i, (u, sep) in enumerate(us))

    for unit in units:
        trial = cur + [unit]
        if not cur or len(text_of(trial)) <= max_chars:
            cur = trial
            continue
        pieces.append(text_of(cur))
        seed: list[tuple[str, str]] = []
        words = 0
        for u in reversed(cur):
            words += len(u[0].split())
            if words > overlap:
                break
            seed.insert(0, u)
        if seed and len(text_of(seed + [unit])) <= max_chars and seed != cur:
            cur = seed + [unit]
        else:
            cur = [unit]
    if cur:
        pieces.append(text_of(cur))
    return [p for p in pieces if p.strip()]
```

In `chunk_text`, replace exactly this line:

```python
            pieces = _split_paragraph(para, max_chars, overlap)
```

with:

```python
            pieces = split_long_paragraph(para, max_chars, overlap)
```

Update `chunk_text`'s docstring to add: "An over-budget paragraph is split by `split_long_paragraph` (lines → sentences → words); newlines are never collapsed. Output is byte-identical to the pre-SPLIT_VERSION splitter whenever no paragraph exceeds the budget."

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_chunking_split.py tests/test_chunking.py -q -n0`
Expected: PASS. If a pre-existing test in `tests/test_chunking.py` asserted the old word-level overlap exactly (e.g. `test_word_split_chunks_overlap_and_lose_nothing`), keep its intent (no chunk empty, none over budget, no words lost) and update only the literal expectation, in this commit, with a comment citing SPLIT_VERSION.

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/chunking.py tests/oracles tests/test_chunking_split.py tests/test_chunking.py
git commit -m "feat(chunking): line -> sentence -> word fallback that never collapses newlines (SPLIT_VERSION 1)"
```

---

### Task 2: Blocks and the block renderer

**Files:**
- Create: `mcpbrain/sync/blocks.py`
- Modify: `mcpbrain/sync/normalise.py:14-19` (`Chunk` gains `spans`)
- Test: `tests/test_blocks.py`

**Interfaces:**
- Consumes: `chunking.split_long_paragraph`, `chunking.prose_max_chars`, `chunking.has_content`, `tabular.normalise_rows`, `tabular._fit_row_sentence`.
- Produces:
  - `Heading(level: int, text: str)`, `Paragraph(text: str)`, `TableBlock(rows: list[list[str]], caption: str = "")` dataclasses; `PartialBlocks(list)`.
  - `EXTRACTION_VERSIONS: dict[str, int]`; `extraction_version(mime: str) -> int`.
  - `to_text(blocks) -> str` (same `PartialText` semantics: returns `PartialText` when given `PartialBlocks`).
  - `from_text(text: str) -> list[Block]` (blank-line paragraphs; `#`-prefixed lines become headings).
  - `Rendered(text: str, meta: dict, spans: list[str])`; `render(blocks, *, max_chars: int | None = None) -> list[Rendered]`.
  - `normalise.Chunk` gains `spans: list[str] = field(default_factory=list)` (never persisted).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_blocks.py
from mcpbrain.sync.blocks import (
    Heading, Paragraph, TableBlock, PartialBlocks, render, to_text, from_text,
    extraction_version,
)
from mcpbrain.sync.extractors import is_partial


def test_extraction_versions():
    assert extraction_version("application/pdf") == 1
    assert extraction_version("text/plain") == 0


def test_short_document_is_one_chunk_joined_by_blank_lines():
    blocks = [Heading(1, "Budget"), Paragraph("Line one\nLine two"), Paragraph("Second")]
    out = render(blocks)
    assert len(out) == 1
    assert out[0].text == "Budget\n\nLine one\nLine two\n\nSecond"
    assert out[0].meta["heading_trail"] == "Budget"
    assert out[0].spans == ["Budget", "Line one\nLine two", "Second"]


def test_heading_trail_nests_and_pops():
    body = "word " * 300
    blocks = [Heading(1, "A"), Paragraph(body), Heading(2, "A1"), Paragraph(body),
              Heading(1, "B"), Paragraph(body)]
    trails = [r.meta.get("heading_trail") for r in render(blocks)]
    assert "A › A1" in trails
    assert trails[-1] == "B"


def test_heading_flushes_a_more_than_half_full_chunk():
    blocks = [Paragraph("x " * 500), Heading(1, "Next"), Paragraph("y " * 100)]
    out = render(blocks)
    assert out[1].text.startswith("Next")


def test_table_renders_as_row_sentences_with_cells_as_spans():
    t = TableBlock([["Item", "Cost"], ["Chairs", "120"], ["", "5"]], caption="")
    out = render([Heading(1, "Capital works"), t])
    joined = "\n".join(r.text for r in out)
    assert "Item: Chairs" in joined and "Cost: 120" in joined
    assert "Capital works" in joined
    spans = [s for r in out for s in r.spans]
    assert "Chairs" in spans and "120" in spans
    assert "Item: Chairs" not in spans          # synthesised text is not a span


def test_oversize_paragraph_is_split_without_collapsing_lines():
    para = "\n".join(f"Row {i} value" for i in range(400))
    out = render([Paragraph(para)])
    assert len(out) > 1
    assert all(len(r.text) <= 1800 for r in out)
    assert all("\n" in r.text for r in out)


def test_to_text_and_partial():
    b = PartialBlocks([Paragraph("a"), TableBlock([["h1", "h2"], ["x", "y"]])])
    t = to_text(b)
    assert is_partial(t)
    assert "a" in t and "x | y" in t


def test_from_text_headings_and_paragraphs():
    blocks = from_text("# Title\n\nBody one\nstill one\n\n## Sub\n\nBody two")
    assert blocks == [Heading(1, "Title"), Paragraph("Body one\nstill one"),
                      Heading(2, "Sub"), Paragraph("Body two")]


def test_empty_and_contentless_blocks_are_dropped():
    assert render([Paragraph("   "), Paragraph("---")]) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_blocks.py -q -n0`
Expected: FAIL with `ModuleNotFoundError: mcpbrain.sync.blocks`.

- [ ] **Step 3: Implement**

`mcpbrain/sync/normalise.py` — change the dataclass import and `Chunk`:

```python
from dataclasses import dataclass, field


@dataclass
class Chunk:
    doc_id: str
    text: str
    content_hash: str
    metadata: dict
    # Source text this chunk carries, excluding renderer-synthesised text
    # (table captions, row-sentence column labels). In-memory only — never
    # written to the store. reflow.plan uses it to prove coverage. Empty
    # means "the whole text is source text".
    spans: list[str] = field(default_factory=list)
```

`mcpbrain/sync/blocks.py`:

```python
"""Structured extraction output and the one renderer that chunks it.

Extractors (sync/extractors.py) return an ordered list of Blocks instead of a
flat string, so reading order, paragraphs, headings and tables survive to the
chunker (2026-09-24 extraction-fidelity spec). `render` is the single place a
Block list becomes chunks; `to_text` is the flat-string view kept for callers
that only want text.
"""
import re
from dataclasses import dataclass, field

from mcpbrain.chunking import has_content, prose_max_chars, split_long_paragraph
from mcpbrain.sync import tabular

# Bump a MIME's number whenever its extractor's OUTPUT changes; the reflow
# selector re-chunks every owner of that MIME below the new number, and the
# shared-drive ingest-cache fingerprint includes it.
EXTRACTION_VERSIONS: dict[str, int] = {
    "application/pdf": 1,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": 1,
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": 1,
    "application/rtf": 1,
    "application/vnd.google-apps.document": 1,
    "application/vnd.google-apps.presentation": 1,
}

TRAIL_SEP = " › "


def extraction_version(mime: str) -> int:
    return EXTRACTION_VERSIONS.get(mime or "", 0)


@dataclass
class Heading:
    level: int
    text: str


@dataclass
class Paragraph:
    text: str


@dataclass
class TableBlock:
    rows: list[list[str]]
    caption: str = ""


class PartialBlocks(list):
    """list[Block] from an extraction that died partway (see extractors.PartialTables)."""


@dataclass
class Rendered:
    text: str
    meta: dict = field(default_factory=dict)
    spans: list[str] = field(default_factory=list)


def _table_line(row: list[str]) -> str:
    return " | ".join(c for c in row)


def to_text(blocks) -> str:
    from mcpbrain.sync.extractors import PartialText
    parts: list[str] = []
    for b in blocks:
        if isinstance(b, (Heading, Paragraph)):
            if b.text.strip():
                parts.append(b.text.strip())
        elif isinstance(b, TableBlock):
            lines = [_table_line(r) for r in b.rows if any(c.strip() for c in r)]
            if lines:
                parts.append("\n".join(lines))
    text = "\n\n".join(parts)
    return PartialText(text) if isinstance(blocks, PartialBlocks) else text


_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


def from_text(text: str) -> list:
    out: list = []
    for para in re.split(r"\n\s*\n", text or ""):
        para = para.strip("\n")
        if not para.strip():
            continue
        lines = para.split("\n")
        m = _MD_HEADING.match(lines[0].strip())
        if m and len(lines) == 1:
            out.append(Heading(len(m.group(1)), m.group(2).strip()))
        else:
            out.append(Paragraph(para))
    return out


@dataclass
class _Piece:
    text: str
    spans: list[str]
    kind: str            # "heading" | "para" | "table"
    trail: str


def _table_pieces(t: TableBlock, trail: str, max_chars: int) -> list[_Piece]:
    rows = tabular.normalise_rows([[("" if c is None else str(c)).strip() for c in r]
                                   for r in t.rows])
    if not rows:
        return []
    if len(rows) == 1:
        line = _table_line(rows[0])
        return [_Piece(line, [c for c in rows[0] if c], "table", trail)] if has_content(line) else []
    header, body = rows[0], rows[1:]
    where = f" (in {trail})" if trail else ""
    caption = f"Table{': ' + t.caption if t.caption else ''}{where}"
    budget = max_chars - len(caption) - 1
    pieces, cur, cur_cells = [], [], []
    for r in body:
        sent = tabular._fit_row_sentence(header, r, budget)
        if cur and len("\n".join(cur + [sent])) > budget:
            pieces.append(_Piece(caption + "\n" + "\n".join(cur), cur_cells, "table", trail))
            cur, cur_cells = [], []
        cur.append(sent)
        cur_cells.extend(c for c in r if c)
    if cur:
        pieces.append(_Piece(caption + "\n" + "\n".join(cur), cur_cells, "table", trail))
    # header cells are source text too: attribute them to the first piece
    if pieces:
        pieces[0].spans = [c for c in header if c] + pieces[0].spans
    return pieces


def render(blocks, *, max_chars: int | None = None) -> list[Rendered]:
    max_chars = max_chars or prose_max_chars()
    trail: list[tuple[int, str]] = []
    pieces: list[_Piece] = []
    for b in blocks:
        tr = TRAIL_SEP.join(t for _, t in trail)
        if isinstance(b, Heading):
            text = b.text.strip()
            if not has_content(text):
                continue
            while trail and trail[-1][0] >= b.level:
                trail.pop()
            trail.append((b.level, text[:120]))
            pieces.append(_Piece(text[:max_chars], [text[:max_chars]], "heading",
                                 TRAIL_SEP.join(t for _, t in trail)))
        elif isinstance(b, Paragraph):
            text = b.text.strip("\n")
            if not has_content(text):
                continue
            parts = [text] if len(text) <= max_chars else split_long_paragraph(text, max_chars)
            pieces.extend(_Piece(p, [p], "para", tr) for p in parts)
        elif isinstance(b, TableBlock):
            pieces.extend(_table_pieces(b, tr, max_chars))

    total = sum(len(p.text) + 2 for p in pieces)
    single = total - 2 <= max_chars       # one-chunk documents never split on headings
    out: list[Rendered] = []
    cur: list[_Piece] = []

    def flush():
        if not cur:
            return
        text = "\n\n".join(p.text for p in cur)
        meta = {}
        tr = cur[0].trail
        if tr:
            meta["heading_trail"] = tr[:300]
        out.append(Rendered(text, meta, [s for p in cur for s in p.spans]))
        cur.clear()

    for p in pieces:
        size = sum(len(x.text) + 2 for x in cur) + len(p.text)
        if cur and (size > max_chars
                    or (not single and p.kind == "heading" and size - len(p.text) > max_chars // 2)
                    or (p.kind == "table" and cur[-1].kind != "table" and size > max_chars)):
            flush()
        cur.append(p)
    flush()
    return [r for r in out if has_content(r.text)]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_blocks.py tests/test_normalise.py -q -n0`
Expected: PASS (`test_normalise.py` proves the `Chunk` change is backward compatible; if that file does not exist, run `uv run pytest tests -q -n0 -k normalise`).

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/sync/blocks.py mcpbrain/sync/normalise.py tests/test_blocks.py
git commit -m "feat(blocks): structured extraction blocks and one renderer with heading trail and source spans"
```

---

### Task 3: PDF → blocks (reading order, headings, tables)

**Files:**
- Modify: `mcpbrain/sync/extractors.py` (add `extract_blocks_from_pdf`; `extract_text_from_pdf` becomes `to_text(extract_blocks_from_pdf(...))`)
- Test: `tests/test_extract_blocks_pdf.py`

**Interfaces:**
- Consumes: `blocks.Heading/Paragraph/TableBlock/PartialBlocks/to_text/from_text`.
- Produces: `extract_blocks_from_pdf(content_bytes: bytes) -> list` (a `PartialBlocks` when extraction died partway; `[]` on open failure). `extract_text_from_pdf` keeps its signature and contract (returns `str`, `""` on failure).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_extract_blocks_pdf.py
import fitz

from mcpbrain.sync.blocks import Heading, Paragraph, TableBlock
from mcpbrain.sync.extractors import extract_blocks_from_pdf, extract_text_from_pdf

LEFT = "Left column talks about the Northgate Trust grant and its reporting dates. " * 3
RIGHT = "Right column covers the Southbank hall booking and cleaning roster. " * 3


def _two_column_pdf() -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    # Insert the RIGHT column first so content-stream order is wrong.
    page.insert_textbox(fitz.Rect(310, 72, 560, 400), RIGHT, fontsize=10)
    page.insert_textbox(fitz.Rect(40, 72, 290, 400), LEFT, fontsize=10)
    return doc.tobytes()


def _heading_pdf() -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Annual Report", fontsize=22)
    page.insert_textbox(fitz.Rect(72, 90, 520, 300),
                        "The year in review for every campus and ministry. " * 4, fontsize=10)
    return doc.tobytes()


def _table_pdf() -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_textbox(fitz.Rect(72, 40, 520, 70), "Capital works budget below.", fontsize=10)
    rows = [["Item", "Cost"], ["Chairs", "120"], ["Lighting", "900"]]
    x0, y0, w, h = 72, 100, 150, 24
    for r, row in enumerate(rows):
        for c, val in enumerate(row):
            rect = fitz.Rect(x0 + c * w, y0 + r * h, x0 + (c + 1) * w, y0 + (r + 1) * h)
            page.draw_rect(rect, color=(0, 0, 0), width=0.8)
            page.insert_text((rect.x0 + 4, rect.y0 + 16), val, fontsize=10)
    return doc.tobytes()


def test_two_columns_come_out_in_reading_order():
    text = extract_text_from_pdf(_two_column_pdf())
    assert text.index("Left column") < text.index("Right column")


def test_large_font_line_becomes_heading():
    blocks = extract_blocks_from_pdf(_heading_pdf())
    assert isinstance(blocks[0], Heading) and blocks[0].text == "Annual Report"
    assert any(isinstance(b, Paragraph) and "year in review" in b.text for b in blocks)


def test_ruled_table_becomes_table_block_without_duplicate_text():
    blocks = extract_blocks_from_pdf(_table_pdf())
    tables = [b for b in blocks if isinstance(b, TableBlock)]
    assert tables, blocks
    assert ["Chairs", "120"] in tables[0].rows
    paras = " ".join(b.text for b in blocks if isinstance(b, Paragraph))
    assert "Chairs" not in paras
    assert "Capital works budget" in paras


def test_garbage_is_empty():
    assert extract_blocks_from_pdf(b"not a pdf") == []
    assert extract_text_from_pdf(b"not a pdf") == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_extract_blocks_pdf.py -q -n0`
Expected: FAIL with `ImportError: cannot import name 'extract_blocks_from_pdf'`.

- [ ] **Step 3: Implement**

In `mcpbrain/sync/extractors.py`, add near the imports:

```python
from mcpbrain.sync.blocks import Heading, Paragraph, PartialBlocks, TableBlock, from_text, to_text
```

(If that creates an import cycle — `blocks.to_text` imports `PartialText` lazily inside the function, so it should not — keep the lazy import.)

Add below `_is_scanned_pages`:

```python
_HEADING_RATIO = 1.2       # span size >= 1.2x the body size reads as a heading
_HEADING_MAX_CHARS = 200


def _page_blocks(page) -> tuple[list[tuple[float, object]], list[float]]:
    """(y0, Block) pairs for one page in reading order, plus span sizes weighted
    by character count (for the document's body size)."""
    tables = []
    try:
        for t in page.find_tables().tables:
            rows = [[("" if c is None else str(c)).strip() for c in r] for r in t.extract()]
            if rows:
                tables.append((fitz_rect(t.bbox), TableBlock(rows)))
    except Exception as exc:  # noqa: BLE001 — table detection is best-effort
        log.debug("pdf: find_tables failed on page %s: %s", page.number, exc)
    out: list[tuple[float, object]] = []
    sizes: list[float] = []
    d = page.get_text("dict", sort=True)
    for blk in d.get("blocks", []):
        if blk.get("type") != 0:
            continue
        x0, y0, x1, y1 = blk["bbox"]
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        if any(r.x0 <= cx <= r.x1 and r.y0 <= cy <= r.y1 for r, _ in tables):
            continue                                   # text belongs to a table
        lines, max_size = [], 0.0
        for ln in blk.get("lines", []):
            txt = "".join(s.get("text", "") for s in ln.get("spans", []))
            for s in ln.get("spans", []):
                n = len(s.get("text", "").strip())
                sizes.extend([float(s.get("size", 0))] * max(n, 0))
                max_size = max(max_size, float(s.get("size", 0)))
            if txt.strip():
                lines.append(txt.rstrip())
        if lines:
            out.append((y0, ("_text", "\n".join(lines), max_size)))
    for r, tb in tables:
        out.append((r.y0, tb))
    out.sort(key=lambda p: p[0])
    return out, sizes


def fitz_rect(bbox):
    import fitz  # pymupdf
    return fitz.Rect(bbox)


def extract_blocks_from_pdf(content_bytes: bytes) -> list:
    """PDF -> Blocks in reading order (`sort=True`), headings by font size,
    ruled tables via find_tables(). Scanned pages keep the tesseract fallback
    and become Paragraphs. [] on open failure; PartialBlocks if extraction
    died partway."""
    try:
        import fitz  # pymupdf
        doc = fitz.open(stream=content_bytes, filetype="pdf")
    except Exception as exc:
        log.warning("pdf: open failed: %s", exc)
        return []
    out: list = []
    try:
        pages_text = [page.get_text() for page in doc]
        scanned = is_scanned_pdf(content_bytes, pages=pages_text)
        if scanned and not _tesseract_available():
            log.warning("pdf: looks scanned (%d pages, %d text chars) and "
                        "tesseract is unavailable — returning the text layer only",
                        len(pages_text), sum(len(p or "") for p in pages_text))
        per_page, all_sizes = [], []
        for i, page in enumerate(doc):
            if scanned and len((pages_text[i] or "").strip()) < _OCR_MIN_PAGE_CHARS \
                    and _tesseract_available():
                ocr = _ocr_page(page)
                if not ocr:
                    log.warning("pdf: OCR produced nothing for page %d", i + 1)
                per_page.append([(0.0, b) for b in from_text(ocr) if isinstance(b, Paragraph)]
                                or [])
                continue
            items, sizes = _page_blocks(page)
            per_page.append(items)
            all_sizes.extend(sizes)
        body = sorted(all_sizes)[len(all_sizes) // 2] if all_sizes else 0.0
        heading_sizes = sorted({round(s, 1) for items in per_page for _, b in items
                                if isinstance(b, tuple) and body and b[2] >= body * _HEADING_RATIO},
                               reverse=True)
        for items in per_page:
            for _, b in items:
                if isinstance(b, tuple):
                    _, text, size = b
                    if (body and size >= body * _HEADING_RATIO
                            and len(text) <= _HEADING_MAX_CHARS and "\n" not in text.strip()):
                        level = heading_sizes.index(round(size, 1)) + 1 if round(size, 1) in heading_sizes else 1
                        out.append(Heading(min(level, 6), text.strip()))
                    else:
                        out.append(Paragraph(text))
                else:
                    out.append(b)
        return out
    except Exception as exc:
        log.warning("pdf: extraction failed after %d blocks: %s", len(out), exc)
        return PartialBlocks(out) if out else []
    finally:
        doc.close()
```

Replace the body of `extract_text_from_pdf` (keep its docstring, adding one line "Now a flat view of extract_blocks_from_pdf.") with:

```python
    return to_text(extract_blocks_from_pdf(content_bytes))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_extract_blocks_pdf.py tests/test_extractors.py -q -n0`
Expected: PASS. Existing PDF tests in `tests/test_extractors.py` (OCR fallback, scanned detection, garbage) must still pass unchanged; if one asserted the old exact `"\n\n".join(pages)` string, change only the literal to the new output and note it in the commit body. If `test_ruled_table_becomes_table_block_without_duplicate_text` fails because `find_tables` does not detect the fixture, first print `page.find_tables().tables` in a scratch run, adjust the fixture lines (e.g. `width=1`), never the assertion. **If `test_two_columns_come_out_in_reading_order` fails, STOP and report: that is the spec's trigger for reconsidering pdf-inspector.**

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/sync/extractors.py tests/test_extract_blocks_pdf.py tests/test_extractors.py
git commit -m "feat(extract): PDF blocks in reading order with font-size headings and find_tables tables"
```

---

### Task 4: DOCX → blocks (body order, headings, merged cells, headers/footers, text boxes)

**Files:**
- Modify: `mcpbrain/sync/extractors.py` (`extract_blocks_from_docx`; `extract_text_from_docx` becomes `to_text(...)`)
- Test: `tests/test_extract_blocks_docx.py`

**Interfaces:**
- Produces: `extract_blocks_from_docx(content_bytes: bytes) -> list` (`[]` on failure).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_extract_blocks_docx.py
import io

from docx import Document

from mcpbrain.sync.blocks import Heading, Paragraph, TableBlock
from mcpbrain.sync.extractors import extract_blocks_from_docx, extract_text_from_docx


def _docx() -> bytes:
    doc = Document()
    doc.sections[0].header.paragraphs[0].text = "Northgate Trust — Confidential"
    doc.sections[0].footer.paragraphs[0].text = "Page footer"
    doc.add_heading("Quarterly review", level=1)
    doc.add_paragraph("Intro paragraph before the table.")
    t = doc.add_table(rows=3, cols=3)
    t.cell(0, 0).text, t.cell(0, 1).text, t.cell(0, 2).text = "Item", "Owner", "Due"
    t.cell(1, 0).text, t.cell(1, 1).text = "Roster", "Dana Okafor"
    t.cell(2, 0).text = "Merged"
    t.cell(2, 1).merge(t.cell(2, 2)).text = "Spans two"
    doc.add_heading("Next steps", level=2)
    doc.add_paragraph("After the table.")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def test_body_order_headings_and_table_in_place():
    blocks = extract_blocks_from_docx(_docx())
    kinds = [type(b).__name__ for b in blocks]
    assert blocks[0] == Paragraph("Northgate Trust — Confidential")
    assert Heading(1, "Quarterly review") in blocks
    ti = next(i for i, b in enumerate(blocks) if isinstance(b, TableBlock))
    intro = blocks.index(Paragraph("Intro paragraph before the table."))
    after = blocks.index(Paragraph("After the table."))
    assert intro < ti < after
    assert Heading(2, "Next steps") in blocks
    assert blocks[-1] == Paragraph("Page footer")
    assert kinds.count("TableBlock") == 1


def test_empty_cells_kept_and_merged_cells_once():
    t = next(b for b in extract_blocks_from_docx(_docx()) if isinstance(b, TableBlock))
    assert t.rows[1] == ["Roster", "Dana Okafor", ""]
    assert t.rows[2] == ["Merged", "Spans two"]


def test_text_view_and_garbage():
    assert "Quarterly review" in extract_text_from_docx(_docx())
    assert extract_blocks_from_docx(b"nope") == []
    assert extract_text_from_docx(b"nope") == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_extract_blocks_docx.py -q -n0`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement** (replace `extract_text_from_docx` in `extractors.py`)

```python
def _docx_heading_level(style_name: str) -> int:
    name = (style_name or "").strip()
    if name == "Title":
        return 1
    if name.startswith("Heading"):
        tail = name.split()[-1]
        return int(tail) if tail.isdigit() else 1
    return 0


def _docx_table_rows(table) -> list[list[str]]:
    rows = []
    for row in table.rows:
        seen, cells = set(), []
        for cell in row.cells:
            key = id(cell._tc)
            if key in seen:
                continue                     # a merged cell spans several grid slots
            seen.add(key)
            cells.append(cell.text.strip())
        rows.append(cells)
    return rows


def extract_blocks_from_docx(content_bytes: bytes) -> list:
    """DOCX -> Blocks walking the body IN ORDER (w:p and w:tbl), so tables sit
    where they appear. Heading/Title styles become Headings; text boxes follow
    their anchor paragraph; section headers first and footers last, each once."""
    try:
        from docx import Document
        from docx.oxml.ns import qn
        from docx.table import Table as DocxTable
        from docx.text.paragraph import Paragraph as DocxParagraph
        doc = Document(io.BytesIO(content_bytes))
    except Exception as exc:
        log.warning("docx: open failed: %s", exc)
        return []
    out: list = []
    try:
        headers, footers = [], []
        for section in doc.sections:
            for part, bucket in ((section.header, headers), (section.footer, footers)):
                text = "\n".join(p.text for p in part.paragraphs if p.text.strip()).strip()
                if text and text not in bucket:
                    bucket.append(text)
        out.extend(Paragraph(h) for h in headers)
        for child in doc.element.body.iterchildren():
            if child.tag == qn("w:p"):
                p = DocxParagraph(child, doc)
                text = p.text.strip()
                if text:
                    level = _docx_heading_level(p.style.name if p.style is not None else "")
                    out.append(Heading(level, text) if level else Paragraph(text))
                for box in child.iter(qn("w:txbxContent")):
                    box_text = "\n".join(
                        "".join(t.text or "" for t in bp.iter(qn("w:t")))
                        for bp in box.iter(qn("w:p"))).strip()
                    if box_text:
                        out.append(Paragraph(box_text))
            elif child.tag == qn("w:tbl"):
                rows = _docx_table_rows(DocxTable(child, doc))
                if any(any(c for c in r) for r in rows):
                    out.append(TableBlock(rows))
        out.extend(Paragraph(f) for f in footers)
        return out
    except Exception as exc:
        log.warning("docx: extraction failed after %d blocks: %s", len(out), exc)
        return PartialBlocks(out) if out else []


def extract_text_from_docx(content_bytes: bytes) -> str:
    """Flat text view of extract_blocks_from_docx ('' on failure)."""
    return to_text(extract_blocks_from_docx(content_bytes))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_extract_blocks_docx.py tests/test_extractors.py -q -n0`
Expected: PASS. The existing `_make_docx_bytes` test asserts "Revenue" and "Expenses" appear — still true (`"Revenue | Expenses"`).

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/sync/extractors.py tests/test_extract_blocks_docx.py
git commit -m "feat(extract): DOCX blocks in body order with headings, in-place tables, merged cells once, headers/footers and text boxes"
```

---

### Task 5: PPTX → blocks (groups, tables, speaker notes)

**Files:**
- Modify: `mcpbrain/sync/extractors.py` (`extract_blocks_from_pptx`; `extract_text_from_pptx` becomes `to_text(...)`)
- Test: `tests/test_extract_blocks_pptx.py`

**Interfaces:**
- Produces: `extract_blocks_from_pptx(content_bytes: bytes) -> list`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_extract_blocks_pptx.py
import io

from pptx import Presentation
from pptx.util import Inches

from mcpbrain.sync.blocks import Heading, Paragraph, TableBlock
from mcpbrain.sync.extractors import extract_blocks_from_pptx, extract_text_from_pptx, is_partial


def _pptx() -> bytes:
    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[5])          # title only
    s.shapes.title.text = "Q3 Ministry Review"
    group = s.shapes.add_group_shape()
    tb = group.shapes.add_textbox(Inches(1), Inches(2), Inches(4), Inches(1))
    tb.text_frame.text = "Inside a group"
    rows = s.shapes.add_table(2, 2, Inches(1), Inches(4), Inches(4), Inches(1)).table
    rows.cell(0, 0).text, rows.cell(0, 1).text = "Campus", "Attendance"
    rows.cell(1, 0).text, rows.cell(1, 1).text = "North", "140"
    s.notes_slide.notes_text_frame.text = "Mention the Northgate Trust grant."
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def test_slide_heading_group_table_and_notes():
    blocks = extract_blocks_from_pptx(_pptx())
    assert blocks[0] == Heading(2, "Slide 1: Q3 Ministry Review")
    assert Paragraph("Inside a group") in blocks
    assert TableBlock([["Campus", "Attendance"], ["North", "140"]]) in blocks
    assert blocks[-1] == Paragraph("Notes: Mention the Northgate Trust grant.")


def test_text_view_and_garbage():
    assert "Q3 Ministry Review" in extract_text_from_pptx(_pptx())
    assert extract_text_from_pptx(b"not a pptx") == ""
    assert not is_partial(extract_text_from_pptx(_pptx()))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_extract_blocks_pptx.py -q -n0`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement** (replace `extract_text_from_pptx`)

```python
def _pptx_walk(shapes, skip_id, out: list) -> None:
    from pptx.enum.shapes import MSO_SHAPE_TYPE
    for shape in shapes:
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            _pptx_walk(shape.shapes, skip_id, out)
            continue
        if shape.shape_id == skip_id:
            continue
        if getattr(shape, "has_text_frame", False) and shape.has_text_frame:
            lines = ["".join(r.text for r in p.runs).strip()
                     for p in shape.text_frame.paragraphs]
            text = "\n".join(line for line in lines if line)
            if text:
                out.append(Paragraph(text))
        if getattr(shape, "has_table", False) and shape.has_table:
            rows = [[c.text.strip() for c in row.cells] for row in shape.table.rows]
            if any(any(c for c in r) for r in rows):
                out.append(TableBlock(rows))


def extract_blocks_from_pptx(content_bytes: bytes) -> list:
    """PPTX -> Blocks: a Heading per slide ('Slide N: <title>'), shapes walked
    recursively through groups, tables as TableBlocks, and speaker notes as a
    trailing 'Notes: ' Paragraph."""
    try:
        from pptx import Presentation
        prs = Presentation(io.BytesIO(content_bytes))
    except Exception as exc:
        log.warning("pptx: presentation open failed: %s", exc)
        return []
    out: list = []
    try:
        for n, slide in enumerate(prs.slides, start=1):
            title_shape = slide.shapes.title
            title = title_shape.text_frame.text.strip() if title_shape is not None else ""
            out.append(Heading(2, f"Slide {n}: {title}" if title else f"Slide {n}"))
            _pptx_walk(slide.shapes,
                       title_shape.shape_id if title_shape is not None else None, out)
            if slide.has_notes_slide:
                notes = slide.notes_slide.notes_text_frame.text.strip()
                if notes:
                    out.append(Paragraph(f"Notes: {notes}"))
        return out
    except Exception as exc:
        log.warning("pptx: extraction failed after %d blocks: %s", len(out), exc)
        return PartialBlocks(out) if out else []


def extract_text_from_pptx(content_bytes: bytes) -> str:
    """Flat text view of extract_blocks_from_pptx ('' on failure; PartialText
    when extraction died partway — see PartialTables)."""
    return to_text(extract_blocks_from_pptx(content_bytes))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_extract_blocks_pptx.py tests/test_extractors.py tests/test_drive_sync.py -q -n0 -k "pptx or partial"`
Expected: PASS. The existing partial test (`tests/test_extractors.py` ~line 547, monkeypatching a failure mid-deck) must still see `is_partial(...) is True`; if it patched `Presentation` so no slide completes, `[]` now becomes `""` (not partial) — adjust that test so at least one slide completes before the injected failure, preserving its intent.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/sync/extractors.py tests/test_extract_blocks_pptx.py tests/test_extractors.py
git commit -m "feat(extract): PPTX blocks with grouped shapes, tables and speaker notes"
```

---

### Task 6: RTF decoder

**Files:**
- Create: `mcpbrain/sync/rtf.py`
- Test: `tests/test_rtf.py`

**Interfaces:**
- Produces: `rtf.rtf_to_text(data: bytes | str) -> str`; `extractors.extract_blocks_from_rtf(content_bytes: bytes) -> list` (= `from_text(rtf_to_text(...))`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_rtf.py
from mcpbrain.sync.rtf import rtf_to_text

SAMPLE = (r"{\rtf1\ansi\ansicpg1252\deff0{\fonttbl{\f0 Arial;}}{\colortbl;\red0\green0\blue0;}"
          r"{\*\generator Riched20;}{\info{\title Secret}}"
          r"\f0\fs20 Caf\'e9 budget\par Second line\line same para\par\par "
          r"Unicode \u8212? dash and \{braces\}\par}")


def test_decodes_text_and_skips_destinations():
    out = rtf_to_text(SAMPLE)
    assert "Café budget" in out
    assert "Second line\nsame para" in out
    assert "Unicode — dash and {braces}" in out
    for junk in ("Arial", "Riched20", "Secret", "rtf1", "\\"):
        assert junk not in out


def test_paragraph_breaks_become_blank_lines():
    out = rtf_to_text(SAMPLE)
    assert "\n\n" in out


def test_not_rtf_passes_through():
    assert rtf_to_text(b"plain words") == "plain words"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_rtf.py -q -n0`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
# mcpbrain/sync/rtf.py
"""Minimal RTF -> text decoder (no dependency).

Before 2026-09-24, RTF was fetched and utf-8-decoded verbatim, so control words
({\\rtf1\\ansi...}) were chunked and embedded. This handles what matters for
retrieval: groups, \\par/\\line, \\'hh (codepage), \\uN with its skip count,
escaped braces/backslash, and the destinations whose content is not document
text (fonttbl, colortbl, stylesheet, info, pict, and any \\* group).
"""
import re

_IGNORABLE = {"fonttbl", "colortbl", "stylesheet", "info", "pict", "header",
              "footer", "object", "themedata", "datastore", "latentstyles",
              "listtable", "listoverridetable", "rsidtbl", "generator"}
_TOKEN = re.compile(r"\\([a-zA-Z]+)(-?\d+)? ?|\\'([0-9a-fA-F]{2})|\\([{}\\*~_-])|([{}])|\r?\n|([^\\{}\r\n]+)")


def rtf_to_text(data) -> str:
    text = data.decode("latin-1") if isinstance(data, bytes) else data
    if not text.lstrip().startswith("{\\rtf"):
        return data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    codepage = "cp1252"
    out: list[str] = []
    stack: list[tuple[bool, int]] = []
    ignoring, uc, skip = False, 1, 0
    pending_star = False
    for m in _TOKEN.finditer(text):
        word, arg, hexv, esc, brace, plain = m.groups()
        if brace == "{":
            stack.append((ignoring, uc))
            pending_star = False
            continue
        if brace == "}":
            ignoring, uc = stack.pop() if stack else (False, 1)
            continue
        if esc == "*":
            ignoring = True
            continue
        if skip and (plain or hexv):
            if plain:
                take = min(skip, len(plain))
                skip -= take
                plain = plain[take:]
                if not plain:
                    continue
            else:
                skip -= 1
                continue
        if word:
            if word in _IGNORABLE:
                ignoring = True
            elif word == "ansicpg" and arg:
                codepage = f"cp{arg}"
            elif ignoring:
                pass
            elif word == "par":
                out.append("\n\n")
            elif word in ("line", "row"):
                out.append("\n")
            elif word in ("tab", "cell"):
                out.append("\t")
            elif word == "uc" and arg:
                uc = int(arg)
            elif word == "u" and arg:
                cp = int(arg)
                out.append(chr(cp + 65536 if cp < 0 else cp))
                skip = uc
            continue
        if ignoring:
            continue
        if hexv:
            out.append(bytes([int(hexv, 16)]).decode(codepage, "replace"))
        elif esc in ("{", "}", "\\"):
            out.append(esc)
        elif esc == "~":
            out.append(" ")
        elif plain:
            out.append(plain)
    del pending_star
    joined = "".join(out)
    joined = re.sub(r"[ \t]+\n", "\n", joined)
    joined = re.sub(r"\n{3,}", "\n\n", joined)
    return joined.strip()
```

In `extractors.py` add:

```python
def extract_blocks_from_rtf(content_bytes: bytes) -> list:
    from mcpbrain.sync.rtf import rtf_to_text
    try:
        return from_text(rtf_to_text(content_bytes))
    except Exception as exc:  # noqa: BLE001
        log.warning("rtf: decode failed: %s", exc)
        return []
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_rtf.py -q -n0`
Expected: PASS. If `\u8212?` leaves a stray `?`, the `skip` handling of the next `plain` token is wrong — fix the decoder, not the test.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/sync/rtf.py mcpbrain/sync/extractors.py tests/test_rtf.py
git commit -m "feat(extract): in-tree RTF decoder so control words are no longer indexed"
```

---

### Task 7: Wire blocks into Drive, attachments and every normaliser; version stamps; heading trail in the contextual prefix

**Files:**
- Modify: `mcpbrain/sync/drive.py` (`_EXPORT`, `_DOWNLOAD_BINARY`, `Content`, `_fetch_text`, `fetch_content`, `normalise_drive`)
- Modify: `mcpbrain/sync/attachments.py` (`_EXTRACTORS` for pdf/docx/pptx → blocks; `normalise_attachment`)
- Modify: `mcpbrain/sync/normalise.py:326-360`, `mcpbrain/sync/anarlog.py:338-373`, `mcpbrain/sync/calendar.py:66-92` (stamp `split_version`)
- Modify: `mcpbrain/embed.py` (`contextual_prefix`: append `section <heading_trail>`)
- Test: `tests/test_drive_blocks_wiring.py`

**Interfaces:**
- Consumes: Tasks 1-6.
- Produces:
  - `drive.Content` gains `blocks: list | None = None`.
  - `drive.normalise_drive(file_meta, text, drive_id=None, *, tables=None, folder="", blocks=None) -> list[Chunk]` (new `blocks` kwarg; chunks carry `spans`).
  - `drive._GOOGLE_EXPORT_FALLBACK` behaviour: Google Docs export DOCX → on export-size error `text/markdown` → `text/plain`; Slides PPTX → `text/plain`.
  - Metadata: `split_version` on every chunk from drive/gmail/attachments/anarlog/calendar; `extraction_version` on block-extracted Drive chunks and attachment chunks; `heading_trail` where present.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_drive_blocks_wiring.py
import io

from docx import Document

from mcpbrain.chunking import SPLIT_VERSION
from mcpbrain.embed import contextual_prefix
from mcpbrain.sync import drive
from mcpbrain.sync.blocks import Heading, Paragraph
from mcpbrain.sync.normalise import normalise_gmail

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
GDOC = "application/vnd.google-apps.document"


def _docx_bytes():
    d = Document()
    d.add_heading("Budget", 1)
    d.add_paragraph("Line one\nLine two")
    b = io.BytesIO(); d.save(b); return b.getvalue()


class _Req:
    def __init__(self, payload=None, exc=None):
        self.payload, self.exc = payload, exc
    def execute(self, num_retries=0):
        if self.exc:
            raise self.exc
        return self.payload


class _Files:
    def __init__(self, media=None, exports=None):
        self.media, self.exports, self.export_calls = media, exports or {}, []
    def get_media(self, fileId, supportsAllDrives=True):
        return _Req(self.media)
    def export(self, fileId, mimeType):
        self.export_calls.append(mimeType)
        val = self.exports.get(mimeType)
        return _Req(exc=val) if isinstance(val, Exception) else _Req(val)


class _Svc:
    def __init__(self, files):
        self._f = files
    def files(self):
        return self._f


def test_docx_drive_file_uses_blocks_and_stamps_versions():
    svc = _Svc(_Files(media=_docx_bytes()))
    meta = {"id": "f1", "name": "b.docx", "mimeType": DOCX, "modifiedTime": "2026-01-01T00:00:00Z"}
    content = drive.fetch_content(svc, meta)
    assert content.blocks and isinstance(content.blocks[0], Heading)
    chunks = drive.normalise_drive(meta, content.text, blocks=content.blocks)
    assert chunks[0].text == "Budget\n\nLine one\nLine two"
    md = chunks[0].metadata
    assert md["extraction_version"] == 1 and md["split_version"] == SPLIT_VERSION
    assert md["heading_trail"] == "Budget"
    assert chunks[0].spans == ["Budget", "Line one\nLine two"]


def test_google_doc_exports_docx_then_falls_back_on_size_error():
    from googleapiclient.errors import HttpError

    class _Resp(dict):
        status = 403
        reason = "exportSizeLimitExceeded"
    err = HttpError(_Resp(), b'{"error": {"errors": [{"reason": "exportSizeLimitExceeded"}]}}')
    files = _Files(exports={DOCX: err, "text/markdown": b"# Title\n\nBody text"})
    meta = {"id": "g1", "name": "Doc", "mimeType": GDOC, "modifiedTime": "2026-01-01T00:00:00Z"}
    content = drive.fetch_content(_Svc(files), meta)
    assert files.export_calls == [DOCX, "text/markdown"]
    assert content.blocks == [Heading(1, "Title"), Paragraph("Body text")]


def test_gmail_body_chunks_carry_split_version():
    import base64
    body = base64.urlsafe_b64encode(b"Hello there\n\nSecond para").decode()
    raw = {"id": "m1", "threadId": "t1", "labelIds": [],
           "payload": {"mimeType": "text/plain", "headers": [{"name": "Subject", "value": "Hi"}],
                       "body": {"data": body}}}
    chunks = normalise_gmail(raw)
    assert chunks and all(c.metadata["split_version"] == SPLIT_VERSION for c in chunks)


def test_contextual_prefix_includes_heading_trail():
    p = contextual_prefix({"source_type": "gdrive", "file_name": "b.docx",
                           "heading_trail": "Budget › Capital works"})
    assert "section Budget › Capital works" in p
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_drive_blocks_wiring.py -q -n0`
Expected: FAIL (`Content` has no `blocks`, etc.).

- [ ] **Step 3: Implement**

**`drive.py`:**

1. Imports: add `from mcpbrain.chunking import SPLIT_VERSION` beside the existing chunking import; add `from mcpbrain.sync import blocks as blocks_mod` and import `extract_blocks_from_pdf, extract_blocks_from_docx, extract_blocks_from_pptx, extract_blocks_from_rtf` from `extractors`.
2. Replace `_EXPORT` and add the block tables:

```python
_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

# Google Docs/Slides export as DOCX/PPTX so they share the structure-preserving
# DOCX/PPTX extractors (2026-09-24). Drive caps exports at 10 MB; on that error
# the chain falls back so no file that ingested before stops ingesting.
_EXPORT = {
    "application/vnd.google-apps.spreadsheet": "text/csv",
}
_EXPORT_BLOCKS = {
    "application/vnd.google-apps.document": (
        (_DOCX, extract_blocks_from_docx), ("text/markdown", None), ("text/plain", None)),
    "application/vnd.google-apps.presentation": (
        (_PPTX, extract_blocks_from_pptx), ("text/plain", None)),
}
_DOWNLOAD_BLOCKS = {
    "application/pdf": extract_blocks_from_pdf,
    _DOCX: extract_blocks_from_docx,
    _PPTX: extract_blocks_from_pptx,
    "application/rtf": extract_blocks_from_rtf,
}
```

Remove PDF/DOCX/PPTX from `_DOWNLOAD_BINARY` (keep `message/rfc822`), and remove `application/rtf` from `_DOWNLOAD_TEXT`.

3. `Content` gains `blocks: list | None = None`.
4. Add:

```python
def _export_too_large(exc) -> bool:
    return "exportSizeLimitExceeded" in str(getattr(exc, "content", b"") or exc) \
        or "exportSizeLimitExceeded" in str(exc)


def _fetch_blocks(service, file_meta: dict) -> list | None:
    """Blocks for a structure-preserving MIME, or None if the MIME isn't one."""
    mime = file_meta.get("mimeType", "")
    fid = file_meta["id"]
    if mime in _DOWNLOAD_BLOCKS:
        raw = service.files().get_media(fileId=fid, supportsAllDrives=True
                                        ).execute(num_retries=_NUM_RETRIES)
        data = raw if isinstance(raw, bytes) else str(raw).encode("utf-8", "replace")
        return _DOWNLOAD_BLOCKS[mime](data)
    if mime in _EXPORT_BLOCKS:
        chain = _EXPORT_BLOCKS[mime]
        for i, (target, fn) in enumerate(chain):
            try:
                raw = service.files().export(fileId=fid, mimeType=target
                                             ).execute(num_retries=_NUM_RETRIES)
            except Exception as exc:  # noqa: BLE001
                if _export_too_large(exc) and i < len(chain) - 1:
                    log.info("drive: %s export as %s too large; falling back", fid, target)
                    continue
                raise
            if fn is not None:
                data = raw if isinstance(raw, bytes) else str(raw).encode("utf-8", "replace")
                return fn(data)
            text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
            return blocks_mod.from_text(text)
    return None
```

5. In `fetch_content`, before `text = _fetch_text(service, file_meta)`:

```python
    blocks = _fetch_blocks(service, file_meta)
    if blocks is not None:
        text = blocks_mod.to_text(blocks)
        if not text.strip():
            _note_skip(store, report, "extraction_empty", fid, mime, name)
            return Content(partial=is_partial(text))
        return Content(text=text, blocks=list(blocks), partial=is_partial(text))
```

6. `normalise_drive`: add keyword `blocks: list | None = None`; in `base_meta` add `"split_version": SPLIT_VERSION` and, when `blocks_mod.extraction_version(mime)`, `"extraction_version": blocks_mod.extraction_version(mime)`. Replace the rendering branch:

```python
    if tables:
        rendered = [(t, extra, [t]) for t, extra in tabular.render_chunks(
            tables, file_name=base_meta["file_name"], max_chars=tabular.CHUNK_CHARS)]
    elif blocks:
        rendered = [(r.text, r.meta, r.spans) for r in blocks_mod.render(blocks)]
    else:
        rendered = [(t, {}, [t]) for t in chunk_text(text)]

    kept = [(t, extra, spans) for t, extra, spans in rendered if has_content(t)]
    out = []
    for i, (chunk, extra, spans) in enumerate(kept):
        meta = {**base_meta, **extra, "chunk_index": i, "chunk_total": len(kept)}
        out.append(Chunk(doc_id=f"gdrive-{fid}-{i}", text=chunk,
                         content_hash=content_hash(chunk), metadata=meta, spans=spans))
    return out
```

   Also accept `blocks` when the early-return guard runs: `if not tables and not blocks and (not text or not text.strip()): return []`.
7. Pass `blocks=content.blocks` at every `normalise_drive(...)` call site in `drive.py` (`handle_drive_item`, `_cache_first_extract_one`, `backfill_drive` and any other; `grep -n "normalise_drive(" mcpbrain` and update all).

**`attachments.py`:** import `extract_blocks_from_pdf/docx/pptx` and `from mcpbrain.sync import blocks as blocks_mod`, `from mcpbrain.chunking import SPLIT_VERSION`. Add:

```python
_BLOCK_EXTRACTORS = {
    "application/pdf": extract_blocks_from_pdf,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        extract_blocks_from_docx,
    "application/vnd.openxmlformats-officedocument.presentationml.presentation":
        extract_blocks_from_pptx,
}
```

Remove those three from `_EXTRACTORS` (keep them in `_supported` by changing it to `return mime in _EXTRACTORS or mime in _BLOCK_EXTRACTORS or mime in _TABLE_EXTRACTORS or mime in _CSV_MIMES`). In `normalise_attachment`: add `blocks = None`; add the branch `elif mime in _BLOCK_EXTRACTORS: blocks = _BLOCK_EXTRACTORS[mime](data); text = blocks_mod.to_text(blocks)` before the `_EXTRACTORS` branch; in `base` add `"split_version": SPLIT_VERSION` and `if blocks_mod.extraction_version(mime): base["extraction_version"] = blocks_mod.extraction_version(mime)`. Rendering becomes the same three-way `(text, extra, spans)` shape as `normalise_drive` (tables → `[t]` spans; blocks → `blocks_mod.render(blocks)`; else `chunk_text(text)`), and the `Chunk(...)` gets `spans=spans`.

**`normalise.py`:** `from mcpbrain.chunking import CHUNKER_VERSION, SPLIT_VERSION, chunk_text, content_hash, has_content`; add `"split_version": SPLIT_VERSION,` to `base_metadata` beside `chunker_version`.

**`anarlog.py`:** add `"split_version": SPLIT_VERSION,` to `base` beside `chunker_version` (import it).

**`calendar.py`:** add `"split_version": SPLIT_VERSION,` to `meta` beside `chunker_version` (import it).

**`embed.py` `contextual_prefix`:** immediately before `if not parts:` add:

```python
    trail = metadata.get("heading_trail", "")
    if trail:
        parts.append(f"section {trail}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_drive_blocks_wiring.py tests/test_drive_sync.py tests/test_drive_extraction.py tests/test_attachments.py tests/test_normalise.py tests/test_calendar_sync.py tests/test_anarlog_doc_ids.py tests/test_embed.py -q -n0`
Expected: PASS. Use `ls tests | grep -E "drive|attach|normalise|calendar|anarlog|embed"` to confirm file names; run whichever exist. Existing tests that mock `files().export` with `mimeType="text/plain"` for Google Docs must be updated to the new DOCX-first chain (return `_docx_bytes()` for the DOCX export) — keep their assertions about the resulting text. Tests asserting an exact metadata dict must add `split_version` (and `extraction_version` where applicable).

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/sync/drive.py mcpbrain/sync/attachments.py mcpbrain/sync/normalise.py mcpbrain/sync/anarlog.py mcpbrain/sync/calendar.py mcpbrain/embed.py tests/
git commit -m "feat(sync): route PDF/DOCX/PPTX/RTF/Google Docs+Slides through blocks; stamp split/extraction versions; heading trail in contextual prefix"
```

---

### Task 8: Per-MIME extraction version in the ingest-cache fingerprint

**Files:**
- Modify: `mcpbrain/ingest_cache.py` (`effective_chunker_version`, `_pf8`, `try_import`, `_import_artifact`, `publish_file` and every `artifact_filename(...)` caller)
- Modify: `mcpbrain/sync/drive.py` (`_cache_first_extract_one` passes `mime`)
- Test: `tests/test_ingest_cache_extraction_version.py`

**Interfaces:**
- Produces: `ingest_cache.effective_chunker_version(pin, mime: str = "") -> str` — returns the current value, suffixed `+x<N>` when `blocks.extraction_version(mime) > 0`; `try_import(..., mime: str = "")`; `publish_file` derives `mime` from the file's stored chunk metadata (`mime_type`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_ingest_cache_extraction_version.py
from mcpbrain import ingest_cache
from mcpbrain.org_contracts import FleetPin   # adjust import to where FleetPin lives: grep -rn "class FleetPin" mcpbrain

PIN = FleetPin(embed_model="bge-small", dim=4, chunker_version="v1",
               enrich_logic_floor=1, fleet_secret="s3cret")


def test_block_mime_suffixes_version():
    base = ingest_cache.effective_chunker_version(PIN)
    assert ingest_cache.effective_chunker_version(PIN, "application/pdf") == base + "+x1"


def test_non_block_mime_unchanged():
    base = ingest_cache.effective_chunker_version(PIN)
    xlsx = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert ingest_cache.effective_chunker_version(PIN, xlsx) == base
    assert ingest_cache.effective_chunker_version(PIN, "") == base
```

Plus one round-trip test modelled on `tests/test_a4_enrich_cache.py`'s publish → import flow (copy its `_store`, `LocalDirFleetStorage` and publish setup): publish a PDF file's chunks, assert `try_import(..., mime="application/pdf")` succeeds, and assert an artifact published under the pre-change fingerprint (call the internal filename builder with the unsuffixed version) is NOT imported for `mime="application/pdf"`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_ingest_cache_extraction_version.py -q -n0`
Expected: FAIL (`effective_chunker_version()` takes 1 positional argument).

- [ ] **Step 3: Implement**

```python
def effective_chunker_version(pin, mime: str = "") -> str:
    base = (str(pin.chunker_version)
            if _version_int(pin.chunker_version) >= CHUNKER_VERSION
            else str(CHUNKER_VERSION))
    from mcpbrain.sync.blocks import extraction_version
    xv = extraction_version(mime)
    return f"{base}+x{xv}" if xv else base
```

(Keep the existing docstring and add: "`mime` appends the file type's extraction_version so an extractor change invalidates only that type's artifacts, never spreadsheets.")

Then `grep -n "effective_chunker_version\|_pf8\|artifact_filename" mcpbrain/ingest_cache.py mcpbrain/sync/drive.py` and thread `mime` through every call: `_pf8(pin, mime="")`; `try_import(..., contextual_retrieval=None, mime: str = "")` passes it to the filename lookup and to `_import_artifact`, whose version check becomes `art.chunker_version == effective_chunker_version(pin, mime)`; `publish_file` reads `mime` from the first collected chunk's `metadata["mime_type"]`; `_cache_first_extract_one` passes `mime=fmeta.get("mimeType", "")` to both `try_import` calls.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_ingest_cache_extraction_version.py tests/test_a4_enrich_cache.py tests/test_ingest_cache.py -q -n0`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/ingest_cache.py mcpbrain/sync/drive.py tests/test_ingest_cache_extraction_version.py
git commit -m "feat(ingest-cache): per-MIME extraction version in the pipeline fingerprint"
```

---

### Task 9: `reflow.plan` — stitch, coverage, remap (pure)

**Files:**
- Create: `mcpbrain/reflow.py`
- Test: `tests/test_reflow_plan.py`

**Interfaces:**
- Consumes: `normalise.Chunk` (with `spans`).
- Produces:
  - `norm(s: str) -> str`; `lineage_key(doc_id: str, metadata: dict) -> str`.
  - `stitch(texts: list[str]) -> tuple[str, list[tuple[int, int]]]`.
  - `@dataclass NewRow(chunk: Chunk, covered: bool, enriched: int, enriched_version: int, enrich_state: str | None, salience, memory_tier, memory_type)`.
  - `@dataclass ReflowPlan(rows: list[NewRow], remap: dict[str, str], reasons: dict[str, str], deletes: list[str], content_equal: bool)`.
  - `plan(old: list[dict], new: list[Chunk]) -> ReflowPlan` — `old` rows are dicts with keys `doc_id, text, metadata (dict), enriched, enriched_version, enrich_state, salience, memory_tier, memory_type`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_reflow_plan.py
from mcpbrain.reflow import lineage_key, norm, plan, stitch
from mcpbrain.sync.normalise import Chunk


def _old(i, text, total, enriched=1, state=None, fid="F"):
    return {"doc_id": f"gdrive-{fid}-{i}", "text": text,
            "metadata": {"source_type": "gdrive", "file_id": fid, "chunk_index": i,
                         "chunk_total": total},
            "enriched": enriched, "enriched_version": 3, "enrich_state": state,
            "salience": 0.5, "memory_tier": "warm", "memory_type": "semantic"}


def _new(i, text, spans=None, fid="F"):
    return Chunk(f"gdrive-{fid}-{i}", text, "h", {"source_type": "gdrive", "file_id": fid,
                 "chunk_index": i}, spans or [text])


def test_stitch_removes_word_overlap():
    a = "one two three four five six"
    b = "five six seven eight"
    o, offs = stitch([a, b])
    assert o == "one two three four five six seven eight"
    assert offs[0][0] == 0 and offs[1][0] == o.index("seven") - len("five six ")


def test_lineage_keys():
    assert lineage_key("gdrive-F-3", {"source_type": "gdrive"}) == "gdrive-F"
    assert lineage_key("gmail-M-att-2-0", {"source_type": "gmail"}) == "gmail-M-att-2"
    assert lineage_key("cal-E", {"source_type": "calendar", "event_id": "E"}) == "cal-E"
    assert lineage_key("cal-E-1", {"source_type": "calendar", "event_id": "E"}) == "cal-E"


def test_full_coverage_inherits_and_remaps():
    old = [_old(0, "alpha beta gamma delta", 2), _old(1, "gamma delta epsilon zeta", 2)]
    new = [_new(0, "alpha beta\ngamma", ["alpha beta\ngamma"]),
           _new(1, "delta epsilon zeta", ["delta epsilon zeta"])]
    p = plan(old, new)
    assert p.content_equal
    assert all(r.covered and r.enriched == 1 for r in p.rows)
    assert p.remap["gdrive-F-0"] == "gdrive-F-0"
    assert p.remap["gdrive-F-1"] in {"gdrive-F-0", "gdrive-F-1"}
    assert p.deletes == []


def test_new_text_is_uncovered_and_not_inherited():
    old = [_old(0, "alpha beta", 1)]
    new = [_new(0, "alpha beta"), _new(1, "Notes: brand new speaker notes")]
    p = plan(old, new)
    assert p.rows[0].covered and not p.rows[1].covered
    assert p.rows[1].enriched == 0 and p.rows[1].enrich_state is None
    assert not p.content_equal


def test_unenriched_old_means_not_enriched_new():
    old = [_old(0, "alpha beta", 1, enriched=0)]
    p = plan(old, [_new(0, "alpha beta")])
    assert p.rows[0].covered and p.rows[0].enriched == 0


def test_fewer_new_chunks_deletes_and_maps_old_ids():
    old = [_old(i, f"part{i} words here", 3) for i in range(3)]
    new = [_new(0, "part0 words here\npart1 words here\npart2 words here",
                ["part0 words here", "part1 words here", "part2 words here"])]
    p = plan(old, new)
    assert sorted(p.deletes) == ["gdrive-F-1", "gdrive-F-2"]
    assert set(p.remap.values()) == {"gdrive-F-0"}
    assert all(p.reasons[k] == "exact" for k in p.remap)


def test_plan_repeated_text_maps_monotonically():
    boiler = "Confidential disclaimer text"
    old = [_old(0, f"{boiler} first", 2), _old(1, f"{boiler} second", 2)]
    new = [_new(0, f"{boiler} first"), _new(1, f"{boiler} second")]
    p = plan(old, new)
    assert p.remap == {"gdrive-F-0": "gdrive-F-0", "gdrive-F-1": "gdrive-F-1"}


def test_plan_gap_in_old_chunks_is_safe():
    old = [_old(0, "alpha beta", 4), _old(2, "epsilon zeta", 4)]      # 1 and 3 missing
    new = [_new(0, "alpha beta"), _new(1, "gamma delta"), _new(2, "epsilon zeta")]
    p = plan(old, new)
    assert [r.covered for r in p.rows] == [True, False, True]
    assert p.remap["gdrive-F-2"] == "gdrive-F-2"


def test_table_chunk_covered_by_cell_values():
    old = [_old(0, "Item Cost Chairs 120", 1)]
    new = [_new(0, "Table (in Budget)\nItem: Chairs; Cost: 120", ["Item", "Cost", "Chairs", "120"])]
    assert plan(old, new).rows[0].covered


def test_largest_overlap_old_supplies_state():
    old = [_old(0, "a b c d e f g h", 2, state="cold"), _old(1, "i j", 2, state=None)]
    new = [_new(0, "a b c d e f g h i j")]
    p = plan(old, new)
    assert p.rows[0].enrich_state == "cold"


def test_norm():
    assert norm("  a\n\tb  c ") == "a b c"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_reflow_plan.py -q -n0`
Expected: FAIL with `ModuleNotFoundError: mcpbrain.reflow`.

- [ ] **Step 3: Implement**

```python
# mcpbrain/reflow.py
"""Content-preserving reflow: re-chunk an owner without discarding enrichment.

`plan` is pure: given an owner's OLD chunk rows (with their enrichment state)
and its NEW chunks (with source spans), it decides per new chunk whether its
source text is provably covered by already-enriched old text, and maps every
old doc_id to the new chunk now holding its text. Store.apply_reflow applies
the plan in one transaction. Spec: 2026-09-24 extraction-fidelity §3.
"""
from dataclasses import dataclass, field

from mcpbrain.sync.normalise import Chunk

_MAX_OVERLAP_WORDS = 60     # chunk_text overlaps by 50; allow slack


def norm(s: str) -> str:
    return " ".join((s or "").split())


def lineage_key(doc_id: str, metadata: dict) -> str:
    if (metadata or {}).get("source_type") == "calendar":
        return f"cal-{metadata.get('event_id', '')}"
    return doc_id.rsplit("-", 1)[0]


def stitch(texts: list[str]) -> tuple[str, list[tuple[int, int]]]:
    """Join chunk texts into one normalised text, removing each seam's word
    overlap (longest suffix/prefix match up to _MAX_OVERLAP_WORDS). Returns the
    text and each input's (start, end) offsets in it."""
    words: list[str] = []
    spans: list[tuple[int, int]] = []
    char_len = 0

    def joined_len(ws):
        return len(" ".join(ws))

    for t in texts:
        tw = norm(t).split()
        k = 0
        for n in range(min(_MAX_OVERLAP_WORDS, len(words), len(tw)), 0, -1):
            if words[-n:] == tw[:n]:
                k = n
                break
        start_words = len(words) - k
        start = joined_len(words[:start_words]) + (1 if start_words else 0)
        words.extend(tw[k:])
        char_len = joined_len(words)
        spans.append((start if tw else char_len, char_len))
    return " ".join(words), spans


@dataclass
class NewRow:
    chunk: Chunk
    covered: bool
    enriched: int = 0
    enriched_version: int = 0
    enrich_state: str | None = None
    salience: object = None
    memory_tier: str | None = None
    memory_type: str | None = None


@dataclass
class ReflowPlan:
    rows: list[NewRow] = field(default_factory=list)
    remap: dict[str, str] = field(default_factory=dict)
    reasons: dict[str, str] = field(default_factory=dict)
    deletes: list[str] = field(default_factory=list)
    content_equal: bool = False
    # Lineage keys whose stitched old and new text differ. The handler uses this
    # to tell "a block extractor changed this attachment's text" (expected)
    # from "the source itself changed" (take the ordinary path).
    unequal: list[str] = field(default_factory=list)


def _locate(o: str, spans: list[str], cursor: int) -> tuple[int, int] | None:
    """(start, end) of a new chunk's spans inside `o`, searching forward from
    `cursor` first so repeated text maps monotonically. None if any span is
    missing."""
    start = end = None
    pos = cursor
    for s in spans:
        ns = norm(s)
        if not ns:
            continue
        at = o.find(ns, pos)
        if at < 0:
            at = o.find(ns)
        if at < 0:
            return None
        if start is None:
            start = at
        end = max(end or 0, at + len(ns))
        pos = at + len(ns)
    if start is None:
        return None
    return start, end


def _plan_lineage(key: str, old: list[dict], new: list[Chunk], plan_: ReflowPlan) -> None:
    old = sorted(old, key=lambda r: (r["metadata"] or {}).get("chunk_index", 0))
    o, offs = stitch([r["text"] for r in old])
    n_text, _ = stitch([c.text for c in new])
    positions: list[tuple[int, int] | None] = []
    cursor = 0
    for c in new:
        loc = _locate(o, c.spans or [c.text], cursor)
        positions.append(loc)
        if loc:
            cursor = loc[0]
        overlapped = [] if loc is None else [
            (i, min(loc[1], e) - max(loc[0], s))
            for i, (s, e) in enumerate(offs) if s < loc[1] and e > loc[0]]
        overlapped = [(i, w) for i, w in overlapped if w > 0]
        row = NewRow(chunk=c, covered=loc is not None and bool(overlapped))
        if row.covered:
            best = old[max(overlapped, key=lambda p: p[1])[0]]
            row.enriched = 1 if all(old[i]["enriched"] == 1 for i, _ in overlapped) else 0
            row.enriched_version = best["enriched_version"] if row.enriched else 0
            row.enrich_state = best["enrich_state"]
            row.salience = best["salience"]
            row.memory_tier = best["memory_tier"]
            row.memory_type = best["memory_type"]
        plan_.rows.append(row)
    placed = [(i, p) for i, p in enumerate(positions) if p is not None]
    for r, (s, _e) in zip(old, offs):
        target, reason = None, "exact"
        for i, (ps, pe) in placed:
            if ps <= s < pe:
                target = new[i].doc_id
                break
        if target is None and placed:
            i, _ = min(placed, key=lambda ip: abs(ip[1][0] - s))
            target, reason = new[i].doc_id, "nearest"
        if target is None:
            target, reason = new[0].doc_id, "fallback"
        plan_.remap[r["doc_id"]] = target
        plan_.reasons[r["doc_id"]] = reason
    if o != n_text:
        plan_.content_equal = False
        plan_.unequal.append(key)


def plan(old: list[dict], new: list[Chunk]) -> ReflowPlan:
    p = ReflowPlan(content_equal=True)
    if not new:
        raise ValueError("reflow.plan: no new chunks — never plan a deletion")
    by_old: dict[str, list[dict]] = {}
    for r in old:
        by_old.setdefault(lineage_key(r["doc_id"], r["metadata"] or {}), []).append(r)
    by_new: dict[str, list[Chunk]] = {}
    for c in new:
        by_new.setdefault(lineage_key(c.doc_id, c.metadata), []).append(c)
    for key, chunks in by_new.items():
        if key in by_old:
            _plan_lineage(key, by_old[key], chunks, p)
        else:
            p.content_equal = False
            p.unequal.append(key)
            p.rows.extend(NewRow(chunk=c, covered=False) for c in chunks)
    for key, rows in by_old.items():
        if key not in by_new:
            p.content_equal = False
            p.unequal.append(key)
            for r in rows:
                p.remap[r["doc_id"]] = new[0].doc_id
                p.reasons[r["doc_id"]] = "lineage_gone"
    order = {c.doc_id: i for i, c in enumerate(new)}
    p.rows.sort(key=lambda r: order[r.chunk.doc_id])
    new_ids = set(order)
    p.deletes = sorted(r["doc_id"] for r in old if r["doc_id"] not in new_ids)
    return p
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_reflow_plan.py -q -n0`
Expected: PASS. If `test_stitch_removes_word_overlap`'s offset assertion is off by one, fix `stitch`'s start computation — the invariant is `o[start:end] == norm(text)` minus its dropped overlap prefix; add that as an extra assertion while fixing.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/reflow.py tests/test_reflow_plan.py
git commit -m "feat(reflow): pure carry-over planner — stitch, coverage proof, positional remap"
```

---

### Task 10: Store — `reflow_map`/`reflow_owners`, `apply_reflow` with in-transaction orphan guard, queue enqueue, stale-id fallbacks

**Files:**
- Modify: `mcpbrain/store.py` (schema in `init()`; new methods `owner_chunks`, `apply_reflow`, `enqueue_items`, `latest_reflow_target`, `reflow_stats`; `read_doc` fallback)
- Modify: `mcpbrain/drain.py` (`_resolve_doc_ids` fallback), `mcpbrain/org_contrib.py` (`_chunk_provenance` fallback)
- Test: `tests/test_store_reflow.py`

**Interfaces:**
- Consumes: `reflow.ReflowPlan`, `NewRow`.
- Produces:
  - `class ReflowOrphanError(RuntimeError)` in `store.py`.
  - `Store.owner_chunks(doc_id_prefixes: list[str]) -> list[dict]` — rows with `doc_id, text, metadata (dict), enriched, enriched_version, enrich_state, salience, memory_tier, memory_type`, for every chunk whose doc_id starts with any prefix (`LIKE prefix || '%' ESCAPE`). Callers pass e.g. `["gdrive-<fid>-"]`, `["gmail-<mid>-"]`, `["anarlog-<sid>-"]`, `["cal-<eid>"]`.
  - `Store.apply_reflow(owner: str, source: str, plan, vectors: list[list[float]], *, home=None) -> dict` → `{"written", "carried", "reenrich", "deleted", "remapped"}`; raises `ReflowOrphanError` (transaction rolled back) if any reference to the owner's ids is left dangling.
  - `Store.enqueue_items(items: list[dict], *, source: str) -> int` — the enqueue half of `enqueue_and_advance`, no cursor.
  - `Store.latest_reflow_target(doc_id: str) -> str | None`.
  - `Store.reflow_stats() -> dict` → `{"owners_done", "chunks_carried", "chunks_reenrich", "queued"}`.

The reference targets (table, column) — define once as a module constant:

```python
_REFLOW_REF_COLUMNS = (
    ("entity_relations", "source_doc_id"),
    ("entity_observations", "source"),
    ("actions", "source_doc_id"),
    ("actions", "waiting_on_cleared_by_doc_id"),
    ("graph_actions_legacy", "source_doc_id"),
    ("graph_decisions_legacy", "source_doc_id"),
    ("recall_feedback", "doc_id"),
)
```

(Skip a table that does not exist on this store: check `sqlite_master` once per call.)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_store_reflow.py
import json

import pytest

from mcpbrain.reflow import plan
from mcpbrain.store import ReflowOrphanError, Store
from mcpbrain.sync.normalise import Chunk


def _store(tmp_path):
    s = Store(tmp_path / "a.sqlite3", dim=4)
    s.init()
    return s


V = [0.1, 0.2, 0.3, 0.4]


def _seed(s, fid="F", texts=("alpha beta gamma", "delta epsilon"), state=None):
    for i, t in enumerate(texts):
        meta = {"source_type": "gdrive", "file_id": fid, "chunk_index": i,
                "chunk_total": len(texts), "mime_type": "application/pdf"}
        s.upsert_chunk(f"gdrive-{fid}-{i}", t, f"h{i}", meta)
        with s._connect(write=True) as db:
            db.execute("UPDATE chunks SET enriched=1, enriched_version=3, enrich_state=? "
                       "WHERE doc_id=?", (state, f"gdrive-{fid}-{i}"))


def _new(fid, texts):
    return [Chunk(f"gdrive-{fid}-{i}", t, f"n{i}",
                  {"source_type": "gdrive", "file_id": fid, "chunk_index": i,
                   "chunk_total": len(texts), "extraction_version": 1}, [t])
            for i, t in enumerate(texts)]


def _relation(s, doc_id):
    with s._connect(write=True) as db:
        db.execute("INSERT INTO entities(id, name, type) VALUES('e1','Dana Okafor','person')"
                   " ON CONFLICT DO NOTHING")
        db.execute("INSERT INTO entities(id, name, type) VALUES('e2','Northgate Trust','org')"
                   " ON CONFLICT DO NOTHING")
        db.execute("INSERT INTO entity_relations(entity_a, relation, entity_b, source_doc_id)"
                   " VALUES('e1','works_at','e2',?)", (doc_id,))


def test_apply_reflow_merges_to_one_chunk_and_remaps_relation(tmp_path):
    s = _store(tmp_path)
    _seed(s)
    _relation(s, "gdrive-F-1")
    old = s.owner_chunks(["gdrive-F-"])
    p = plan(old, _new("F", ["alpha beta gamma\ndelta epsilon"]))
    out = s.apply_reflow("F", "drive", p, [V])
    assert out["deleted"] == 1 and out["carried"] == 1
    with s._connect() as db:
        rel = db.execute("SELECT source_doc_id FROM entity_relations").fetchone()[0]
        row = db.execute("SELECT enriched, embedded, text FROM chunks WHERE doc_id='gdrive-F-0'").fetchone()
        n = db.execute("SELECT count(*) FROM chunks").fetchone()[0]
        logged = db.execute("SELECT old_doc_id, new_doc_id FROM reflow_map ORDER BY old_doc_id").fetchall()
    assert rel == "gdrive-F-0"
    assert tuple(row) == (1, 1, "alpha beta gamma\ndelta epsilon")
    assert n == 1
    assert [tuple(r) for r in logged] == [("gdrive-F-0", "gdrive-F-0"), ("gdrive-F-1", "gdrive-F-0")]
    assert s.latest_reflow_target("gdrive-F-1") == "gdrive-F-0"
    assert s.read_doc("gdrive-F-1")["doc_id"] == "gdrive-F-0"


def test_apply_reflow_simultaneous_swap(tmp_path):
    """Remap i->j and j->i in one statement must not chain."""
    s = _store(tmp_path)
    _seed(s, texts=("first part", "second part"))
    _relation(s, "gdrive-F-0")
    old = s.owner_chunks(["gdrive-F-"])
    p = plan(old, _new("F", ["first part", "second part"]))
    p.remap = {"gdrive-F-0": "gdrive-F-1", "gdrive-F-1": "gdrive-F-0"}
    s.apply_reflow("F", "drive", p, [V, V])
    with s._connect() as db:
        assert db.execute("SELECT source_doc_id FROM entity_relations").fetchone()[0] == "gdrive-F-1"


def test_apply_reflow_preserves_cold(tmp_path):
    s = _store(tmp_path)
    _seed(s, state="cold")
    old = s.owner_chunks(["gdrive-F-"])
    s.apply_reflow("F", "drive", plan(old, _new("F", ["alpha beta gamma", "delta epsilon"])), [V, V])
    with s._connect() as db:
        states = {r[0] for r in db.execute("SELECT enrich_state FROM chunks")}
    assert states == {"cold"}


def test_orphan_guard_rolls_back(tmp_path):
    s = _store(tmp_path)
    _seed(s)
    _relation(s, "gdrive-F-1")
    old = s.owner_chunks(["gdrive-F-"])
    p = plan(old, _new("F", ["alpha beta gamma\ndelta epsilon"]))
    p.remap["gdrive-F-1"] = "gdrive-F-9"          # a target that will not exist
    with pytest.raises(ReflowOrphanError):
        s.apply_reflow("F", "drive", p, [V])
    with s._connect() as db:
        assert db.execute("SELECT count(*) FROM chunks").fetchone()[0] == 2
        assert db.execute("SELECT source_doc_id FROM entity_relations").fetchone()[0] == "gdrive-F-1"
        assert db.execute("SELECT count(*) FROM reflow_map").fetchone()[0] == 0


def test_uncovered_chunk_is_hot_and_unenriched(tmp_path):
    s = _store(tmp_path)
    _seed(s, texts=("alpha beta gamma",))
    old = s.owner_chunks(["gdrive-F-"])
    s.apply_reflow("F", "drive", plan(old, _new("F", ["alpha beta gamma", "Notes: new"])), [V, V])
    with s._connect() as db:
        r = db.execute("SELECT enriched, enrich_state FROM chunks WHERE doc_id='gdrive-F-1'").fetchone()
    assert tuple(r) == (0, None)


def test_enqueue_items_does_not_touch_cursor(tmp_path):
    s = _store(tmp_path)
    n = s.enqueue_items([{"ref_id": "F", "event": "reflow",
                          "modified_at": "1970-01-01T00:00:00"}], source="reflow:drive")
    assert n == 1
    assert s.get_cursor("reflow:drive") is None
    assert s.reflow_stats()["queued"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_store_reflow.py -q -n0`
Expected: FAIL with `ImportError: cannot import name 'ReflowOrphanError'`.

- [ ] **Step 3: Implement**

Schema, in `init()` next to `sync_queue`:

```python
            db.execute(f"""CREATE TABLE IF NOT EXISTS reflow_map(
                id          INTEGER PRIMARY KEY,
                owner       TEXT NOT NULL,
                old_doc_id  TEXT NOT NULL,
                new_doc_id  TEXT NOT NULL,
                reason      TEXT NOT NULL,
                at          TEXT NOT NULL){_S}""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_reflow_map_old "
                       "ON reflow_map(old_doc_id, id)")
            db.execute(f"""CREATE TABLE IF NOT EXISTS reflow_owners(
                owner       TEXT PRIMARY KEY,
                source      TEXT NOT NULL,
                at          TEXT NOT NULL,
                chunks_new  INTEGER NOT NULL,
                carried     INTEGER NOT NULL,
                reenrich    INTEGER NOT NULL){_S}""")
```

Methods (add near `sync_queue` methods; use this module's existing `datetime`/`json` imports and `sqlite_vec`):

```python
    def enqueue_items(self, items, *, source: str) -> int:
        """enqueue_and_advance without a cursor: for producers (the reflow
        seed) that have no feed position to persist."""
        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        with self._connect(write=True) as db:
            for it in items:
                db.execute(
                    "INSERT INTO sync_queue(source, ref_id, version, event, modified_at,"
                    " discovered_at) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(source, ref_id) DO NOTHING",
                    (source, it["ref_id"], it.get("version", ""), it["event"],
                     it["modified_at"], now))
        return len(items)

    def owner_chunks(self, doc_id_prefixes: list[str]) -> list[dict]:
        out = []
        with self._connect() as db:
            for pfx in doc_id_prefixes:
                esc = pfx.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                for r in db.execute(
                        "SELECT doc_id, text, metadata, enriched, enriched_version, "
                        "enrich_state, salience, memory_tier, memory_type FROM chunks "
                        "WHERE doc_id LIKE ? ESCAPE '\\' ORDER BY rowid", (esc + "%",)):
                    d = dict(r)
                    d["metadata"] = json.loads(d["metadata"] or "{}")
                    out.append(d)
        return out

    def latest_reflow_target(self, doc_id: str) -> str | None:
        with self._connect() as db:
            r = db.execute("SELECT new_doc_id FROM reflow_map WHERE old_doc_id=? "
                           "ORDER BY id DESC LIMIT 1", (doc_id,)).fetchone()
        return r["new_doc_id"] if r else None

    def reflow_stats(self) -> dict:
        with self._connect() as db:
            o = db.execute("SELECT count(*) n, COALESCE(sum(carried),0) c, "
                           "COALESCE(sum(reenrich),0) r FROM reflow_owners").fetchone()
            q = db.execute("SELECT count(*) FROM sync_queue WHERE source LIKE 'reflow:%'"
                           ).fetchone()[0]
        return {"owners_done": o["n"], "chunks_carried": o["c"],
                "chunks_reenrich": o["r"], "queued": q}

    def apply_reflow(self, owner: str, source: str, plan, vectors, *, home=None) -> dict:
        """Apply a reflow.ReflowPlan in ONE transaction (spec §3). Any failure,
        including a dangling reference found by the orphan check, rolls back to
        the untouched old chunks."""
        if len(vectors) != len(plan.rows):
            raise ValueError("apply_reflow: one vector per new chunk")
        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        new_ids = [r.chunk.doc_id for r in plan.rows]
        old_ids = list(plan.remap)
        with self._connect(write=True) as db:
            tables = {r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            for row, vec in zip(plan.rows, vectors):
                c = row.chunk
                self._write_cached_chunk_row(
                    db, c.doc_id, c.text, c.content_hash, c.metadata, vec,
                    enriched=bool(row.enriched), enriched_version=row.enriched_version,
                    home=home)
                db.execute("UPDATE chunks SET enrich_state=?, salience=COALESCE(?, salience),"
                           " memory_tier=COALESCE(?, memory_tier),"
                           " memory_type=COALESCE(?, memory_type), enrich_attempts=0"
                           " WHERE doc_id=?",
                           (row.enrich_state, row.salience, row.memory_tier,
                            row.memory_type, c.doc_id))
            db.execute("CREATE TEMP TABLE IF NOT EXISTS reflow_tmp("
                       "old TEXT PRIMARY KEY, new TEXT NOT NULL)")
            db.execute("DELETE FROM reflow_tmp")
            db.executemany("INSERT INTO reflow_tmp(old, new) VALUES(?,?)",
                           list(plan.remap.items()))
            remapped = 0
            for table, col in _REFLOW_REF_COLUMNS:
                if table not in tables:
                    continue
                cur = db.execute(
                    f"UPDATE {table} SET {col}=(SELECT new FROM reflow_tmp WHERE old={table}.{col})"
                    f" WHERE {col} IN (SELECT old FROM reflow_tmp)")
                remapped += cur.rowcount
            if "chunk_quality" in tables:
                qrows = db.execute(
                    f"SELECT * FROM chunk_quality WHERE doc_id IN "
                    f"({','.join('?' * len(old_ids))})", old_ids).fetchall() if old_ids else []
                merged: dict[str, dict] = {}
                for q in qrows:
                    q = dict(q)
                    tgt = plan.remap[q["doc_id"]]
                    m = merged.setdefault(tgt, {**q, "doc_id": tgt, "exposures": 0, "uses": 0})
                    m["exposures"] += q.get("exposures") or 0
                    m["uses"] += q.get("uses") or 0
                    for k in ("memory_strength", "last_accessed", "quality", "updated_at"):
                        if k in q and q[k] is not None and (m.get(k) is None or q[k] > m[k]):
                            m[k] = q[k]
                if qrows:
                    db.execute(f"DELETE FROM chunk_quality WHERE doc_id IN "
                               f"({','.join('?' * len(old_ids))})", old_ids)
                    for m in merged.values():
                        cols = ",".join(m)
                        db.execute(f"INSERT OR REPLACE INTO chunk_quality({cols}) VALUES"
                                   f"({','.join('?' * len(m))})", list(m.values()))
            if plan.deletes:
                ph = ",".join("?" * len(plan.deletes))
                rowids = [r[0] for r in db.execute(
                    f"SELECT rowid FROM chunks WHERE doc_id IN ({ph})", plan.deletes)]
                if rowids:
                    rp = ",".join("?" * len(rowids))
                    db.execute(f"DELETE FROM vec_chunks WHERE rowid IN ({rp})", rowids)
                    db.execute(f"DELETE FROM fts_chunks WHERE rowid IN ({rp})", rowids)
                    db.execute(f"DELETE FROM chunks WHERE rowid IN ({rp})", rowids)
            db.executemany(
                "INSERT INTO reflow_map(owner, old_doc_id, new_doc_id, reason, at) "
                "VALUES(?,?,?,?,?)",
                [(owner, o, n, plan.reasons.get(o, "exact"), now) for o, n in plan.remap.items()])
            # Orphan guard: nothing may reference one of this owner's ids that no
            # longer has a chunk row. Raise INSIDE the transaction -> rollback.
            ids = sorted(set(old_ids) | set(new_ids))
            ph = ",".join("?" * len(ids))
            dangling = 0
            for table, col in _REFLOW_REF_COLUMNS:
                if table not in tables:
                    continue
                dangling += db.execute(
                    f"SELECT count(*) FROM {table} WHERE {col} IN ({ph}) "
                    f"AND {col} NOT IN (SELECT doc_id FROM chunks)", ids).fetchone()[0]
            if dangling:
                raise ReflowOrphanError(f"reflow {owner}: {dangling} dangling reference(s)")
            carried = sum(1 for r in plan.rows if r.covered)
            db.execute("INSERT OR REPLACE INTO reflow_owners(owner, source, at, chunks_new,"
                       " carried, reenrich) VALUES(?,?,?,?,?,?)",
                       (owner, source, now, len(plan.rows), carried,
                        sum(1 for r in plan.rows if not r.enriched)))
            db.execute("DELETE FROM reflow_tmp")
        return {"written": len(plan.rows), "carried": carried,
                "reenrich": sum(1 for r in plan.rows if not r.enriched),
                "deleted": len(plan.deletes), "remapped": remapped}
```

Verify `_connect(write=True)` rolls back when an exception escapes the `with` block (read `_connect` at `store.py:718`). If it does not, wrap the body in `try: ... except BaseException: db.rollback(); raise` — the orphan test is what proves it.

`read_doc` fallback — after the existing `row = self.get_chunk(doc_id)` early return and before the `note-` check:

```python
        target = self.latest_reflow_target(doc_id)
        if target and target != doc_id:
            row = self.get_chunk(target)
            if row is not None:
                return row
```

`drain._resolve_doc_ids`: in the `part_doc_ids` branch, before `store.drop_cold(...)`, replace each id that has no chunk row with `store.latest_reflow_target(id)` when that exists (drop it otherwise), de-duplicated in order.

`org_contrib._chunk_provenance`: when the `SELECT` returns `None`, try `t = store.latest_reflow_target(doc_id)` and, if present, re-run the same `SELECT` for `t` before failing closed.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_store_reflow.py tests/test_drain.py tests/test_org_contrib.py -q -n0`
Expected: PASS. (If `entities`/`entity_relations` columns in `_relation` differ, adjust the INSERT to the real schema — `grep -n "CREATE TABLE IF NOT EXISTS entity_relations" -A12 mcpbrain/store.py`.)

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/store.py mcpbrain/drain.py mcpbrain/org_contrib.py tests/test_store_reflow.py
git commit -m "feat(store): apply_reflow — one-transaction carry-over with simultaneous remap, reflow_map log and in-transaction orphan guard"
```

---

### Task 11: Reflow handlers and queue wiring

**Files:**
- Create: `mcpbrain/sync/reflow_handler.py`
- Modify: `mcpbrain/sync/queue.py` (`DEFER` sentinel), `mcpbrain/sync/__init__.py` (register `handlers["reflow"]`), `mcpbrain/config.py` (`reflow_enabled`)
- Test: `tests/test_reflow_handler.py`

**Interfaces:**
- Consumes: `reflow.plan`, `Store.owner_chunks/apply_reflow`, `drive.fetch_content/normalise_drive/folder_path/handle_drive_item`, `gmail._fetch_one`, `normalise.normalise_gmail`, `gmail.handle_gmail_item`, `anarlog.connect_ro/read_session/normalise_session/handle_anarlog_item`, `calendar.normalise_calendar/handle_calendar_item`.
- Produces:
  - `queue.DEFER = object()`; `work_queue` leaves a row untouched (not completed, not failed) when a handler returns it.
  - `config.reflow_enabled(home) -> bool`.
  - `reflow_handler.ReflowContext(store, embedder, home, *, drive_service=None, gmail_service=None, calendar_service=None, anarlog_db=None, max_items=10, max_seconds=15.0, clock=time.monotonic)` with `.handle(item) -> None | DEFER`.
  - Halt flag: `store.get_cursor("reflow:halted")` non-empty ⇒ handler returns `DEFER` for everything.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_reflow_handler.py
import pytest

from mcpbrain.store import Store
from mcpbrain.sync import queue
from mcpbrain.sync.reflow_handler import ReflowContext

PDF = "application/pdf"


class _Emb:
    dim = 4
    def embed_passages(self, xs):
        return [[0.1, 0.2, 0.3, 0.4] for _ in xs]


def _store(tmp_path):
    s = Store(tmp_path / "a.sqlite3", dim=4); s.init(); return s


def _seed_drive(s, fid="F", modified="2026-01-01T00:00:00Z", texts=("Budget Line one", "Line two")):
    for i, t in enumerate(texts):
        s.upsert_chunk(f"gdrive-{fid}-{i}", t, f"h{i}",
                       {"source_type": "gdrive", "file_id": fid, "mime_type": PDF,
                        "modified": modified, "chunk_index": i, "chunk_total": len(texts)})
        with s._connect(write=True) as db:
            db.execute("UPDATE chunks SET enriched=1 WHERE doc_id=?", (f"gdrive-{fid}-{i}",))


class _DriveSvc:
    """files().get returns fmeta; fetch_content is monkeypatched."""
    def __init__(self, modified):
        self.modified = modified
    def files(self):
        return self
    def get(self, **kw):
        m = self.modified
        class R:
            def execute(self, num_retries=0):
                return {"id": kw["fileId"], "name": "r.pdf", "mimeType": PDF,
                        "modifiedTime": m, "parents": []}
        return R()


def _ctx(s, tmp_path, **kw):
    return ReflowContext(s, _Emb(), str(tmp_path), **kw)


def test_reflow_drive_unchanged_carries_over(tmp_path, monkeypatch):
    from mcpbrain.sync import drive
    from mcpbrain.sync.blocks import Heading, Paragraph
    s = _store(tmp_path); _seed_drive(s)
    monkeypatch.setattr(drive, "fetch_content", lambda svc, fm, **k: drive.Content(
        text="Budget\n\nLine one\nLine two",
        blocks=[Heading(1, "Budget"), Paragraph("Line one\nLine two")]))
    monkeypatch.setattr(drive, "folder_path", lambda *a, **k: "")
    ctx = _ctx(s, tmp_path, drive_service=_DriveSvc("2026-01-01T00:00:00Z"))
    assert ctx.handle({"source": "reflow:drive", "ref_id": "F", "attempts": 0}) is None
    rows = s.owner_chunks(["gdrive-F-"])
    assert len(rows) == 1 and rows[0]["enriched"] == 1
    assert rows[0]["metadata"]["extraction_version"] == 1


def test_reflow_drive_changed_file_takes_normal_path(tmp_path, monkeypatch):
    from mcpbrain.sync import drive
    s = _store(tmp_path); _seed_drive(s)
    called = {}
    monkeypatch.setattr(drive, "handle_drive_item",
                        lambda svc, st, item, **k: called.setdefault("item", item))
    ctx = _ctx(s, tmp_path, drive_service=_DriveSvc("2026-05-05T00:00:00Z"))
    ctx.handle({"source": "reflow:drive", "ref_id": "F", "attempts": 0})
    assert called["item"]["event"] == "upsert" and called["item"]["ref_id"] == "F"


def test_reflow_empty_extraction_fails_without_deleting(tmp_path, monkeypatch):
    from mcpbrain.sync import drive
    s = _store(tmp_path); _seed_drive(s)
    monkeypatch.setattr(drive, "fetch_content", lambda *a, **k: drive.Content())
    ctx = _ctx(s, tmp_path, drive_service=_DriveSvc("2026-01-01T00:00:00Z"))
    with pytest.raises(RuntimeError):
        ctx.handle({"source": "reflow:drive", "ref_id": "F", "attempts": 0})
    assert len(s.owner_chunks(["gdrive-F-"])) == 2


def test_reflow_gmail_404_stamps_and_completes(tmp_path, monkeypatch):
    from mcpbrain.sync import gmail
    s = _store(tmp_path)
    s.upsert_chunk("gmail-M-body-0", "hi", "h", {"source_type": "gmail", "message_id": "M",
                   "chunk_index": 0, "chunk_total": 2})
    monkeypatch.setattr(gmail, "_fetch_one", lambda *a, **k: (None, []))
    ctx = _ctx(s, tmp_path, gmail_service=object())
    assert ctx.handle({"source": "reflow:gmail", "ref_id": "M", "attempts": 0}) is None
    md = s.owner_chunks(["gmail-M-"])[0]["metadata"]
    assert md["split_version"] == 1 and md["reflow_skipped"] == "source_gone"


def test_reflow_gmail_changed_pdf_attachment_still_carries_over(tmp_path, monkeypatch):
    """A block-extracted attachment's text is EXPECTED to differ; that must not
    be read as 'the message changed' (which would skip the carry-over)."""
    from mcpbrain.sync import gmail
    from mcpbrain.sync.normalise import Chunk
    s = _store(tmp_path)
    s.upsert_chunk("gmail-M-body-0", "hello there friend", "b0",
                   {"source_type": "gmail", "message_id": "M", "chunk_index": 0, "chunk_total": 1})
    s.upsert_chunk("gmail-M-att-0-0", "Item Cost Chairs 120", "a0",
                   {"source_type": "gmail", "message_id": "M", "attachment_mime": PDF,
                    "chunk_index": 0, "chunk_total": 1})
    body = Chunk("gmail-M-body-0", "hello there friend", "b0",
                 {"source_type": "gmail", "message_id": "M", "chunk_index": 0, "chunk_total": 1,
                  "split_version": 1}, ["hello there friend"])
    att = Chunk("gmail-M-att-0-0", "Table\nItem: Chairs; Cost: 120", "a1",
                {"source_type": "gmail", "message_id": "M", "attachment_mime": PDF,
                 "extraction_version": 1, "chunk_index": 0, "chunk_total": 1},
                ["Item", "Cost", "Chairs", "120"])
    monkeypatch.setattr(gmail, "_fetch_one", lambda *a, **k: ({"id": "M"}, [att]))
    import mcpbrain.sync.normalise as nm
    monkeypatch.setattr(nm, "normalise_gmail", lambda raw, **k: [body])
    called = {}
    monkeypatch.setattr(gmail, "handle_gmail_item", lambda *a, **k: called.setdefault("n", 1))
    ctx = _ctx(s, tmp_path, gmail_service=object())
    assert ctx.handle({"source": "reflow:gmail", "ref_id": "M", "attempts": 0}) is None
    assert "n" not in called                       # did NOT take the ordinary path
    md = {r["doc_id"]: r["metadata"] for r in s.owner_chunks(["gmail-M-"])}
    assert md["gmail-M-att-0-0"]["extraction_version"] == 1


def test_cap_defers_after_max_items(tmp_path, monkeypatch):
    s = _store(tmp_path)
    ctx = _ctx(s, tmp_path, max_items=0)
    assert ctx.handle({"source": "reflow:drive", "ref_id": "F", "attempts": 0}) is queue.DEFER


def test_halted_defers(tmp_path):
    s = _store(tmp_path)
    s.set_cursor("reflow:halted", "orphan in X")
    assert _ctx(s, tmp_path).handle({"source": "reflow:drive", "ref_id": "F",
                                     "attempts": 0}) is queue.DEFER


def test_owner_in_pending_enrich_unit_defers(tmp_path):
    import json, os
    s = _store(tmp_path); _seed_drive(s)
    units = tmp_path / "enrich_queue" / "units"; os.makedirs(units)
    (units / "u-1.json").write_text(json.dumps({"unit_id": "u-1", "kind": "thread",
        "threads": [{"thread_id": "F", "messages": [{"message_id": "F",
                     "chunk_doc_ids": ["gdrive-F-0"]}]}]}))
    ctx = _ctx(s, tmp_path, drive_service=_DriveSvc("2026-01-01T00:00:00Z"))
    assert ctx.handle({"source": "reflow:drive", "ref_id": "F", "attempts": 0}) is queue.DEFER


def test_work_queue_leaves_deferred_rows(tmp_path):
    s = _store(tmp_path)
    s.enqueue_items([{"ref_id": "F", "event": "reflow", "modified_at": "1970-01-01T00:00:00"}],
                    source="reflow:drive")
    out = queue.work_queue(s, handlers={"reflow": lambda it: queue.DEFER}, limit=5)
    assert out == {"processed": 0, "failed": 0}
    assert s.reflow_stats()["queued"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_reflow_handler.py -q -n0`
Expected: FAIL with `ModuleNotFoundError: mcpbrain.sync.reflow_handler`.

- [ ] **Step 3: Implement**

`queue.py`: add `DEFER = object()  # handler return: leave the row as-is, try again next cycle` at module level; in `work_queue`, replace `handler(item)` with:

```python
            result = handler(item)
        except Exception as exc:  # (existing except block unchanged)
            ...
        if result is DEFER:
            continue
        store.complete_sync_item(source, item["ref_id"])
```

(Keep the existing `try/except` structure; only capture `result` and add the `DEFER` check before `complete_sync_item`.)

`config.py`, beside `retrieval_expand_enabled`:

```python
def reflow_enabled(home) -> bool:
    """Kill switch for the background reflow (2026-09-24 extraction-fidelity).
    Fleet-flippable; the new extractors apply to fresh ingests regardless."""
    return bool(fleet_flag(home, "reflow_enabled", True))
```

`mcpbrain/sync/reflow_handler.py`:

```python
"""Queue handler for `reflow:<source>` items (spec §4).

Re-fetch one owner, prove its source unchanged, re-extract and re-chunk it,
then apply reflow.plan via Store.apply_reflow. A changed source takes the
source's ORDINARY handler instead. Never deletes on an empty/partial
re-extraction: that raises, so work_queue backs the item off.
"""
import json
import logging
import time
from pathlib import Path

from mcpbrain import config, reflow
from mcpbrain.chunking import SPLIT_VERSION
from mcpbrain.embed import contextual_prefix
from mcpbrain.store import ReflowOrphanError
from mcpbrain.sync import queue
from mcpbrain.sync.blocks import extraction_version

log = logging.getLogger("mcpbrain.sync.reflow")
HALT_CURSOR = "reflow:halted"
_GIVE_UP_ATTEMPTS = 5


class ReflowContext:
    def __init__(self, store, embedder, home, *, drive_service=None, gmail_service=None,
                 calendar_service=None, anarlog_db=None, max_items: int = 10,
                 max_seconds: float = 15.0, clock=time.monotonic):
        self.store, self.embedder, self.home = store, embedder, str(home)
        self.drive, self.gmail, self.calendar, self.anarlog_db = (
            drive_service, gmail_service, calendar_service, anarlog_db)
        self.max_items, self.max_seconds, self.clock = max_items, max_seconds, clock
        self._done = 0
        self._started = None
        self._unit_refs = None

    # ---- guards -----------------------------------------------------------
    def _pending_unit_refs(self) -> set[str]:
        if self._unit_refs is None:
            refs: set[str] = set()
            for p in (Path(self.home) / "enrich_queue" / "units").glob("*.json"):
                try:
                    u = json.loads(p.read_text())
                except (OSError, ValueError):
                    continue
                for t in u.get("threads", []) or []:
                    refs.add(str(t.get("thread_id", "")))
                    refs.update(t.get("part_doc_ids") or [])
                    for m in t.get("messages", []) or []:
                        refs.add(str(m.get("message_id", "")))
                        refs.update(m.get("chunk_doc_ids") or [])
                for it in u.get("items", []) or []:
                    if isinstance(it, dict):
                        refs.update(str(v) for v in it.values() if isinstance(v, str))
            self._unit_refs = refs
        return self._unit_refs

    def _over_cap(self) -> bool:
        if self._started is None:
            self._started = self.clock()
        return self._done >= self.max_items or self.clock() - self._started > self.max_seconds

    # ---- entry ------------------------------------------------------------
    def handle(self, item):
        if self.store.get_cursor(HALT_CURSOR):
            return queue.DEFER
        if self._over_cap():
            return queue.DEFER
        kind = item["source"].split(":", 1)[1]
        owner = item["ref_id"]
        prefixes = self._prefixes(kind, owner)
        old = self.store.owner_chunks(prefixes)
        if not old:
            return None                                     # nothing left to reflow
        refs = self._pending_unit_refs()
        if owner in refs or any(r["doc_id"] in refs for r in old):
            return queue.DEFER
        self._done += 1
        if int(item.get("attempts") or 0) >= _GIVE_UP_ATTEMPTS:
            self._stamp(old, "gave_up")
            return None
        new = getattr(self, f"_new_{kind}")(owner, old)
        if new is None:
            return None                                     # routed to normal path / stamped
        p = reflow.plan(old, new)
        if kind in ("gmail", "anarlog", "calendar") and self._source_changed(p, new):
            # These sources' prose extraction is unchanged, so differing text
            # means the SOURCE changed: take the ordinary path. A Gmail
            # attachment whose MIME has a block extractor is EXPECTED to differ.
            self._normal(kind, owner)
            return None
        vectors = self._embed([r.chunk for r in p.rows])
        try:
            self.store.apply_reflow(owner, kind, p, vectors, home=self.home)
        except ReflowOrphanError as exc:
            self.store.set_cursor(HALT_CURSOR, str(exc)[:500])
            log.error("reflow halted: %s", exc)
            raise
        return None

    # ---- helpers ----------------------------------------------------------
    @staticmethod
    def _prefixes(kind: str, owner: str) -> list[str]:
        return {"drive": [f"gdrive-{owner}-"], "gmail": [f"gmail-{owner}-"],
                "anarlog": [f"anarlog-{owner}-"], "calendar": [f"cal-{owner}"]}[kind]

    @staticmethod
    def _source_changed(p, new) -> bool:
        mime_by_lineage = {reflow.lineage_key(c.doc_id, c.metadata):
                           (c.metadata or {}).get("attachment_mime", "") for c in new}
        return any(not extraction_version(mime_by_lineage.get(k, "")) for k in p.unequal)

    def _embed(self, chunks) -> list:
        use = config.contextual_retrieval_enabled(self.home)
        passages = [(contextual_prefix(c.metadata) + c.text) if use else c.text for c in chunks]
        return self.embedder.embed_passages(passages)

    def _stamp(self, old, reason: str) -> None:
        for r in old:
            md = r["metadata"] or {}
            patch = {"split_version": SPLIT_VERSION, "reflow_skipped": reason}
            mime = md.get("mime_type") or md.get("attachment_mime") or ""
            if extraction_version(mime):
                patch["extraction_version"] = extraction_version(mime)
            self.store.patch_chunk_metadata(r["doc_id"], **patch)

    def _normal(self, kind: str, owner: str) -> None:
        item = {"ref_id": owner, "event": "upsert", "version": "",
                "modified_at": "1970-01-01T00:00:00"}
        if kind == "drive":
            from mcpbrain.sync import drive
            drive.handle_drive_item(self.drive, self.store, item, folder_cache={})
        elif kind == "gmail":
            from mcpbrain.sync import gmail
            gmail.handle_gmail_item(self.gmail, self.store, item, fetch_attachments=True)
        elif kind == "anarlog":
            from mcpbrain.sync import anarlog
            anarlog.handle_anarlog_item(self.store, item, db_path=self.anarlog_db)
        elif kind == "calendar":
            from mcpbrain.sync import calendar
            calendar.handle_calendar_item(self.calendar, self.store, item)

    def _new_drive(self, fid, old):
        from mcpbrain.sync import drive
        fmeta = self.drive.files().get(
            fileId=fid, supportsAllDrives=True,
            fields="id,name,mimeType,modifiedTime,version,parents,md5Checksum,size,owners"
        ).execute(num_retries=3)
        stored = (old[0]["metadata"] or {}).get("modified", "")
        if fmeta.get("modifiedTime", "") != stored:
            self._normal("drive", fid)
            return None
        content = drive.fetch_content(self.drive, fmeta, store=self.store)
        if content is None:
            self._stamp(old, "unsupported")
            return None
        if content.partial or (not content.text and not content.tables):
            raise RuntimeError(f"reflow {fid}: empty or partial re-extraction")
        drive_id = (old[0]["metadata"] or {}).get(drive.DRIVE_ID_META_KEY)
        chunks = drive.normalise_drive(fmeta, content.text, drive_id=drive_id,
                                       tables=content.tables, blocks=content.blocks,
                                       folder=drive.folder_path(self.drive, fmeta, {}))
        if not chunks:
            raise RuntimeError(f"reflow {fid}: re-extraction produced no chunks")
        if drive_id:
            from mcpbrain.sync.drive import _file_content_hash
            self.store.record_pending_publish(drive_id, fid, _file_content_hash(fmeta))
        return chunks

    def _new_gmail(self, mid, old):
        from mcpbrain.sync import gmail
        from mcpbrain.sync.normalise import normalise_gmail
        raw, atts = gmail._fetch_one(self.gmail, mid, fetch_attachments=True, att_report=None)
        if raw is None:
            self._stamp(old, "source_gone")
            return None
        chunks = normalise_gmail(raw) + list(atts)
        if not chunks:
            raise RuntimeError(f"reflow {mid}: re-extraction produced no chunks")
        return chunks

    def _new_anarlog(self, sid, old):
        from mcpbrain.sync import anarlog
        with anarlog.connect_ro(self.anarlog_db) as db:
            session = anarlog.read_session(db, sid)
        if session is None:
            self._stamp(old, "source_gone")
            return None
        return anarlog.normalise_session(session) or None

    def _new_calendar(self, eid, old):
        from mcpbrain.sync import calendar
        ev = self.calendar.events().get(calendarId="primary", eventId=eid).execute(num_retries=3)
        chunks = calendar.normalise_calendar(ev)
        if not chunks:
            self._stamp(old, "source_gone")
            return None
        return chunks
```

(`record_pending_publish` makes a shared-drive file republish under the new fingerprint after it is embedded; confirm the method name with `grep -n "def record_pending_publish" mcpbrain/store.py`. Confirm `drive.DRIVE_ID_META_KEY` exists; it is used by `normalise_drive`.)

`sync/__init__.py` — after the `anarlog` handler registration and before `work_queue(...)`:

```python
    if home and config.reflow_enabled(home):
        from mcpbrain.sync.reflow_handler import ReflowContext
        _reflow = ReflowContext(store, embedder, home, drive_service=drive_service,
                                gmail_service=gmail_service,
                                calendar_service=calendar_service, anarlog_db=anarlog_db)
        handlers["reflow"] = _reflow.handle
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_reflow_handler.py tests/test_sync_queue.py tests/test_sync_cycle.py -q -n0`
Expected: PASS (use `ls tests | grep -E "queue|sync_cycle"` for the real queue/cycle test names).

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/sync/reflow_handler.py mcpbrain/sync/queue.py mcpbrain/sync/__init__.py mcpbrain/config.py tests/test_reflow_handler.py
git commit -m "feat(reflow): queue handler — prove unchanged, re-extract, carry over; DEFER, caps, enrich-unit guard, halt"
```

---

### Task 12: Seed cadence, selector, backup gate, attended `bin/reflow.py`

**Files:**
- Modify: `mcpbrain/store.py` (`reflow_candidates`)
- Modify: `mcpbrain/daemon.py` (`CadencePass("reflow_seed", ...)`, `_run_reflow_seed`, `_CADENCE_DEFAULTS`/`_CADENCE_KEYS`, attrs in `__init__`, interval wiring at the config-apply path)
- Create: `bin/reflow.py`
- Test: `tests/test_reflow_seed.py`

**Interfaces:**
- Produces:
  - `Store.reflow_candidates(limit: int) -> list[tuple[str, str]]` — `(source, owner)` pairs, `source ∈ {"reflow:drive","reflow:gmail","reflow:anarlog","reflow:calendar"}`, excluding owners already queued.
  - `Daemon._run_reflow_seed() -> dict | None`; cadence key `reflow_seed_interval_s` default `3600.0`.
  - `bin/reflow.py status|resume` (attended).

Selector rules (SQL, one query per rule, `LIMIT`):
1. **Drive block MIMEs:** `source_type='gdrive' AND mime_type IN (<EXTRACTION_VERSIONS keys>) AND COALESCE(extraction_version,0) < <version>` → owner `file_id`.
2. **Gmail attachments:** `source_type='gmail' AND attachment_mime IN (...) AND COALESCE(extraction_version,0) < <version>` → owner `message_id`.
3. **Split rule:** `COALESCE(split_version,0) < SPLIT_VERSION AND chunk_total > 1 AND COALESCE(content_subtype,'') != 'table'` for `gdrive` (owner `file_id`), `gmail` (owner `message_id`), `anarlog` (owner `session_id`), `calendar` (owner `event_id`).
All rules skip chunks carrying `reflow_skipped` when `split_version`/`extraction_version` are already current (the stamp makes them non-matching automatically — no extra clause needed). Build the version comparison per MIME with a `CASE mime WHEN ? THEN ? ... END` generated from `EXTRACTION_VERSIONS`. Use `_meta_extract("$.field")` for every JSON field (the codebase's single source of truth for metadata reads).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_reflow_seed.py
import json
import time

from mcpbrain.store import Store


def _store(tmp_path):
    s = Store(tmp_path / "a.sqlite3", dim=4); s.init(); return s


def _c(s, doc_id, **md):
    s.upsert_chunk(doc_id, "t " + doc_id, doc_id, md)


def test_selector_rules(tmp_path):
    s = _store(tmp_path)
    _c(s, "gdrive-A-0", source_type="gdrive", file_id="A", mime_type="application/pdf",
       chunk_total=1)                                            # rule 1 (single chunk still)
    _c(s, "gdrive-B-0", source_type="gdrive", file_id="B", mime_type="application/pdf",
       chunk_total=1, extraction_version=1)                       # current -> skip
    _c(s, "gdrive-C-0", source_type="gdrive", file_id="C", mime_type="text/plain",
       chunk_total=2)                                            # rule 3
    _c(s, "gdrive-D-0", source_type="gdrive", file_id="D", mime_type="text/plain",
       chunk_total=1)                                            # single chunk -> skip
    _c(s, "gdrive-X-0", source_type="gdrive", file_id="X", content_subtype="table",
       mime_type="text/csv", chunk_total=5)                      # table -> skip
    _c(s, "gmail-M-att-0-0", source_type="gmail", message_id="M",
       attachment_mime="application/pdf", chunk_total=1)         # rule 2
    _c(s, "gmail-N-body-0", source_type="gmail", message_id="N", chunk_total=2)   # rule 3
    _c(s, "gmail-O-body-0", source_type="gmail", message_id="O", chunk_total=2,
       split_version=1)                                          # current -> skip
    got = set(s.reflow_candidates(50))
    assert got == {("reflow:drive", "A"), ("reflow:drive", "C"),
                   ("reflow:gmail", "M"), ("reflow:gmail", "N")}


def test_selector_excludes_queued(tmp_path):
    s = _store(tmp_path)
    _c(s, "gmail-N-body-0", source_type="gmail", message_id="N", chunk_total=2)
    s.enqueue_items([{"ref_id": "N", "event": "reflow",
                      "modified_at": "1970-01-01T00:00:00"}], source="reflow:gmail")
    assert s.reflow_candidates(50) == []


def test_seed_requires_recent_backup_and_tops_up_window(tmp_path, monkeypatch):
    from mcpbrain import daemon as dmod
    s = _store(tmp_path)
    _c(s, "gmail-N-body-0", source_type="gmail", message_id="N", chunk_total=2)
    d = dmod.Daemon.__new__(dmod.Daemon)          # minimal instance, as other cadence tests do
    d._store, d._clock = s, time.monotonic
    d._reflow_seed_interval_s, d._last_reflow_seed = 1.0, None
    monkeypatch.setattr(dmod, "app_dir", lambda: tmp_path)
    assert d._run_reflow_seed() == {"reflow_seed": "no_recent_backup"}
    (tmp_path / "backup_state.json").write_text(json.dumps({"last_success": time.time()}))
    d._last_reflow_seed = None
    assert d._run_reflow_seed()["enqueued"] == 1
```

Before writing `test_seed_requires_recent_backup_and_tops_up_window`, read one existing cadence test (`grep -rln "_run_action_hygiene" tests`) and build the daemon the SAME way it does; replace the `__new__` lines with that pattern if it differs.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_reflow_seed.py -q -n0`
Expected: FAIL (`AttributeError: 'Store' object has no attribute 'reflow_candidates'`).

- [ ] **Step 3: Implement**

`store.py`:

```python
    def reflow_candidates(self, limit: int) -> list[tuple[str, str]]:
        """Level-triggered reflow selector (spec §4). An owner stops matching
        once its chunks carry the current split/extraction versions."""
        from mcpbrain.chunking import SPLIT_VERSION
        from mcpbrain.sync.blocks import EXTRACTION_VERSIONS
        mimes = list(EXTRACTION_VERSIONS)
        mph = ",".join("?" * len(mimes))
        case = "CASE {m} " + " ".join("WHEN ? THEN ?" for _ in mimes) + " ELSE 0 END"
        case_args = [x for m in mimes for x in (m, EXTRACTION_VERSIONS[m])]
        st, xv, sv = (_meta_extract("$.source_type"), _meta_extract("$.extraction_version"),
                      _meta_extract("$.split_version"))
        total, sub = _meta_extract("$.chunk_total"), _meta_extract("$.content_subtype")
        rules = [
            ("reflow:drive", "$.file_id",
             f"{st}='gdrive' AND {_meta_extract('$.mime_type')} IN ({mph}) "
             f"AND COALESCE({xv},0) < {case.format(m=_meta_extract('$.mime_type'))}",
             mimes + case_args),
            ("reflow:gmail", "$.message_id",
             f"{st}='gmail' AND {_meta_extract('$.attachment_mime')} IN ({mph}) "
             f"AND COALESCE({xv},0) < {case.format(m=_meta_extract('$.attachment_mime'))}",
             mimes + case_args),
        ]
        for src, stype, fld in (("reflow:drive", "gdrive", "$.file_id"),
                                ("reflow:gmail", "gmail", "$.message_id"),
                                ("reflow:anarlog", "anarlog", "$.session_id"),
                                ("reflow:calendar", "calendar", "$.event_id")):
            rules.append((src, fld,
                          f"{st}=? AND COALESCE({sv},0) < ? AND COALESCE({total},1) > 1 "
                          f"AND COALESCE({sub},'') != 'table'",
                          [stype, SPLIT_VERSION]))
        out: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        with self._connect() as db:
            queued = {(r[0], r[1]) for r in db.execute(
                "SELECT source, ref_id FROM sync_queue WHERE source LIKE 'reflow:%'")}
            for src, fld, where, args in rules:
                owner = _meta_extract(fld)
                for r in db.execute(
                        f"SELECT DISTINCT {owner} AS o FROM chunks WHERE {where} "
                        f"AND {owner} IS NOT NULL LIMIT ?", [*args, limit]):
                    key = (src, str(r["o"]))
                    if key in queued or key in seen:
                        continue
                    seen.add(key)
                    out.append(key)
                    if len(out) >= limit:
                        return out
        return out
```

`daemon.py`:
- `_CADENCE_PASSES`: add `CadencePass("reflow_seed", "_reflow_seed_interval_s", "_last_reflow_seed", "_run_reflow_seed"),`.
- `__init__`: `self._reflow_seed_interval_s: float | None = None; self._last_reflow_seed = None`.
- `_CADENCE_DEFAULTS["reflow_seed_interval_s"] = 3600.0`, add `"reflow_seed_interval_s"` to `_CADENCE_KEYS`, and assign `self._reflow_seed_interval_s = cadences["reflow_seed_interval_s"]` beside the other assignments in the config-apply path.
- Constants near the other module constants: `REFLOW_WINDOW = 200`, `REFLOW_BACKUP_MAX_AGE_S = 86400.0`.
- Method:

```python
    def _run_reflow_seed(self):
        """Top the reflow queue up to REFLOW_WINDOW (spec §4). Gated on the
        kill switch, the halt flag and a backup that succeeded in the last 24 h."""
        if not self._is_due("_reflow_seed_interval_s", "_last_reflow_seed"):
            return None
        home = str(app_dir())
        self._last_reflow_seed = self._clock()
        if not config.reflow_enabled(home):
            return {"reflow_seed": "disabled"}
        if self._store.get_cursor("reflow:halted"):
            return {"reflow_seed": "halted"}
        from mcpbrain.probes import _read_backup_state
        st = _read_backup_state(home) or {}
        try:
            fresh = time.time() - float(st.get("last_success") or 0) <= REFLOW_BACKUP_MAX_AGE_S
        except (TypeError, ValueError):
            fresh = False
        if not fresh:
            return {"reflow_seed": "no_recent_backup"}
        try:
            room = REFLOW_WINDOW - self._store.reflow_stats()["queued"]
            if room <= 0:
                return {"reflow_seed": "window_full", "enqueued": 0}
            by_src: dict[str, list[dict]] = {}
            for src, owner in self._store.reflow_candidates(room):
                by_src.setdefault(src, []).append(
                    {"ref_id": owner, "event": "reflow", "modified_at": "1970-01-01T00:00:00"})
            n = sum(self._store.enqueue_items(items, source=src) for src, items in by_src.items())
            if n == 0 and self._store.reflow_stats()["queued"] == 0:
                self._reflow_backlog_empty()
            return {"reflow_seed": "ok", "enqueued": n}
        except Exception as exc:  # noqa: BLE001
            log.warning("reflow_seed failed: %s", exc, exc_info=True)
            return {"reflow_seed": False, "error": str(exc)}

    def _reflow_backlog_empty(self):
        """Once per completion: run integrity_check and record the result."""
        if self._store.get_cursor("reflow:integrity_checked"):
            return
        from mcpbrain.doctor import _run_integrity_check
        problems = _run_integrity_check(str(app_dir()))
        self._store.set_cursor("reflow:integrity_checked",
                               "ok" if not problems else f"{len(problems)} problem(s)")
        if problems:
            log.error("reflow complete but integrity_check reported %d problem(s)", len(problems))
```

(Use the module's existing `time`, `config`, `app_dir` imports; add any that are missing.)

`bin/reflow.py`:

```python
#!/usr/bin/env python3
"""Attended reflow control.

  python bin/reflow.py status          show progress and whether reflow is halted
  python bin/reflow.py resume --yes    clear a halt AFTER investigating it

A halt means apply_reflow found a dangling reference and rolled back. Read the
daemon log and the failing item's last_error before resuming.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcpbrain import config            # noqa: E402
from mcpbrain.store import Store       # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="reflow")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    r = sub.add_parser("resume")
    r.add_argument("--yes", action="store_true")
    ns = ap.parse_args(argv)
    home = str(config.app_dir())
    store = Store(config.store_path(), dim=config.embed_dim(home))
    halted = store.get_cursor("reflow:halted") or ""
    if ns.cmd == "status":
        print({**store.reflow_stats(), "halted": halted or None,
               "integrity": store.get_cursor("reflow:integrity_checked")})
        return 0
    if not halted:
        print("reflow is not halted")
        return 0
    if not ns.yes:
        print(f"halted: {halted}\nre-run with --yes to clear")
        return 1
    store.set_cursor("reflow:halted", "")
    print("halt cleared")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Confirm the embed-dim accessor name (`grep -n "def embed_dim\|def store_dim\|dim=" mcpbrain/config.py bin/*.py | head`) and use whatever existing `bin/` scripts use to construct a `Store` — the plan's own history records three bugs from getting this constructor wrong.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_reflow_seed.py tests/test_daemon_cadences.py -q -n0` (use `ls tests | grep -i cadence` for the real cadence test file) and `uv run python bin/reflow.py status` against a scratch home (`MCPBRAIN_HOME=$(mktemp -d)`; confirm the env var name with `grep -n "MCPBRAIN_HOME" mcpbrain/config.py`).
Expected: PASS; `status` prints a dict.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/store.py mcpbrain/daemon.py bin/reflow.py tests/test_reflow_seed.py
git commit -m "feat(reflow): level-triggered seed cadence with backup gate, halt and completion integrity check; attended bin/reflow.py"
```

---

### Task 13: Carry-over on shared-drive cache import

**Files:**
- Modify: `mcpbrain/ingest_cache.py` (`_import_artifact`)
- Test: `tests/test_ingest_cache_reflow.py`

**Interfaces:**
- Consumes: `reflow.plan`, `Store.owner_chunks`, `Store.apply_reflow`.
- Produces: when `_import_artifact` is about to write rows for a file that already has local chunks whose metadata `modified` equals the artifact rows' `modified`, it builds `Chunk`s from the artifact rows, runs `reflow.plan(old, new)`, forces `row.enriched`/`enriched_version` to the artifact's enrich decision for covered rows only when the artifact itself says enriched (`mark_enriched`), and calls `store.apply_reflow(file_id, "drive_import", plan, vectors)` instead of the plain write. Otherwise the existing write path is unchanged.

- [ ] **Step 1: Write the failing test**

Model it on `tests/test_a4_enrich_cache.py` (copy its `_store`, pin and `LocalDirFleetStorage` setup). Arrange: install A has old chunks `gdrive-F-0/1` for file F (enriched, a relation on `gdrive-F-1`, metadata `modified` = M). Publish an artifact for F from a second store whose chunks are the NEW single chunk (same `modified` M, `extraction_version` 1). Import into A with `try_import(..., mime="application/pdf")`. Assert: A now has one chunk `gdrive-F-0`; the relation points at `gdrive-F-0`; `reflow_map` has two rows; the chunk is enriched.

A second test: artifact `modified` differs from local → plain replace path (existing behaviour), relation untouched.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_ingest_cache_reflow.py -q -n0`
Expected: FAIL (relation still points at `gdrive-F-1`, which no longer exists).

- [ ] **Step 3: Implement**

Read `ingest_cache._import_artifact` (≈ lines 113-215). Immediately before the `with store._connect(write=True) as db:` block that writes `rows`:

```python
    old = store.owner_chunks([f"gdrive-{art.file_id}-"])
    new_modified = (rows[0]["metadata"] or {}).get("modified") if rows else None
    if old and new_modified and all((r["metadata"] or {}).get("modified") == new_modified
                                    for r in old):
        from mcpbrain import reflow
        from mcpbrain.sync.normalise import Chunk
        new = [Chunk(r["doc_id"], r["text"], r["content_hash"], r["metadata"], [r["text"]])
               for r in rows]
        p = reflow.plan(old, new)
        if mark_enriched:
            for nr in p.rows:
                nr.enriched, nr.enriched_version = 1, logic_v
        store.apply_reflow(art.file_id, "drive_import", p, [r["vector"] for r in rows],
                           home=home)
        return True
```

Use the local variable names `_import_artifact` actually has for "mark enriched", "logic version", "home" and the decoded vector (read the function first; rename in the snippet, not in the function). Return whatever the function returns on success today.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_ingest_cache_reflow.py tests/test_a4_enrich_cache.py tests/test_ingest_cache.py -q -n0`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/ingest_cache.py tests/test_ingest_cache_reflow.py
git commit -m "feat(ingest-cache): importing a re-chunked artifact for unchanged content carries enrichment and provenance over"
```

---

### Task 14: Visibility — `/api/status`, `doctor`, dashboard

**Files:**
- Modify: `mcpbrain/daemon.py` (`status()`), `mcpbrain/doctor.py` (`reflow_line`), `mcpbrain/dashboard.py` (`stats()`)
- Test: `tests/test_reflow_visibility.py`

**Interfaces:**
- Produces: `status()["reflow"] = {"owners_done", "chunks_carried", "chunks_reenrich", "queued", "halted": str|None, "integrity": str|None}`; `doctor.reflow_line(store) -> str`; `stats(...)["reflow"]` passthrough of `status["reflow"]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_reflow_visibility.py
from mcpbrain.doctor import reflow_line
from mcpbrain.store import Store


def _store(tmp_path):
    s = Store(tmp_path / "a.sqlite3", dim=4); s.init(); return s


def test_reflow_line_idle_progress_and_halt(tmp_path):
    s = _store(tmp_path)
    assert reflow_line(s).startswith("✅")
    s.enqueue_items([{"ref_id": "F", "event": "reflow", "modified_at": "1970-01-01T00:00:00"}],
                    source="reflow:drive")
    assert "1 queued" in reflow_line(s)
    s.set_cursor("reflow:halted", "reflow F: 1 dangling reference(s)")
    line = reflow_line(s)
    assert line.startswith("❌") and "bin/reflow.py" in line
```

Plus, in the same file, a dashboard test that `dashboard.stats(store, home, {"reflow": {"queued": 3}})["reflow"] == {"queued": 3}` (build `store`/`home` the way `tests/test_dashboard*.py` does).

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_reflow_visibility.py -q -n0`
Expected: FAIL (`ImportError: reflow_line`).

- [ ] **Step 3: Implement**

`doctor.py`:

```python
def reflow_line(store) -> str:
    try:
        st = store.reflow_stats()
        halted = store.get_cursor("reflow:halted")
    except Exception as exc:  # noqa: BLE001
        return f"➖ {'Reflow':<16} skipped ({exc})"
    if halted:
        return (f"❌ {'Reflow':<16} HALTED: {halted} — investigate, then "
                f"`python bin/reflow.py resume --yes`")
    if st["queued"]:
        return (f"⏳ {'Reflow':<16} {st['queued']} queued, {st['owners_done']} done "
                f"({st['chunks_carried']} chunks carried, {st['chunks_reenrich']} re-enrich)")
    return f"✅ {'Reflow':<16} idle ({st['owners_done']} owners reflowed)"
```

Append `lines.append(reflow_line(store))` next to `lines.append(integrity_line(home))` in `run_doctor`, using the read-only store `run_doctor` already opens.

`daemon.status()`: before the return dict, following the `org` block's try/except pattern:

```python
        reflow = None
        try:
            reflow = {**self._store.reflow_stats(),
                      "halted": self._store.get_cursor("reflow:halted") or None,
                      "integrity": self._store.get_cursor("reflow:integrity_checked") or None}
        except Exception as exc:  # noqa: BLE001
            log.debug("status: reflow block degraded: %s", exc)
```

and add `"reflow": reflow,` to the returned dict.

`dashboard.stats()`: add `"reflow": status.get("reflow"),` to the returned dict. If the dashboard HTML renders a per-section list from this payload, add a one-line "Reflow" row there showing `queued`/`owners_done`/`halted`; if it does not render arbitrary sections, the payload field is enough.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_reflow_visibility.py tests/test_doctor.py tests/test_dashboard.py tests/test_daemon_status.py -q -n0` (use `ls tests | grep -E "doctor|dashboard|status"`).
Expected: PASS. Tests snapshotting the full `status()` key set must add `"reflow"`.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/daemon.py mcpbrain/doctor.py mcpbrain/dashboard.py tests/
git commit -m "feat(reflow): progress and halt in /api/status, doctor and the dashboard payload"
```

---

### Task 15: `bin/tenant.py remap-gold`

**Files:**
- Modify: `bin/tenant.py`
- Test: `tests/test_tenant_remap_gold.py`

**Interfaces:**
- Produces: `tenant.remap_gold(gold_path: Path, store) -> list[tuple[str, str]]` — for each `expected_chunk_ids` entry with no chunk row and a `latest_reflow_target`, rewrite the id in place (textual replacement of the exact `- <id>` list line, so YAML comments survive); returns the replacements. CLI: `python bin/tenant.py remap-gold <gold.yaml> [--write]` (dry-run by default).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tenant_remap_gold.py
import importlib.util
from pathlib import Path

from mcpbrain.store import Store
from mcpbrain.reflow import plan
from mcpbrain.sync.normalise import Chunk

_spec = importlib.util.spec_from_file_location(
    "tenant_cli", Path(__file__).resolve().parent.parent / "bin" / "tenant.py")
tenant = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(tenant)


def test_remap_gold_rewrites_only_missing_ids(tmp_path):
    s = Store(tmp_path / "a.sqlite3", dim=4); s.init()
    for i, t in enumerate(("alpha", "beta")):
        s.upsert_chunk(f"gdrive-F-{i}", t, f"h{i}", {"source_type": "gdrive", "file_id": "F",
                       "chunk_index": i, "chunk_total": 2})
    old = s.owner_chunks(["gdrive-F-"])
    new = [Chunk("gdrive-F-0", "alpha\nbeta", "n", {"source_type": "gdrive", "file_id": "F",
                 "chunk_index": 0}, ["alpha", "beta"])]
    s.apply_reflow("F", "drive", plan(old, new), [[0.1, 0.2, 0.3, 0.4]])
    gold = tmp_path / "gold.yaml"
    gold.write_text("# keep me\n- id: c1\n  expected_chunk_ids:\n    - gdrive-F-1\n    - gmail-X-body-0\n")
    changes = tenant.remap_gold(gold, s)
    assert changes == [("gdrive-F-1", "gdrive-F-0")]
    text = gold.read_text()
    assert "# keep me" in text and "- gdrive-F-0" in text and "gdrive-F-1" not in text
    assert "- gmail-X-body-0" in text
```

(`remap_gold` itself always writes; the CLI's `--write` flag decides whether it is called or only previewed via a `dry_run=True` argument — give `remap_gold(gold_path, store, *, dry_run=False)`.)

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_tenant_remap_gold.py -q -n0`
Expected: FAIL (`AttributeError: remap_gold`).

- [ ] **Step 3: Implement** in `bin/tenant.py`

```python
def remap_gold(gold_path: Path, store, *, dry_run: bool = False) -> list[tuple[str, str]]:
    """Point gold expected_chunk_ids that a reflow removed at their new chunk.
    Textual edit of '- <id>' lines so the file's comments survive. Only ids with
    NO chunk row are touched; everything else is left exactly as written."""
    import re
    text = gold_path.read_text()
    changes: list[tuple[str, str]] = []

    def sub(m):
        old = m.group(2)
        if store.get_chunk(old) is not None:
            return m.group(0)
        new = store.latest_reflow_target(old)
        if not new or new == old:
            return m.group(0)
        changes.append((old, new))
        return f"{m.group(1)}{new}"

    out = re.sub(r"^(\s*-\s+)((?:gdrive|gmail|cal|anarlog)-\S+)\s*$", sub, text, flags=re.M)
    if changes and not dry_run:
        gold_path.write_text(out)
    return changes
```

In `main`, register before the `check` fall-through:

```python
    p_gold = sub.add_parser("remap-gold", help="repoint gold chunk ids moved by a reflow")
    p_gold.add_argument("gold", help="path to a gold YAML (in the tenant checkout)")
    p_gold.add_argument("--write", action="store_true")
```

and

```python
    if ns.cmd == "remap-gold":
        from mcpbrain import config
        from mcpbrain.store import Store
        store = Store(config.store_path(), dim=<same constructor bin/reflow.py uses>, read_only=True)
        changes = remap_gold(Path(ns.gold), store, dry_run=not ns.write)
        for o, n in changes:
            print(f"{o} -> {n}")
        print(f"{len(changes)} id(s) {'rewritten' if ns.write else 'would change'}")
        return 0
```

(`add_parser` calls must come before `ns = ap.parse_args(argv)`.)

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_tenant_remap_gold.py tests/test_tenant*.py -q -n0`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add bin/tenant.py tests/test_tenant_remap_gold.py
git commit -m "feat(tenant): remap-gold repoints gold chunk ids a reflow removed"
```

---

### Task 16: Dry run on a store copy, and the records

**Files:**
- Create: `bin/reflow_dryrun.py`
- Modify: `docs/RELEASE-RUNBOOK.md` (a short "Reflow rollout" subsection), `CLAUDE.md` (a short "extraction fidelity" entry under Shipping caveats: what shipped, the reflow gates, `bin/reflow.py`, not released)

**Interfaces:**
- Produces: `python bin/reflow_dryrun.py --store <copy.sqlite3> --limit 200 --yes` — runs `ReflowContext.handle` over up to `limit` `reflow_candidates` against the COPY using the real Google services and embedder (same construction as the daemon), `max_items=limit`, `max_seconds=10**9`; then prints: owners reflowed / skipped / failed, chunks carried vs re-enrich, the orphan count over the whole store (every `_REFLOW_REF_COLUMNS` reference with no chunk row), and `PRAGMA integrity_check`. Refuses to run against the live store path (compare to `config.store_path()` after `resolve()`).

- [ ] **Step 1: Write the script** (attended; no unit test — it is the verification tool). Build services and the embedder exactly as `bin/repair.py` does (`grep -n "def _services\|build(\|Embedder(" bin/repair.py`), open `Store(copy, dim=...)`, loop:

```python
    ctx = ReflowContext(store, embedder, home, drive_service=drive, gmail_service=gmail,
                        calendar_service=cal, anarlog_db=config.anarlog_db_path(home),
                        max_items=ns.limit, max_seconds=10**9)
    done = skipped = failed = 0
    for src, owner in store.reflow_candidates(ns.limit):
        try:
            r = ctx.handle({"source": src, "ref_id": owner, "attempts": 0})
            done += r is None
            skipped += r is not None
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {src} {owner}: {exc}")
```

then the orphan query and `integrity_check`, printing a summary dict. Exit non-zero if orphans > 0 or integrity is not `ok`.

- [ ] **Step 2: Runbook + CLAUDE.md**

`docs/RELEASE-RUNBOOK.md`, new subsection "Reflow rollout (extraction fidelity)":
1. Before release: copy the live store (daemon booted out: `launchctl bootout gui/$(id -u)/com.mcpbrain`, copy, `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.mcpbrain.plist`), run `bin/reflow_dryrun.py --store <copy> --limit 200 --yes`; require 0 orphans and integrity ok; record carried vs re-enrich.
2. Run the gold harness on the copy the same way the runbook's existing gold step runs it, pointed at the copy.
3. After release: watch `doctor` / `/api/status` `reflow`; on backlog 0 confirm `reflow.integrity == "ok"`, run `python bin/tenant.py remap-gold <tenant>/eval/<file>.yaml --write` for each gold file, then the gold gate (recall@10 ≥ 0.850, MRR ≥ 0.546).
4. A halt: read the item's `last_error` (`sync_queue`), fix, `python bin/reflow.py resume --yes`.

`CLAUDE.md`: a concise entry (no real names) stating the feature is implemented and NOT released, the version constants, the gates, and the two attended scripts.

- [ ] **Step 3: Run the dry run** on a fresh copy (attended, Josh present), paste the summary into the commit body.

- [ ] **Step 4: Commit**

```bash
git add bin/reflow_dryrun.py docs/RELEASE-RUNBOOK.md CLAUDE.md
git commit -m "feat(reflow): attended dry run on a store copy; runbook and project record"
```

---

## Self-review notes

- **Spec coverage:** §1 blocks per format → Tasks 2-7; §2 renderer/`chunk_text`/versioning → Tasks 1, 2, 7, 8; §3 carry-over (stitch, coverage, remap, `reflow_map`, ordering, guards) → Tasks 9, 10, 11; §4 queue, seed, shared-drive import, gates, visibility → Tasks 11-14; §5 gold → Task 15; Testing/Rollout → Tasks 1-16 and Task 16's dry run.
- **Deliberate deviation recorded in the spec:** the halt is cleared by `bin/reflow.py resume`, not by `doctor` (doctor reports it).
- **Names used across tasks:** `split_long_paragraph`, `prose_max_chars`, `SPLIT_VERSION`, `EXTRACTION_VERSIONS`, `extraction_version`, `render`, `Rendered`, `Chunk.spans`, `reflow.plan`, `ReflowPlan`, `NewRow`, `Store.owner_chunks`, `Store.apply_reflow`, `ReflowOrphanError`, `Store.enqueue_items`, `Store.latest_reflow_target`, `Store.reflow_stats`, `Store.reflow_candidates`, `ReflowContext.handle`, `queue.DEFER`, `config.reflow_enabled`, `HALT_CURSOR = "reflow:halted"`.
