# mcpbrain Plugin Distribution + System Simplification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship mcpbrain as an org-managed Claude plugin while simplifying the daemon to the smallest system that does the best job — every fresh install fully functional out of the box.

**Architecture:** Three intertwined efforts in one sequenced plan. **Simplify first** (delete dead paths so later work has less surface), then **fix defaults + restructure cadences**, then **utilise the retained features + add monitoring**, then **swap the Claude-facing install** (remove `register`/hooks/`setup.*`, build the plugin assets in an in-repo `plugin/` dir). Maintainer infra (GitHub org + GCP) is a non-code checklist at the end.

**Tech Stack:** Python 3.12, SQLite (+ bge-small embeddings), launchd/Task Scheduler, FastMCP, `uv`, pytest, ruff. Tests run via `uv run pytest`; lint via `uv run ruff check mcpbrain/`.

**Spec:** `docs/superpowers/specs/2026-06-11-plugin-distribution-design.md` (§1–§9G).

---

## Execution order & phase dependencies

Run phases in order. Within a phase, tasks are independent unless noted.

- **Phase A — dead-path removals (§9B/§9C).** Must run first: A1 (relocate constants) before A2 (delete gemini); deleting dead code shrinks everything downstream.
- **Phase B — correctness + feature cut (§9A/§9E).** Depends on A (B1 assumes `_parse_first_json_object` now lives in `chunking.py`; B3 removes `_read_projects`/`_read_areas`).
- **Phase C — cadence refactor + defaults + platform (§9D/§9F).** Depends on B (C1's dispatch table excludes the resolve cadence B1 deleted; C1's `proactive` pass calls the no-op B3 left).
- **Phase D — utilise features + monitor (§9G/§8).** Depends on B+C (D1 guards C2's defaults; D4 targets the post-B3 `_build_context`).
- **Phase E — remove old install + build plugin (§6/§1–5/§8).** Depends on D (E1 edits the `cli.py` D5 last touched).
- **Phase F — maintainer infra (§7).** Non-code checklist; do last (or in parallel by the maintainer).

> **Reconciliation notes baked into this plan:**
> - **C2 owns `_cadences_from_config`.** D1 does NOT re-implement defaults — it only adds a community-focused regression test that passes because C2 did the work.
> - **D4 targets the post-B3 `_build_context`** — after B3 there are no `projects`/`areas` keys and no `_read_projects`/`_read_areas` to monkeypatch.
> - **`cli.py`:** D5 adds the `monitor` subcommand (its rewrite still lists `register`); E1 then removes `register`. Sequential, no conflict.

---

## File structure map

**Deleted (daemon repo):** `mcpbrain/embed_voyage.py`, `mcpbrain/sync/cursors.py`, `mcpbrain/hooks.py`, `mcpbrain/wizard/register.py`, `bin/seed_from_nexus.py`, `bin/seed_records.py`, `bin/dry_run_spool.py`, `install/setup.sh|.command|.ps1`, and the matching test files. `mcpbrain/enrich.py` shrinks to a constants re-export shim. `mcpbrain/proactive.py` becomes a no-op. `mcpbrain/lint_graph.py` loses 3 checks. Linux/systemd code leaves `mcpbrain/agents.py`.

**Created (daemon repo):** `mcpbrain/monitor.py`.

**Created (in-repo `plugin/` dir, published to the marketplace repo by the release step):** `plugin/.claude-plugin/plugin.json`, `plugin/.claude-plugin/marketplace.json`, `plugin/.mcp.json`, `plugin/bin/mcpbrain-mcp`, `plugin/bin/mcpbrain-monitor`, `plugin/skills/install/SKILL.md`, `plugin/skills/backfill/SKILL.md`, `plugin/agents/enrich-batch.md`, `plugin/hooks/hooks.json`, `plugin/monitors/monitors.json`.

**Modified:** `mcpbrain/daemon.py` (cadence dispatch, defaults, instructions, removals), `mcpbrain/store.py`, `mcpbrain/graph_write.py`, `mcpbrain/chunking.py`, `mcpbrain/config.py`, `mcpbrain/resolve.py`, `mcpbrain/contract.py`, `mcpbrain/prepare.py`, `mcpbrain/dashboard.py`, `mcpbrain/wizard/dashboard.html`, `mcpbrain/wizard/index.html`, `mcpbrain/probes.py`, `mcpbrain/control_api.py`, `mcpbrain/cli.py`, `mcpbrain/agents.py`, `pyproject.toml`.

---

# Phase A — Dead-path removals (§9B / §9C)

### Task A1: Relocate constants to canonical homes before deleting gemini code

**Files:**
- Modify: `mcpbrain/chunking.py`, `mcpbrain/contract.py`, `mcpbrain/resolve.py`, `mcpbrain/enrich.py`
- Test: `tests/test_contract.py`, `tests/test_resolve.py`

- [ ] **Step 1: Write the failing tests — assert the canonical import locations work**

```python
# Add to tests/test_contract.py
def test_valid_content_types_importable_from_contract():
    from mcpbrain.contract import _VALID_CONTENT_TYPES
    assert "request" in _VALID_CONTENT_TYPES
    assert "decision" in _VALID_CONTENT_TYPES

def test_valid_types_importable_from_chunking():
    from mcpbrain.chunking import _VALID_TYPES, _is_junk_entity
    assert "person" in _VALID_TYPES
    assert _is_junk_entity("Re: subject", "person") is True

def test_parse_first_json_object_importable_from_chunking():
    from mcpbrain.chunking import _parse_first_json_object
    assert _parse_first_json_object('{"a": 1}') == {"a": 1}
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_contract.py -k "importable" -q`
Expected: `ImportError` — none of these names exist in their new homes yet.

- [ ] **Step 3: Move the four constants into their canonical homes**

In `mcpbrain/chunking.py`, append after the `content_hash` function (after line 80):

```python
import json as _json
import re as _re

_VALID_CONTENT_TYPES = {"request", "update", "decision", "fyi", "notification"}

_VALID_TYPES = ("person", "org", "project", "topic")

_STRUCTURAL_JUNK = [
    _re.compile(r"^(Re|Fwd|FW|RE|FWD)\s*:", _re.IGNORECASE),
    _re.compile(r"https?://"),
    _re.compile(r"\w+@\w+\.\w+"),
    _re.compile(r"[|{}\[\]<>]"),
]

_NUMERIC_JUNK = [
    _re.compile(r"\d{4}"),
    _re.compile(r"\d{2,}/\d{2,}"),
]


def _is_junk_entity(name: str, etype: str) -> bool:
    """Reject obviously-bad person/org entities."""
    if etype not in ("person", "org"):
        return False
    name = (name or "").strip()
    if len(name) < 2 or len(name) > 60:
        return True
    for pattern in _STRUCTURAL_JUNK:
        if pattern.search(name):
            return True
    if etype == "person":
        for pattern in _NUMERIC_JUNK:
            if pattern.search(name):
                return True
    return False


def _strip_fences(raw: str) -> str:
    s = raw.strip()
    if s.startswith("```"):
        s = _re.sub(r"^```[a-zA-Z0-9]*\s*", "", s)
        s = _re.sub(r"\s*```$", "", s)
    return s.strip()


def _parse_first_json_object(raw: str) -> dict:
    """Parse the first complete JSON OBJECT from raw, ignoring trailing content."""
    s = _strip_fences(raw)
    decoder = _json.JSONDecoder()
    pos = 0
    while True:
        start = s.find("{", pos)
        if start == -1:
            raise ValueError("no JSON object in model output")
        try:
            obj, _end = decoder.raw_decode(s[start:])
        except _json.JSONDecodeError:
            pos = start + 1
            continue
        if isinstance(obj, dict):
            return obj
        pos = start + 1
```

In `mcpbrain/contract.py`, replace line 27 `from mcpbrain.enrich import _VALID_CONTENT_TYPES` with:

```python
from mcpbrain.chunking import _VALID_CONTENT_TYPES
```

In `mcpbrain/resolve.py`, replace line 13 `from mcpbrain.enrich import _parse_first_json_object, _DEFAULT_MODEL` with:

```python
from mcpbrain.chunking import _parse_first_json_object
_DEFAULT_MODEL = "gemini-2.5-flash-lite"
```

In `mcpbrain/enrich.py`, add after the existing chunking re-exports (lines 13-14):

```python
from mcpbrain.chunking import (  # noqa: E402 — canonical homes
    _VALID_CONTENT_TYPES,
    _VALID_TYPES,
    _is_junk_entity,
    _parse_first_json_object,
)
```

Then delete the inline definitions of `_VALID_CONTENT_TYPES`, `_VALID_TYPES`, `_STRUCTURAL_JUNK`/`_NUMERIC_JUNK`, `_is_junk_entity`, `_strip_fences`, and `_parse_first_json_object` from `enrich.py` (they are now imported from `chunking`).

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_contract.py tests/test_resolve.py tests/test_enrich.py tests/test_chunking.py -q` and `uv run ruff check mcpbrain/`
Expected: PASS / All checks passed

- [ ] **Step 5: Commit**

`git commit -m "refactor: move enrichment constants to chunking.py canonical home (§9B prep)"`

---

### Task A2: Delete the entire "gemini" enrichment mode

**Files:**
- Modify: `mcpbrain/enrich.py` (reduce to re-export shim), `mcpbrain/daemon.py` (delete `"gemini"` branch + `_enrich_client_from_config`), `mcpbrain/config.py` (drop `gemini` from `ENRICH_MODES`), `mcpbrain/store.py` (delete legacy tables + methods)
- Delete: `tests/test_enrich.py`
- Modify: `tests/test_enrich_mode.py`, `tests/test_run_cycle_modes.py`, `tests/test_config.py`

- [ ] **Step 1: Write the failing test — verify zero callers of removed names**

```python
# tests/test_a2_gemini_removed.py  (temp, deleted at end of task)
import subprocess

def test_no_callers_of_gemini_names():
    result = subprocess.run(
        ["grep", "-rn", "--include=*.py",
         r"run_enrichment\|enrich_document\|build_prompt\|make_gemini_client\|resolve_client",
         "mcpbrain/"],
        capture_output=True, text=True
    )
    hits = [l for l in result.stdout.strip().splitlines() if l]
    assert hits == [], f"still referencing deleted gemini names:\n{chr(10).join(hits)}"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_a2_gemini_removed.py -q`
Expected: `AssertionError` listing `mcpbrain/enrich.py`, `mcpbrain/daemon.py`.

- [ ] **Step 3: Perform the deletions and edits**

**`mcpbrain/enrich.py`** — reduce to:

```python
"""Enrichment constants and helpers; Gemini extraction removed (§9B).

The spool/drain path (extractor_driver + drain.py) is the sole live enrichment
path. Constants used by that path live in chunking.py and are re-exported here
for backward-compat (callers that `from mcpbrain.enrich import …`).
"""
from mcpbrain.chunking import (  # noqa: F401 — public re-exports
    slugify,
    _canonical_name,
    _VALID_CONTENT_TYPES,
    _VALID_TYPES,
    _is_junk_entity,
    _parse_first_json_object,
)

from mcpbrain import orgs as _orgs

_VALID_ORGS = set(_orgs.DEFAULT_TAXONOMY.valid_orgs)
```

**`mcpbrain/daemon.py`:** delete `from mcpbrain.enrich import run_enrichment` (line ~48); delete the `else:  # "gemini"` branch in `run_cycle` (around lines 296–303), leaving the `spool` and `off` branches; delete `_enrich_client_from_config` (lines ~1568–1576); in `apply_config` remove the `enrich_client = _enrich_client_from_config(home)` line and the `self._enrich_client = enrich_client` assignment.

**`mcpbrain/config.py`** line 48: `ENRICH_MODES = {"spool", "gemini", "off"}` → `ENRICH_MODES = {"spool", "off"}`.

**`mcpbrain/store.py`:** delete the `graph_actions_legacy`/`graph_decisions_legacy` CREATE block in `init()`; delete methods `add_action`, `add_decision`, `actions_for_owner`, `list_actions`, `list_decisions`.

**Delete `tests/test_enrich.py`.** In `tests/test_enrich_mode.py` delete `test_enrich_mode_gemini`. In `tests/test_run_cycle_modes.py` delete `test_run_cycle_gemini_unchanged`. In `tests/test_config.py` delete `test_gemini_key_in_config_yields_enrich_client` and `test_no_gemini_key_yields_no_enrich_client`.

- [ ] **Step 4: Run tests and grep check**

```bash
grep -rn --include="*.py" "run_enrichment\|enrich_document\|build_prompt\|make_gemini_client\|resolve_client\|add_action\|add_decision\|list_actions\|list_decisions\|actions_for_owner" mcpbrain/ tests/
```
Expected: zero hits.

Run: `uv run pytest -q --ignore=tests/test_a2_gemini_removed.py` and `uv run ruff check mcpbrain/`
Expected: PASS / All checks passed. Then delete `tests/test_a2_gemini_removed.py`.

- [ ] **Step 5: Commit**

`git commit -m "feat(cleanup): delete gemini enrichment mode — spool is the sole live path (§9B)"`

---

### Task A3: Delete dead bin scripts; move `store_dim_from_path` to store.py

**Files:**
- Modify: `mcpbrain/store.py`
- Delete: `bin/seed_from_nexus.py`, `bin/seed_records.py`, `bin/dry_run_spool.py`, `tests/test_seed.py`, `tests/test_seed_records.py`, `tests/test_dry_run_spool.py`

- [ ] **Step 1: Write the failing test**

```python
# Add to tests/test_store.py
def test_store_dim_from_path_returns_none_for_missing_db(tmp_path):
    from mcpbrain.store import store_dim_from_path
    assert store_dim_from_path(tmp_path / "missing.sqlite3") is None

def test_store_dim_from_path_returns_dim_for_existing_store(tmp_path):
    from mcpbrain.store import Store, store_dim_from_path
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    assert store_dim_from_path(tmp_path / "b.sqlite3") == 4
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_store.py -k "store_dim_from_path" -q`
Expected: `ImportError: cannot import name 'store_dim_from_path'`.

- [ ] **Step 3: Add the helper, then delete the scripts**

Add to `mcpbrain/store.py` (module-level, before the `Store` class):

```python
def store_dim_from_path(path) -> int | None:
    """Read the vector dim a store was built with from its meta table.

    Returns None when the file does not exist or has no dim row.
    """
    from pathlib import Path as _Path
    import sqlite3 as _sqlite3
    p = _Path(path)
    if not p.exists():
        return None
    try:
        db = _sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        row = db.execute("SELECT v FROM meta WHERE k='dim'").fetchone()
        db.close()
        return int(row[0]) if row else None
    except Exception:  # noqa: BLE001
        return None
```

Delete: `bin/seed_from_nexus.py`, `bin/seed_records.py`, `bin/dry_run_spool.py`, `tests/test_seed.py`, `tests/test_seed_records.py`, `tests/test_dry_run_spool.py`.

- [ ] **Step 4: Run tests and grep check**

```bash
grep -rn --include="*.py" "seed_from_nexus\|seed_records\|dry_run_spool\|_existing_store_dim" mcpbrain/ tests/ bin/
```
Expected: zero hits.

Run: `uv run pytest -q` and `uv run ruff check mcpbrain/`
Expected: PASS / All checks passed.

- [ ] **Step 5: Commit**

`git commit -m "feat(cleanup): delete dead bin/ migration scripts; move store_dim_from_path to store.py (§9B)"`

---

### Task A4: Delete `embed_voyage.py`, lock embedder to bge-small

**Files:**
- Modify: `mcpbrain/config.py`, `mcpbrain/embed.py`, `pyproject.toml`, callers of `config.EMBEDDER`
- Delete: `mcpbrain/embed_voyage.py`, `tests/test_embed_voyage.py`

- [ ] **Step 1: Write the failing test**

```python
# Add to tests/test_embed.py
def test_get_embedder_voyage_raises_value_error():
    import pytest
    from mcpbrain.embed import get_embedder
    with pytest.raises(ValueError, match="unknown embedder"):
        get_embedder("voyage")

def test_embed_voyage_module_not_importable():
    import importlib, pytest
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("mcpbrain.embed_voyage")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_embed.py -k "voyage" -q`
Expected: both fail (module exists; `get_embedder("voyage")` returns a `VoyageEmbedder`).

- [ ] **Step 3: Perform deletions and edits**

Delete `mcpbrain/embed_voyage.py` and `tests/test_embed_voyage.py`.

In `mcpbrain/embed.py` delete the `voyage` branch in `get_embedder` (lines ~87-89). Ensure the fall-through raises `ValueError(f"unknown embedder: {kind}")`.

In `mcpbrain/config.py` delete the `EMBEDDER = os.getenv("MCPBRAIN_EMBEDDER", "bge-small")` line. Replace callers with the literal `"bge-small"`:
- `mcpbrain/enrich_backfill.py`, `mcpbrain/daemon.py` (line ~1721), `mcpbrain/mcp_server.py`: `get_embedder(config.EMBEDDER)` → `get_embedder("bge-small")`.
- `mcpbrain/cowork/__init__.py`: remove `"MCPBRAIN_EMBEDDER": config.EMBEDDER` from the env dict.
- `mcpbrain/wizard/register.py`: (will be deleted in E1) — if it still references `--embedder`, ignore; E1 deletes the file.

In `pyproject.toml` remove the `voyage = ["voyageai>=0.3"]` optional-dependency entry.

- [ ] **Step 4: Run tests and grep check**

```bash
grep -rn --include="*.py" --include="*.toml" "voyage\|embed_voyage\|MCPBRAIN_EMBEDDER\|config\.EMBEDDER" mcpbrain/ tests/ pyproject.toml
```
Expected: zero hits (except the deleted `register.py` if present — handled in E1).

Run: `uv run pytest tests/test_embed.py -q` and `uv run ruff check mcpbrain/`
Expected: PASS.

- [ ] **Step 5: Commit**

`git commit -m "feat(cleanup): delete embed_voyage, lock embedder to bge-small (§9B)"`

---

### Task A5: Shrink `lint_graph.py` — delete three dead checks

**Files:**
- Modify: `mcpbrain/lint_graph.py`, `tests/test_lint.py`

- [ ] **Step 1: Write the failing test**

```python
# Add to tests/test_lint.py
def test_deleted_lint_checks_not_importable():
    from mcpbrain import lint_graph
    assert not hasattr(lint_graph, "check_possible_duplicates")
    assert not hasattr(lint_graph, "check_community_singletons")
    assert not hasattr(lint_graph, "check_threads_without_summary")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_lint.py::test_deleted_lint_checks_not_importable -q`
Expected: `AssertionError`.

- [ ] **Step 3: Delete the three functions, their dispatch, and report sections**

In `mcpbrain/lint_graph.py` delete the functions `check_possible_duplicates`, `check_community_singletons`, `check_threads_without_summary`; remove the now-unused `nameparser`/`rapidfuzz` imports; delete their `section(...)` calls in `build_report`; delete their dispatch blocks in `run()` (and the `lint:possible_duplicate`/`lint:community_singleton`/`lint:thread_no_summary` finding types). Keep `check_missing_org`, `check_ambiguous_org`, `check_duplicate_orgs`, `check_orphan_entities`, `check_ownerless_actions`, `check_unenriched_emails`.

In `tests/test_lint.py` delete the tests for the three removed checks and the `_add_thread_context` helper; update `test_lint_records_findings` to drop the thread-without-summary assertions.

- [ ] **Step 4: Run tests and grep check**

```bash
grep -rn --include="*.py" "check_possible_duplicates\|check_community_singletons\|check_threads_without_summary\|lint:possible_duplicate\|lint:community_singleton\|lint:thread_no_summary" mcpbrain/ tests/
```
Expected: zero hits.

Run: `uv run pytest tests/test_lint.py -q` and `uv run ruff check mcpbrain/`
Expected: PASS.

- [ ] **Step 5: Commit**

`git commit -m "feat(cleanup): shrink lint_graph.py — delete 3 redundant checks (§9B)"`

---

### Task A6: Remove dead schema — `doc_context`, `normalised_strength`/`since`, `suppressed_entities`

**Files:**
- Modify: `mcpbrain/store.py`, `mcpbrain/graph_write.py`, `tests/test_store_schema.py`

- [ ] **Step 1: Write the failing tests**

```python
# Add to tests/test_store_schema.py
from mcpbrain.store import Store

def test_doc_context_table_not_created(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4); s.init()
    with s._connect() as db:
        row = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='doc_context'").fetchone()
    assert row is None

def test_suppressed_entities_table_not_created(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4); s.init()
    with s._connect() as db:
        row = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='suppressed_entities'").fetchone()
    assert row is None

def test_entity_relations_has_no_normalised_strength_or_since(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4); s.init()
    with s._connect() as db:
        cols = {r["name"] for r in db.execute("PRAGMA table_info(entity_relations)").fetchall()}
    assert "normalised_strength" not in cols and "since" not in cols
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_store_schema.py -k "doc_context or suppressed_entities or normalised_strength" -q`
Expected: all three fail.

- [ ] **Step 3: Remove from `store.py` and `graph_write.py`**

In `mcpbrain/store.py`: delete the `doc_context` CREATE TABLE + its index; in the `entity_relations` ALTER loop remove the `("normalised_strength", "REAL DEFAULT 0.0")` and `("since", "TEXT")` entries; delete the `suppressed_entities` CREATE TABLE block.

In `mcpbrain/graph_write.py` `upsert_entity`: delete the suppression guard block:

```python
            suppressed = conn.execute(
                "SELECT 1 FROM suppressed_entities WHERE name_lower = ?",
                (normalised,)).fetchone()
            if suppressed:
                return None
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_store_schema.py tests/test_graph_write.py -q` and `uv run ruff check mcpbrain/`
Expected: PASS.

- [ ] **Step 5: Commit**

`git commit -m "feat(cleanup): remove dead schema — doc_context, suppressed_entities, normalised_strength/since (§9C)"`

---

### Task A7: Delete dead/superseded functions

**Files:**
- Delete: `mcpbrain/sync/cursors.py`, `tests/test_cursors.py`, `tests/test_backfill_order.py`
- Modify: `mcpbrain/sync/__init__.py`, `tests/test_backfill_exec.py`, `mcpbrain/clickup.py`, `tests/test_clickup.py`, `mcpbrain/orgs.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_a7_dead_functions_removed.py  (temp)
import pytest

def test_cursors_module_not_importable():
    with pytest.raises(ModuleNotFoundError):
        import mcpbrain.sync.cursors  # noqa: F401

def test_sync_dead_functions_gone():
    from mcpbrain import sync
    for n in ("backfill_windows", "gmail_query", "initial_backfill"):
        assert not hasattr(sync, n), f"{n} should be deleted"

def test_update_task_status_gone():
    from mcpbrain import clickup
    assert not hasattr(clickup, "update_task_status")

def test_orgs_dead_vars_gone():
    from mcpbrain import orgs
    assert not hasattr(orgs, "_DEFAULT_DOMAIN_ORG")
    assert not hasattr(orgs, "_DEFAULT_ALIASES")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_a7_dead_functions_removed.py -q`
Expected: all fail.

- [ ] **Step 3: Perform the deletions**

Delete `mcpbrain/sync/cursors.py`, `tests/test_cursors.py`, `tests/test_backfill_order.py`. In `mcpbrain/sync/__init__.py` delete `backfill_windows`, `gmail_query`, `initial_backfill`. In `tests/test_backfill_exec.py` delete the `initial_backfill` tests and its import. In `mcpbrain/clickup.py` delete `update_task_status`; in `tests/test_clickup.py` delete `TestUpdateTaskStatus`. In `mcpbrain/orgs.py` delete the `_DEFAULT_DOMAIN_ORG` and `_DEFAULT_ALIASES` module vars.

- [ ] **Step 4: Run tests and grep check**

```bash
grep -rn --include="*.py" "sync.cursors\|backfill_windows\|gmail_query\|initial_backfill\|update_task_status\|_DEFAULT_DOMAIN_ORG\|_DEFAULT_ALIASES" mcpbrain/ tests/
```
Expected: zero hits.

Run: `uv run pytest -q` and `uv run ruff check mcpbrain/`
Expected: PASS. Delete `tests/test_a7_dead_functions_removed.py`.

- [ ] **Step 5: Commit**

`git commit -m "feat(cleanup): delete cursors.py, dead backfill fns, update_task_status, orgs dead vars (§9C)"`

---

### Task A8: Collapse duplicated helpers

**Files:**
- Modify: `mcpbrain/config.py` (add `spool_home`), `mcpbrain/drain.py`, `mcpbrain/extractor_driver.py`, `mcpbrain/extractor_io.py`, `mcpbrain/graph_write.py`, `mcpbrain/profile_synth.py`, `mcpbrain/profile_audit.py`, `mcpbrain/enrich_backfill.py`

- [ ] **Step 1: Write the failing test**

```python
# Add to tests/test_config.py
def test_spool_home_default_is_app_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain.config import spool_home, app_dir
    assert spool_home() == app_dir()

def test_spool_home_override(tmp_path):
    from mcpbrain.config import spool_home
    assert spool_home(str(tmp_path / "x")) == (tmp_path / "x")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_config.py -k "spool_home" -q`
Expected: `ImportError`.

- [ ] **Step 3: Add `spool_home` and route the duplicates through it**

Add to `mcpbrain/config.py`:

```python
def spool_home(home=None) -> Path:
    """Resolve the spool root: explicit override first, else app_dir().

    Single canonical implementation replacing the duplicate _home() helpers
    in drain.py and extractor_driver.py (§9C).
    """
    return Path(home) if home is not None else app_dir()
```

In `mcpbrain/drain.py` and `mcpbrain/extractor_driver.py`, change `_home` to delegate: `return config.spool_home(home)`.

In `mcpbrain/extractor_driver.py`, replace `_write_inbox` with a call to `extractor_io.atomic_write_inbox` (delete the local body, keep the call signature `run_extractor` uses). In `mcpbrain/extractor_io.py`, replace the local `_VALID_CONTENT_TYPES` with `from mcpbrain.chunking import _VALID_CONTENT_TYPES`.

In `mcpbrain/graph_write.py`, delete one of the two Jaccard functions (`_jaccard_action` / `_token_jaccard`) and point both callers at the survivor; replace `_norm_action` with `chunking._normalise_title_for_dedup`.

In `mcpbrain/profile_synth.py` and `mcpbrain/profile_audit.py`, replace the two private `_fetch_role` with a shared `fetch_role(store, entity_id)` added to `graph_write.py` (preserve `profile_audit`'s `valid_to` filter via a parameter `current_only=True`).

In `mcpbrain/enrich_backfill.py`, delete `local_claude_runner` and default the runner to `extractor_io.claude_runner` (pass `run_claude=extractor_io.claude_runner` into `extractor_driver.run_extractor`; ensure the JSON envelope is unwrapped via `extract_answer` where `run_extractor` does its `json.loads`).

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_config.py tests/test_drain.py tests/test_extractor_driver.py tests/test_graph_write.py tests/test_profile_synth.py tests/test_profile_audit.py tests/test_enrich_backfill.py -q` and `uv run ruff check mcpbrain/`
Expected: PASS.

- [ ] **Step 5: Commit**

`git commit -m "refactor: collapse duplicated helpers (_home, _write_inbox, jaccard, _fetch_role, claude runner) (§9C)"`

---

# Phase B — Correctness fixes + feature cut (§9A / §9E)

### Task B1: Delete the dead resolve cadence + LLM-adjudication tier

**Files:**
- Modify: `mcpbrain/daemon.py`, `mcpbrain/resolve.py`, `tests/test_daemon.py`, `tests/test_backfill_singleflight.py`, `tests/test_resolve.py`

> **Depends on A1** — `_parse_first_json_object` now lives in `chunking.py`; `_adjudicate` (the only remaining importer of it via `resolve.py`) is deleted here.

- [ ] **Step 1: Verify merge_review owns adjudication (grep, no code change)**

```bash
grep -n "merge_review\|resolution_due" mcpbrain/prepare.py mcpbrain/daemon.py
grep -rn "resolve_entities" mcpbrain/ --include="*.py"
```
Expected for the second grep: only `daemon.py` (inside `maybe_resolve`) and `resolve.py`. If any other production caller appears, keep `resolve_entities`; otherwise it can be reduced to deterministic-only.

- [ ] **Step 2: Write the failing test**

```python
# Add to tests/test_daemon.py
def test_maybe_resolve_does_not_exist():
    import inspect
    from mcpbrain.daemon import Daemon
    assert not hasattr(Daemon, "maybe_resolve")
    assert not hasattr(Daemon, "_resolve_due")
    sig = inspect.signature(Daemon.__init__)
    assert "resolve_interval_s" not in sig.parameters
```
Also in `tests/test_backfill_singleflight.py`, remove the `maybe_resolve` half of `test_maybe_resolve_and_backup_skip_while_backfill_active` (drop the `d._resolve_interval_s`/`d._last_resolve` lines and the `assert d.maybe_resolve() is None`); keep the backup half.

- [ ] **Step 3: Run to verify it fails**

Run: `uv run pytest tests/test_daemon.py::test_maybe_resolve_does_not_exist -q`
Expected: `AssertionError`.

- [ ] **Step 4: Delete from `daemon.py`**

Remove the `resolve_interval_s` constructor param and `self._resolve_interval_s`/`self._last_resolve` init; remove `_resolve_due` and `maybe_resolve` methods; remove the `self.maybe_resolve()` call in the run loop. Where `resolution_due = self._resolve_due()` fed `run_cycle`, replace with `resolution_due = True` (merge_review candidates are cheap to generate every cycle; the Cowork extractor decides adjudication).

- [ ] **Step 5: Delete from `resolve.py`**

Remove `_adjudicate`, `_pick_winner`, and the import of `_parse_first_json_object`/`_DEFAULT_MODEL`. Reduce `resolve_entities` to deterministic-only:

```python
def resolve_entities(store, client=None, *, max_adjudications: int = 200) -> dict:
    """Resolve duplicate entities (deterministic tier only; §9A).

    The LLM-adjudication tier is removed — spool merge_review handles it. Fuzzy
    candidate generation (_candidate_pairs) is preserved for prepare._merge_review_block.
    """
    auto = _deterministic_merges(store)
    return {"mode": "deterministic", "auto_merges": auto, "llm_merges": 0,
            "llm_calls": 0, "kept_distinct": 0}
```
Keep `_candidate_pairs`, `_tokens`, `_token_set_ratio`, `_STOPWORDS`, `_CANDIDATE_GATE`, `canonical_key`, `_deterministic_merges`.

In `tests/test_resolve.py` remove the `_adjudicate`-based tests + helper clients + the `_adjudicate` import; update `test_resolve_mode_reflects_client_presence` to expect `mode == "deterministic"` even when a client is passed.

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/test_resolve.py tests/test_daemon.py tests/test_backfill_singleflight.py -q` and `uv run ruff check mcpbrain/`
Expected: PASS.

- [ ] **Step 7: Commit**

`git commit -m "refactor(resolve): delete dead daemon resolve cadence + LLM adjudication tier (§9A)"`

---

### Task B2: Unify name slugs — `slugify` gains 80-char cap, delete `entity_slug`

**Files:**
- Modify: `mcpbrain/chunking.py`, `mcpbrain/graph_write.py`, `tests/test_chunking.py`

- [ ] **Step 1: Write the failing test**

```python
# Add to tests/test_chunking.py
def test_slugify_truncates_to_80_chars():
    from mcpbrain.chunking import slugify
    assert len(slugify("A" * 90)) <= 80

def test_slugify_and_entity_path_agree_on_accented_name(tmp_path):
    from mcpbrain.chunking import slugify
    from mcpbrain.store import Store
    from mcpbrain.graph_write import upsert_entity
    from mcpbrain.resolve import canonical_key
    assert slugify("Chané") == "chane"
    store = Store(tmp_path / "slug.sqlite3", dim=4); store.init()
    eid = upsert_entity(store, name="Chané", entity_type="person")
    assert eid == "chane" == canonical_key("Chané")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_chunking.py -k "slugify_truncates or agree_on_accented" -q`
Expected: both fail (`slugify` has no cap; `entity_slug("Chané")` → `"chan"`).

- [ ] **Step 3: Add 80-char cap to `slugify`; replace `entity_slug` with `slugify`**

In `mcpbrain/chunking.py`, change the final line of `slugify` from `return s.strip("-")` to `return s.strip("-")[:80]` (update the docstring to note the cap matches the former `entity_slug`).

In `mcpbrain/graph_write.py`: add `slugify` to the `from mcpbrain.chunking import (...)` block; delete `entity_slug`; replace its four call sites (owner `entity_id`, `org_id`, `candidate_id`, `eid`) with `slugify(...)`. Keep the `import re` (still used elsewhere).

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_chunking.py tests/test_resolve.py tests/test_store.py tests/test_graph_write.py -q` and `uv run ruff check mcpbrain/`
Expected: PASS.

- [ ] **Step 5: Commit**

`git commit -m "fix(graph_write): unify name slugs on slugify (80-char cap), delete entity_slug (§9A)"`

---

### Task B3: Cut projects/areas GTD tables, methods, proactive checks, context keys

**Files:**
- Modify: `mcpbrain/store.py`, `mcpbrain/proactive.py`, `mcpbrain/prompt.py`, `mcpbrain/prepare.py`, `mcpbrain/mcp_server.py`, `mcpbrain/graph_write.py`, `mcpbrain/enrich_prompt.md`, `mcpbrain/cowork/enrichment.md`
- Modify tests: `tests/test_proactive.py`, `tests/test_mcp_server.py`, `tests/test_store_schema.py`, `tests/test_prompt.py`, `tests/test_daemon_p3.py`

> **Keeps** the `project_id`/`area_id` columns on the `actions` table (existing data preserved); only the standalone `projects`/`areas` tables and their readers go.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_mcp_server.py (replace test_brain_context_includes_projects_areas)
def test_brain_context_profile_has_no_projects_areas(tmp_path):
    s = Store(tmp_path / "no_pa.sqlite3", dim=4); s.init()
    s.upsert_entity("sam", "Sam Chen", "person", org="Acme")
    tool = make_brain_context(s)
    out = asyncio.run(tool("sam"))
    assert "projects" not in out and "areas" not in out
    assert {"entity", "relations", "actions"} <= set(out)

# tests/test_prompt.py (replace the projects/areas block tests)
def test_read_projects_and_read_areas_removed():
    import mcpbrain.prompt as p
    assert not hasattr(p, "read_projects") and not hasattr(p, "read_areas")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_mcp_server.py::test_brain_context_profile_has_no_projects_areas tests/test_prompt.py::test_read_projects_and_read_areas_removed -q`
Expected: both fail.

- [ ] **Step 3: Remove DDL + store methods**

In `mcpbrain/store.py` remove the `projects` and `areas` CREATE TABLE blocks + indexes, and the methods `get_project`, `get_area`, `projects_for_org`, `areas_for_org`, `projects_owned_by`, `areas_owned_by`. Keep `add_unified_action`'s `project_id`/`area_id` params and the `actions` columns.

- [ ] **Step 4: Stub `proactive.py`**

Replace `mcpbrain/proactive.py` with:

```python
"""Proactive detection pass — GTD projects/areas detectors removed (§9E).

run() is kept as a no-op so daemon._run_periodic_passes() calls it unchanged.
"""


def run(store, *, now: str | None = None) -> dict:
    return {"project_no_next_action": 0, "area_overdue": 0}
```

- [ ] **Step 5: Remove `read_projects`/`read_areas` from `prompt.py`; `_read_projects`/`_read_areas` from `prepare.py`**

In `mcpbrain/prompt.py` delete `read_projects` and `read_areas` and their docstring references. In `mcpbrain/prepare.py` delete `_read_projects`/`_read_areas` and remove their two keys from `_build_context`'s returned dict, leaving:

```python
def _build_context(store, thread_ids) -> dict:
    home = str(config.app_dir())
    return {
        "owner_name": config.owner_full_name(home) or config.owner_name(home),
        "known_people": _build_known_people(store, batch_thread_ids=thread_ids),
        "org_domain_map": _org_domain_lines(),
        "valid_orgs": _valid_org_tags(),
    }
```

> **Note for D4:** D4 re-extends this `_build_context`; it must build on THIS shape (no `projects`/`areas`).

- [ ] **Step 6: Remove projects/areas from `mcp_server.py` and `graph_write.py`**

In `mcpbrain/mcp_server.py` `brain_context` profile mode, drop the `projects = store.projects_owned_by(...)` / `areas = store.areas_owned_by(...)` lines and the `"projects"`/`"areas"` keys in the return; update the docstring.

In `mcpbrain/graph_write.py` delete `_active_project_ids`, `_active_area_ids`, `_validate_action_targets`, and their calls in `apply_actions` (project_id/area_id pass through unvalidated).

In `mcpbrain/enrich_prompt.md` and `mcpbrain/cowork/enrichment.md`, remove the `projects`/`areas` context-block instructions.

- [ ] **Step 7: Update tests**

Replace `tests/test_proactive.py` with tests that `run()` returns zero counts and the GTD detectors are gone. In `tests/test_store_schema.py` replace `test_projects_and_areas_tables_exist` with a "tables removed" assertion. In `tests/test_daemon_p3.py` confirm the proactive mock return shape (`{"project_no_next_action": …, "area_overdue": …}`) is unchanged (no edit needed beyond verifying).

- [ ] **Step 8: Grep + run tests**

```bash
grep -rn --include="*.py" "read_projects\|read_areas\|projects_owned_by\|areas_owned_by\|get_project\|get_area\|projects_for_org\|areas_for_org\|_active_project_ids\|_active_area_ids\|_validate_action_targets\|detect_projects_without_next_action\|detect_areas_overdue_for_review" mcpbrain/ tests/
```
Expected: zero hits.

Run: `uv run pytest tests/test_proactive.py tests/test_store_schema.py tests/test_mcp_server.py tests/test_prompt.py tests/test_daemon_p3.py -q` and `uv run ruff check mcpbrain/`
Expected: PASS.

- [ ] **Step 9: Commit**

`git commit -m "feat(store): remove projects/areas GTD tables + proactive detectors; keep communities + brain_graph (§9E)"`

---

# Phase C — Cadence refactor + defaults ON + platform (§9D / §9F)

### Task C1: Cadence dispatch table

**Files:**
- Modify: `mcpbrain/daemon.py`
- Test: `tests/test_cadence_dispatch.py`

> **Depends on B1** (resolve cadence already deleted) and **B3** (`proactive.run` is now a no-op). The dispatch table excludes resolve.

- [ ] **Step 1: Write the failing test**

```python
"""Tests for the CadencePass dispatch table in _run_periodic_passes."""
import json
from mcpbrain.daemon import Daemon
from mcpbrain.lock import SingleWriterLock
from mcpbrain.store import Store


class _FakeEmbedder:
    dim = 4
    def embed(self, texts):
        import numpy as np
        return np.zeros((len(texts), self.dim), dtype="float32")


class _Clock:
    def __init__(self, t=0.0): self.t = t
    def __call__(self): return self.t


def _configured_daemon(tmp_path, **kw):
    (tmp_path / "config.json").write_text(json.dumps(
        {"owner_name": "A", "owner_email": "a@x.com", "orgs": [{"name": "O"}]}))
    import os; os.environ["MCPBRAIN_HOME"] = str(tmp_path)
    store = Store(tmp_path / "d.sqlite3", dim=4); store.init()
    clock = _Clock()
    d = Daemon(store, _FakeEmbedder(), services={},
               lock=SingleWriterLock(tmp_path / "d.lock"), clock=clock, **kw)
    return d, clock


def test_dispatch_table_pass_fires_when_due(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    d, clock = _configured_daemon(tmp_path, communities_interval_s=100.0)
    fired = []
    monkeypatch.setattr("mcpbrain.communities.run", lambda store: fired.append(1) or {"communities": 1})
    d._run_periodic_passes()
    assert len(fired) == 1


def test_dispatch_table_pass_skipped_when_not_due(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    d, clock = _configured_daemon(tmp_path, communities_interval_s=100.0)
    fired = []
    monkeypatch.setattr("mcpbrain.communities.run", lambda store: fired.append(1) or {"communities": 1})
    d._run_periodic_passes(); d._run_periodic_passes()
    assert len(fired) == 1


def test_dispatch_table_pass_refires_after_interval(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    d, clock = _configured_daemon(tmp_path, communities_interval_s=100.0)
    fired = []
    monkeypatch.setattr("mcpbrain.communities.run", lambda store: fired.append(1) or {"communities": 1})
    d._run_periodic_passes(); clock.t = 101.0; d._run_periodic_passes()
    assert len(fired) == 2


def test_dispatch_table_backfill_suppresses_all_graph_passes(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    d, clock = _configured_daemon(tmp_path, communities_interval_s=1.0)
    fired = []
    monkeypatch.setattr("mcpbrain.communities.run", lambda store: fired.append(1) or {})
    d._backfill_active.set()
    d._run_periodic_passes()
    assert fired == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_cadence_dispatch.py -q`
Expected: FAIL — no dispatch mechanism yet.

- [ ] **Step 3: Replace the 13 maybe_X bodies with a dispatch table**

In `mcpbrain/daemon.py`, near the top add:

```python
from dataclasses import dataclass

@dataclass
class CadencePass:
    name: str
    interval_attr: str
    last_attr: str
    fn_name: str
    needs_configured: bool = True
    needs_backfill_clear: bool = True
```

Near `_CADENCE_KEYS`, add the registry (communities before lint; auto_update/verify identity-independent):

```python
_CADENCE_PASSES: tuple[CadencePass, ...] = (
    CadencePass("auto_update", "_auto_update_interval_s", "_last_auto_update", "_run_auto_update", needs_configured=False, needs_backfill_clear=False),
    CadencePass("verify", "_verify_interval_s", "_last_verify", "_run_verify", needs_configured=False, needs_backfill_clear=False),
    CadencePass("communities", "_communities_interval_s", "_last_communities", "_run_communities"),
    CadencePass("lint", "_lint_interval_s", "_last_lint", "_run_lint"),
    CadencePass("synthesise", "_synthesise_interval_s", "_last_synthesise", "_run_synthesise"),
    CadencePass("proactive", "_proactive_interval_s", "_last_proactive", "_run_proactive"),
    CadencePass("waiting_on", "_waiting_on_interval_s", "_last_waiting_on", "_run_waiting_on"),
    CadencePass("blocks", "_blocks_interval_s", "_last_blocks", "_run_blocks"),
    CadencePass("audit", "_audit_interval_s", "_last_audit", "_run_audit"),
    CadencePass("clickup_sync", "_clickup_interval_s", "_last_clickup", "_run_clickup_sync"),
    CadencePass("stale_reextract", "_stale_reextract_interval_s", "_last_stale_reextract", "_run_stale_reextract"),
)
```

Replace `_run_periodic_passes`:

```python
def _run_periodic_passes(self) -> None:
    """Iterate _CADENCE_PASSES; each entry self-gates on its cadence."""
    if self._backfill_active.is_set():
        return
    configured = config.is_configured(str(app_dir()))
    for cp in _CADENCE_PASSES:
        if cp.needs_configured and not configured:
            continue
        try:
            getattr(self, cp.fn_name)()
        except Exception as exc:  # noqa: BLE001
            log.warning("periodic pass %s failed: %s", cp.name, exc, exc_info=True)
```

Add `_is_due` and convert each former `maybe_X` body into a `_run_X` that does the work, plus a thin `maybe_X` wrapper that guards `_backfill_active` and calls `_run_X` (tests call `maybe_X`). Example for two; replicate the pattern for communities, lint, synthesise, proactive, waiting_on, blocks, audit, stale_reextract:

```python
def _is_due(self, interval_attr: str, last_attr: str) -> bool:
    interval = getattr(self, interval_attr)
    if interval is None:
        return False
    last = getattr(self, last_attr)
    if last is None:
        return True
    return (self._clock() - last) >= interval

def _run_communities(self) -> dict | None:
    if not self._is_due("_communities_interval_s", "_last_communities"):
        return None
    now = self._clock()
    try:
        from mcpbrain.communities import run
        summary = run(self._store)
    except Exception as exc:  # noqa: BLE001
        log.warning("community detection failed: %s", exc, exc_info=True)
        return {"communities": False, "error": str(exc)}
    self._last_communities = now
    return summary

def maybe_communities(self) -> dict | None:
    if self._backfill_active.is_set():
        return None
    return self._run_communities()

def _run_auto_update(self) -> dict | None:
    return self.maybe_auto_update()

def _run_verify(self) -> dict | None:
    return self.maybe_verify_connections()
```

Preserve the existing per-pass implementation bodies (lint reads `now_iso`; blocks stashes `self._pending_blocks`; audit stashes `self._pending_audit`; synthesise stashes `self._pending_synthesis`; waiting_on passes `identity=owner_email`; clickup is rewritten in C3). Keep `maybe_backup`, `maybe_auto_update`, `maybe_verify_connections` as-is (they keep their existing shape; the dispatch table calls them via `_run_auto_update`/`_run_verify` and `maybe_backup` stays in the run loop).

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_cadence_dispatch.py tests/test_daemon_p3.py tests/test_daemon.py tests/test_backfill_singleflight.py -q` and `uv run ruff check mcpbrain/`
Expected: PASS.

- [ ] **Step 5: Commit**

`git commit -m "refactor(daemon): collapse 13 maybe_X bodies into CadencePass dispatch table (§9D)"`

---

### Task C2: Defaults ON for all intelligence cadences (authoritative `_cadences_from_config`)

**Files:**
- Modify: `mcpbrain/daemon.py`, `tests/test_cadence_defaults.py`, `tests/test_daemon_p3.py`

> This is the **single authoritative** rewrite of `_cadences_from_config`. (D1 only adds a regression test on top of this.)

- [ ] **Step 1: Write the failing test**

```python
import json
from mcpbrain.daemon import _cadences_from_config, _CADENCE_DEFAULTS

def test_empty_config_yields_defaults(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({}))
    r = _cadences_from_config(str(tmp_path))
    assert r["communities_interval_s"] == 86400.0
    assert r["blocks_interval_s"] == 86400.0
    assert r["proactive_interval_s"] == 86400.0
    assert r["waiting_on_interval_s"] == 86400.0
    assert r["lint_interval_s"] == 86400.0
    assert r["stale_reextract_interval_s"] == 86400.0
    assert r["synthesise_interval_s"] == 604800.0
    assert r["audit_interval_s"] == 604800.0
    assert r["verify_interval_s"] == 3600.0
    assert r["auto_update_interval_s"] == 86400.0
    assert "clickup_interval_s" not in r

def test_missing_config_file_yields_defaults(tmp_path):
    r = _cadences_from_config(str(tmp_path))
    assert r["communities_interval_s"] == 86400.0

def test_explicit_override_wins(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"cadences": {"communities_interval_s": 7200}}))
    r = _cadences_from_config(str(tmp_path))
    assert r["communities_interval_s"] == 7200.0
    assert r["proactive_interval_s"] == 86400.0

