# Stale-Action Auto-Close + ClickUp Reopen — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close actions that are no longer needed by giving the existing LLM closer another at-bat on stuck threads (never by keyword), and make a ClickUp reopen propagate back into the brain regardless of who closed the action.

**Architecture:** Two independent parts. **Part 1 (Gap A):** a daily daemon sweep flips already-`enriched=1` threads back to `enriched=0` when an open action in them looks stale and the thread is otherwise idle; the normal enrichment cycle then re-extracts with `open_actions` context and the existing `resolved_action_ids` path makes any close. A per-thread content-signature guards against re-triggering loops. **Part 2 (reopen):** persist the last-synced ClickUp closed-state on each action (`actions.clickup_closed`) and key inbound reopen off the closed→open transition instead of `resolved_by=="clickup"`.

**Tech Stack:** Python 3, SQLite (`mcpbrain/store.py`), pytest. Follows the existing `maybe_*` cadence pattern in `mcpbrain/daemon.py` and the fake-client/real-Store test pattern in `tests/test_clickup_sync.py`.

**Spec:** `docs/superpowers/specs/2026-06-09-stale-action-autoclose-design.md`

---

## File Structure

- `mcpbrain/store.py` (modify) — schema migration for the `stale_reextract` table and the `clickup_closed` column; new helper methods.
- `mcpbrain/stale_reextract.py` (create) — the pure `sweep()` candidate-selection + trigger logic (no daemon import; unit-testable with a real Store).
- `mcpbrain/daemon.py` (modify) — wire `maybe_stale_reextract` into the cadence machinery.
- `mcpbrain/clickup_sync.py` (modify) — reopen-by-transition rule + `clickup_closed` maintenance.
- `tests/test_store.py` (modify) — store-method tests. (If a more specific store test module is conventional, co-locate there; `tests/test_store.py` is the default.)
- `tests/test_stale_reextract.py` (create) — sweep tests.
- `tests/test_daemon_p3.py` (modify) — cadence test + reload-rewire dict update.
- `tests/test_clickup_sync.py` (modify) — reopen tests.

---

## Part 1 — Gap A: stale flag as a re-extraction trigger

### Task 1: Store — `stale_reextract` table + get/set

**Files:**
- Modify: `mcpbrain/store.py` (migration block near line 542, after the `meeting_packs` table create; new methods near the other action/meta helpers ~line 1241)
- Test: `tests/test_store.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_store.py`:

```python
def test_stale_reextract_roundtrip(tmp_path):
    from mcpbrain.store import Store
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    assert s.get_stale_reextract("thread-A") is None
    s.set_stale_reextract("thread-A", "sig123", "2026-06-09T00:00:00Z")
    row = s.get_stale_reextract("thread-A")
    assert row["thread_id"] == "thread-A"
    assert row["signature"] == "sig123"
    assert row["triggered_at"] == "2026-06-09T00:00:00Z"
    # upsert replaces in place
    s.set_stale_reextract("thread-A", "sig456", "2026-06-09T01:00:00Z")
    assert s.get_stale_reextract("thread-A")["signature"] == "sig456"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_store.py::test_stale_reextract_roundtrip -v`
Expected: FAIL with `AttributeError: 'Store' object has no attribute 'get_stale_reextract'`

- [ ] **Step 3: Add the table to the migration in `init()`**

In `mcpbrain/store.py`, inside `init()` immediately after the `meeting_packs` `CREATE TABLE IF NOT EXISTS` block (around line 543-545), add:

```python
            # --- stale_reextract (Gap A re-extraction trigger, 2026-06-09) ----
            # Records that a thread was reset to enriched=0 for a fresh LLM
            # at-bat, keyed by a content signature so the same unchanged thread
            # is never re-triggered (would re-pay the re-extraction token cost).
            db.execute("""CREATE TABLE IF NOT EXISTS stale_reextract(
                thread_id    TEXT PRIMARY KEY,
                signature    TEXT NOT NULL,
                triggered_at TEXT NOT NULL
            )""")
```

- [ ] **Step 4: Add the get/set methods**

In `mcpbrain/store.py`, near the other small helpers (e.g. after `action_by_clickup_id`, ~line 1248), add:

