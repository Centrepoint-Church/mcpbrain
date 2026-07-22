# Recall Quality: Expansion + Cross-Encoder Rerank + Contextual BM25 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve `brain_search`/recall quality by (A) expanding retrieved chunks into coherent parent/neighbour context, (B) adding a cross-encoder rerank stage, and (C) contextualising the keyword (BM25) arm to match the already-contextual embedding arm.

**Architecture:** All three are post-fusion enhancements to the daemon-side recall path (`daemon.search` → `query_router.route` → `retrieval.hybrid_search`), each behind a `config *_enabled()` flag defaulting OFF (except C, which extends the existing default-ON `contextual_retrieval` feature). Rank on small chunks; expand last. Ranking/rerank/gates stay on small chunks; expansion is the final step before results are returned.

**Tech Stack:** Python 3.12, SQLite (`sqlite-vec` + FTS5), `fastembed` (embeddings + cross-encoder rerank, daemon-only dep), pytest (`-n0` to disable xdist for single-test runs).

## Global Constraints

- **Feature flags default OFF; shipping code ≠ enabling.** New flags (`retrieval_expand`, `retrieval_rerank`+`rerank_model`) default OFF. Flip ON only after live-store gold-gate validation. (Phase C rides the existing `contextual_retrieval` flag, default ON.)
- **Gold gate per phase:** `uv run python tests/eval/run_eval.py --gold --k 10` must hold recall@10 ≥ 0.55 and MRR ≥ 0.35 (target: hold ≥ 0.750 / 0.514) before enabling a flag.
- **Recall must never raise into the prompt path** — every new stage degrades to the pre-stage result on exception (mirror the existing `try/except … return []`/pass idioms in `daemon.search`/`route`).
- **MCP client stays native-dep-free** — the cross-encoder model loads in the DAEMON only (via `fastembed`, already a `[daemon]` dep). Never import it in the MCP server.
- **Passage-only contextual prefix** — `embed.contextual_prefix` is applied to indexed passages ONLY, never to the query side.
- **Run scoped tests** (`-n0` for single tests); the human runs the full suite. Commit after each green step.
- **Version lives in FIVE files** — do NOT bump versions in this plan; release is a separate explicit step.

---

## File Structure

- **Create:** `mcpbrain/retrieval_expand.py` — pure expansion functions (Phase A).
- **Create:** `tests/test_retrieval_expand.py`, `tests/test_cross_encoder_rerank.py`, `tests/test_contextual_bm25.py`.
- **Modify:** `mcpbrain/config.py` — new flag/param readers (A, B).
- **Modify:** `mcpbrain/embed.py` — `get_reranker()` loader (B).
- **Modify:** `mcpbrain/query_router.py` — cross-encoder rerank branch in `route()` (B).
- **Modify:** `mcpbrain/daemon.py` — call `expand_hits` at the end of `search()` (A).
- **Modify:** `mcpbrain/store.py` — contextual FTS in `write_embedding` + `_write_cached_chunk_row`; `reindex_fts_batch` backfill (C).
- **Modify:** `mcpbrain/index.py` — call the FTS backfill from the index pass, or a new cadence (C).

---

## PHASE A — Read-side expansion

### Task A1: Parent-keying + grouping (pure)

**Files:**
- Create: `mcpbrain/retrieval_expand.py`
- Test: `tests/test_retrieval_expand.py`

**Interfaces:**
- Produces: `parent_key(meta: dict, doc_id: str) -> tuple[str, str]` returning `(kind, key)` where kind ∈ {"thread","file","chunk"}; `group_by_parent(hits: list[dict], store) -> list[dict]` returning parent groups ordered by best hit rank: each group `{"kind","key","rank","hit_indices":[chunk_index...],"rep_doc_id","score"}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_retrieval_expand.py
from mcpbrain import retrieval_expand as rx

def test_parent_key_prefers_thread_then_file_then_doc():
    assert rx.parent_key({"thread_id": "t1", "file_id": "f1"}, "d0") == ("thread", "t1")
    assert rx.parent_key({"file_id": "f1", "chunk_index": 7}, "gdrive-f1-7") == ("file", "f1")
    assert rx.parent_key({}, "note-9") == ("chunk", "note-9")

def test_group_by_parent_orders_by_best_rank_and_collects_indices():
    hits = [
        {"doc_id": "gdrive-f1-5", "score": 1.0, "metadata": {"file_id": "f1", "chunk_index": 5}},
        {"doc_id": "gmail-t1-0", "score": 0.9, "metadata": {"thread_id": "t1"}},
        {"doc_id": "gdrive-f1-6", "score": 0.8, "metadata": {"file_id": "f1", "chunk_index": 6}},
    ]
    groups = rx.group_by_parent(hits)
    assert [(g["kind"], g["key"]) for g in groups] == [("file", "f1"), ("thread", "t1")]
    assert groups[0]["hit_indices"] == [5, 6]
    assert groups[0]["rep_doc_id"] == "gdrive-f1-5"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_retrieval_expand.py -q -n0`