def test_explicit_zero_disables_pass(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"cadences": {"lint_interval_s": 0}}))
    assert _cadences_from_config(str(tmp_path))["lint_interval_s"] is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_cadence_defaults.py -q`
Expected: `ImportError: _CADENCE_DEFAULTS` + assertion failures.

- [ ] **Step 3: Add `_CADENCE_DEFAULTS`; rewrite `_cadences_from_config`; drop clickup knob**

Above `_CADENCE_KEYS`:

```python
_CADENCE_DEFAULTS: dict[str, float] = {
    "communities_interval_s":    86400.0,
    "blocks_interval_s":         86400.0,
    "proactive_interval_s":      86400.0,
    "waiting_on_interval_s":     86400.0,
    "lint_interval_s":           86400.0,
    "stale_reextract_interval_s":86400.0,
    "synthesise_interval_s":     604800.0,
    "audit_interval_s":          604800.0,
    "verify_interval_s":         3600.0,
    "auto_update_interval_s":    86400.0,
}
```

Remove `"clickup_interval_s"` from `_CADENCE_KEYS`. Rewrite:

```python
def _cadences_from_config(home) -> dict:
    """Read the cadences block. Absent keys use _CADENCE_DEFAULTS (so a fresh
    install is fully functional); an explicit entry overrides, and an explicit
    0/negative maps to None (OFF) as a power-user kill-switch. clickup is NOT
    here — it is gated on api_key+list_id (C3)."""
    cfg = config.read_config(home)
    cadences_block = cfg.get("cadences") or {}
    result = {}
    for key in _CADENCE_KEYS:
        if key not in cadences_block:
            result[key] = _CADENCE_DEFAULTS.get(key)
            continue
        raw = cadences_block[key]
        try:
            val = float(raw)
            if val <= 0:
                raise ValueError("must be positive")
            result[key] = val
        except (TypeError, ValueError) as exc:
            log.warning("cadences.%s invalid (%r); disabling: %s", key, raw, exc)
            result[key] = None
    return result