```python
    def get_stale_reextract(self, thread_id: str) -> dict | None:
        """Return the stale-reextract marker row for a thread, or None."""
        with self._connect() as db:
            r = db.execute(
                "SELECT thread_id, signature, triggered_at "
                "FROM stale_reextract WHERE thread_id=?",
                (thread_id,)).fetchone()
            return dict(r) if r else None

    def set_stale_reextract(self, thread_id: str, signature: str,
                            triggered_at: str) -> None:
        """Upsert the marker recording that `thread_id` was re-triggered at the
        given content `signature` and time."""
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO stale_reextract"
                "(thread_id, signature, triggered_at) VALUES(?,?,?)",
                (thread_id, signature, triggered_at))
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_store.py::test_stale_reextract_roundtrip -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/store.py tests/test_store.py
git commit -m "feat(store): stale_reextract marker table + get/set"
```

---

### Task 2: Store — thread helpers (`mark_thread_unenriched`, `thread_has_unenriched`, `thread_signature`)

**Files:**
- Modify: `mcpbrain/store.py` (near `thread_chunks`, ~line 1450)
- Test: `tests/test_store.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_store.py`:

```python
def _add_thread_chunk(s, doc_id, thread_id, text, chash, enriched):
    # Insert a chunk with a thread_id in metadata, then set enriched directly.
    s.upsert_chunk(doc_id, text, chash, {"thread_id": thread_id})
    with s._connect() as db:
        db.execute("UPDATE chunks SET enriched=? WHERE doc_id=?", (enriched, doc_id))


def test_thread_helpers(tmp_path):
    from mcpbrain.store import Store
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    _add_thread_chunk(s, "d1", "T1", "hello", "h1", enriched=1)
    _add_thread_chunk(s, "d2", "T1", "world", "h2", enriched=1)
    _add_thread_chunk(s, "d3", "T2", "other", "h3", enriched=0)

    # T1 fully enriched -> no unenriched; T2 has an unenriched chunk
    assert s.thread_has_unenriched("T1") is False
    assert s.thread_has_unenriched("T2") is True

    # signature is stable and order-independent of insertion
    sig_before = s.thread_signature("T1")
    assert isinstance(sig_before, str) and len(sig_before) == 64

    # mark_thread_unenriched flips only T1's chunks, returns the count
    assert s.mark_thread_unenriched("T1") == 2
    assert s.thread_has_unenriched("T1") is True
    assert s.thread_has_unenriched("T2") is True  # untouched

    # resetting enriched does NOT change content -> signature unchanged
    assert s.thread_signature("T1") == sig_before

    # changing content DOES change the signature
    _add_thread_chunk(s, "d1", "T1", "hello edited", "h1b", enriched=1)
    assert s.thread_signature("T1") != sig_before
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_store.py::test_thread_helpers -v`
Expected: FAIL with `AttributeError: 'Store' object has no attribute 'thread_has_unenriched'`

- [ ] **Step 3: Add the three methods**

In `mcpbrain/store.py`, after `thread_chunks` (~line 1450), add. Note: `hashlib` is already imported at the top of store.py; if not, add `import hashlib`.

```python
    def thread_has_unenriched(self, thread_id: str) -> bool:
        """True if any chunk in the thread is enriched=0 (so the normal
        enrichment path already owns it; the stale sweep must not double-trigger)."""
        with self._connect() as db:
            r = db.execute(
                "SELECT 1 FROM chunks "
                "WHERE json_extract(metadata,'$.thread_id')=? AND enriched=0 "
                "LIMIT 1",
                (thread_id,)).fetchone()
            return r is not None

    def mark_thread_unenriched(self, thread_id: str) -> int:
        """Set enriched=0 on every enriched chunk in the thread so the next
        enrichment cycle re-extracts it. Returns the number of rows flipped.
        Touches only this thread; leaves embedded untouched."""
        with self._connect() as db:
            cur = db.execute(
                "UPDATE chunks SET enriched=0 "
                "WHERE json_extract(metadata,'$.thread_id')=? AND enriched=1",
                (thread_id,))
            return cur.rowcount

    def thread_signature(self, thread_id: str) -> str:
        """sha256 over the thread's (doc_id, content_hash) pairs in doc_id order.
        Stable across enriched-flag changes; changes iff thread content changes.
        Empty thread -> a fixed empty-set digest."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT doc_id, content_hash FROM chunks "
                "WHERE json_extract(metadata,'$.thread_id')=? ORDER BY doc_id",
                (thread_id,)).fetchall()
        h = hashlib.sha256()
        for r in rows:
            h.update(r["doc_id"].encode())
            h.update(b"\x1f")
            h.update((r["content_hash"] or "").encode())
            h.update(b"\x1e")
        return h.hexdigest()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_store.py::test_thread_helpers -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/store.py tests/test_store.py
git commit -m "feat(store): thread re-extraction helpers (mark/has-unenriched, signature)"
```