Expected: FAIL — `AttributeError: module 'mcpbrain.retrieval_expand' has no attribute 'parent_key'`

- [ ] **Step 3: Write minimal implementation**

```python
# mcpbrain/retrieval_expand.py
"""Read-side small-to-big expansion for recall. Pure functions over ranked hits
+ a store; called last in daemon.search (after ranking/rerank/sufficiency) so
expansion never blunts the reranker or triggers lost-in-the-middle."""


def parent_key(meta: dict, doc_id: str) -> tuple[str, str]:
    """(kind, key) for grouping a chunk to its parent: thread > file > chunk."""
    if meta.get("thread_id"):
        return ("thread", meta["thread_id"])
    if meta.get("file_id"):
        return ("file", meta["file_id"])
    return ("chunk", doc_id)


def group_by_parent(hits: list[dict]) -> list[dict]:
    """Group ranked hits by parent, preserving best (first-seen) rank order.
    Each hit dict carries doc_id, score, metadata."""
    groups: dict[tuple, dict] = {}
    for rank, h in enumerate(hits):
        meta = h.get("metadata") or {}
        kind, key = parent_key(meta, h["doc_id"])
        g = groups.get((kind, key))
        if g is None:
            g = {"kind": kind, "key": key, "rank": rank, "hit_indices": [],
                 "rep_doc_id": h["doc_id"], "score": h.get("score", 0.0)}
            groups[(kind, key)] = g
        idx = (meta or {}).get("chunk_index")
        if idx is not None:
            g["hit_indices"].append(int(idx))
    return sorted(groups.values(), key=lambda g: g["rank"])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_retrieval_expand.py -q -n0`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/retrieval_expand.py tests/test_retrieval_expand.py
git commit -m "feat(recall): parent-keying + grouping for read-side expansion (Phase A)"
```

### Task A2: Per-parent expansion (thread stuff / short-doc / span-stitch)

**Files:**
- Modify: `mcpbrain/retrieval_expand.py`
- Test: `tests/test_retrieval_expand.py`

**Interfaces:**
- Consumes: `store.thread_chunks(thread_id) -> [{doc_id,text,metadata}]`; `store.chunks_for_file(file_id) -> [{doc_id,text,content_hash,metadata,idx}]` (sorted by idx).
- Produces: `expand_parent(store, group: dict, *, window_n: int, short_doc_max_chunks: int) -> str` — the stitched context text for one parent group.

- [ ] **Step 1: Write the failing test**

```python
class _FakeStore:
    def __init__(self, threads=None, files=None):
        self._threads = threads or {}
        self._files = files or {}
    def thread_chunks(self, tid):
        return self._threads.get(tid, [])
    def chunks_for_file(self, fid):
        return self._files.get(fid, [])

def test_expand_thread_stuffs_whole_thread_in_date_order():
    store = _FakeStore(threads={"t1": [
        {"doc_id": "m2", "text": "second", "metadata": {"date": "2026-02-02"}},
        {"doc_id": "m1", "text": "first",  "metadata": {"date": "2026-01-01"}},
    ]})
    g = {"kind": "thread", "key": "t1", "hit_indices": [], "rep_doc_id": "m1"}
    out = rx.expand_parent(store, g, window_n=3, short_doc_max_chunks=15)
    assert out == "first\n\nsecond"

def test_expand_short_file_returns_whole_doc():
    files = {"f1": [{"doc_id": f"gdrive-f1-{i}", "text": f"p{i}",
                     "metadata": {"chunk_index": i}, "idx": i} for i in range(3)]}
    g = {"kind": "file", "key": "f1", "hit_indices": [1], "rep_doc_id": "gdrive-f1-1"}
    out = rx.expand_parent(_FakeStore(files=files), g, window_n=3, short_doc_max_chunks=15)
    assert out == "p0\n\np1\n\np2"

def test_expand_large_file_span_stitches_window_only():
    files = {"f1": [{"doc_id": f"gdrive-f1-{i}", "text": f"p{i}",
                     "metadata": {"chunk_index": i}, "idx": i} for i in range(50)]}
    g = {"kind": "file", "key": "f1", "hit_indices": [10], "rep_doc_id": "gdrive-f1-10"}
    out = rx.expand_parent(_FakeStore(files=files), g, window_n=2, short_doc_max_chunks=15)
    # window ±2 around idx 10 => 8,9,10,11,12
    assert out == "p8\n\np9\n\np10\n\np11\n\np12"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_retrieval_expand.py -q -n0`
Expected: FAIL — `AttributeError: … has no attribute 'expand_parent'`

- [ ] **Step 3: Write minimal implementation**

```python
_JOIN = "\n\n"