```

Remove `clickup_interval_s=cadences["clickup_interval_s"]` from the `Daemon(...)` call in `main()` and the clickup re-wire line in `apply_config`. Update `tests/test_daemon_p3.py`'s `test_cadences_from_config_absent_keys_map_to_none` → assert defaults instead; remove `"clickup_interval_s"` from any patched cadence dicts there.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_cadence_defaults.py tests/test_daemon_p3.py -q` and `uv run ruff check mcpbrain/`
Expected: PASS.

- [ ] **Step 5: Commit**

`git commit -m "feat(daemon): cadences default ON — fresh install runs all intelligence passes (§9D)"`

---

### Task C3: ClickUp gating — key+list driven, no cadence knob

**Files:**
- Modify: `mcpbrain/daemon.py`, `tests/test_clickup_gating.py`

- [ ] **Step 1: Write the failing test**

```python
import json
from unittest.mock import patch
from mcpbrain.daemon import Daemon
from mcpbrain.lock import SingleWriterLock
from mcpbrain.store import Store


class _FakeEmbedder:
    dim = 4
    def embed(self, texts):
        import numpy as np
        return np.zeros((len(texts), self.dim), dtype="float32")


class _Clock:
    def __init__(self, t=0.0): self.t = t
    def __call__(self): return self.t


def _daemon(tmp_path, extra=None):
    cfg = {"owner_name": "A", "owner_email": "a@x.com", "orgs": [{"name": "O"}]}
    if extra: cfg.update(extra)
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    import os; os.environ["MCPBRAIN_HOME"] = str(tmp_path)
    store = Store(tmp_path / "cu.sqlite3", dim=4); store.init()
    clock = _Clock()
    d = Daemon(store, _FakeEmbedder(), services={},
               lock=SingleWriterLock(tmp_path / "d.lock"), clock=clock)
    return d, clock


def test_clickup_inactive_without_key_or_list(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    d, _ = _daemon(tmp_path)
    assert d.maybe_clickup_sync() is None

def test_clickup_inactive_with_key_only(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    d, _ = _daemon(tmp_path, {"clickup_api_key": "pk_x"})
    assert d.maybe_clickup_sync() is None

def test_clickup_active_with_key_and_list(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    d, _ = _daemon(tmp_path, {"clickup_api_key": "pk_x", "clickup_list_id": "L1"})
    with patch("mcpbrain.clickup_sync.sync", return_value={"synced": 1}) as m:
        assert d.maybe_clickup_sync() == {"synced": 1}
    m.assert_called_once()

def test_clickup_respects_fixed_interval(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    d, clock = _daemon(tmp_path, {"clickup_api_key": "pk_x", "clickup_list_id": "L1"})
    with patch("mcpbrain.clickup_sync.sync", return_value={}):
        assert d.maybe_clickup_sync() == {}
    clock.t = 299.0
    with patch("mcpbrain.clickup_sync.sync", return_value={"x": 1}) as m2:
        assert d.maybe_clickup_sync() is None
    m2.assert_not_called()
    clock.t = 301.0
    with patch("mcpbrain.clickup_sync.sync", return_value={"x": 1}) as m3:
        assert d.maybe_clickup_sync() == {"x": 1}
    m3.assert_called_once()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_clickup_gating.py -q`
