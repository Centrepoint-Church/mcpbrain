# Injection-Only Expansion — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Re-introduce small-to-big expansion so it enriches ONLY the UserPromptSubmit auto-RAG injection path (`prompt_recall.py`), never `brain_search`'s flat candidate list — the mis-wiring that made the first attempt regress recall.

**Architecture:** Expansion stays daemon-side (needs the store). `daemon.search` gains an `expand` param; `/api/recall` reads `expand` from the request body; `brain_search` never sets it (stays flat), `prompt_recall` sets it (gets stitched context). A `maybe_expand` gate applies `expand_hits` only when `expand=True` AND the `retrieval_expand` config flag is ON (default OFF). `prompt_recall` uses larger formatting caps when expansion is active so the richer context isn't re-truncated.

**Tech Stack:** Python 3.12, SQLite, pytest (`-n0` for single tests). Reuses the reverted-but-sound `retrieval_expand.expand_hits` (git history `bdb18c2`..`db0044e`).

## Global Constraints

- **`retrieval_expand` flag defaults OFF.** Shipping this code changes NOTHING until the flag is set. `brain_search` recall@k is unaffected by construction (it never sets `expand`).
- **Recall must never raise** — `maybe_expand` and every new branch degrade to the pre-expansion hits on exception.
- **`brain_search` path unchanged:** the retrieval gold gate must still read recall@10=0.750 / MRR=0.514 (expansion is off that path).
- **Validation is deterministic (no LLM judge):** (1) gold gate unchanged; (2) context-completeness — with the flag ON, the injected context for a multi-chunk doc contains more of that doc than the old 200-char snippet; (3) a bloat cap bounds injected chars. A qualitative on/off eyeball is a post-release manual step, not automated.
- **Run scoped tests** (`-n0`); the human runs the full suite. Commit after each green step. Do NOT bump versions (release is a separate step).

## File Structure

- **Restore:** `mcpbrain/retrieval_expand.py`, `tests/test_retrieval_expand.py` (from git `db0044e`).
- **Modify:** `mcpbrain/retrieval_expand.py` (add `maybe_expand`), `mcpbrain/config.py` (flag + params), `mcpbrain/daemon.py` (`search` expand param), `mcpbrain/control_api.py` (`/api/recall` expand body param), `mcpbrain/prompt_recall.py` (request expand + expanded-aware formatting).
- **Test:** `tests/test_retrieval_expand.py`, `tests/test_prompt_recall.py` (extend), plus a config test.

---

### Task 1: Restore the expansion module from history

**Files:**
- Restore: `mcpbrain/retrieval_expand.py`, `tests/test_retrieval_expand.py`

**Interfaces:**
- Produces: `expand_hits(store, hits, *, window_n=3, short_doc_max_chunks=15, max_parents=5, token_budget=6000) -> list[dict]`, plus `parent_key`, `group_by_parent`, `expand_parent`, `_attach_metadata`, `_head_tail` (all as they were at commit `db0044e`).

- [ ] **Step 1: Restore the two files from git history**

Run:
```bash
git checkout db0044e -- mcpbrain/retrieval_expand.py tests/test_retrieval_expand.py
```

- [ ] **Step 2: Run the restored tests to confirm green**

Run: `uv run pytest tests/test_retrieval_expand.py -q -n0`
Expected: PASS (8 passed) — the module + its tests are self-contained and pure.

- [ ] **Step 3: Confirm ruff clean**

Run: `uv run ruff check mcpbrain/retrieval_expand.py`
Expected: `All checks passed!`

- [ ] **Step 4: Commit**

```bash
git add mcpbrain/retrieval_expand.py tests/test_retrieval_expand.py
git commit -m "feat(recall): restore expand_hits module (injection-only rework, Task 1)"
```

---

### Task 2: `maybe_expand` gate + config readers

**Files:**
- Modify: `mcpbrain/retrieval_expand.py`, `mcpbrain/config.py`
- Test: `tests/test_retrieval_expand.py`

**Interfaces:**
- Consumes: `config.retrieval_expand_enabled(home) -> bool`, `config.expand_params(home) -> dict`, `expand_hits(...)`.
- Produces: `maybe_expand(store, hits, *, home, expand) -> list[dict]` — returns `hits` unchanged unless `expand` is True AND the flag is on; then returns `expand_hits(store, hits, **expand_params)`; degrades to `hits` on any exception.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_retrieval_expand.py
from mcpbrain import config as _config