def _by_date(chunks: list[dict]) -> list[dict]:
    return sorted(chunks, key=lambda c: (c.get("metadata") or {}).get("date", "") or "")


def expand_parent(store, group: dict, *, window_n: int, short_doc_max_chunks: int) -> str:
    kind, key = group["kind"], group["key"]
    if kind == "thread":
        chunks = _by_date(store.thread_chunks(key))
        return _JOIN.join(c["text"] for c in chunks)
    if kind == "file":
        chunks = store.chunks_for_file(key)  # already sorted by idx
        if len(chunks) <= short_doc_max_chunks:
            return _JOIN.join(c["text"] for c in chunks)
        # large file: contiguous span-stitch around each hit index
        wanted: set[int] = set()
        for hi in group["hit_indices"]:
            wanted.update(range(hi - window_n, hi + window_n + 1))
        kept = [c for c in chunks if c["idx"] in wanted]
        return _JOIN.join(c["text"] for c in kept)
    # bare chunk: no parent context available
    return ""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_retrieval_expand.py -q -n0`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/retrieval_expand.py tests/test_retrieval_expand.py
git commit -m "feat(recall): per-parent expansion (thread stuff / short-doc / span-stitch)"
```

### Task A3: `expand_hits` orchestration (cap, budget, head-tail, metadata fetch)

**Files:**
- Modify: `mcpbrain/retrieval_expand.py`
- Test: `tests/test_retrieval_expand.py`

**Interfaces:**
- Consumes: `store.get_chunk(doc_id) -> {doc_id,text,metadata,memory_tier} | None`.
- Produces: `expand_hits(store, hits: list[dict], *, window_n=3, short_doc_max_chunks=15, max_parents=5, token_budget=6000) -> list[dict]`. Input `hits` are reduced recall dicts `{doc_id,score,distance,text}` (no metadata — fetched via get_chunk). Returns expanded dicts `{doc_id,score,distance,text}` (text = stitched parent context), ≤ max_parents, within `token_budget` (≈ chars/4), ordered head-and-tail by rank.

- [ ] **Step 1: Write the failing test**

```python
class _StoreWithMeta(_FakeStore):
    def __init__(self, chunks, **kw):
        super().__init__(**kw)
        self._chunks = chunks
    def get_chunk(self, doc_id):
        return self._chunks.get(doc_id)

def test_expand_hits_caps_parents_and_orders_head_tail():
    # 3 distinct short files; max_parents=2 keeps the top 2 by rank
    chunks, files = {}, {}
    hits = []
    for i, fid in enumerate(["fa", "fb", "fc"]):
        doc = f"gdrive-{fid}-0"
        meta = {"file_id": fid, "chunk_index": 0}
        chunks[doc] = {"doc_id": doc, "text": fid, "metadata": meta, "memory_tier": ""}
        files[fid] = [{"doc_id": doc, "text": fid, "metadata": meta, "idx": 0}]
        hits.append({"doc_id": doc, "score": 1.0 - i * 0.1, "distance": 0.1, "text": fid})
    store = _StoreWithMeta(chunks, files=files)
    out = rx.expand_hits(store, hits, max_parents=2, token_budget=10_000)
    assert len(out) == 2
    assert {h["doc_id"] for h in out} == {"gdrive-fa-0", "gdrive-fb-0"}

def test_expand_hits_respects_token_budget_dropping_lowest_rank():
    chunks, files = {}, {}
    hits = []
    for i, fid in enumerate(["fa", "fb"]):
        doc = f"gdrive-{fid}-0"
        meta = {"file_id": fid, "chunk_index": 0}
        big = "x" * 400
        chunks[doc] = {"doc_id": doc, "text": big, "metadata": meta, "memory_tier": ""}
        files[fid] = [{"doc_id": doc, "text": big, "metadata": meta, "idx": 0}]
        hits.append({"doc_id": doc, "score": 1.0 - i, "distance": 0.1, "text": big})
    store = _StoreWithMeta(chunks, files=files)
    # budget ~100 tokens ≈ 400 chars: only the top parent fits
    out = rx.expand_hits(store, hits, max_parents=5, token_budget=100)
    assert [h["doc_id"] for h in out] == ["gdrive-fa-0"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_retrieval_expand.py -q -n0`
Expected: FAIL — `has no attribute 'expand_hits'`

- [ ] **Step 3: Write minimal implementation**