Expected: FAIL (current gating is on `_clickup_interval_s` from config).

- [ ] **Step 3: Rewrite `_run_clickup_sync` to gate on key+list**

```python
_CLICKUP_SYNC_INTERVAL_S: float = 300.0

def _run_clickup_sync(self) -> dict | None:
    """Run ClickUp sync if key+list are configured and the fixed interval elapsed.
    Presence of clickup_api_key AND clickup_list_id is the single on/off switch."""
    from mcpbrain import config as _cfg
    home = str(app_dir())
    if not (_cfg.clickup_api_key(home) and _cfg.clickup_list_id(home)):
        return None
    if self._clickup_interval_s is None:
        self._clickup_interval_s = _CLICKUP_SYNC_INTERVAL_S
    if not self._is_due("_clickup_interval_s", "_last_clickup"):
        return None
    now = self._clock()
    try:
        from mcpbrain import clickup_sync
        summary = clickup_sync.sync(self._store, home)
    except Exception as exc:  # noqa: BLE001
        log.warning("clickup sync failed: %s", exc, exc_info=True)
        return {"clickup": False, "error": str(exc)}
    self._last_clickup = now
    return summary

def maybe_clickup_sync(self) -> dict | None:
    if self._backfill_active.is_set():
        return None
    return self._run_clickup_sync()
```