---

### Task 3: `mcpbrain/stale_reextract.py` — the sweep

**Files:**
- Create: `mcpbrain/stale_reextract.py`
- Test: `tests/test_stale_reextract.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_stale_reextract.py`:

```python
"""Tests for the stale -> re-extraction trigger sweep (Gap A)."""
from mcpbrain import stale_reextract
from mcpbrain.store import Store


def _store(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    return s


def _chunk(s, doc_id, thread_id, text, chash, *, enriched, date):
    s.upsert_chunk(doc_id, text, chash, {"thread_id": thread_id, "date": date})
    with s._connect() as db:
        db.execute("UPDATE chunks SET enriched=? WHERE doc_id=?", (enriched, doc_id))


def _stale_thread(s, thread_id, *, enriched):
    # source message (older) + a later message carrying a resolution marker.
    _chunk(s, f"{thread_id}-src", thread_id, "Please send the report",
           f"{thread_id}h1", enriched=enriched,
           date="Mon, 01 Jun 2026 09:00:00 +0000")
    _chunk(s, f"{thread_id}-rep", thread_id, "All done, sent it through",
           f"{thread_id}h2", enriched=enriched,
           date="Tue, 02 Jun 2026 09:00:00 +0000")
    return s.add_unified_action(
        text="Send the report", owner="Joshua", status="open",
        source_doc_id=f"{thread_id}-src", thread_id=thread_id)


def test_sweep_triggers_idle_stale_thread(tmp_path):
    s = _store(tmp_path)
    _stale_thread(s, "T1", enriched=1)        # idle + stale -> candidate
    out = stale_reextract.sweep(s, now="2026-06-09T00:00:00Z")
    assert out["triggered"] == 1
    assert "T1" in out["threads"]
    assert s.thread_has_unenriched("T1") is True          # reset for re-extract
    assert s.get_stale_reextract("T1") is not None         # marker recorded


def test_sweep_skips_thread_with_pending_chunks(tmp_path):
    s = _store(tmp_path)
    _stale_thread(s, "T2", enriched=0)        # already has unenriched chunks
    out = stale_reextract.sweep(s, now="2026-06-09T00:00:00Z")
    assert out["triggered"] == 0


def test_sweep_loop_guard_same_state(tmp_path):
    s = _store(tmp_path)
    _stale_thread(s, "T3", enriched=1)
    first = stale_reextract.sweep(s, now="2026-06-09T00:00:00Z")
    assert first["triggered"] == 1
    # Simulate the normal enrichment cycle having re-enriched the thread
    # (content unchanged) so it's idle again at the SAME signature.
    s.mark_enriched(["T3-src", "T3-rep"])
    second = stale_reextract.sweep(s, now="2026-06-09T02:00:00Z")
    assert second["triggered"] == 0          # not re-triggered at same state


def test_sweep_rearms_after_content_change(tmp_path):
    s = _store(tmp_path)
    _stale_thread(s, "T4", enriched=1)
    stale_reextract.sweep(s, now="2026-06-09T00:00:00Z")
    s.mark_enriched(["T4-src", "T4-rep"])
    # New message arrives in the thread (content + signature change), idle again.
    _chunk(s, "T4-rep2", "T4", "still all done", "T4h3", enriched=1,
           date="Wed, 03 Jun 2026 09:00:00 +0000")
    out = stale_reextract.sweep(s, now="2026-06-09T03:00:00Z")
    assert out["triggered"] == 1             # re-armed by the content change


def test_sweep_respects_cap_and_reports_deferred(tmp_path):
    s = _store(tmp_path)
    for i in range(3):
        _stale_thread(s, f"C{i}", enriched=1)
    out = stale_reextract.sweep(s, now="2026-06-09T00:00:00Z", cap=2)
    assert out["triggered"] == 2
    assert out["deferred"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_stale_reextract.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mcpbrain.stale_reextract'`

- [ ] **Step 3: Write the module**