```python
def _attach_metadata(store, hits: list[dict]) -> list[dict]:
    out = []
    for h in hits:
        c = store.get_chunk(h["doc_id"])
        out.append({**h, "metadata": (c or {}).get("metadata", {})})
    return out


def _head_tail(items: list) -> list:
    """Reorder by rank so the top passages sit at head AND tail (lost-in-the-middle)."""
    if len(items) <= 2:
        return items
    head, tail = [], []
    for i, it in enumerate(items):
        (head if i % 2 == 0 else tail).append(it)
    return head + tail[::-1]


def expand_hits(store, hits: list[dict], *, window_n: int = 3,
                short_doc_max_chunks: int = 15, max_parents: int = 5,
                token_budget: int = 6000) -> list[dict]:
    if not hits:
        return hits
    with_meta = _attach_metadata(store, hits)
    groups = group_by_parent(with_meta)[:max_parents]
    by_doc = {h["doc_id"]: h for h in hits}
    expanded, used = [], 0
    for g in groups:
        text = expand_parent(store, g, window_n=window_n,
                             short_doc_max_chunks=short_doc_max_chunks)
        if not text:
            text = by_doc[g["rep_doc_id"]].get("text", "")
        cost = len(text) // 4  # ~4 chars/token
        if expanded and used + cost > token_budget:
            continue  # budget exhausted; drop this (lower-ranked) parent
        used += cost
        base = by_doc[g["rep_doc_id"]]
        expanded.append({"doc_id": base["doc_id"], "score": base.get("score", 0.0),
                         "distance": base.get("distance", 0.0), "text": text})
    return _head_tail(expanded)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_retrieval_expand.py -q -n0`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/retrieval_expand.py tests/test_retrieval_expand.py
git commit -m "feat(recall): expand_hits orchestration — cap, token budget, head-tail"
```

### Task A4: Config flags + wire into `daemon.search`

**Files:**
- Modify: `mcpbrain/config.py` (add readers)
- Modify: `mcpbrain/daemon.py:~896` (after `filter_by_sufficiency`, before `return result_hits`)
- Test: `tests/test_retrieval_expand.py` (config default), plus a manual daemon check

**Interfaces:**
- Consumes: `config.retrieval_expand_enabled(home)`, `config.expand_params(home)`.

- [ ] **Step 1: Write the failing test**

```python
def test_retrieval_expand_defaults_off(tmp_path):
    from mcpbrain import config
    assert config.retrieval_expand_enabled(str(tmp_path)) is False
    p = config.expand_params(str(tmp_path))
    assert p == {"window_n": 3, "short_doc_max_chunks": 15,
                 "max_parents": 5, "token_budget": 6000}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_retrieval_expand.py::test_retrieval_expand_defaults_off -q -n0`
Expected: FAIL — `AttributeError: module 'mcpbrain.config' has no attribute 'retrieval_expand_enabled'`

- [ ] **Step 3: Write minimal implementation**

Add to `mcpbrain/config.py` (near the other `retrieval_*` readers):

```python
def retrieval_expand_enabled(home) -> bool:
    """Whether read-side small-to-big expansion runs in daemon.search (default OFF)."""
    return bool(read_config(home).get("retrieval_expand", False))


def expand_params(home) -> dict:
    """Expansion tunables (config 'expand_*'); defaults from the 2026-07-22 spec."""
    c = read_config(home)
    return {
        "window_n": int(c.get("expand_window_n", 3)),
        "short_doc_max_chunks": int(c.get("expand_short_doc_max_chunks", 15)),
        "max_parents": int(c.get("expand_max_parents", 5)),
        "token_budget": int(c.get("expand_token_budget", 6000)),
    }
```

Then wire into `daemon.py` `search()`, immediately before `return result_hits`:

```python
        # Expansion runs LAST (after ranking/rerank/sufficiency): small-to-big
        # context stitching. Behind a flag (default OFF); degrades to the
        # unexpanded hits on any error — recall must never raise.
        if config.retrieval_expand_enabled(home):
            try:
                from mcpbrain.retrieval_expand import expand_hits
                result_hits = expand_hits(self._store, result_hits,
                                          **config.expand_params(home))
            except Exception:  # noqa: BLE001
                log.warning("recall expansion failed for %r", query, exc_info=True)
        return result_hits