In the constructor, drop the `clickup_interval_s` param and set `self._clickup_interval_s: float | None = None` directly (kept for cadence-clock bookkeeping). Confirm `apply_config`/`main()` no longer reference it (done in C2).

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_clickup_gating.py tests/test_daemon_p3.py -q` and `uv run ruff check mcpbrain/`
Expected: PASS.

- [ ] **Step 5: Commit**

`git commit -m "feat(daemon): gate ClickUp sync on api_key+list_id; drop clickup_interval_s knob (§9D)"`

---

### Task C4: Remove Linux/systemd platform code

**Files:**
- Modify: `mcpbrain/agents.py`, `tests/test_agents.py`, `tests/test_agents_cadence_xplat.py`, `tests/test_agents_cowork_xplat.py`
- Test: `tests/test_agents_no_linux.py`

- [ ] **Step 1: Write the failing test**

```python
import pytest
import mcpbrain.agents as agents

def test_systemd_symbols_gone():
    for n in ("systemd_unit", "systemd_tray_unit", "prune_timer_units",
              "health_timer_units", "gardener_timer_units", "meeting_packs_timer_units"):
        assert not hasattr(agents, n), f"{n} should be deleted"

def test_dispatchers_reject_linux():
    for fn in (lambda: agents.install_agent("linux", mcpbrain_bin="/x", home="/h"),
               lambda: agents.uninstall_agent("linux"),
               lambda: agents.restart_agent("linux"),
               lambda: agents.install_cadences("linux", mcpbrain_bin="/x", home="/h")):
        with pytest.raises(ValueError, match="[Uu]nsupported"):
            fn()

def test_darwin_and_win32_still_accepted(monkeypatch):
    calls = []
    monkeypatch.setattr(agents, "_install_cadences_launchd", lambda **k: calls.append("darwin"))
    monkeypatch.setattr(agents, "_install_cadences_schtasks", lambda **k: calls.append("win32"))
    agents.install_cadences("darwin", mcpbrain_bin="/x", home="/h")
    agents.install_cadences("win32", mcpbrain_bin="/x", home="/h")
    assert calls == ["darwin", "win32"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_agents_no_linux.py -q`
Expected: FAIL (symbols still present).

- [ ] **Step 3: Delete systemd code; update dispatchers**

In `mcpbrain/agents.py` delete: `_SYSTEMD_PATH`/`_TRAY_SYSTEMD_PATH` constants; `_systemd_unit`/`systemd_unit`/`systemd_tray_unit`; `_install_systemd`/`_uninstall_systemd`/`_restart_systemd` (+ tray variants); `_timer_units`/`prune_timer_units`/`health_timer_units`/`gardener_timer_units`/`meeting_packs_timer_units`; `_install_cadences_systemd`. In every dispatcher (`install_agent`, `uninstall_agent`, `restart_agent`, `install_tray_agent`, `uninstall_tray_agent`, `install_cadences`) remove the `linux` branch, leaving `darwin`/`win32`/`else: raise ValueError("Unsupported platform: …")`.

In `tests/test_agents.py` delete the systemd unit tests + their imports. In `tests/test_agents_cadence_xplat.py` and `tests/test_agents_cowork_xplat.py` delete the systemd-specific tests and the linux branch of the dispatch test.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_agents_no_linux.py tests/test_agents.py tests/test_agents_cadence_xplat.py tests/test_agents_cowork_xplat.py -q` and `uv run ruff check mcpbrain/`
Expected: PASS.

- [ ] **Step 5: Commit**

`git commit -m "feat(platform): remove Linux/systemd — macOS (launchd) + Windows (schtasks) only (§9F)"`

---

### Task C5: Fix Windows MCPBRAIN_HOME embedding in the task action

**Files:**
- Modify: `mcpbrain/agents.py`, `tests/test_agents.py`
- Test: `tests/test_schtasks_home_embed.py`

- [ ] **Step 1: Write the failing test**

```python
import mcpbrain.agents as agents

def test_schtasks_args_embeds_home_in_action():
    home = r"C:\Users\j\.mcpbrain"
    args = agents.schtasks_args(mcpbrain_bin=r"C:\Users\j\.local\bin\mcpbrain.exe", home=home)
    action = args[args.index("/tr") + 1]
    assert "MCPBRAIN_HOME" in action and home in action

def test_schtasks_args_home_with_spaces_quoted():
    home = r"C:\Users\Josh Kemp\.mcpbrain"
    args = agents.schtasks_args(mcpbrain_bin=r"C:\Users\Josh Kemp\.local\bin\mcpbrain.exe", home=home)
    action = args[args.index("/tr") + 1]
    assert "MCPBRAIN_HOME" in action and home in action

def test_schtasks_tray_args_also_embeds_home():
    home = r"C:\Users\j\.mcpbrain"
    args = agents.schtasks_tray_args(mcpbrain_bin=r"C:\Users\j\.local\bin\mcpbrain.exe", home=home)
    action = args[args.index("/tr") + 1]
    assert "MCPBRAIN_HOME" in action and home in action

def test_schtasks_args_subcommand_present():
    args = agents.schtasks_args(mcpbrain_bin=r"C:\mcpbrain.exe", home=r"C:\h")
    assert "daemon" in args[args.index("/tr") + 1]
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_schtasks_home_embed.py -q`
Expected: FAIL — `_schtasks_args` ignores `home`.

- [ ] **Step 3: Embed home in the task action**

```python
def _schtasks_args(*, task_name: str, subcommand: str, mcpbrain_bin: str, home: str) -> list[str]:
    """schtasks args registering an on-logon task whose action embeds MCPBRAIN_HOME
    so the daemon starts correctly even if the env var is cleared."""
    quoted_bin = f'"{mcpbrain_bin}"' if any(c.isspace() for c in mcpbrain_bin) else mcpbrain_bin
    quoted_home = f'"{home}"' if any(c.isspace() for c in home) else home
    action = f'cmd /c "set MCPBRAIN_HOME={quoted_home} && {quoted_bin} {subcommand}"'
    return ["schtasks", "/create", "/tn", task_name, "/sc", "onlogon", "/tr", action, "/f"]


def schtasks_args(*, mcpbrain_bin: str, home: str) -> list[str]:
    return _schtasks_args(task_name=_TASK_NAME, subcommand="daemon", mcpbrain_bin=mcpbrain_bin, home=home)


def schtasks_tray_args(*, mcpbrain_bin: str, home: str) -> list[str]:
    return _schtasks_args(task_name=_TRAY_TASK_NAME, subcommand="tray", mcpbrain_bin=mcpbrain_bin, home=home)
```

Update `_install_schtasks` to drop the `setx` call (home is embedded now). Update the existing `tests/test_agents.py` schtasks tests to pass the required `home=` argument.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_schtasks_home_embed.py tests/test_agents.py -q` and `uv run ruff check mcpbrain/`
Expected: PASS.

- [ ] **Step 5: Commit**

`git commit -m "fix(windows): embed MCPBRAIN_HOME in schtasks action; drop setx (§9F)"`

---

# Phase D — Utilise retained features + monitor (§9G / §8)

### Task D1: Regression guard — community detection runs on a fresh config

**Files:**
- Test: `tests/test_community_default_cadence.py`

> **No new implementation** — C2 added `_CADENCE_DEFAULTS["communities_interval_s"] = 86400.0`. This task adds a focused regression guard so a future change that drops the default is caught. The test PASSES immediately after C2 (it is a guard, not a red→green task).

- [ ] **Step 1: Write the guard test**

```python
import json
from mcpbrain.daemon import _cadences_from_config

def test_communities_runs_on_fresh_config(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({}))
    c = _cadences_from_config(str(tmp_path))
    assert c["communities_interval_s"] is not None and c["communities_interval_s"] > 0

def test_communities_explicit_zero_disables(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"cadences": {"communities_interval_s": 0}}))
    assert _cadences_from_config(str(tmp_path))["communities_interval_s"] is None
```

- [ ] **Step 2: Run to verify it passes (guard for C2)**

Run: `uv run pytest tests/test_community_default_cadence.py -q`
Expected: PASS (C2 implemented the default). If it FAILS, C2 was not completed — fix C2 before continuing.

- [ ] **Step 3: (no code change)**

- [ ] **Step 4: Confirm suite green**

Run: `uv run pytest tests/test_community_default_cadence.py tests/test_cadence_defaults.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

`git commit -m "test(cadence): guard that community detection defaults ON for fresh installs (§9G)"`

---

### Task D2: Add `brain_graph` + communities to project instructions

**Files:**
- Modify: `mcpbrain/daemon.py` (`_render_project_instructions`)
- Test: `tests/test_project_instructions.py`

- [ ] **Step 1: Write the failing test**

```python
from mcpbrain.daemon import _render_project_instructions

def test_instructions_mention_brain_graph():
    assert "brain_graph" in _render_project_instructions("Josh", ["Centrepoint"])

def test_instructions_mention_communities_mode():
    t = _render_project_instructions("Josh", ["Centrepoint"])
    assert "communities" in t.lower()

def test_instructions_include_examples():
    t = _render_project_instructions("Josh", [])
    assert "hops" in t or "connected" in t
    assert "community" in t.lower() or "circle" in t.lower()

def test_instructions_still_mention_existing_tools():
    t = _render_project_instructions("Alice", ["Acme"])
    for tool in ("brain_search", "brain_context", "brain_actions"):
        assert tool in t
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_project_instructions.py -q`
Expected: `brain_graph`/`communities` assertions fail.

- [ ] **Step 3: Expand `_render_project_instructions`**

Replace the tool-list section so it reads:

```python
    return f"""\
You're {name}'s assistant, working from here on. Memory + tools come from the mcpbrain MCP server:
- brain_search / brain_context / brain_actions — recall by meaning, profile a person/org, see what's open
- brain_graph — traverse the relationship graph: "how is X connected to Y?", "who are the key people around <org>?", "everyone within 2 hops of …" — use hops=2 for broader reach; at_time="YYYY-MM-DD" for time-travel
- brain_context(mode="communities") — list detected clusters/circles; brain_context(mode="communities", community_id=N) — who's in cluster N; use when asked "what are the main groups here?" or "which circle is X in?"
- brain_draft_reply / brain_draft_refine — draft email in my voice

Read my identity, voice, preferences, reference and decisions from the mcpbrain @-resources; apply my voice to everything. Run brain_search before answering from memory.

Keep my brain current as we work:
- A decision that changes how things are done -> brain_decision
- A "just decided / where we're up to" note -> brain_note
- A durable learning, preference, or fact worth keeping -> brain_memory_write
- When a system or project materially changes, propose an edit to the matching reference file and I'll approve it.

Captures are queued (the daemon writes them to my records repo within ~a minute; don't hand-edit those files). If something is clearly tied to one of my orgs{org_phrase} pass that org on a write; otherwise leave it — classifying people, orgs and relationships is automatic background enrichment, you don't tag anything.
"""
```
(Keep the existing `org_phrase = f" ({', '.join(orgs)})" if orgs else ""` line above the return.)

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_project_instructions.py -q` and `uv run ruff check mcpbrain/`
Expected: PASS.

- [ ] **Step 5: Commit**

`git commit -m "feat(instructions): advertise brain_graph + communities usage in project instructions (§9G)"`

---

### Task D3: Dashboard "Circles" element

**Files:**
- Modify: `mcpbrain/dashboard.py`, `mcpbrain/wizard/dashboard.html`
- Test: `tests/test_dashboard_circles.py`

- [ ] **Step 1: Write the failing test**

```python
import json
from pathlib import Path
from unittest import mock
from mcpbrain import dashboard
from mcpbrain.store import Store


def _store_with_communities(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4); s.init()
    with s._connect() as db:
        db.execute("INSERT INTO community_summaries(community_id, level, title, summary, member_count, key_entities, updated)"
                   " VALUES (1,0,'Leadership Team','Senior leaders.',5,'alice|bob','2026-06-01')")
        db.execute("INSERT INTO community_summaries(community_id, level, title, summary, member_count, key_entities, updated)"
                   " VALUES (2,0,'Tech Circle','Engineers.',3,'carol','2026-06-02')")
    return s

def test_circles_today_returns_list(tmp_path):
    assert len(dashboard.circles_today(_store_with_communities(tmp_path))) == 2

def test_circles_today_fields(tmp_path):
    c = dashboard.circles_today(_store_with_communities(tmp_path))[0]
    for f in ("community_id", "title", "summary", "member_count"):
        assert f in c