Create `mcpbrain/stale_reextract.py`:

```python
"""Re-extraction trigger for stale-looking open actions (Gap A).

The keyword stale heuristic (retrieval.action_is_stale) decides ONLY which
already-enriched threads deserve another LLM at-bat — never whether to close.
Resetting a thread to enriched=0 lets the normal enrichment cycle re-extract it
with its open_actions in context; the existing resolved_action_ids path makes
any actual close decision. A per-thread content signature prevents re-triggering
the same unchanged thread (which would re-pay the re-extraction token cost).
"""
from __future__ import annotations

import logging

from mcpbrain.retrieval import action_is_stale

log = logging.getLogger(__name__)

STALE_REEXTRACT_MAX = 20


def sweep(store, *, now: str, cap: int = STALE_REEXTRACT_MAX) -> dict:
    """Trigger re-extraction for stale open actions whose threads are idle.

    `now` is an ISO timestamp string (injected so the daemon owns the clock).
    Returns {"triggered": int, "deferred": int, "threads": [thread_id, ...]}.
    """
    candidates: list[tuple[str, str]] = []   # (thread_id, signature)
    seen: set[str] = set()
    for action in store.unified_actions(status="open"):
        thread_id = action.get("thread_id")
        if not thread_id or thread_id in seen:
            continue
        if not action_is_stale(store, action):
            continue
        if store.thread_has_unenriched(thread_id):
            continue  # the normal enrichment path already owns this thread
        sig = store.thread_signature(thread_id)
        prev = store.get_stale_reextract(thread_id)
        if prev and prev.get("signature") == sig:
            continue  # already had its at-bat at this content-state
        seen.add(thread_id)
        candidates.append((thread_id, sig))

    triggered: list[str] = []
    for thread_id, sig in candidates[:cap]:
        store.mark_thread_unenriched(thread_id)
        store.set_stale_reextract(thread_id, sig, now)
        triggered.append(thread_id)

    deferred = max(0, len(candidates) - cap)
    if deferred:
        log.info("stale-reextract: triggered %d thread(s), deferred %d to next "
                 "run (cap=%d)", len(triggered), deferred, cap)
    return {"triggered": len(triggered), "deferred": deferred,
            "threads": triggered}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_stale_reextract.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/stale_reextract.py tests/test_stale_reextract.py
git commit -m "feat(stale): re-extraction trigger sweep with content-signature loop guard"
```

---

### Task 4: Daemon — wire `maybe_stale_reextract` cadence

**Files:**
- Modify: `mcpbrain/daemon.py` (constructor ~296; instance vars ~383; `_run_periodic_passes` ~1211; reload-rewire ~581; `_CADENCE_KEYS` ~1407; `main()` Daemon(...) ~1481; new `maybe_stale_reextract` method near `maybe_clickup_sync` ~893)
- Modify: `tests/test_daemon_p3.py` (reload-rewire dict ~705; new cadence test)

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_daemon_p3.py` (reuses `_FakeEmbedder`, `_Clock`, `_make_store`, `SingleWriterLock` already in the file):

```python
def _stale_daemon(tmp_path, *, stale_reextract_interval_s=None, clock=None, **kw):
    store = _make_store(tmp_path)
    return store, Daemon(
        store, _FakeEmbedder(),
        services={},
        lock=SingleWriterLock(tmp_path / "d.lock"),
        stale_reextract_interval_s=stale_reextract_interval_s,
        clock=clock or _Clock(),
        **kw,
    )


def test_maybe_stale_reextract_off_when_unconfigured(tmp_path):
    store, daemon = _stale_daemon(tmp_path)  # no interval
    with patch("mcpbrain.stale_reextract.sweep") as mock_sweep:
        result = daemon.maybe_stale_reextract()
    assert result is None
    mock_sweep.assert_not_called()


def test_maybe_stale_reextract_runs_when_due(tmp_path):
    store, daemon = _stale_daemon(tmp_path, stale_reextract_interval_s=86400.0)
    fake = {"triggered": 1, "deferred": 0, "threads": ["T1"]}
    with patch("mcpbrain.stale_reextract.sweep", return_value=fake) as mock_sweep:
        result = daemon.maybe_stale_reextract()
    assert result == fake
    mock_sweep.assert_called_once()
    assert "now" in mock_sweep.call_args[1]   # now= passed as kwarg