```

(`home` is already bound earlier in `search()` as `home = str(app_dir())`.)

- [ ] **Step 4: Run test + verify wiring**

Run: `uv run pytest tests/test_retrieval_expand.py -q -n0`
Expected: PASS (8 passed)
Run: `uv run ruff check mcpbrain/retrieval_expand.py mcpbrain/config.py mcpbrain/daemon.py`
Expected: `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/config.py mcpbrain/daemon.py tests/test_retrieval_expand.py
git commit -m "feat(recall): wire expand_hits into daemon.search behind retrieval_expand flag"
```

### Task A5: Gold-gate validation for Phase A

**Files:** none (validation only)

- [ ] **Step 1: Baseline (flag OFF)**

Run: `uv run python tests/eval/run_eval.py --gold --k 10`
Record recall@10 / MRR.

- [ ] **Step 2: Enable expansion in a scratch config and re-run**

Set `{"retrieval_expand": true}` in the eval store's config (or a temp home), re-run the gold gate. Confirm recall@10 ≥ 0.55, MRR ≥ 0.35, and no regression vs baseline; watch total returned text stays within budget (no bloat).

- [ ] **Step 3: Record result in the plan / spec.** Only enable the flag on the live store if the gate holds. If it regresses, tune `expand_params` (smaller `window_n`, tighter budget) and re-run before proceeding to Phase B.

---

## PHASE B — Cross-encoder rerank

### Task B1: `get_reranker()` loader in `embed.py`

**Files:**
- Modify: `mcpbrain/embed.py`
- Test: `tests/test_cross_encoder_rerank.py`

**Interfaces:**
- Produces: `get_reranker(model_name: str = "Xenova/ms-marco-MiniLM-L-6-v2")` → object with `rerank(query: str, passages: list[str]) -> list[float]` (higher = more relevant). Memoised (`lru_cache`), lazy `fastembed` import (mirrors `get_embedder`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cross_encoder_rerank.py
import pytest
from mcpbrain import embed

@pytest.mark.slow
def test_get_reranker_scores_relevant_higher():
    rr = embed.get_reranker()
    scores = rr.rerank("how do I reset my password?",
                       ["Password reset instructions are here.",
                        "The cafeteria menu for Tuesday."])
    assert scores[0] > scores[1]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cross_encoder_rerank.py::test_get_reranker_scores_relevant_higher -q -n0`
Expected: FAIL — `AttributeError: module 'mcpbrain.embed' has no attribute 'get_reranker'`

- [ ] **Step 3: Write minimal implementation**

Add to `mcpbrain/embed.py`:

```python
class _LocalReranker:
    def __init__(self, model_name: str):
        from fastembed.rerank.cross_encoder import TextCrossEncoder  # lazy
        self._model = TextCrossEncoder(model_name=model_name, cache_dir=_model_cache_dir())

    def rerank(self, query: str, passages: list[str]) -> list[float]:
        if not passages:
            return []
        return [float(s) for s in self._model.rerank(query, list(passages))]


@lru_cache(maxsize=None)
def get_reranker(model_name: str = "Xenova/ms-marco-MiniLM-L-6-v2"):
    """Memoised cross-encoder reranker (fastembed). Daemon-only — never import
    from the MCP server. Model is lazy-downloaded on first use (~80 MB)."""
    return _LocalReranker(model_name)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cross_encoder_rerank.py::test_get_reranker_scores_relevant_higher -q -n0`
Expected: PASS (downloads the model on first run; may take ~30s).

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/embed.py tests/test_cross_encoder_rerank.py
git commit -m "feat(recall): fastembed cross-encoder reranker loader (Phase B)"
```

### Task B2: Cross-encoder rerank function + config

**Files:**
- Modify: `mcpbrain/query_router.py`
- Modify: `mcpbrain/config.py`
- Test: `tests/test_cross_encoder_rerank.py`

**Interfaces:**
- Consumes: `get_reranker(model).rerank(query, passages)`.
- Produces: `_cross_encoder_rerank(query, results, reranker) -> list[dict]` — reorders `results` (each `{doc_id,text,score,...}`) by reranker score, descending, preserving dicts. `config.rerank_model(home) -> str` (default `"Xenova/ms-marco-MiniLM-L-6-v2"`, `"lexical"` selects the pure-python fallback).

- [ ] **Step 1: Write the failing test**

```python
from mcpbrain import query_router as qr

class _FakeRR:
    def rerank(self, query, passages):
        # score = 1.0 if "match" in passage else 0.0
        return [1.0 if "match" in p else 0.0 for p in passages]

def test_cross_encoder_rerank_reorders_by_score():
    results = [{"doc_id": "a", "text": "nope", "score": 0.9},
               {"doc_id": "b", "text": "match here", "score": 0.5}]
    out = qr._cross_encoder_rerank("q", results, _FakeRR())
    assert [r["doc_id"] for r in out] == ["b", "a"]

def test_rerank_model_default(tmp_path):
    from mcpbrain import config
    assert config.rerank_model(str(tmp_path)) == "Xenova/ms-marco-MiniLM-L-6-v2"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cross_encoder_rerank.py -q -n0 -k "reorders or model_default"`
Expected: FAIL — `has no attribute '_cross_encoder_rerank'`

- [ ] **Step 3: Write minimal implementation**

Add to `mcpbrain/query_router.py` (near `_token_overlap_rerank`):

```python
def _cross_encoder_rerank(query: str, results: list[dict], reranker) -> list[dict]:
    """Reorder fused results by a cross-encoder's (query, passage) scores."""
    if not results:
        return results
    scores = reranker.rerank(query, [r.get("text", "") for r in results])
    ranked = sorted(zip(scores, results), key=lambda t: -t[0])
    return [r for _, r in ranked]