def test_circles_today_empty_store(tmp_path):
    s = Store(tmp_path / "e.sqlite3", dim=4); s.init()
    assert dashboard.circles_today(s) == []

def test_assemble_carries_circles(tmp_path):
    s = _store_with_communities(tmp_path)
    with mock.patch("mcpbrain.dashboard.calendar_today", return_value=[]), \
         mock.patch("mcpbrain.dashboard.clickup_today", return_value=[]):
        out = dashboard.assemble(s, str(tmp_path))
    assert "circles" in out and {c["title"] for c in out["circles"]} == {"Leadership Team", "Tech Circle"}

def test_assemble_circles_degrades(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4); s.init()
    with mock.patch("mcpbrain.dashboard.circles_today", side_effect=RuntimeError("x")), \
         mock.patch("mcpbrain.dashboard.calendar_today", return_value=[]), \
         mock.patch("mcpbrain.dashboard.clickup_today", return_value=[]):
        out = dashboard.assemble(s, str(tmp_path))
    assert out["circles"] == []

DASH = Path("mcpbrain/wizard/dashboard.html").read_text()
def test_html_has_circles_card(): assert 'id="card-circles"' in DASH
def test_html_has_circles_body(): assert 'id="circles-body"' in DASH
def test_html_renders_circles(): assert "renderCircles" in DASH and "data.circles" in DASH
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_dashboard_circles.py -q`
Expected: `AttributeError: circles_today` + HTML assertions fail.

- [ ] **Step 3: Add `circles_today`, wire into `assemble`, add the card**

In `mcpbrain/dashboard.py` add:

```python
def circles_today(store) -> list[dict]:
    """Detected community clusters (level=0) as a compact list. Degrades to []."""
    try:
        return [
            {"community_id": r["community_id"],
             "title": r.get("title") or f"Circle {r['community_id']}",
             "summary": r.get("summary") or "",
             "member_count": r.get("member_count") or 0,
             "key_entities": r.get("key_entities") or ""}
            for r in store.list_communities()
        ]
    except Exception as exc:  # noqa: BLE001
        log.warning("circles_today failed: %s", exc)
        return []
```

In `assemble`, submit `fut_circles = pool.submit(circles_today, store)`, resolve it with the same try/except degradation pattern as the other futures (default `[]`), and add `"circles": circles_result` to the returned dict.

In `mcpbrain/wizard/dashboard.html`, add a card after the Inbox card:

```html
    <div class="card" id="card-circles">
      <div class="card-head"><h2>Circles</h2><span id="circles-count" class="count-badge">0</span></div>
      <div id="circles-body"><p class="empty">Loading…</p></div>
    </div>
```

Add a `renderCircles(circles)` JS function (mirror `renderInbox`: set `#circles-count`, render each circle's title + member count + truncated summary into `#circles-body`, empty-state message when none), and call `renderCircles(data.circles || []);` in `refresh()`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_dashboard_circles.py -q` and `uv run ruff check mcpbrain/`
Expected: PASS.

- [ ] **Step 5: Commit**

`git commit -m "feat(dashboard): add Circles card surfacing community clusters (§9G)"`

---

### Task D4: Include community summaries in enrichment prompt context

**Files:**
- Modify: `mcpbrain/prepare.py`
- Test: `tests/test_prepare_community_context.py`

> **Targets the post-B3 `_build_context`** — no `projects`/`areas` keys, no `_read_projects`/`_read_areas` to monkeypatch.

- [ ] **Step 1: Write the failing test**

```python
from mcpbrain import prepare
from mcpbrain.store import Store


def _store_with_community(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4); s.init()
    s.upsert_entity("alice", "Alice Smith", "person")
    with s._connect() as db:
        db.execute("INSERT INTO community_summaries(community_id, level, title, summary, member_count, key_entities, updated)"
                   " VALUES (1,0,'Leadership','Senior leaders.',3,'alice','2026-06-01')")
        db.execute("INSERT INTO entity_communities(entity_id, community_id, level) VALUES ('alice',1,0)")
    return s


def test_build_context_has_community_summaries_key(tmp_path, monkeypatch):
    s = _store_with_community(tmp_path)
    monkeypatch.setattr(prepare, "_build_known_people", lambda store, **kw: [])
    monkeypatch.setattr(prepare, "_org_domain_lines", lambda: [])
    monkeypatch.setattr(prepare, "_valid_org_tags", lambda: [])
    assert "community_summaries" in prepare._build_context(s, ["t1"])


def test_build_context_includes_entity_community(tmp_path, monkeypatch):
    s = _store_with_community(tmp_path)
    monkeypatch.setattr(prepare, "_build_known_people", lambda store, **kw: [{"id": "alice", "name": "Alice Smith"}])
    monkeypatch.setattr(prepare, "_org_domain_lines", lambda: [])
    monkeypatch.setattr(prepare, "_valid_org_tags", lambda: [])
    ctx = prepare._build_context(s, ["t1"])
    assert any(c.get("title") == "Leadership" for c in ctx["community_summaries"])


def test_build_context_empty_when_no_communities(tmp_path, monkeypatch):
    s = Store(tmp_path / "b.sqlite3", dim=4); s.init()
    monkeypatch.setattr(prepare, "_build_known_people", lambda store, **kw: [])
    monkeypatch.setattr(prepare, "_org_domain_lines", lambda: [])
    monkeypatch.setattr(prepare, "_valid_org_tags", lambda: [])
    assert prepare._build_context(s, [])["community_summaries"] == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_prepare_community_context.py -q`
Expected: `community_summaries` key missing.

- [ ] **Step 3: Extend `_build_context` (post-B3 shape)**

```python
def _community_summaries_for_people(store, known_people: list) -> list[dict]:
    """Deduplicated community summaries for the communities the known-people
    entities belong to. Degrades to [] on any error."""
    if not known_people:
        return []
    try:
        entity_ids = [p["id"] for p in known_people if p.get("id")]
        if not entity_ids:
            return []
        memberships = store.communities_for(entity_ids)
        cids = {m["community_id"] for m in memberships}
        if not cids:
            return []
        return [s for s in store.list_communities() if s["community_id"] in cids]
    except Exception as exc:  # noqa: BLE001
        log.warning("_community_summaries_for_people failed: %s", exc)
        return []


def _build_context(store, thread_ids) -> dict:
    home = str(config.app_dir())
    known_people = _build_known_people(store, batch_thread_ids=thread_ids)
    return {
        "owner_name": config.owner_full_name(home) or config.owner_name(home),
        "known_people": known_people,
        "org_domain_map": _org_domain_lines(),
        "valid_orgs": _valid_org_tags(),
        "community_summaries": _community_summaries_for_people(store, known_people),
    }
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_prepare_community_context.py tests/test_prepare.py -q` and `uv run ruff check mcpbrain/`
Expected: PASS.

- [ ] **Step 5: Commit**

`git commit -m "feat(prepare): include community summaries for batch entities in enrichment context (§9G)"`

---

### Task D5: `mcpbrain monitor` CLI + `monitor.py`

**Files:**
- Create: `mcpbrain/monitor.py`
- Modify: `mcpbrain/cli.py`
- Test: `tests/test_monitor.py`

> E1 later removes `register` from `cli.py`; this task adds `monitor` (its rewrite still lists `register`).

- [ ] **Step 1: Write the failing test**

```python
import json, time, os
from datetime import datetime, timedelta, timezone
from mcpbrain.monitor import run_monitor


def _home(tmp_path, cfg=None):
    (tmp_path / "config.json").write_text(json.dumps(cfg or {})); return str(tmp_path)

def _hb(tmp_path, days=0):
    ts = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    (tmp_path / "mcp_heartbeat.json").write_text(json.dumps({"last_seen": ts}))

def _enrich(tmp_path, days=0):
    logs = tmp_path / "logs"; logs.mkdir(exist_ok=True)
    p = logs / "enrich.log"; p.write_text("[ts] drained\n")
    if days: os.utime(str(p), (time.time()-days*86400,)*2)

def test_healthy_returns_ok_zero(tmp_path):
    home = _home(tmp_path); _hb(tmp_path); _enrich(tmp_path)
    code, msg = run_monitor(home)
    assert code == 0 and "ok" in msg.lower()

def test_daemon_down_exits_1(tmp_path):
    home = _home(tmp_path); _enrich(tmp_path)
    code, _ = run_monitor(home); assert code == 1

def test_enrichment_idle_exits_1(tmp_path):
    home = _home(tmp_path); _hb(tmp_path)
    code, msg = run_monitor(home); assert code == 1 and "enrich" in msg.lower()

def test_sync_error_exits_1(tmp_path):
    home = _home(tmp_path); _hb(tmp_path); _enrich(tmp_path)
    logs = tmp_path / "logs"; (logs / "error.log").write_text("sync failed\n")
    code, msg = run_monitor(home); assert code == 1

def test_cli_monitor_registered(tmp_path, monkeypatch):
    import mcpbrain.monitor as mon
    called = {}
    monkeypatch.setattr(mon, "run_monitor", lambda home: (called.setdefault("home", home), (0, "ok"))[1])
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path)); (tmp_path / "config.json").write_text("{}")
    import pytest
    from mcpbrain import cli
    with pytest.raises(SystemExit):
        cli.main(["monitor"])
    assert "home" in called
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_monitor.py -q`
Expected: `ImportError` / dispatch `KeyError`.

- [ ] **Step 3: Create `mcpbrain/monitor.py`**

```python
"""mcpbrain monitor — reads local state only and reports daemon/enrichment health.

Exit 0 = healthy; exit 1 = one or more problems (daemon down, sync error,
enrichment idle, backup stale). Reuses probes.all_connections so the CLI and the
wizard never disagree.
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)
_FAIL = "needs_action"
_MONITORED = {
    "claude":     "Daemon down — MCP server not seen recently",
    "enrichment": "Enrichment idle — run the backfill skill in Cowork",
    "backup":     "Backup stale — snapshot is overdue",
}


def _has_recent_error_log(home: str) -> bool:
    p = Path(home) / "logs" / "error.log"
    try:
        return p.exists() and p.stat().st_size > 0
    except OSError:
        return False


def run_monitor(home: str) -> tuple[int, str]:
    from mcpbrain import probes
    try:
        conns = probes.all_connections(home, store=None)
    except Exception as exc:  # noqa: BLE001
        return 1, f"monitor: could not read probes: {exc}"
    problems: list[str] = []
    if _has_recent_error_log(home):
        problems.append("sync error — check logs/error.log")
    for key, message in _MONITORED.items():
        if conns.get(key, {}).get("state") == _FAIL:
            problems.append(message)
    return (1, "; ".join(problems)) if problems else (0, "ok")


def main(argv=None) -> None:
    import sys
    from mcpbrain import config
    code, msg = run_monitor(str(config.app_dir()))
    print(msg)
    sys.exit(code)
```

- [ ] **Step 4: Register the `monitor` subcommand in `mcpbrain/cli.py`**

Add `_monitor_main()` helper (`from mcpbrain.monitor import main as m; m()`), add `"monitor"` to the `sub.add_parser` name tuple, and add `"monitor": _monitor_main` to the dispatch dict. (Leave `register` in place — E1 removes it.)

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_monitor.py tests/test_cli.py -q` and `uv run ruff check mcpbrain/`
Expected: PASS.

- [ ] **Step 6: Commit**

`git commit -m "feat(monitor): add mcpbrain monitor CLI + monitor.py reusing probes (§8)"`

---

# Phase E — Remove old Claude-facing install + build plugin assets (§6 / §1–5 / §8)

### Task E1: Remove `mcpbrain register`

**Files:**
- Delete: `mcpbrain/wizard/register.py`, `tests/test_register.py`
- Modify: `mcpbrain/probes.py`, `mcpbrain/daemon.py`, `mcpbrain/control_api.py`, `mcpbrain/cli.py`, `mcpbrain/wizard/index.html`
- Modify tests: `tests/test_control_api_post.py`, `tests/test_probes.py`, `tests/test_wizard_serve.py`, `tests/test_cli.py`

- [ ] **Step 1: Adjust the tests that assert the removed behaviour**

In `tests/test_probes.py`: drop the `_claude_registered` monkeypatches; `probe_claude` now keys off the heartbeat alone. Replace the register-dependent tests with:

```python
def test_claude_not_started_when_no_heartbeat(tmp_path):
    r = probes.probe_claude(_home(tmp_path, {}))
    assert r["state"] == "not_started" and r["last_verified"] is None

def test_claude_ok_when_heartbeat_present(tmp_path):
    home = _home(tmp_path, {})
    (tmp_path / "mcp_heartbeat.json").write_text(
        json.dumps({"last_seen": datetime(2026, 6, 10, tzinfo=timezone.utc).isoformat()}))
    r = probes.probe_claude(home)
    assert r["state"] == "ok" and r["last_verified"].startswith("2026-06-10")
```
Remove `test_claude_not_registered` and `test_claude_registered_awaiting_restart`.

In `tests/test_control_api_post.py`: remove `FakeDaemon.register()`, the `/api/register` block in `test_post_endpoints`, `test_post_register_failure_returns_json_error`, `test_register_returns_path`.

In `tests/test_wizard_serve.py`: drop `"step-register"` from the `test_root_serves_wizard_with_token` loop.

In `tests/test_cli.py`: remove the `_register_main` monkeypatch, the `cli.main(["register"])` call, and `"register"` from the assertion set.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_register.py tests/test_probes.py tests/test_control_api_post.py tests/test_wizard_serve.py tests/test_cli.py -q`
Expected: failures (register.py still imported; `_claude_registered` still present; step-register still in HTML).