def test_maybe_expand_passthrough_when_expand_false(monkeypatch, tmp_path):
    monkeypatch.setattr(_config, "retrieval_expand_enabled", lambda home: True)
    hits = [{"doc_id": "d1", "score": 1.0, "distance": 0.1, "text": "x"}]
    assert rx.maybe_expand(_StoreWithMeta({}), hits, home=str(tmp_path), expand=False) is hits

def test_maybe_expand_passthrough_when_flag_off(monkeypatch, tmp_path):
    monkeypatch.setattr(_config, "retrieval_expand_enabled", lambda home: False)
    hits = [{"doc_id": "d1", "score": 1.0, "distance": 0.1, "text": "x"}]
    assert rx.maybe_expand(_StoreWithMeta({}), hits, home=str(tmp_path), expand=True) is hits

def test_maybe_expand_stitches_when_both_on(monkeypatch, tmp_path):
    monkeypatch.setattr(_config, "retrieval_expand_enabled", lambda home: True)
    doc = "gdrive-f1-0"
    meta = {"file_id": "f1", "chunk_index": 0}
    chunks = {doc: {"doc_id": doc, "text": "page0", "metadata": meta, "memory_tier": ""}}
    files = {"f1": [{"doc_id": doc, "text": "page0", "metadata": meta, "idx": 0}]}
    store = _StoreWithMeta(chunks, files=files)
    hits = [{"doc_id": doc, "score": 1.0, "distance": 0.1, "text": "page0"}]
    out = rx.maybe_expand(store, hits, home=str(tmp_path), expand=True)
    assert out and out[0]["doc_id"] == doc  # went through expand_hits (grouped by file)

def test_config_retrieval_expand_defaults_off(tmp_path):
    assert _config.retrieval_expand_enabled(str(tmp_path)) is False
    assert _config.expand_params(str(tmp_path)) == {
        "window_n": 3, "short_doc_max_chunks": 15, "max_parents": 5, "token_budget": 6000}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_retrieval_expand.py -q -n0 -k maybe_expand`
Expected: FAIL — `module 'mcpbrain.retrieval_expand' has no attribute 'maybe_expand'`

- [ ] **Step 3: Write minimal implementation**

Add to `mcpbrain/retrieval_expand.py`:
```python
def maybe_expand(store, hits, *, home, expand):
    """Apply small-to-big expansion ONLY when a consumer asks (expand=True) AND
    the retrieval_expand flag is on. brain_search never sets expand → flat hits.
    Degrades to the input hits on any error — recall must never raise."""
    if not expand:
        return hits
    from mcpbrain import config
    if not config.retrieval_expand_enabled(home):
        return hits
    try:
        return expand_hits(store, hits, **config.expand_params(home))
    except Exception:  # noqa: BLE001
        return hits
```

Add to `mcpbrain/config.py` (near the other `retrieval_*` readers):
```python
def retrieval_expand_enabled(home) -> bool:
    """Whether small-to-big expansion enriches the UserPromptSubmit injection path
    (prompt_recall). Never affects brain_search's flat list. Default OFF."""
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

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_retrieval_expand.py -q -n0`
Expected: PASS (12 passed: 8 restored + 4 new)

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/retrieval_expand.py mcpbrain/config.py tests/test_retrieval_expand.py
git commit -m "feat(recall): maybe_expand gate + retrieval_expand config (default OFF)"
```

---

### Task 3: Wire `expand` through `daemon.search` and `/api/recall`

**Files:**
- Modify: `mcpbrain/daemon.py` (`search` method), `mcpbrain/control_api.py` (`/api/recall` handler)
- Test: `tests/test_retrieval_expand.py` (forwarding), `tests/test_control_api_post.py` (endpoint param)

**Interfaces:**
- Consumes: `maybe_expand(store, hits, *, home, expand)`.
- Produces: `Daemon.search(query, limit=5, *, expand=False)`; `/api/recall` accepts `{"expand": bool}` (default False).

- [ ] **Step 1: Write the failing test (search forwards expand to maybe_expand)**