```

Add to `mcpbrain/config.py`:

```python
def rerank_model(home) -> str:
    """Rerank backend: a fastembed cross-encoder model name, or 'lexical' for the
    pure-python token-overlap fallback. Default: MiniLM-L-6 cross-encoder."""
    return str(read_config(home).get("rerank_model", "Xenova/ms-marco-MiniLM-L-6-v2"))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cross_encoder_rerank.py -q -n0 -k "reorders or model_default"`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/query_router.py mcpbrain/config.py tests/test_cross_encoder_rerank.py
git commit -m "feat(recall): cross-encoder rerank fn + rerank_model config"
```

### Task B3: Wire rerank backend into `route()` with fallback

**Files:**
- Modify: `mcpbrain/query_router.py:328-333` (the rerank block)
- Test: `tests/test_cross_encoder_rerank.py`

**Interfaces:**
- Consumes: `config.retrieval_rerank_enabled(home)`, `config.rerank_model(home)`, `embed.get_reranker(model)`.

- [ ] **Step 1: Write the failing test** (backend selection + graceful degrade)

```python
def test_route_rerank_falls_back_to_lexical_when_model_is_lexical(monkeypatch, tmp_path):
    # rerank_model='lexical' must use the token-overlap path, not load a model
    import mcpbrain.config as config
    (tmp_path / "config.json").write_text(
        '{"retrieval_rerank": true, "rerank_model": "lexical"}')
    called = {"cross": False}
    monkeypatch.setattr(qr, "_cross_encoder_rerank",
                        lambda *a, **k: called.__setitem__("cross", True) or a[1])
    results = [{"doc_id": "a", "text": "alpha beta", "score": 1.0}]
    out = qr._apply_rerank("alpha", results, home=str(tmp_path))
    assert called["cross"] is False
    assert out == results  # lexical path ran, order unchanged for single result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cross_encoder_rerank.py::test_route_rerank_falls_back_to_lexical_when_model_is_lexical -q -n0`
Expected: FAIL — `has no attribute '_apply_rerank'`

- [ ] **Step 3: Write minimal implementation**

Add `_apply_rerank` to `query_router.py` and call it from `route()`:

```python
def _apply_rerank(query: str, results: list[dict], *, home: str) -> list[dict]:
    """Rerank backend selector: cross-encoder (default) or lexical fallback.
    Degrades to the input order on any error — recall must never raise."""
    from mcpbrain import config
    model = config.rerank_model(home)
    if model == "lexical":
        return _token_overlap_rerank(query, results)
    try:
        from mcpbrain.embed import get_reranker
        return _cross_encoder_rerank(query, results, get_reranker(model))
    except Exception:  # noqa: BLE001 — model missing/not-downloaded, etc.
        log.warning("cross-encoder rerank unavailable (%s); lexical fallback", model)
        return _token_overlap_rerank(query, results)
```

Replace the existing rerank block in `route()` (lines ~328-333):

```python
    # ---- rerank (cross-encoder default, lexical fallback) ---------------
    if config.retrieval_rerank_enabled(home):
        try:
            results = _apply_rerank(query, results, home=home)
        except Exception:  # noqa: BLE001
            pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_cross_encoder_rerank.py -q -n0`
Expected: PASS (all)
Run: `uv run ruff check mcpbrain/query_router.py mcpbrain/embed.py mcpbrain/config.py`
Expected: `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/query_router.py tests/test_cross_encoder_rerank.py
git commit -m "feat(recall): route() rerank backend selector w/ lexical fallback"
```

### Task B4: Gold-gate validation for Phase B

**Files:** none (validation only)

- [ ] **Step 1:** With `{"retrieval_rerank": true}` (cross-encoder default), run `uv run python tests/eval/run_eval.py --gold --k 10`. The rerank stage lives in `route()`, which the gold harness exercises via `production_search_kwargs`; confirm it engages (widen candidates if needed).
- [ ] **Step 2:** Confirm recall@10 ≥ 0.55, MRR ≥ 0.35, ideally improved vs Phase A. Compare cross-encoder vs `"rerank_model":"lexical"` to quantify the model's marginal lift.
- [ ] **Step 3:** Record; enable on the live store only if the gate holds and latency is acceptable.

---

## PHASE C — Contextual BM25 (complete the existing contextual-retrieval feature)

### Task C1: Index the contextual prefix into FTS

**Files:**
- Modify: `mcpbrain/store.py` — `write_embedding` (~1278) and `_write_cached_chunk_row` (~1288)
- Test: `tests/test_contextual_bm25.py`

**Interfaces:**
- Consumes: `embed.contextual_prefix(metadata) -> str`; `config.contextual_retrieval_enabled(home)`.
- Note: `write_embedding` currently takes `(rowid, vector)`; it must read the chunk's metadata to build the FTS text. Keep the signature; fetch metadata inside.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_contextual_bm25.py
from mcpbrain.store import Store