- [ ] **Step 3: Implement removals**

Delete `tests/test_register.py` and `mcpbrain/wizard/register.py`.

In `mcpbrain/probes.py`: remove `_claude_registered()` and its lazy `claude_desktop_config_path` import; rewrite `probe_claude` to key off the heartbeat only:

```python
def probe_claude(home) -> dict:
    """No heartbeat → not_started; heartbeat present and fresh → ok."""
    p = Path(home) / "mcp_heartbeat.json"
    if not p.exists():
        return _state("not_started", "Plugin not connected — install the mcpbrain plugin")
    try:
        last = json.loads(p.read_text()).get("last_seen")
        if last is None:
            raise ValueError("missing last_seen")
        last_dt = datetime.fromisoformat(last)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - last_dt > timedelta(days=_CLAUDE_STALE_DAYS):
            return _state("needs_action", "Not seen recently — open Claude Desktop", last_verified=last)
    except (OSError, ValueError):
        return _state("not_started", "Plugin not connected — install the mcpbrain plugin")
    return _state("ok", "Connected", last_verified=last)
```

In `mcpbrain/daemon.py`: delete the `register()` method. In `mcpbrain/control_api.py`: delete the `/api/register` route line. In `mcpbrain/cli.py`: delete `_register_main` and `"register"` from the subcommand tuple and dispatch dict. In `mcpbrain/wizard/index.html`: delete the `#step-register` card and the `reg()` JS function.

Grep check:
```bash
grep -rn "wizard.register\|_claude_registered\|/api/register\|_register_main\|step-register\| reg()" mcpbrain/ tests/
```
Expected: zero hits.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_probes.py tests/test_control_api_post.py tests/test_control_api_actions.py tests/test_wizard_serve.py tests/test_cli.py -q` then `uv run pytest -q`
Expected: full suite green.

- [ ] **Step 5: Commit**

`git commit -m "feat(plugin): remove mcpbrain register — plugin .mcp.json is the sole MCP registration (§6)"`

---

### Task E2: Remove the hook installer

**Files:**
- Delete: `mcpbrain/hooks.py`, `tests/test_hooks.py`
- Modify: `mcpbrain/probes.py`, `mcpbrain/control_api.py`, `mcpbrain/wizard/index.html`
- Modify tests: `tests/test_control_api_actions.py`, `tests/test_wizard_serve.py`, `tests/test_probes.py`

> **Keeps** `session_hooks.py` + the `session-start`/`session-end` CLI subcommands — the plugin's `hooks.json` invokes them. Only the settings.json-writing installer goes.

- [ ] **Step 1: Adjust tests**

In `tests/test_wizard_serve.py`: remove `assert 'id="step-hooks"' in WIZ` and `assert "/api/hooks/install" in WIZ` from `test_guided_elements_present`; in `test_connection_order_includes_new_cards` drop `'"memory-hooks"'`; in `test_step_badges_reflect_server_state_on_load` drop `'"memory-hooks"'` from the key loop.

In `tests/test_probes.py`: remove `test_memory_hooks_probe` and `test_all_connections_has_new_keys`; update the all-connections key test to expect `{"google","claude","clickup","backup","records","enrichment"}` (no `memory-hooks`).

In `tests/test_control_api_actions.py`: delete `test_hooks_install`; change `from mcpbrain import records, hooks` to `from mcpbrain import records`.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_probes.py tests/test_control_api_actions.py tests/test_wizard_serve.py -q`
Expected: failures.

- [ ] **Step 3: Implement removals**

Delete `tests/test_hooks.py` and `mcpbrain/hooks.py`.

In `mcpbrain/probes.py`: change `from mcpbrain import auth, config, hooks` to `from mcpbrain import auth, config`; delete `probe_memory_hooks`; remove `"memory-hooks": probe_memory_hooks(home),` from the `cheap` dict in `all_connections`.

In `mcpbrain/control_api.py`: delete the `/api/hooks/install` route block.

In `mcpbrain/wizard/index.html`: delete the `#step-hooks` card and the `installHooks()` JS function.

Grep check:
```bash
grep -rn "from mcpbrain import.*hooks\b\|mcpbrain.hooks\|probe_memory_hooks\|install_session_hooks\|/api/hooks/install\|step-hooks\|installHooks" mcpbrain/ tests/
```
Expected: zero hits (matches for `session_hooks` are fine — that module stays).

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_probes.py tests/test_control_api_actions.py tests/test_wizard_serve.py tests/test_session_hooks.py -q` then `uv run pytest -q`
Expected: full suite green (`session_hooks` tests still pass).

- [ ] **Step 5: Commit**

`git commit -m "feat(plugin): remove hook installer — plugin hooks.json replaces settings.json writer (§6)"`

---

### Task E3: Delete the curl|sh installers

**Files:**
- Delete: `install/setup.sh`, `install/setup.command`, `install/setup.ps1`
- Test: `tests/test_install_scripts_removed.py`

- [ ] **Step 1: Write the failing test**

```python
from pathlib import Path
_INSTALL = Path(__file__).parent.parent / "install"

def test_setup_sh_deleted():       assert not (_INSTALL / "setup.sh").exists()
def test_setup_command_deleted():  assert not (_INSTALL / "setup.command").exists()
def test_setup_ps1_deleted():      assert not (_INSTALL / "setup.ps1").exists()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_install_scripts_removed.py -q`
Expected: 3 failures.

- [ ] **Step 3: Delete the scripts**

Delete `install/setup.sh`, `install/setup.command`, `install/setup.ps1`. Grep for stray references:
```bash
grep -rn "setup\.sh\|setup\.command\|setup\.ps1" mcpbrain/ tests/ --include="*.py" --include="*.html"
```
Expected: zero hits.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_install_scripts_removed.py -q` then `uv run pytest -q`
Expected: PASS / full suite green.

- [ ] **Step 5: Commit**

`git commit -m "feat(plugin): delete curl|sh installers — the install skill is the only path (§6)"`

---

### Task E4: Create plugin manifests

**Files:**
- Create: `plugin/.claude-plugin/plugin.json`, `plugin/.claude-plugin/marketplace.json`, `plugin/.mcp.json`
- Test: `tests/test_plugin_manifest.py`

- [ ] **Step 1: Write the failing test**

```python
import json
from pathlib import Path
_PLUGIN = Path(__file__).parent.parent / "plugin"

def test_plugin_json_valid():
    d = json.loads((_PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    assert d.get("name") == "mcpbrain"
    assert d.get("version") and d.get("description") and d.get("author")

def test_marketplace_lists_plugin():
    d = json.loads((_PLUGIN / ".claude-plugin" / "marketplace.json").read_text())
    assert "mcpbrain" in [p.get("name") for p in d["plugins"]]

def test_mcp_json_points_at_shim():
    d = json.loads((_PLUGIN / ".mcp.json").read_text())
    cmd = d["mcpServers"]["mcpbrain"]["command"]
    assert "${CLAUDE_PLUGIN_ROOT}/bin/mcpbrain-mcp" in cmd
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_plugin_manifest.py -q`
Expected: failures — `plugin/` does not exist.

- [ ] **Step 3: Create the manifests**

`plugin/.claude-plugin/plugin.json`:
```json
{
  "name": "mcpbrain",
  "version": "0.5.0",
  "description": "Personal AI brain for Centrepoint — syncs Gmail and Drive, surfaces context, actions, and memory inside Cowork.",
  "author": "Centrepoint",
  "homepage": "https://github.com/centrepoint/mcpbrain-plugin"
}
```

`plugin/.claude-plugin/marketplace.json`:
```json
{
  "plugins": [
    {
      "name": "mcpbrain",
      "version": "0.5.0",
      "description": "Personal AI brain for Centrepoint — syncs Gmail and Drive, surfaces context, actions, and memory inside Cowork.",
      "author": "Centrepoint",
      "path": "."
    }
  ]
}
```

`plugin/.mcp.json`:
```json
{
  "mcpServers": {
    "mcpbrain": {
      "command": "${CLAUDE_PLUGIN_ROOT}/bin/mcpbrain-mcp",
      "env": { "MCPBRAIN_HOME": "" }
    }
  }
}
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_plugin_manifest.py -q` then `uv run pytest -q`
Expected: PASS / full suite green.

- [ ] **Step 5: Commit**

`git commit -m "feat(plugin): create plugin.json, marketplace.json, .mcp.json (§1–2)"`

---

### Task E5: Create the PATH-proof shims

**Files:**
- Create: `plugin/bin/mcpbrain-mcp`, `plugin/bin/mcpbrain-monitor`
- Modify: `tests/test_plugin_manifest.py`

- [ ] **Step 1: Write the failing test**

```python
import subprocess, os, stat

def test_shims_executable():
    for name in ("mcpbrain-mcp", "mcpbrain-monitor"):
        shim = _PLUGIN / "bin" / name
        assert shim.exists() and (stat.S_IMODE(shim.stat().st_mode) & 0o111)

def test_mcp_shim_execs_mcp_server(tmp_path):
    fake = tmp_path / "mcpbrain"; fake.write_text('#!/bin/sh\necho "ARGS:$*"\n'); fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ.get('PATH','')}",
           "MCPBRAIN_HOME": str(tmp_path / ".mcpbrain")}
    r = subprocess.run(["/bin/sh", str(_PLUGIN / "bin" / "mcpbrain-mcp")], env=env,
                       capture_output=True, text=True, timeout=5)
    assert "mcp-server" in r.stdout

def test_monitor_shim_execs_monitor(tmp_path):
    fake = tmp_path / "mcpbrain"; fake.write_text('#!/bin/sh\necho "ARGS:$*"\n'); fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ.get('PATH','')}",
           "MCPBRAIN_HOME": str(tmp_path / ".mcpbrain")}
    r = subprocess.run(["/bin/sh", str(_PLUGIN / "bin" / "mcpbrain-monitor")], env=env,
                       capture_output=True, text=True, timeout=5)
    assert "monitor" in r.stdout

def test_mcp_shim_errors_when_binary_absent(tmp_path):
    env = {**os.environ, "PATH": str(tmp_path), "HOME": str(tmp_path),
           "MCPBRAIN_HOME": str(tmp_path / ".mcpbrain")}
    r = subprocess.run(["/bin/sh", str(_PLUGIN / "bin" / "mcpbrain-mcp")], env=env,
                       capture_output=True, text=True, timeout=5)
    assert r.returncode != 0 and "mcpbrain" in (r.stderr + r.stdout).lower()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_plugin_manifest.py -k shim -q`
Expected: failures — shims absent.

- [ ] **Step 3: Create the shims**

`plugin/bin/mcpbrain-mcp`:
```sh
#!/bin/sh
# PATH-proof shim: locate the installed mcpbrain daemon binary, set MCPBRAIN_HOME,
# and exec `mcpbrain mcp-server`. Resolution: ~/.local/bin → uv tool dir → PATH.
set -eu
_find() {
  if [ -x "$HOME/.local/bin/mcpbrain" ]; then echo "$HOME/.local/bin/mcpbrain"; return; fi
  if command -v uv >/dev/null 2>&1; then
    b="$(uv tool dir 2>/dev/null)/bin/mcpbrain"; [ -x "$b" ] && { echo "$b"; return; }
  fi
  command -v mcpbrain 2>/dev/null && return
  return 1
}
BIN="$(_find)" || { echo "mcpbrain: binary not found. Run the install skill in Cowork." >&2; exit 1; }
export MCPBRAIN_HOME="${MCPBRAIN_HOME:-$HOME/.mcpbrain}"
exec "$BIN" mcp-server "$@"
```

`plugin/bin/mcpbrain-monitor` (same resolver, execs `monitor`):
```sh
#!/bin/sh
# PATH-proof shim for monitors/monitors.json: exec `mcpbrain monitor`.
set -eu
_find() {
  if [ -x "$HOME/.local/bin/mcpbrain" ]; then echo "$HOME/.local/bin/mcpbrain"; return; fi
  if command -v uv >/dev/null 2>&1; then
    b="$(uv tool dir 2>/dev/null)/bin/mcpbrain"; [ -x "$b" ] && { echo "$b"; return; }
  fi
  command -v mcpbrain 2>/dev/null && return
  return 1
}
BIN="$(_find)" || { echo "mcpbrain: binary not found. Run the install skill in Cowork." >&2; exit 1; }
export MCPBRAIN_HOME="${MCPBRAIN_HOME:-$HOME/.mcpbrain}"
exec "$BIN" monitor "$@"
```

Then: `chmod +x plugin/bin/mcpbrain-mcp plugin/bin/mcpbrain-monitor`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_plugin_manifest.py -q` then `uv run pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

`git commit -m "feat(plugin): add PATH-proof mcpbrain-mcp and mcpbrain-monitor shims (§2, §8)"`

---

### Task E6: Create the install skill

**Files:**
- Create: `plugin/skills/install/SKILL.md`
- Test: `tests/test_plugin_assets.py`

- [ ] **Step 1: Write the failing test**