def test_maybe_stale_reextract_swallows_errors(tmp_path):
    clock = _Clock()
    store, daemon = _stale_daemon(tmp_path, stale_reextract_interval_s=100.0,
                                  clock=clock)
    with patch("mcpbrain.stale_reextract.sweep",
               side_effect=RuntimeError("boom")):
        result = daemon.maybe_stale_reextract()
    assert result["stale_reextract"] is False
    # _last not advanced -> next call retries
    assert daemon._last_stale_reextract is None
```

Also update the existing reload-rewire test's patched dict (around line 705-713) to include the new key so `apply_config` doesn't `KeyError`:

```python
         patch("mcpbrain.daemon._cadences_from_config", return_value={
             "communities_interval_s": 500.0,
             "lint_interval_s": None,
             "synthesise_interval_s": None,
             "proactive_interval_s": None,
             "waiting_on_interval_s": None,
             "blocks_interval_s": None,
             "audit_interval_s": None,
             "clickup_interval_s": None,
             "stale_reextract_interval_s": None,
         }):
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_daemon_p3.py::test_maybe_stale_reextract_runs_when_due -v`
Expected: FAIL with `TypeError: __init__() got an unexpected keyword argument 'stale_reextract_interval_s'`

- [ ] **Step 3: Add the constructor param**

In `mcpbrain/daemon.py` `__init__`, after the `clickup_interval_s: float | None = None,` parameter (~line 296):

```python
                 clickup_interval_s: float | None = None,
                 stale_reextract_interval_s: float | None = None,
```

- [ ] **Step 4: Add the instance vars**

After the `self._last_clickup = None` line (~line 383):

```python
        # Periodic stale -> re-extraction trigger (Gap A) is OFF unless
        # stale_reextract_interval_s is set. Same three-shape cadence contract.
        self._stale_reextract_interval_s: float | None = stale_reextract_interval_s
        self._last_stale_reextract = None
```

- [ ] **Step 5: Add the `maybe_stale_reextract` method**

After `maybe_clickup_sync` (~line 892), add:

```python
    # -- periodic stale -> re-extraction trigger (Gap A) --------------------

    def maybe_stale_reextract(self) -> dict | None:
        """Reset stale, idle threads to enriched=0 so the normal cycle gives the
        LLM closer another at-bat, if due.

        OFF unless stale_reextract_interval_s is set (returns None). Does no LLM
        work itself; the re-extraction happens in the normal enrichment cycle. A
        failure is logged and swallowed so the loop keeps running.
        """
        if self._stale_reextract_interval_s is None:
            return None
        if self._last_stale_reextract is not None:
            if (self._clock() - self._last_stale_reextract) < self._stale_reextract_interval_s:
                return None
        now = self._clock()
        import datetime as _dt
        now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
        try:
            from mcpbrain import stale_reextract
            summary = stale_reextract.sweep(self._store, now=now_iso)
        except Exception as exc:  # noqa: BLE001 — must never crash the loop
            log.warning("stale-reextract sweep failed (will retry next due): %s",
                        exc, exc_info=True)
            return {"stale_reextract": False, "error": str(exc)}
        self._last_stale_reextract = now
        return summary
```

- [ ] **Step 6: Register in `_run_periodic_passes`**

In the tuple in `_run_periodic_passes` (~line 1219), after `self.maybe_clickup_sync,`:

```python
            self.maybe_clickup_sync,
            self.maybe_stale_reextract,
```

- [ ] **Step 7: Add to `_CADENCE_KEYS`**

In `_CADENCE_KEYS` (~line 1407), after `"clickup_interval_s",`:

```python
    "clickup_interval_s",
    "stale_reextract_interval_s",
```

- [ ] **Step 8: Add to the reload-rewire block**

In the `with self._config_lock:` rewire block (~line 581), after `self._clickup_interval_s = cadences["clickup_interval_s"]`:

```python
            self._clickup_interval_s = cadences["clickup_interval_s"]
            self._stale_reextract_interval_s = cadences["stale_reextract_interval_s"]
```

- [ ] **Step 9: Pass it in `main()`**

In the `Daemon(...)` construction in `main()` (~line 1487), change the final `audit_interval_s` line to add the new kwarg (keep the trailing comment):

```python
                    audit_interval_s=cadences["audit_interval_s"],
                    stale_reextract_interval_s=cadences["stale_reextract_interval_s"])  # services=None -> auto-build from token