```python
# tests/test_retrieval_expand.py — verifies the wiring calls maybe_expand with the flag
def test_daemon_search_forwards_expand(monkeypatch):
    import mcpbrain.retrieval_expand as rxmod
    from mcpbrain import daemon as dmod
    calls = {}
    monkeypatch.setattr(rxmod, "maybe_expand",
                        lambda store, hits, *, home, expand: calls.update(expand=expand) or hits)
    # minimal daemon stub exercising only the tail of search(): build a Daemon
    # instance is heavy, so assert the module-level call contract instead.
    # (Integration is covered by the /api/recall endpoint test below.)
    assert "maybe_expand" in dir(rxmod)
```

> NOTE to implementer: a full `Daemon` is heavy to construct in a unit test. The binding assertion here is minimal; the real forwarding is verified by the `/api/recall` endpoint test in Step 4 (which drives the handler with a stub daemon that records the `expand` it receives). If you can cheaply construct the daemon in this repo's existing daemon-test harness (see `tests/test_daemon_control_wiring.py`), prefer a direct `search(..., expand=True)` assertion and replace this placeholder.

- [ ] **Step 2: Run to verify current state**

Run: `uv run pytest tests/test_control_api_post.py -q -n0`
Expected: PASS (existing endpoint tests still green — baseline before change).

- [ ] **Step 3: Implement the wiring**

In `mcpbrain/daemon.py`, change the `search` signature and its final return:
```python
    def search(self, query: str, limit: int = 5, *, expand: bool = False) -> list[dict]:
```
Replace the final `return result_hits` with:
```python
        from mcpbrain.retrieval_expand import maybe_expand
        try:
            return maybe_expand(self._store, result_hits, home=home, expand=expand)
        except Exception:  # noqa: BLE001 — recall must never raise
            return result_hits
```
(`home` is already bound as `home = str(app_dir())` earlier in `search`.)

In `mcpbrain/control_api.py`, the `/api/recall` handler:
```python
                q = (body.get("query") or "").strip()
                limit = min(int(body.get("limit") or 5), 10)
                expand = bool(body.get("expand"))
                results = d.search(q, limit, expand=expand) if q else []
```

- [ ] **Step 4: Write + run the endpoint test (expand plumbs through)**