```python
from pathlib import Path
import re
_PLUGIN = Path(__file__).parent.parent / "plugin"
def _read(rel): return (_PLUGIN / rel).read_text()

def test_install_skill_exists():
    assert (_PLUGIN / "skills" / "install" / "SKILL.md").exists()

def test_install_skill_bootstrap_steps():
    b = _read("skills/install/SKILL.md")
    assert "uv tool install" in b and "--python 3.12" in b
    assert "mcpbrain setup" in b and "/reload-plugins" in b

def test_install_skill_vm_sandbox_fallback():
    b = _read("skills/install/SKILL.md")
    assert "Claude Code" in b and ("~/.local" in b or "sandbox" in b.lower())

def test_install_skill_os_detection():
    b = _read("skills/install/SKILL.md")
    assert "launchd" in b.lower() and ("task scheduler" in b.lower() or "schtasks" in b.lower())

def test_install_skill_description_no_angle_brackets():
    b = _read("skills/install/SKILL.md")
    m = re.match(r'^---\n(.*?)\n---', b, re.DOTALL)
    assert m, "must have YAML frontmatter"
    for line in m.group(1).splitlines():
        if line.strip().startswith("description"):
            assert "<" not in line and ">" not in line
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_plugin_assets.py -k install_skill -q`
Expected: failures — file absent.

- [ ] **Step 3: Create `plugin/skills/install/SKILL.md`**

```markdown
---
name: mcpbrain-install
description: Bootstrap the mcpbrain daemon onto this machine. Installs uv, installs the mcpbrain daemon from the Centrepoint wheel index, registers the launchd (macOS) or Task Scheduler (Windows) background agent, and opens the setup wizard for Google sign-in. Idempotent — safe to run again.
---

# Install mcpbrain

Run this once in Cowork. If Cowork is running in full VM-sandbox mode (it cannot write to your home directory), run this skill in Claude Code instead, then return to Cowork.

## Steps

### 0. Check host access
```bash
touch ~/.local/.mcpbrain_probe 2>/dev/null && rm ~/.local/.mcpbrain_probe && echo HOST_OK || echo SANDBOX
```
If `SANDBOX`: stop and tell the user to run this skill in Claude Code, then return to Cowork. If `HOST_OK`: continue.

### 1. Install uv (if missing)
```bash
command -v uv || curl -LsSf https://astral.sh/uv/install.sh | sh
[ -f "$HOME/.local/bin/uv" ] && export PATH="$HOME/.local/bin:$PATH"
```

### 2. Install the mcpbrain daemon
```bash
uv tool install --python 3.12 --index "mcpbrain=https://centrepoint.github.io/mcpbrain-dist/simple/" mcpbrain --force
export PATH="$HOME/.local/bin:$PATH"
```

### 3. Register the background agent + open the setup wizard
`mcpbrain setup` registers the right background agent for your OS — **launchd** on macOS, **Task Scheduler** on Windows — installs the periodic cadences, starts the daemon, and opens the setup wizard in a browser tab:
```bash
mcpbrain setup
```
Complete Google sign-in, identity, and timezone in the wizard. (You do not run a separate scheduler command — `mcpbrain setup` detects the OS and does the right thing.)

### 4. Reload plugins
Run `/reload-plugins` in Cowork so the mcpbrain MCP server connects.

## Idempotency
Each step is safe to re-run: `uv tool install` is a no-op at the same version, agent registration is idempotent, and the wizard skips already-filled fields.
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_plugin_assets.py -k install_skill -q` then `uv run pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

`git commit -m "feat(plugin): add install skill — terminal-free OS-aware bootstrap (§3)"`

---

### Task E7: Create the backfill skill + enrich-batch agent

**Files:**
- Create: `plugin/skills/backfill/SKILL.md`, `plugin/agents/enrich-batch.md`
- Modify: `tests/test_plugin_assets.py`

- [ ] **Step 1: Write the failing test**

```python
def test_backfill_skill_exists():
    assert (_PLUGIN / "skills" / "backfill" / "SKILL.md").exists()

def test_enrich_batch_agent_exists():
    assert (_PLUGIN / "agents" / "enrich-batch.md").exists()

def test_enrich_batch_embeds_rules():
    b = _read("agents/enrich-batch.md")
    for token in ("enrich_queue/pending.json", "enrich_inbox", "batch_id", "content_type", "merge_review"):
        assert token in b

def test_backfill_skill_orchestrates_loop():
    b = _read("skills/backfill/SKILL.md")
    assert "enrich-batch" in b
    assert any(w in b.lower() for w in ("loop", "while", "repeat"))
    assert "pending.json" in b or "spool" in b.lower()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_plugin_assets.py -k "backfill or enrich_batch" -q`
Expected: failures.

- [ ] **Step 3: Create the assets**

Create `plugin/agents/enrich-batch.md` — embed the rules from `mcpbrain/cowork/enrichment.md` verbatim (the extraction schema, the field notes, merge-review and synthesis rules), framed as a fresh-context single-batch agent: read `~/.mcpbrain/enrich_queue/pending.json`, write `~/.mcpbrain/enrich_inbox/<batch_id>.json`, return a one-line status (`DONE: batch <id> — N threads…`, `DONE: spool empty`, or `ERROR: …`). It must mention `enrich_queue/pending.json`, `enrich_inbox`, `batch_id`, `content_type`, and `merge_review` (the test asserts these).

> **Build note:** copy the current body of `mcpbrain/cowork/enrichment.md` into the agent file so the rules stay in sync at ship time; do not paraphrase the schema.

Create `plugin/skills/backfill/SKILL.md`:
```markdown
---
name: mcpbrain-backfill
description: Enrich your email history using Cowork subagents — processes the spool in batches with a fresh context per batch, so very large histories do not hit context limits. Loops until the spool is dry. Each batch is one Cowork subagent call (subscription usage, not pay-per-token).
---

# Backfill enrichment

Processes all pending email threads in the mcpbrain spool, one batch at a time, each in a fresh-context `enrich-batch` subagent so large histories never hit context limits.

## How it works
1. Check the spool: read `~/.mcpbrain/enrich_queue/pending.json`.
2. If non-empty: dispatch the `enrich-batch` subagent; wait for its status line.
3. Wait ~60s for the daemon to drain the result and prepare the next batch (it writes `enrich_inbox/<batch_id>.json`, applies it, stamps `logs/enrich.log`, prepares the next `pending.json`).
4. Repeat until `pending.json` is absent/empty or the subagent returns `DONE: spool empty`.
5. After 3 consecutive empty checks, stop and report total progress.

## Loop
```
WHILE spool not dry AND empty_checks < 3:
  result = run_subagent("enrich-batch")
  IF "spool empty" in result OR pending.json absent: empty_checks += 1
  ELSE: empty_checks = 0; record status
  WAIT ~60s for the daemon to drain + prepare the next batch
REPORT: batches processed, final spool state, last drain log line
```

## Checks
```bash
[ -s ~/.mcpbrain/enrich_queue/pending.json ] && echo PENDING || echo EMPTY
tail -5 ~/.mcpbrain/logs/enrich.log 2>/dev/null || echo "(no drain log yet)"
```

Stop early on `/stop` or three consecutive `ERROR:` lines.
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_plugin_assets.py -q` then `uv run pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

`git commit -m "feat(plugin): add backfill skill + enrich-batch subagent (§4)"`

---

### Task E8: Create plugin hooks.json

**Files:**
- Create: `plugin/hooks/hooks.json`
- Modify: `tests/test_plugin_manifest.py`

- [ ] **Step 1: Write the failing test**

```python
def test_hooks_json_declares_both_events():
    d = json.loads((_PLUGIN / "hooks" / "hooks.json").read_text())
    assert {"SessionStart", "SessionEnd"} <= set(d["hooks"].keys())

def test_hooks_commands_reference_mcpbrain():
    d = json.loads((_PLUGIN / "hooks" / "hooks.json").read_text())
    def cmds(ev):
        return [h.get("command","") for blk in d["hooks"].get(ev, [])
                for h in blk.get("hooks", [])]
    assert any("session-start" in c for c in cmds("SessionStart"))
    assert any("session-end" in c for c in cmds("SessionEnd"))
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_plugin_manifest.py -k hooks -q`
Expected: failures.

- [ ] **Step 3: Create `plugin/hooks/hooks.json`**

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [ { "type": "command", "command": "mcpbrain session-start" } ] }
    ],
    "SessionEnd": [
      { "hooks": [ { "type": "command", "command": "mcpbrain session-end" } ] }
    ]
  }
}
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_plugin_manifest.py -q` then `uv run pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

`git commit -m "feat(plugin): add hooks.json (SessionStart/SessionEnd → mcpbrain session hooks) (§5)"`

---

### Task E9: Create monitors.json

**Files:**
- Create: `plugin/monitors/monitors.json`
- Modify: `tests/test_plugin_manifest.py`

- [ ] **Step 1: Write the failing test**

```python
def test_monitors_json_points_at_shim():
    d = json.loads((_PLUGIN / "monitors" / "monitors.json").read_text())
    assert len(d["monitors"]) >= 1
    assert "${CLAUDE_PLUGIN_ROOT}/bin/mcpbrain-monitor" in d["monitors"][0]["command"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_plugin_manifest.py -k monitors -q`
Expected: failure.

- [ ] **Step 3: Create `plugin/monitors/monitors.json`**

```json
{
  "monitors": [
    {
      "name": "mcpbrain-health",
      "command": "${CLAUDE_PLUGIN_ROOT}/bin/mcpbrain-monitor",
      "description": "Daemon and enrichment health"
    }
  ]
}
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_plugin_manifest.py -q` then `uv run pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

`git commit -m "feat(plugin): add monitors.json pointing at mcpbrain-monitor shim (§8)"`

---

# Phase F — Maintainer infra migration (§7) — NON-CODE CHECKLIST

> Not TDD. A maintainer (Josh) performs these once. No daemon-repo commits; the only code touch is bumping the wheel-index/marketplace URLs (already written into the install skill in E6 — verify they match the real Centrepoint URLs before publishing).

- [ ] **F1.** Create a Centrepoint GitHub org (if not present). Owner: Josh.
- [ ] **F2.** Create public repo `centrepoint/mcpbrain-dist` and publish the PEP 503 wheel index there (move `bin/release.py`'s publish target + GitHub Pages). Verify `https://centrepoint.github.io/mcpbrain-dist/simple/` serves the `mcpbrain` wheel.
- [ ] **F3.** Create public repo `centrepoint/mcpbrain-plugin`; publish the contents of the in-repo `plugin/` dir there (the release step copies `plugin/` → repo root). Confirm `.claude-plugin/marketplace.json` resolves.
- [ ] **F4.** Create a Centrepoint Google Cloud project; configure the OAuth consent screen; create a **desktop** OAuth client. Bundle the new client ID/secret in the daemon wheel (replace the personal `itsjoshuakemp` client). Add pilot members as OAuth test users in the Console.
- [ ] **F5.** Verify the install skill's URLs (`mcpbrain-dist` index) and `plugin.json`/`marketplace.json` `homepage` point at the Centrepoint org. Update if needed and re-publish.
- [ ] **F6.** In the org's Claude Team admin, add the `mcpbrain-plugin` marketplace and set the plugin to **available** (members click Install). Pilot = members who already have Claude.
- [ ] **F7.** Archive the personal `itsjoshuakemp` repos once the pilot is on the new infra.

---

# Final verification

### Task V1: Full suite, lint, and clean-machine validation

- [ ] **Step 1: Full test suite + lint**

Run: `uv run pytest -q` and `uv run ruff check mcpbrain/`
Expected: all green; no ruff findings.

- [ ] **Step 2: Build + dead-import sweep**

```bash
rm -rf build *.egg-info
uv build
grep -rn --include="*.py" "import skills\|wizard.register\|embed_voyage\|sync.cursors\|run_enrichment\|_claude_registered\|update_task_status" mcpbrain/
```
Expected: clean build; zero grep hits.

- [ ] **Step 3: Plugin asset sanity**

Run: `uv run pytest tests/test_plugin_manifest.py tests/test_plugin_assets.py -q`
Expected: PASS. Manually confirm `plugin/bin/*` are executable and the SKILL.md descriptions contain no angle brackets.

- [ ] **Step 4: Verify §-coverage of the spec**

Re-read `docs/superpowers/specs/2026-06-11-plugin-distribution-design.md` and tick each section against a task: §1–2 (E4–E5), §3 (E6), §4 (E7), §5 (E8), §6 (E1–E3), §7 (Phase F), §8 (D5, E5, E9), §9A (B1–B2), §9B (A2–A5), §9C (A6–A8), §9D (C1–C3), §9E (B3), §9F (C4–C5), §9G (D1–D4).

- [ ] **Step 5: Clean-machine validation (runbook)**

Per the spec's "out of scope / verify at build": (a) macOS clean install via the install skill end-to-end; (b) **Windows** clean install via the install skill (Task Scheduler path, MCPBRAIN_HOME embedded); (c) confirm the plugin's local stdio MCP connects in a Cowork session after `/reload-plugins`; (d) confirm the monitor surfaces a failure in Cowork. Record results in `docs/RELEASE-RUNBOOK.md`.

- [ ] **Step 6: Finalise**

Use **superpowers:finishing-a-development-branch** to merge/PR the `plugin-distribution` branch.