```

- [ ] **Step 10: Run the daemon tests**

Run: `python -m pytest tests/test_daemon_p3.py -v`
Expected: PASS (the three new tests + the existing reload-rewire test still green)

- [ ] **Step 11: Commit**

```bash
git add mcpbrain/daemon.py tests/test_daemon_p3.py
git commit -m "feat(daemon): maybe_stale_reextract cadence (Gap A)"
```

---

## Part 2 — ClickUp reopen by tracking last-synced state

### Task 5: Store — `clickup_closed` column + setter

**Files:**
- Modify: `mcpbrain/store.py` (migration column list ~530; new method near `set_action_clickup_id` ~1206)
- Test: `tests/test_store.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_store.py`:

```python
def test_clickup_closed_setter(tmp_path):
    from mcpbrain.store import Store
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    aid = s.add_unified_action(text="Do thing", owner="Joshua")
    # default is NULL (never observed)
    assert s.get_unified_action(aid)["clickup_closed"] is None
    s.set_action_clickup_closed(aid, True)
    assert s.get_unified_action(aid)["clickup_closed"] == 1
    s.set_action_clickup_closed(aid, False)
    assert s.get_unified_action(aid)["clickup_closed"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_store.py::test_clickup_closed_setter -v`
Expected: FAIL with `KeyError: 'clickup_closed'` (column not yet added)

- [ ] **Step 3: Add the migration column**

In `mcpbrain/store.py`, in the `act_cols` ALTER loop (~line 529-531), add a third tuple:

```python
                # ClickUp two-way sync (2026-06-08): link anchor + priority.
                ("clickup_task_id",               "TEXT DEFAULT ''"),
                ("priority",                      "TEXT DEFAULT ''"),
                # Last-synced ClickUp closed-state for reopen detection
                # (2026-06-09). Nullable: NULL = never observed.
                ("clickup_closed",                "INTEGER"),
```

- [ ] **Step 4: Add the setter**

In `mcpbrain/store.py`, after `set_action_clickup_id` (~line 1214), add:

```python
    def set_action_clickup_closed(self, action_id: int, closed: bool) -> int:
        """Record the last-observed ClickUp closed-state (bookkeeping for reopen
        detection). Stores 1/0; does not touch updated_at."""
        with self._connect() as db:
            cur = db.execute(
                "UPDATE actions SET clickup_closed=? WHERE id=?",
                (1 if closed else 0, action_id))
            return cur.rowcount
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_store.py::test_clickup_closed_setter -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/store.py tests/test_store.py
git commit -m "feat(store): actions.clickup_closed column + setter"
```

---

### Task 6: clickup_sync — reopen by transition + maintain `clickup_closed`

**Files:**
- Modify: `mcpbrain/clickup_sync.py` (`_apply_inbound` ~51-86; outbound create ~184-190; outbound close ~192-201; `import_baseline` ~129-143)
- Test: `tests/test_clickup_sync.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_clickup_sync.py` (reuses `_task`, `FakeClient`, `_store` already in the file):

```python
class FailingCloseClient(FakeClient):
    """close_task always fails (ClickUp API error) -> returns False."""
    def close_task(self, home, task_id):
        return False


def test_reopen_when_clickup_reopens_llm_closed_action(tmp_path):
    s = _store(tmp_path)
    aid = s.add_unified_action(text="Do thing", owner="Joshua")
    s.set_action_clickup_id(aid, "t1")
    # action was closed by the LLM (resolved_by = an email msg id), not ClickUp
    s.set_action_status(aid, "done", "gmail-19a2b3")
    # we last synced the task as CLOSED
    s.set_action_clickup_closed(aid, True)
    # now the task is OPEN in ClickUp -> the user reopened it
    client = FakeClient([_task("t1", "Do thing", closed=False)])
    clickup_sync.sync(s, "/h", client=client)
    a = s.get_unified_action(aid)
    assert a["status"] == "open"                 # reopened despite non-clickup close
    assert "t1" not in client.closed             # and not re-closed outbound


def test_no_reopen_midpropagation_race(tmp_path):
    s = _store(tmp_path)
    aid = s.add_unified_action(text="Do thing", owner="Joshua")
    s.set_action_clickup_id(aid, "t1")
    s.set_action_status(aid, "done", "local")    # just closed locally
    # clickup_closed is NULL (task never observed closed) -> outbound-close pending
    client = FakeClient([_task("t1", "Do thing", closed=False)])
    clickup_sync.sync(s, "/h", client=client)
    a = s.get_unified_action(aid)
    assert a["status"] == "done"                 # NOT reopened (race protected)
    assert "t1" in client.closed                 # outbound close fired instead


def test_failed_close_leaves_clickup_closed_false(tmp_path):
    s = _store(tmp_path)
    aid = s.add_unified_action(text="Do thing", owner="Joshua")
    s.set_action_clickup_id(aid, "t1")
    s.set_action_status(aid, "done", "local")
    client = FailingCloseClient([_task("t1", "Do thing", closed=False)])
    clickup_sync.sync(s, "/h", client=client)
    # close failed -> we did NOT record it closed, so next cycle retries (not reopen)
    assert s.get_unified_action(aid)["clickup_closed"] in (None, 0)


def test_clickup_closed_set_on_outbound_create(tmp_path):
    s = _store(tmp_path)
    aid = s.add_unified_action(text="Fresh action", owner="Joshua")
    client = FakeClient([])
    clickup_sync.sync(s, "/h", client=client)
    assert s.get_unified_action(aid)["clickup_closed"] == 0   # created open


def test_clickup_closed_set_on_outbound_close(tmp_path):
    s = _store(tmp_path)
    aid = s.add_unified_action(text="Do thing", owner="Joshua")
    s.set_action_clickup_id(aid, "t1")
    s.set_action_status(aid, "done", "local")
    client = FakeClient([_task("t1", "Do thing", closed=False)])
    clickup_sync.sync(s, "/h", client=client)
    assert s.get_unified_action(aid)["clickup_closed"] == 1    # we closed it

def test_roundtrip_close_then_clickup_reopen(tmp_path):
    s = _store(tmp_path)
    aid = s.add_unified_action(text="Do thing", owner="Joshua")
    s.set_action_clickup_id(aid, "t1")
    s.set_action_status(aid, "done", "local")
    client = FakeClient([_task("t1", "Do thing", closed=False)])
    # cycle 1: outbound close -> task closed, clickup_closed=1
    clickup_sync.sync(s, "/h", client=client)
    assert s.get_unified_action(aid)["clickup_closed"] == 1
    # user reopens the task in ClickUp
    client.tasks[0]["closed"] = False
    # cycle 2: inbound reopens the brain action
    clickup_sync.sync(s, "/h", client=client)
    assert s.get_unified_action(aid)["status"] == "open"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_clickup_sync.py::test_reopen_when_clickup_reopens_llm_closed_action -v`
Expected: FAIL — the action stays `done` (old rule needs `resolved_by=="clickup"`).

- [ ] **Step 3: Rewrite the status block in `_apply_inbound`**

In `mcpbrain/clickup_sync.py`, replace the status reconciliation block (lines 56-69, the comment through the `elif`) with:

```python
    # status — ClickUp is authoritative for edits, but a brain-local close must
    # propagate OUT (handled by sync's outbound close), not be reverted here.
    # The reopen signal is the closed->open TRANSITION: if we last synced the
    # task as closed (clickup_closed truthy) and it is open now, the user
    # reopened it, so reopen the action regardless of who closed it. If the task
    # is open but we never saw it closed, that is a brain-close whose outbound
    # close has not applied yet — leave it.
    a_done = (action.get("status") or "").lower() in ("done", "closed")
    prev_closed = bool(action.get("clickup_closed"))
    if task["closed"] and not a_done:
        store.set_action_status(aid, "done", "clickup")
        diff["status"] = "done"
    elif (not task["closed"]) and a_done and prev_closed:
        store.set_action_status(aid, "open", "")
        diff["status"] = "open"
```

Then, at the **end** of `_apply_inbound`, just before `return diff`, record the observed state (bookkeeping, not part of `diff`):

```python
    # Record the observed ClickUp closed-state for next cycle's reopen check.
    if bool(task["closed"]) != prev_closed:
        store.set_action_clickup_closed(aid, bool(task["closed"]))
    return diff
```

- [ ] **Step 4: Maintain `clickup_closed` on outbound create**

In `sync()`, in the outbound-create block, after `store.set_action_clickup_id(a["id"], created["id"])` (~line 189), add:

```python
        if created and created.get("id"):
            store.set_action_clickup_id(a["id"], created["id"])
            store.set_action_clickup_closed(a["id"], False)   # created open
            summary["created"] += 1
```

- [ ] **Step 5: Maintain `clickup_closed` on outbound close (success only)**

In `sync()`, in the outbound-close block (~line 198-200), set it True only when the close succeeds:

```python
        t = tasks_by_id.get(tid)
        if t is not None and not t["closed"]:
            if client.close_task(home, tid):
                store.set_action_clickup_closed(a["id"], True)
                summary["closed"] += 1
```

- [ ] **Step 6: Seed `clickup_closed` in `import_baseline`**

In `import_baseline`, in the create branch (after `store.update_action_fields(new_id, priority=..., clickup_task_id=t["id"])`, ~line 139-142), add:

```python
                store.update_action_fields(
                    new_id, priority=t["priority"], clickup_task_id=t["id"])
                store.set_action_clickup_closed(new_id, bool(t["closed"]))
                if t["closed"]:
                    store.set_action_status(new_id, "done", "clickup")
```

(The link branch routes through `_apply_inbound`, which now records `clickup_closed` itself, so no extra call is needed there.)

- [ ] **Step 7: Run the clickup_sync tests**

Run: `python -m pytest tests/test_clickup_sync.py -v`
Expected: PASS (all new tests + existing tests still green)

- [ ] **Step 8: Commit**

```bash
git add mcpbrain/clickup_sync.py tests/test_clickup_sync.py
git commit -m "feat(clickup): reopen by closed->open transition; track clickup_closed"
```

---

### Task 7: Full suite + enablement

**Files:**
- Modify: `~/.mcpbrain/config.json` (runtime, NOT in git) — cadences block
- No code changes.

- [ ] **Step 1: Run the changed-module test set**

Run:
```bash
python -m pytest tests/test_store.py tests/test_stale_reextract.py \
  tests/test_daemon_p3.py tests/test_clickup_sync.py -v
```
Expected: PASS. (Per the iCloud gotcha in project memory, run the *full* suite on Nexus, not the Mac.)

- [ ] **Step 2: Enable the cadence in the runtime config**

This turns the sweep on and causes flagged threads to be re-extracted (a bounded, one-time token cost per thread). **Confirm with Josh before enabling** — he asked to be asked before anything with meaningful per-run token cost. Add to `~/.mcpbrain/config.json` under `cadences`:

```json
  "cadences": {
    "stale_reextract_interval_s": 86400
  }
```
(Merge into the existing `cadences` object; do not drop the other keys.)

- [ ] **Step 3: Restart the daemon and verify**

Restart so the new code + config load (use the project's restart path, e.g. `mcpbrain` agent restart / `launchctl kickstart -k gui/$(id -u)/church.centrepoint.mcpbrain`). Then confirm the sweep ran once on startup and observe the daemon log for a `stale-reextract:` line (only emitted when something is deferred) or check that previously-stuck stale actions begin closing over subsequent enrichment cycles.

- [ ] **Step 4: Commit any non-runtime artifacts**

No code to commit here (config.json is runtime-only). If the plan checkboxes are tracked in-repo, commit the updated plan doc.

---

## Self-Review Notes

- **Spec coverage:** Part 1 candidate selection (stale + idle + signature) → Task 3; `mark_thread_unenriched`/`thread_has_unenriched`/`thread_signature` → Task 2; loop-guard table → Task 1; daily cadence + cap + deferred logging → Tasks 3-4; reopen-by-transition + `clickup_closed` maintenance at inbound/outbound-close/create/baseline → Tasks 5-6. Out-of-scope items (keyword close, Gap B) intentionally absent.
- **Reopen race:** `test_no_reopen_midpropagation_race` guards the exact regression the old `resolved_by` rule protected against (action done, task open, never-observed-closed → must NOT reopen).
- **Type/name consistency:** `sweep(store, *, now, cap)` returns `{"triggered","deferred","threads"}`; `maybe_stale_reextract` passes `now=` and returns that dict or `{"stale_reextract": False, ...}`. `set_action_clickup_closed(action_id, bool)` stores 1/0; `clickup_closed` read as truthy. `stale_reextract_interval_s` used identically across constructor, `_CADENCE_KEYS`, rewire, `main()`, and the test's patched dict.