Add to `tests/test_control_api_post.py` (follow that file's existing handler-invocation pattern; use a stub daemon that records the `expand` kwarg it receives):
```python
def test_api_recall_passes_expand_flag(...):
    # Drive the /api/recall handler with body {"query": "hi", "expand": true}
    # against a stub daemon whose search() records kwargs; assert expand=True received.
    # With {"query": "hi"} (no expand), assert expand=False.
```
Run: `uv run pytest tests/test_control_api_post.py tests/test_retrieval_expand.py -q -n0`
Expected: PASS. Then `uv run ruff check mcpbrain/daemon.py mcpbrain/control_api.py`.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/daemon.py mcpbrain/control_api.py tests/test_control_api_post.py tests/test_retrieval_expand.py
git commit -m "feat(recall): thread expand through daemon.search + /api/recall (brain_search stays flat)"
```

---

### Task 4: `prompt_recall` requests expansion + expanded-aware formatting

**Files:**
- Modify: `mcpbrain/prompt_recall.py`
- Test: `tests/test_prompt_recall.py`

**Interfaces:**
- Consumes: `config.retrieval_expand_enabled(home)`.
- Produces: `_recall` posts `"expand": <flag>`; `_format_context(results, seen, *, expanded=False)` uses larger caps when `expanded`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_prompt_recall.py
def test_recall_requests_expand_when_flag_on(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(json.dumps({"retrieval_expand": True}))
    captured = {}
    # patch urlopen path indirectly: assert _recall builds a payload with expand True
    # by monkeypatching config and inspecting the posted body via a fake urlopen.
    # (Follow the file's fail-open urlopen pattern; assert body["expand"] is True.)

def test_format_context_expanded_keeps_larger_context(tmp_path):
    from mcpbrain import prompt_recall as pr
    long_text = "sentence. " * 200  # ~2000 chars — a stitched parent
    results = [{"doc_id": "d1", "score": 1.0, "text": long_text}]
    block, injected = pr._format_context(results, set(), expanded=True)
    assert len(injected["d1"]) > pr._SNIPPET          # not truncated to the flat 200-char cap
    assert len(block) <= pr._EXPANDED_MAX_TOTAL + 100  # bounded by the expanded budget

def test_format_context_flat_unchanged(tmp_path):
    from mcpbrain import prompt_recall as pr
    results = [{"doc_id": "d1", "score": 1.0, "text": "x" * 999}]
    block, injected = pr._format_context(results, set(), expanded=False)
    assert len(injected["d1"]) <= pr._SNIPPET          # flat path still 200-char capped
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_prompt_recall.py -q -n0 -k "expand or expanded or flat_unchanged"`
Expected: FAIL — `_format_context() got an unexpected keyword argument 'expanded'` / `_EXPANDED_MAX_TOTAL` undefined.

- [ ] **Step 3: Implement**

In `mcpbrain/prompt_recall.py`:
- Add caps near the existing ones:
```python
_EXPANDED_SNIPPET = 1500     # per-item cap when expansion is active (stitched context)
_EXPANDED_MAX_TOTAL = 4000   # total cap for the expanded injection block
```
- `_recall(home, query)` — request expansion only when the flag is on, so the daemon skips the work otherwise:
```python
    expand = config.retrieval_expand_enabled(home)
    payload = json.dumps({"query": query, "limit": _LIMIT, "expand": expand}).encode()
```
- `_format_context(results, seen, *, expanded=False)` — choose caps by mode:
```python
def _format_context(results, seen, *, expanded=False):
    snip_cap = _EXPANDED_SNIPPET if expanded else _SNIPPET
    total_cap = _EXPANDED_MAX_TOTAL if expanded else _MAX_TOTAL
    ...
        snippet = " ".join((r.get("text") or "").split())[:snip_cap].strip()
        ...
        if total + len(snippet) > total_cap:
            break
    ...
```
- In `user_prompt_submit`, pass the mode through:
```python
    expanded = config.retrieval_expand_enabled(home)
    block, injected = _format_context(results, seen, expanded=expanded)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_prompt_recall.py -q -n0`
Expected: PASS (existing prompt_recall tests + 3 new). Then `uv run ruff check mcpbrain/prompt_recall.py`.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/prompt_recall.py tests/test_prompt_recall.py
git commit -m "feat(recall): prompt_recall requests expansion + larger caps for stitched context"
```

---

### Task 5: Deterministic validation (controller-run)

**Files:** none (validation only)

- [ ] **Step 1: brain_search path unchanged** — run the gold gate; confirm recall@10=0.750, MRR=0.514 (expansion is off that path, so it MUST be unchanged): `uv run python tests/eval/run_eval.py --gold --k 10`.
- [ ] **Step 2: Context-completeness (flag ON)** — with `retrieval_expand: true` in a scratch home, drive `user_prompt_submit` for a query hitting a multi-chunk doc; assert the injected `additionalContext` contains more of the expected document than the flat 200-char snippet would (e.g. neighbouring chunk text present). Record the before/after char counts.
- [ ] **Step 3: Bloat cap** — confirm the injected block stays within `_EXPANDED_MAX_TOTAL`.
- [ ] **Step 4: Hand off** — the human runs the full suite; and post-release, with the flag flipped on this machine, eyeballs injected context on/off on real prompts before any fleet-wide enable.

---

## Self-Review

**Spec coverage** (`2026-07-22-expansion-injection-followup.md`):
- Injection-only expansion (not brain_search) → Tasks 2–4 (consumer-split via `expand` param + `maybe_expand` gate). ✓
- brain_search recall@k unaffected → by construction (never sets expand) + Task 5 Step 1 gate. ✓
- Deterministic validation (chosen over the LLM-judge harness, per the 2026-07-22 decision) → Task 5. ✓ (The full RAGAS/faithfulness harness remains a deferred enhancement, not in this plan.)
- Flag default OFF → Task 2 config. ✓
- Reuse reverted expansion logic → Task 1 restore. ✓

**Placeholder scan:** Task 3 Step 1 and Task 4 Step 1 contain prose-guided test stubs rather than fully-spelled code, because they depend on each test file's existing harness (stub-daemon handler invocation in `test_control_api_post.py`; fake-`urlopen` pattern in `test_prompt_recall.py`). Each names the exact file, pattern to follow, and the assertion required. The implementer must fill them concretely following the cited existing tests — flagged here so the reviewer checks they are real assertions, not vacuous.

**Type consistency:** `maybe_expand(store, hits, *, home, expand)` defined in Task 2, consumed identically in Task 3; `expand_params` keys match `expand_hits` kwargs; `_format_context(..., *, expanded)` signature consistent Task 4 Steps 1/3.
