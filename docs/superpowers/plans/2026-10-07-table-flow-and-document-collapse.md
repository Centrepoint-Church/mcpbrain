# Table Flow + Document Collapse Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore the gold ranking the extraction-fidelity reflow lost by (a) letting prose keep packing across tables in `blocks.render` and (b) collapsing search results to one per document, then roll it out through one more reflow.

**Architecture:** `blocks.render` packs its existing pieces into two open chunks (prose / table) instead of one, ordering emitted chunks by their first piece; `EXTRACTION_VERSIONS` 1 → 2 so the existing reflow re-chunks every block-format owner. `hybrid_search` gains `collapse_documents` (MaxP: best chunk represents its document, 6× retrieval depth), switched on by a fleet flag that `daemon.search`, `query_router.route` and the gold harness all honour.

**Tech Stack:** Python 3.12, SQLite store, pytest, uv.

**Spec:** `docs/superpowers/specs/2026-10-07-table-flow-and-document-collapse-design.md`

## Global Constraints

- Block MIMEs `EXTRACTION_VERSIONS` go **1 → 2**, all six; `CHUNKER_VERSION` stays 3 and `chunking.SPLIT_VERSION` stays 1.
- Collapse score is **MaxP only** (best chunk's score). No summed/bonus aggregation anywhere.
- Collapse retrieval depth: each ranker fetches **`limit * 6`** when collapsing; `limit * 2` otherwise (unchanged).
- Fleet flag name **`retrieval_collapse_documents`**, default **True**, via `config.fleet_flag`.
- Gmail groups **by message (`_doc_root`), never by thread.**
- Spans stay source-verbatim; output for a document with no tables is byte-identical to today.
- Never write real people's names, or name-derived ids, into the public repo (code, tests, docs, commits). Gold files are tenant data: `tests/eval/*.yaml` is gitignored; their home is `../mcpbrain-tenant/eval/`.
- The user runs the full suite; tasks run only touched + directly impacted test files, plus `uv run ruff check .`.
- Never touch the live store or daemon from an implementation task. Do not push or release (Task 6 is the controller's, after explicit approval).

## Review Focus

1. A document whose only content is tables (spreadsheet-like PDF) — every chunk comes from the table stream; output must still be non-empty, ordered, and within `max_chars`. Pinned in Task 1.
2. A table at the very start of a document followed by prose — the table chunk is emitted first (its first piece is first). Pinned in Task 1.
3. An oversize table (pieces each near `max_chars`) between two short prose runs — prose still joins into one chunk; no chunk exceeds `max_chars`. Pinned in Task 1.
4. A search where every hit belongs to one document — collapse returns exactly one hit with `doc_hits` = pool count, not an empty list and not duplicates. Pinned in Task 2.
5. A hit with metadata stored as a JSON string (legacy rows) or with no `file_id` — `_document_key` must not raise and falls back to `_doc_root`. Pinned in Task 2.

---

### Task 1: Two-stream packing in `blocks.render` + version bump

**Files:**
- Modify: `mcpbrain/sync/blocks.py` (`EXTRACTION_VERSIONS` at ~line 21; the packing loop at the end of `render`, ~lines 268-300)
- Modify (version pins only): `tests/test_blocks.py:9`, `tests/test_drive_blocks_wiring.py` (lines asserting `extraction_version == 1`), `tests/test_ingest_cache_reflow.py:136`
- Test: `tests/test_blocks_table_flow.py` (create)

**Interfaces:**
- Consumes: existing `_Piece(text, spans, kind, trail)` with `kind in {"heading","para","table"}`; `Heading`, `Paragraph`, `TableBlock`, `Rendered`, `has_content`.
- Produces: `render(blocks, *, max_chars=None) -> list[Rendered]` — same signature, new packing. `extraction_version(mime)` returns 2 for the six block MIMEs.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_blocks_table_flow.py
"""Two-stream packing: prose flows across tables; tables pack together within a
section; chunks are ordered by their first piece (spec 2026-10-07 §3.1)."""
from mcpbrain.sync.blocks import Heading, Paragraph, TableBlock, render

MAX = 1800


def _words(tag: str, n: int) -> str:
    return " ".join(f"{tag}{i}" for i in range(n))


def _table(tag: str, rows: int) -> TableBlock:
    return TableBlock([["Item", "Owner", "Status"]] +
                      [[f"{tag}item{i}", f"{tag}owner{i}", "open"] for i in range(rows)])


def _texts(blocks):
    return [r.text for r in render(blocks, max_chars=MAX)]


def _chunk_with(texts, needle):
    hits = [i for i, t in enumerate(texts) if needle in t]
    assert hits, f"{needle!r} not rendered"
    return hits[0]


def test_prose_either_side_of_a_large_table_shares_one_chunk():
    blocks = [Paragraph(_words("alpha", 80)), _table("big", 60), Paragraph(_words("omega", 80))]
    texts = _texts(blocks)
    assert _chunk_with(texts, "alpha0") == _chunk_with(texts, "omega0")
    assert _chunk_with(texts, "bigitem0") != _chunk_with(texts, "alpha0")
    assert all(len(t) <= MAX for t in texts)


def test_small_tables_separated_by_prose_share_a_chunk():
    blocks = [_table("one", 2), Paragraph("A short note between tables."), _table("two", 2)]
    texts = _texts(blocks)
    assert _chunk_with(texts, "oneitem0") == _chunk_with(texts, "twoitem0")
    assert "A short note" not in texts[_chunk_with(texts, "oneitem0")]


def test_a_heading_closes_the_open_table_chunk():
    blocks = [_table("one", 2), Heading(1, "Finance"), _table("two", 2)]
    texts = _texts(blocks)
    assert _chunk_with(texts, "oneitem0") != _chunk_with(texts, "twoitem0")


def test_chunks_are_ordered_by_their_first_piece():
    texts = _texts([_table("lead", 2), Paragraph(_words("body", 30))])
    assert _chunk_with(texts, "leaditem0") < _chunk_with(texts, "body0")
    texts = _texts([Paragraph(_words("body", 30)), _table("tail", 2)])
    assert _chunk_with(texts, "body0") < _chunk_with(texts, "tailitem0")


def test_table_only_document_renders_in_order_within_budget():
    texts = _texts([_table("a", 40), _table("b", 40)])
    assert texts and all(len(t) <= MAX for t in texts)
    assert _chunk_with(texts, "aitem0") <= _chunk_with(texts, "bitem0")


def test_spans_stay_verbatim_in_their_chunk():
    blocks = [Paragraph(_words("alpha", 80)), _table("big", 60), Heading(2, "Next"),
              Paragraph(_words("omega", 80)), _table("small", 2)]
    for r in render(blocks, max_chars=MAX):
        for s in r.spans:
            assert s in r.text


def test_document_without_tables_is_unchanged():
    blocks = [Heading(1, "Minutes"), Paragraph(_words("p", 120)), Heading(2, "Finance"),
              Paragraph(_words("q", 300)), Paragraph(_words("r", 40))]
    # Reference: the pre-change single-stream packer over the same pieces. With
    # no table pieces the two-stream packer must emit exactly this.
    out = render(blocks, max_chars=MAX)
    joined = "\n\n".join(r.text for r in out)
    assert joined.count("p0") == 1 and joined.count("q0") == 1 and joined.count("r0") == 1
    assert [r.meta.get("heading_trail") for r in out][0] == "Minutes"
```

Also strengthen `test_document_without_tables_is_unchanged` before Step 3 by capturing today's output: run `render` on those blocks at HEAD (before editing), paste the resulting list of `(text, meta)` into the test as `EXPECTED`, and assert `[(r.text, r.meta) for r in out] == EXPECTED`. That pins byte-identity rather than a proxy.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_blocks_table_flow.py -v`
Expected: the prose-across-table, small-tables, and ordering tests FAIL; the no-table test PASSES (it pins today's output).

- [ ] **Step 3: Implement two-stream packing**

Replace the packing section of `render` (from `out: list[Rendered] = []` to the final `return`) with:

```python
    # Two open chunks (spec 2026-10-07 §3.1). Tables mid-document used to flush
    # the open prose chunk, so prose either side of a table landed in different
    # chunks and no single chunk matched a query spanning them (gold MRR
    # 0.548 -> 0.464 after the extraction-fidelity reflow). Prose and table
    # pieces now pack independently; a heading closes the open TABLE chunk so
    # tables never pack across sections; emitted chunks are ordered by their
    # first piece, so output order is still document order by chunk start.
    out: list[tuple[int, Rendered]] = []
    open_: dict[str, list[_Piece]] = {"prose": [], "table": []}
    first: dict[str, int] = {}

    def flush(stream: str) -> None:
        cur = open_[stream]
        if not cur:
            return
        text = "\n\n".join(p.text for p in cur)
        meta = {}
        tr = cur[0].trail
        if tr:
            meta["heading_trail"] = tr[:300]
        out.append((first.pop(stream), Rendered(text, meta, [s for p in cur for s in p.spans])))
        open_[stream] = []

    for seq, p in enumerate(pieces):
        stream = "table" if p.kind == "table" else "prose"
        if p.kind == "heading":
            flush("table")
        cur = open_[stream]
        size = sum(len(x.text) + 2 for x in cur) + len(p.text)
        if cur and (size > max_chars
                    or (p.kind == "heading" and size - len(p.text) > max_chars // 2)):
            flush(stream)
        if not open_[stream]:
            first[stream] = seq
        open_[stream].append(p)
    flush("prose")
    flush("table")
    out.sort(key=lambda t: t[0])
    return [r for _, r in out if has_content(r.text)]
```

Bump every entry of `EXTRACTION_VERSIONS` from `1` to `2`, and add above the dict:

```python
# 2 (2026-10-07): two-stream packing in render -- prose flows across tables.
```

- [ ] **Step 4: Update the version pins**

In `tests/test_blocks.py`, `tests/test_drive_blocks_wiring.py` and `tests/test_ingest_cache_reflow.py`, replace each literal `extraction_version ... == 1` with a comparison to `extraction_version(<that test's mime>)` (import `from mcpbrain.sync.blocks import extraction_version`), so the next bump does not need these edits. `tests/test_blocks.py:9` becomes `assert extraction_version("application/pdf") == 2`.

- [ ] **Step 5: Run the impacted tests**

Run: `uv run pytest tests/test_blocks_table_flow.py tests/test_blocks.py tests/test_drive_blocks_wiring.py tests/test_ingest_cache_reflow.py tests/test_extract_blocks_docx.py tests/test_extract_blocks_pdf.py tests/test_extract_blocks_pptx.py tests/test_enrich_blocks.py tests/test_prune_blocks.py tests/test_reflow.py tests/test_ingest_cache.py -q -p no:cacheprovider`
Expected: all pass. A pre-existing test that asserted a table-interrupted chunk boundary is now wrong by design; update its expectation and say so in the report. Then `uv run ruff check .`.

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/sync/blocks.py tests/test_blocks_table_flow.py tests/test_blocks.py tests/test_drive_blocks_wiring.py tests/test_ingest_cache_reflow.py
git commit -m "feat(blocks): prose flows across tables; extraction_version 2"
```

---

### Task 2: Document collapse in `hybrid_search` + fleet flag + wiring

**Files:**
- Modify: `mcpbrain/retrieval.py` (`hybrid_search` signature ~line 314, ranker fetch ~line 334, final truncation ~line 518; new helpers after `_dedupe_by_cluster`)
- Modify: `mcpbrain/config.py` (new accessor next to `retrieval_expand_enabled`, ~line 727)
- Modify: `mcpbrain/daemon.py` (`search`, the `search_kwargs` block ~line 1745)
- Modify: `tests/eval/run_eval.py` (`production_search_kwargs`)
- Test: `tests/test_retrieval_collapse.py` (create)

**Interfaces:**
- Consumes: `_cluster_key(chunk)`, `_doc_root(doc_id)` (existing, `mcpbrain/retrieval.py`).
- Produces: `hybrid_search(..., collapse_documents: bool = False)`; `retrieval._document_key(hit: dict) -> str`; `retrieval._collapse_documents(hits: list[dict]) -> list[dict]` (adds `doc_hits: int` to each kept hit); `config.retrieval_collapse_documents_enabled(home) -> bool`. `query_router.route` needs no change: it forwards `**search_kwargs` to every `hybrid_search` call.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_retrieval_collapse.py
"""One result per document (MaxP) — spec 2026-10-07 §3.2."""
import json

from mcpbrain.retrieval import _collapse_documents, _document_key, hybrid_search
from mcpbrain.store import Store


def _hit(doc_id, score, **meta):
    return {"doc_id": doc_id, "score": score, "metadata": meta}


def test_drive_chunks_of_one_file_share_a_key():
    a = _hit("gdrive-F1-0", 1.0, file_id="F1")
    b = _hit("gdrive-F1-3", 0.5, file_id="F1")
    assert _document_key(a) == _document_key(b)


def test_digest_groups_with_its_own_drive_file():
    raw = _hit("gdrive-F1-0", 1.0, file_id="F1")
    digest = _hit("enriched-F1", 0.9, file_id="F1")
    assert _document_key(raw) == _document_key(digest)


def test_gmail_groups_by_message_not_thread():
    m1 = _hit("gmail-M1-body-0", 1.0, thread_id="T1", message_id="M1")
    m2 = _hit("gmail-M2-body-0", 0.9, thread_id="T1", message_id="M2")
    assert _document_key(m1) != _document_key(m2)


def test_legacy_string_metadata_and_missing_file_id_do_not_raise():
    h = {"doc_id": "gdrive-F9-2", "score": 1.0, "metadata": json.dumps({"x": 1})}
    assert _document_key(h) == "gdrive-F9"
    assert _document_key({"doc_id": "note-abc", "score": 1.0}) == "note-abc"


def test_collapse_keeps_best_chunk_and_counts_the_rest():
    hits = [_hit("gdrive-F1-2", 1.0, file_id="F1"), _hit("gdrive-F2-0", 0.8, file_id="F2"),
            _hit("gdrive-F1-0", 0.6, file_id="F1")]
    out = _collapse_documents(hits)
    assert [h["doc_id"] for h in out] == ["gdrive-F1-2", "gdrive-F2-0"]
    assert [h["doc_hits"] for h in out] == [2, 1]


def test_collapse_when_every_hit_is_one_document():
    hits = [_hit(f"gdrive-F1-{i}", 1.0 - i / 10, file_id="F1") for i in range(5)]
    out = _collapse_documents(hits)
    assert len(out) == 1 and out[0]["doc_hits"] == 5


class _Emb:
    dim = 4

    def embed_passages(self, texts):
        return [[1.0, 0, 0, 0] if "budget" in t else [0, 1.0, 0, 0] for t in texts]

    def embed_query(self, text):
        return [1.0, 0, 0, 0] if "budget" in text else [0, 1.0, 0, 0]


def _seed(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    for i in range(4):
        s.upsert_chunk(f"gdrive-F1-{i}", f"budget section {i}", f"h1{i}", {"file_id": "F1"})
    s.upsert_chunk("gdrive-F2-0", "budget summary", "h2", {"file_id": "F2"})
    from mcpbrain.index import index_pending
    index_pending(s, _Emb())
    return s


def test_hybrid_search_collapse_returns_one_hit_per_document(tmp_path):
    s = _seed(tmp_path)
    out = hybrid_search(s, _Emb(), "budget", limit=5, collapse_documents=True)
    files = [h["metadata"]["file_id"] for h in out]
    assert sorted(files) == ["F1", "F2"]
    assert {h["metadata"]["file_id"]: h["doc_hits"] for h in out}["F1"] == 4


def test_hybrid_search_without_collapse_is_unchanged(tmp_path):
    s = _seed(tmp_path)
    out = hybrid_search(s, _Emb(), "budget", limit=5)
    assert len(out) == 5 and all("doc_hits" not in h for h in out)


def test_flag_defaults_on_and_honours_local_kill_switch(tmp_path):
    from mcpbrain import config
    assert config.retrieval_collapse_documents_enabled(str(tmp_path)) is True
    config.write_config(str(tmp_path), {"retrieval_collapse_documents": False})
    assert config.retrieval_collapse_documents_enabled(str(tmp_path)) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_retrieval_collapse.py -v`
Expected: FAIL with `ImportError: cannot import name '_collapse_documents'`.

- [ ] **Step 3: Implement the helpers** (in `mcpbrain/retrieval.py`, after `_dedupe_by_cluster`)

```python
def _document_key(hit: dict) -> str:
    """The document a hit belongs to, for one-result-per-document collapse
    (spec 2026-10-07 §3.2). A Drive chunk -> its file; an `enriched-` digest ->
    its own cluster (so a file's digest and its raw chunks are ONE document);
    anything else (a Gmail MESSAGE, calendar event, note) -> its positional root.
    Gmail is never grouped by thread: the gold spike measured that hiding a
    thread's other messages loses the expected message."""
    d = hit.get("doc_id", "")
    meta = hit.get("metadata") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    if not isinstance(meta, dict):
        meta = {}
    if d.startswith("enriched-"):
        key = _cluster_key({"metadata": meta})
        return f"doc:{key}" if key else d
    if d.startswith("gdrive-") and meta.get("file_id"):
        return f"doc:{meta['file_id']}"
    return _doc_root(d)


def _collapse_documents(hits: list[dict]) -> list[dict]:
    """Keep each document's best-ranked hit (MaxP: `hits` arrive best-first) and
    record how many of its chunks were in the pool as `doc_hits`. No score
    aggregation: summing or bonusing extra chunks lost on the gold set (long
    threads and many-chunk documents crowd out the precise answer)."""
    kept: dict[str, dict] = {}
    out: list[dict] = []
    for h in hits:
        k = _document_key(h)
        if k in kept:
            kept[k]["doc_hits"] += 1
            continue
        h["doc_hits"] = 1
        kept[k] = h
        out.append(h)
    return out
```

Note `_document_key` keys a Drive digest `enriched-<fid>` to `doc:<fid>` because `_cluster_key` strips `gdrive-`; a raw Drive chunk keys to `doc:<file_id>` too, so they match (test `test_digest_groups_with_its_own_drive_file`).

- [ ] **Step 4: Wire it into `hybrid_search`**

Add `collapse_documents: bool = False` to the keyword-only parameters and document it in the docstring (`one hit per document, best chunk, doc_hits; fetches limit*6 per ranker`). Change the two ranker fetches to:

```python
    depth = limit * 6 if collapse_documents else limit * 2
    sem = [d for d, _ in store.vec_knn(qv, depth)]
    kw = [d for d, _ in store.fts_search(query, depth)]
```

Immediately before the final `results = []` loop, add:

```python
    if collapse_documents:
        candidates = _collapse_documents(candidates)
```

- [ ] **Step 5: Flag accessor and wiring**

`mcpbrain/config.py`, after `retrieval_expand_enabled`:

```python
def retrieval_collapse_documents_enabled(home) -> bool:
    """One search result per document (MaxP), spec 2026-10-07 §3.2. Fleet-
    flippable via org-config.json {"flags": {"retrieval_collapse_documents":
    false}}; a local False is the kill switch. Default ON."""
    return bool(fleet_flag(home, "retrieval_collapse_documents", True))
```

`mcpbrain/daemon.py` `search`, right after `search_kwargs: dict = {"query_vec": qv}`:

```python
            search_kwargs["collapse_documents"] = config.retrieval_collapse_documents_enabled(home)
```

`tests/eval/run_eval.py` `production_search_kwargs` return value:

```python
    return {"exclude_cold": False,
            "collapse_documents": config.retrieval_collapse_documents_enabled(home),
            **config.importance_weights(home)}
```

- [ ] **Step 6: Run the impacted tests**

Run: `uv run pytest tests/test_retrieval_collapse.py tests/test_retrieval.py tests/test_query_router.py tests/test_recall_gate.py tests/test_prompt_recall.py tests/test_retrieval_expand.py tests/test_config*.py tests/test_daemon_search*.py -q -p no:cacheprovider` (drop any glob that matches nothing). Expected: all pass. Then `uv run ruff check .`.

- [ ] **Step 7: Commit**

```bash
git add mcpbrain/retrieval.py mcpbrain/config.py mcpbrain/daemon.py tests/eval/run_eval.py tests/test_retrieval_collapse.py
git commit -m "feat(retrieval): one result per document (MaxP), fleet flag default on"
```

---

### Task 3: Gate 1 — re-measure the final code on the spike's competition set (controller-run, nothing committed)

**Files:** scratch only — `/private/tmp/claude-501/docagg/` (`compset.json`, `reingest.py`, `score.py`, `spike2_lib.py` from the 2026-10-07 spike).

**Interfaces:**
- Consumes: Task 1's `render`, Task 2's `hybrid_search(collapse_documents=True)`, both from the REPO working tree (confirm `mcpbrain.__file__`).
- Produces: a results row in `/private/tmp/claude-501/docagg/gate1-report.md`.

- [ ] **Step 1:** Copy the live store read-only via the sqlite3 backup API to `/private/tmp/claude-501/docagg/gate1.sqlite3`. Never open the live store writable.
- [ ] **Step 2:** Re-ingest the 344 files in `compset.json` into the copy with the repo's `drive.reingest_files` (no render patch: the repo code IS the variant) and embed pending, exactly as `reingest.py` did.
- [ ] **Step 3:** Score both gold files with `score.py`, using production kwargs from `tests/eval/run_eval.py` (which now carry `collapse_documents`). Record MRR / recall@10 per set, the 4 regressed cases' ranks, and the under-600 share for the re-rendered files.
- [ ] **Step 4: Decide.** PASS when main ≥ 0.543 / 0.90, ops ≥ 0.314 / 0.40, and under-600 ≤ 6.3%. If the table-stream rule loses to the spike's V1 (prose-only flow, table chunk flushed at every prose piece), change Task 1's `flush("table")` trigger to flush on every prose piece, re-run, and record the ruling in the ledger. Delete the scratch store afterwards.

---

### Task 4: Gold set growth — draft candidates for owner review

**Files:**
- Create: `bin/gold_candidates.py` (public, contains no data)
- Test: `tests/test_gold_candidates.py`
- Writes data only to `../mcpbrain-tenant/eval/candidates-2026-10.yaml` (private) — never to this repo.

**Interfaces:**
- Consumes: `Store` (read-only), `hybrid_search`.
- Produces: `bin/gold_candidates.py draft --store <path> --out <yaml> --n 20` — selects multi-chunk documents (≥ 3 chunks, block MIMEs and Gmail messages with ≥ 2 body chunks), and for each writes a stub `{id, query: "", expected_chunk_ids: [<two chunk ids from different chunks of the doc>], notes: "<first 120 chars of each chunk>"}` with an EMPTY query for the owner to write. `verify --store <path> <yaml>` reports which expected ids exist.

- [ ] **Step 1: Write the failing test** — on a tmp `Store` seeded with one 4-chunk Drive file and one 1-chunk file, `draft` emits exactly one candidate whose two `expected_chunk_ids` are distinct chunks of the 4-chunk file and whose `query` is `""`; `verify` reports 2/2 present.

```python
# tests/test_gold_candidates.py
import importlib.util, pathlib, yaml
from mcpbrain.store import Store

spec = importlib.util.spec_from_file_location(
    "gold_candidates", pathlib.Path(__file__).parents[1] / "bin" / "gold_candidates.py")
gc = importlib.util.module_from_spec(spec); spec.loader.exec_module(gc)


def test_draft_picks_multi_chunk_documents_and_leaves_query_blank(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4); s.init()
    for i in range(4):
        s.upsert_chunk(f"gdrive-F1-{i}", f"section {i} text", f"h{i}",
                       {"file_id": "F1", "mime_type": "application/pdf"})
    s.upsert_chunk("gdrive-F2-0", "single", "hx", {"file_id": "F2", "mime_type": "application/pdf"})
    out = tmp_path / "c.yaml"
    gc.draft(str(tmp_path / "b.sqlite3"), str(out), n=5)
    cases = yaml.safe_load(out.read_text())
    assert len(cases) == 1
    ids = cases[0]["expected_chunk_ids"]
    assert len(set(ids)) == 2 and all(i.startswith("gdrive-F1-") for i in ids)
    assert cases[0]["query"] == ""
    assert gc.verify(str(tmp_path / "b.sqlite3"), str(out)) == (2, 2)
```

- [ ] **Step 2:** Run it, see it fail (`FileNotFoundError` / missing module).
- [ ] **Step 3:** Implement `draft(store_path, out_path, n)` and `verify(store_path, yaml_path) -> (present, total)` with argparse `draft`/`verify` subcommands; open the store `read_only=True`; refuse an `--out` path inside this repo (resolve and compare to the repo root; exit 2 with a message).
- [ ] **Step 4:** Run `uv run pytest tests/test_gold_candidates.py -q`; ruff.
- [ ] **Step 5:** Commit `bin/gold_candidates.py tests/test_gold_candidates.py` — `feat(eval): draft multi-part gold candidates for owner review`.
- [ ] **Step 6 (controller, attended):** run `draft` against the live store (read-only) into `../mcpbrain-tenant/eval/candidates-2026-10.yaml`, then hand it to the owner to write the queries and accept/reject. Accepted cases are appended to the gold files; this does not block Task 6.

---

### Task 5: Dry run + latency (controller-run)

- [ ] **Step 1:** `uv run python bin/reflow_dryrun.py --limit 250 --per-mime` on a `VACUUM INTO` copy (runbook §8). PASS: 0 failures, no new orphans, `integrity_check` ok.
- [ ] **Step 2:** Latency: on the live store (read-only, through `hybrid_search` with production kwargs), time 40 gold queries 3× each after 3 warm-up queries, collapse off vs on. PASS: on-p95 ≤ 1.25 × off-p95. Record both.

---

### Task 6: Release (controller, after the owner's explicit go-ahead)

Follow `docs/RELEASE-RUNBOOK.md` exactly as 0.7.139 did: fleet-resolved full suite (`uv run --with` the resolved pins), ruff, tenant check, bump the four version files + `uv.lock`, push, `bin/release.py` (purge the old wheel from `dist/` AND the pages worktree), assert wheel contents (`two-stream` comment in `blocks.py`, `EXTRACTION_VERSIONS` values 2, `_collapse_documents` in `retrieval.py`, `retrieval_collapse_documents` in `config.py`, tenant files present, no gold set, no `tenant_people.json`), push gh-pages, sync plugin, verify the index + fleet resolution, bootout → reinstall from the index → clear `__pycache__` → bootstrap, `/api/status` version, CLAUDE.md entry.

---

### Task 7: Binding gold gate at backlog 0 (controller)

- [ ] When `reflow` reports queued 0, remaining 0, held 0 and the daemon has run its integrity check: `bin/tenant.py remap-gold <file> --write` for both gold files (no `--from-start`), then `uv run python tests/eval/run_eval.py --gold --k 10` and the ops set via the spike scorer. PASS: main recall@10 ≥ 0.850 and MRR ≥ 0.546; ops not below the same-day pre-drain baseline. Copy both gold files to `../mcpbrain-tenant/eval/` and commit there. Record the result in CLAUDE.md. Remind the owner to delete `brain.sqlite3.pre-reflow-drain-*`.
