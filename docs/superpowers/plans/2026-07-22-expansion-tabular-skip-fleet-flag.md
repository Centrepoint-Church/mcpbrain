# Expansion tabular-skip + fleet-flippable flag — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Stop expansion from stitching CSV walls on tabular/cold docs (fall back to the flat snippet), and make `retrieval_expand` enable-able fleet-wide via `org-config.json`.

**Architecture:** Two small, independent changes to existing files. Change 1 adds a low-signal guard in `retrieval_expand.expand_hits`. Change 2 adds a generic `flags` overlay to the org-config allowlist + a `config.fleet_flag` helper that `retrieval_expand_enabled` delegates to.

**Tech Stack:** Python 3.12, pytest (`-n0` for single tests).

## Global Constraints

- `retrieval_expand` still defaults OFF. brain_search stays flat (untouched). Gold gate must hold recall@10 0.750 / MRR 0.514.
- Recall never raises — the skip guard degrades to expanding (or flat) on any error.
- `fleet_flag` precedence: **org overlay wins** (`org_config.flags[name]`) → top-level config → default (so a fleet enable reaches everyone).
- Run scoped tests (`-n0`); human runs full suite. Commit after each green step. No version bump in this plan (release is a separate step).

## File Structure
- Modify: `mcpbrain/retrieval_expand.py` (low-signal skip), `mcpbrain/config.py` (`fleet_flag` + delegate), `mcpbrain/fleet.py` (allowlist).
- Test: `tests/test_retrieval_expand.py`, `tests/test_config_fleet_flag.py` (new) or extend an existing config test.

---

### Task 1: Skip expansion for tabular/cold parents

**Files:**
- Modify: `mcpbrain/retrieval_expand.py`
- Test: `tests/test_retrieval_expand.py`

**Interfaces:**
- Consumes: `store.get_chunk(doc_id) -> {doc_id,text,metadata,memory_tier}|None`.
- Produces: `_is_low_signal(store, doc_id) -> bool`; `expand_hits` uses it to fall back to the flat snippet for low-signal parents.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_retrieval_expand.py — append
def test_expand_hits_skips_tabular_and_cold_parents():
    # one prose file (expands), one table file, one cold file (both fall back to flat)
    chunks, files = {}, {}
    def mk(fid, subtype, tier, nchunks):
        for i in range(nchunks):
            doc = f"gdrive-{fid}-{i}"
            meta = {"file_id": fid, "chunk_index": i, "content_subtype": subtype}
            chunks[doc] = {"doc_id": doc, "text": f"{fid}-body{i}", "metadata": meta, "memory_tier": tier}
            files.setdefault(fid, []).append({"doc_id": doc, "text": f"{fid}-body{i}",
                                              "metadata": meta, "idx": i})
    mk("prose1", "prose", "", 5)
    mk("tab1", "table", "", 5)
    mk("cold1", "prose", "cold", 5)
    store = _StoreWithMeta(chunks, files=files)
    hits = [{"doc_id": "gdrive-prose1-0", "score": 1.0, "distance": 0.1, "text": "prose1-body0"},
            {"doc_id": "gdrive-tab1-0", "score": 0.9, "distance": 0.1, "text": "tab1-body0"},
            {"doc_id": "gdrive-cold1-0", "score": 0.8, "distance": 0.1, "text": "cold1-body0"}]
    out = {h["doc_id"]: h["text"] for h in rx.expand_hits(store, hits, max_parents=5, token_budget=100000)}
    # prose expands to the whole short doc (all 5 chunks joined)
    assert "prose1-body4" in out["gdrive-prose1-0"]
    # table + cold fall back to the flat single-chunk snippet only
    assert out["gdrive-tab1-0"] == "tab1-body0"
    assert out["gdrive-cold1-0"] == "cold1-body0"

def test_is_low_signal_table_or_cold():
    store = _StoreWithMeta({
        "t": {"doc_id": "t", "text": "x", "metadata": {"content_subtype": "table"}, "memory_tier": ""},
        "c": {"doc_id": "c", "text": "x", "metadata": {"content_subtype": "prose"}, "memory_tier": "cold"},
        "p": {"doc_id": "p", "text": "x", "metadata": {"content_subtype": "prose"}, "memory_tier": ""},
    })
    assert rx._is_low_signal(store, "t") is True
    assert rx._is_low_signal(store, "c") is True
    assert rx._is_low_signal(store, "p") is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_retrieval_expand.py -q -n0 -k "low_signal or skips_tabular"`
Expected: FAIL — `_is_low_signal` undefined.

- [ ] **Step 3: Implement**

Add to `mcpbrain/retrieval_expand.py`:
```python
def _is_low_signal(store, doc_id) -> bool:
    """True for parents not worth stitching: tabular content (CSV-wall risk) or
    cold-tier (salience-gated low signal). Such parents keep their flat snippet."""
    try:
        c = store.get_chunk(doc_id)
    except Exception:  # noqa: BLE001
        return False
    if not c:
        return False
    meta = c.get("metadata") or {}
    return meta.get("content_subtype") == "table" or c.get("memory_tier") == "cold"