def _store(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4); s.init(); return s

def test_fts_indexes_contextual_prefix_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr("mcpbrain.config.contextual_retrieval_enabled", lambda home: True)
    s = _store(tmp_path)
    # a gdrive chunk whose body never mentions the title
    s.upsert_chunk("gdrive-F1-0", "attendance rota rows",
                   "h0", {"source_type": "gdrive", "file_name": "Citywide Youth Term Plan"})
    s.write_embedding(_rowid(s, "gdrive-F1-0"), [0.0, 0.0, 0.0, 0.0])
    # keyword search for a title-only term now hits via the FTS prefix
    hits = [d for d, _ in s.fts_search("Citywide Youth", 5)]
    assert "gdrive-F1-0" in hits

def _rowid(store, doc_id):
    with store._connect() as db:
        return db.execute("SELECT rowid FROM chunks WHERE doc_id=?", (doc_id,)).fetchone()["rowid"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_contextual_bm25.py -q -n0`
Expected: FAIL — the FTS row holds only "attendance rota rows"; "Citywide Youth" doesn't match.

- [ ] **Step 3: Write minimal implementation**

In `store.py`, add a private helper and use it in both FTS-write sites:

```python
    def _fts_text(self, text: str, metadata: dict) -> str:
        """FTS-indexed text: contextual prefix + body when contextual_retrieval is
        on (mirrors the embed side), else raw body. Keeps the chunks.text column
        and all returned text RAW — only the FTS mirror is contextualised."""
        from mcpbrain import config
        from mcpbrain.embed import contextual_prefix
        if config.contextual_retrieval_enabled(str(config.app_dir())):
            return contextual_prefix(metadata) + text
        return text
```

Change `write_embedding` to build the FTS text from metadata:

```python
    def write_embedding(self, rowid: int, vector: list[float]) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM vec_chunks WHERE rowid=?", (rowid,))
            db.execute("INSERT INTO vec_chunks(rowid, embedding) VALUES(?,?)",
                       (rowid, sqlite_vec.serialize_float32(vector)))
            row = db.execute("SELECT text, metadata FROM chunks WHERE rowid=?",
                             (rowid,)).fetchone()
            fts_text = self._fts_text(row["text"], json.loads(row["metadata"]))
            db.execute("DELETE FROM fts_chunks WHERE rowid=?", (rowid,))
            db.execute("INSERT INTO fts_chunks(rowid, text) VALUES(?,?)", (rowid, fts_text))
            db.execute("UPDATE chunks SET embedded=1 WHERE rowid=?", (rowid,))
```

In `_write_cached_chunk_row`, replace the final FTS insert (line ~1313):

```python
        db.execute("DELETE FROM fts_chunks WHERE rowid=?", (rowid,))
        # NOTE: _write_cached_chunk_row is a @staticmethod (no self); build the
        # contextual FTS text inline to match write_embedding.
        from mcpbrain import config as _config
        from mcpbrain.embed import contextual_prefix as _cp
        _fts = (_cp(metadata) + text) if _config.contextual_retrieval_enabled(
            str(_config.app_dir())) else text
        db.execute("INSERT INTO fts_chunks(rowid, text) VALUES(?,?)", (rowid, _fts))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_contextual_bm25.py -q -n0`
Expected: PASS
Run: `uv run pytest tests/test_store.py -q -n0`
Expected: PASS (no regression in existing FTS/store tests)

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/store.py tests/test_contextual_bm25.py
git commit -m "feat(recall): contextual BM25 — index contextual prefix into FTS (Phase C)"
```

### Task C2: FTS re-index backfill (no re-embed)

**Files:**
- Modify: `mcpbrain/store.py` (add `reindex_fts_batch`)
- Test: `tests/test_contextual_bm25.py`

**Interfaces:**
- Produces: `reindex_fts_batch(cap: int = 5000) -> int` — re-inserts the FTS row (contextual text) for up to `cap` embedded chunks whose FTS row is stale, returns count. Idempotent + resumable via a `fts_context_version` marker on the chunk (or a bounded scan of embedded chunks lacking the marker).

- [ ] **Step 1: Write the failing test**

```python
def test_reindex_fts_batch_refreshes_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr("mcpbrain.config.contextual_retrieval_enabled", lambda home: True)
    s = _store(tmp_path)
    s.upsert_chunk("gdrive-F2-0", "rota rows", "h", {"source_type": "gdrive",
                   "file_name": "Master Rosters"})
    rid = _rowid(s, "gdrive-F2-0")
    # simulate a legacy raw-text FTS row (pre-Phase-C)
    with s._connect() as db:
        db.execute("DELETE FROM fts_chunks WHERE rowid=?", (rid,))
        db.execute("INSERT INTO fts_chunks(rowid, text) VALUES(?,?)", (rid, "rota rows"))
        db.execute("UPDATE chunks SET embedded=1 WHERE rowid=?", (rid,))
    assert "gdrive-F2-0" not in [d for d, _ in s.fts_search("Master Rosters", 5)]
    n = s.reindex_fts_batch(cap=100)
    assert n >= 1
    assert "gdrive-F2-0" in [d for d, _ in s.fts_search("Master Rosters", 5)]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_contextual_bm25.py::test_reindex_fts_batch_refreshes_prefix -q -n0`
Expected: FAIL — `has no attribute 'reindex_fts_batch'`

- [ ] **Step 3: Write minimal implementation**

Add to `store.py` (use a `fts_context_version` column; ALTER in `init()` if absent — follow the existing additive-migration pattern used for `enrich_attempts`):

```python
    FTS_CONTEXT_VERSION = 1

    def reindex_fts_batch(self, cap: int = 5000) -> int:
        """Rebuild the FTS row (contextual text) for up to `cap` embedded chunks
        whose fts_context_version is behind. Resumable; no re-embed. Returns count."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT rowid, text, metadata FROM chunks "
                "WHERE embedded=1 AND COALESCE(fts_context_version,0) < ? LIMIT ?",
                (self.FTS_CONTEXT_VERSION, cap)).fetchall()
            n = 0
            for r in rows:
                fts_text = self._fts_text(r["text"], json.loads(r["metadata"]))
                db.execute("DELETE FROM fts_chunks WHERE rowid=?", (r["rowid"],))
                db.execute("INSERT INTO fts_chunks(rowid, text) VALUES(?,?)",
                           (r["rowid"], fts_text))
                db.execute("UPDATE chunks SET fts_context_version=? WHERE rowid=?",
                           (self.FTS_CONTEXT_VERSION, r["rowid"]))
                n += 1
            return n
```

Add the column in `init()` alongside the other additive `ALTER`s:

```python
        # additive migration: FTS contextual-reindex marker (Phase C backfill)
        self._add_column_if_missing(db, "chunks", "fts_context_version", "INTEGER DEFAULT 0")
```

(Use the existing helper the codebase uses for additive columns; if none, wrap `ALTER TABLE chunks ADD COLUMN …` in a `try/except sqlite3.OperationalError`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_contextual_bm25.py -q -n0`
Expected: PASS

- [ ] **Step 5: Wire a bounded backfill call** into the index pass (`index.py:index_pending`, after embedding pending) OR the daemon `index` cadence, so it drains over cycles:

```python
    # Phase C: drain the contextual-BM25 FTS backfill in bounded batches.
    try:
        store.reindex_fts_batch(cap=5000)
    except Exception:  # noqa: BLE001
        pass
```

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/store.py mcpbrain/index.py tests/test_contextual_bm25.py
git commit -m "feat(recall): bounded FTS contextual re-index backfill (Phase C)"
```

### Task C3: Gold-gate validation for Phase C

**Files:** none (validation only)

- [ ] **Step 1:** Run `reindex_fts_batch` to completion on the eval store, then `uv run python tests/eval/run_eval.py --gold --k 10`.
- [ ] **Step 2:** Confirm recall@10 ≥ 0.55, MRR ≥ 0.35, ideally improved (contextual BM25 should help keyword-driven title queries). `contextual_retrieval` is already ON, so no flag flip — just verify the FTS backfill drained on the live store and holds the gate.
- [ ] **Step 3:** Record final numbers across all three phases in the spec.

---

## Self-Review

**Spec coverage:**
- Phase A expansion (thread=stuff / short-doc / span-stitch, cap, budget, head-tail, flag) → Tasks A1–A4. ✓
- Phase B cross-encoder rerank (fastembed, lazy, lexical fallback, small-chunk, before expansion) → Tasks B1–B3. ✓
- Phase C contextual BM25 (FTS prefix + backfill, existing flag, no re-embed) → Tasks C1–C2. ✓
- Per-phase gold gate → A5/B4/C3. ✓
- Pipeline ordering (rank→rerank→sufficiency→expand) → enforced by wiring expansion last in `search()` (A4) and rerank inside `route()` (B3). ✓
- Bloat guard → token_budget in A3 + A5 validation note. ✓

**Placeholder scan:** every code step has concrete code; the only "follow the existing helper" note (C2 additive column) references a real, described fallback (`try/except sqlite3.OperationalError`). ✓

**Type consistency:** `parent_key`/`group_by_parent`/`expand_parent`/`expand_hits` signatures consistent A1→A3; `_cross_encoder_rerank`/`_apply_rerank`/`get_reranker.rerank` consistent B1→B3; `_fts_text`/`reindex_fts_batch`/`FTS_CONTEXT_VERSION` consistent C1→C2. ✓