```
In `expand_hits`, change the per-group text assignment from:
```python
        text = expand_parent(store, g, window_n=window_n,
                             short_doc_max_chunks=short_doc_max_chunks)
        if not text:
            text = by_doc[g["rep_doc_id"]].get("text", "")
```
to:
```python
        if _is_low_signal(store, g["rep_doc_id"]):
            text = by_doc[g["rep_doc_id"]].get("text", "")   # tabular/cold: keep flat snippet
        else:
            text = expand_parent(store, g, window_n=window_n,
                                 short_doc_max_chunks=short_doc_max_chunks)
            if not text:
                text = by_doc[g["rep_doc_id"]].get("text", "")
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_retrieval_expand.py -q -n0` (expect all pass) and `uv run ruff check mcpbrain/retrieval_expand.py`.

- [ ] **Step 5: Commit**
```bash
git add mcpbrain/retrieval_expand.py tests/test_retrieval_expand.py
git commit -m "feat(recall): skip expansion for tabular/cold parents (keep flat snippet)"
```

---

### Task 2: Generic fleet-flippable flags

**Files:**
- Modify: `mcpbrain/fleet.py` (allowlist), `mcpbrain/config.py` (`fleet_flag` + delegate)
- Test: `tests/test_config_fleet_flag.py` (new)

**Interfaces:**
- Produces: `config.fleet_flag(home, name, default=False)`; `config.retrieval_expand_enabled` delegates to it; `fleet._ALLOWLIST` includes `"flags"`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config_fleet_flag.py
import json
from mcpbrain import config, fleet

def _write(tmp_path, obj):
    (tmp_path / "config.json").write_text(json.dumps(obj))

def test_fleet_flag_org_overlay_wins(tmp_path):
    _write(tmp_path, {"retrieval_expand": False,
                      "org_config": {"flags": {"retrieval_expand": True}}})
    assert config.fleet_flag(str(tmp_path), "retrieval_expand", False) is True

def test_fleet_flag_top_level_fallback(tmp_path):
    _write(tmp_path, {"retrieval_expand": True})
    assert config.fleet_flag(str(tmp_path), "retrieval_expand", False) is True

def test_fleet_flag_default(tmp_path):
    _write(tmp_path, {})
    assert config.fleet_flag(str(tmp_path), "retrieval_expand", False) is False

def test_retrieval_expand_enabled_delegates_to_fleet_flag(tmp_path):
    _write(tmp_path, {"org_config": {"flags": {"retrieval_expand": True}}})
    assert config.retrieval_expand_enabled(str(tmp_path)) is True

def test_flags_is_allowlisted():
    assert "flags" in fleet._ALLOWLIST
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_config_fleet_flag.py -q -n0`
Expected: FAIL — `config.fleet_flag` undefined / `"flags"` not in allowlist.

- [ ] **Step 3: Implement**

In `mcpbrain/fleet.py`:
```python
_ALLOWLIST = frozenset({"cadences", "org_pin", "flags"})
```
In `mcpbrain/config.py`, add near `retrieval_expand_enabled`:
```python
def fleet_flag(home, name, default=False):
    """A feature flag resolvable fleet-wide. Precedence: the org-config overlay
    (config['org_config']['flags'][name], staged by fleet.merge_org_config from
    org-config.json — org wins so a fleet enable reaches everyone) → the user's
    top-level config[name] → default."""
    cfg = read_config(home)
    overlay = (cfg.get("org_config") or {}).get("flags") or {}
    if name in overlay:
        return overlay[name]
    return cfg.get(name, default)
```
Change `retrieval_expand_enabled` to delegate:
```python
def retrieval_expand_enabled(home) -> bool:
    """Whether injection-path expansion runs. Fleet-flippable via
    org-config.json {"flags": {"retrieval_expand": true}}. Default OFF."""
    return bool(fleet_flag(home, "retrieval_expand", False))
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_config_fleet_flag.py tests/test_retrieval_expand.py -q -n0` and `uv run ruff check mcpbrain/config.py mcpbrain/fleet.py`.

- [ ] **Step 5: Commit**
```bash
git add mcpbrain/config.py mcpbrain/fleet.py tests/test_config_fleet_flag.py
git commit -m "feat(fleet): generic org-config feature flags; retrieval_expand fleet-flippable"
```

---

### Task 3: Validation (controller-run)
- [ ] Gold gate unchanged: `uv run python tests/eval/run_eval.py --gold --k 10` → 0.750/0.514.
- [ ] Injection comparison (flag on): tabular queries (roster, youth calendar) fall back to flat (≈ OFF size); prose queries (Aaron Close, College sem-2, board, SOM) still expand.
- [ ] Full suite (human) before release.

## Self-Review
- Skip rule (`content_subtype='table'` OR cold → flat) → Task 1. ✓
- Generic fleet flag (allowlist `flags` + `fleet_flag` + delegate, org-wins precedence) → Task 2. ✓
- brain_search untouched / flag default OFF → unchanged from prior; Task 3 gold gate confirms. ✓
- No placeholders; `_is_low_signal`/`fleet_flag` signatures consistent across tasks.
