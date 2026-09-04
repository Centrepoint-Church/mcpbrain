# Sync Queue Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the batch-then-commit sync round in Drive, Gmail and Calendar with `discover → sync_queue → work`, so a budget cutoff can never destroy or repeat work.

**Architecture:** Discovery pages a provider's delta and does nothing but UPSERT rows into a `sync_queue` table and advance that source's cursor — both in ONE transaction, per page. A single shared work loop then claims items newest-first, fetches/extracts/upserts them, and deletes the queue row in the same transaction as the chunk write. Progress becomes per-item and durable, so budgets stop being correctness-critical.

**Tech Stack:** Python 3.12, SQLite (via `mcpbrain/store.py`), pytest + pytest-xdist, ruff, `uv` for all commands.

**Spec:** `docs/superpowers/specs/2026-09-04-sync-queue-design.md` — read it before Task 1.

## Global Constraints

- **Run everything through `uv`**: `uv run pytest ...`, `uv run ruff check mcpbrain/`. A bare `pytest` is not installed.
- **The queue MUST live in the store's SQLite file.** This is load-bearing: the cursor now advances ahead of the work, and only a single-file backup/restore keeps queue, cursors and chunks consistent. Never move it to disk files.
- **`store.py` carries no logger.** Store methods return counts; callers log.
- **Never use `Store(str(config.app_dir()))`** — that bug has shipped four separate times. The correct construction is `Store(config.store_path(), dim=384)`; `store_path()` takes **no** arguments.
- **`_S = _strict_suffix()`** must be appended to every new `CREATE TABLE` in `init()`, matching the surrounding tables.
- **Gold gate is non-negotiable:** recall@10 ≥ 0.780 and MRR ≥ 0.550, measured before and after (Task 10).
- **Do not touch** the enrichment spool, `CAPTURES_BUDGET_S`, or `ENRICH_SPOOL_BUDGET_S`. Out of scope.
- Commit after every task. Never push; releasing is a separate, explicitly-instructed step.

---

### Task 1: `sync_queue` schema and the discovery write path

**Files:**
- Modify: `mcpbrain/store.py` (add table + index in `init()`, ~line 551 after `sync_cursors`; add methods near `get_cursor`/`set_cursor`, ~line 2781)
- Test: `tests/test_sync_queue_store.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `Store.enqueue_and_advance(items: list[dict], *, source: str, cursor: str) -> int` — UPSERT every item and set `sync_cursors[source] = cursor` in ONE transaction; returns rows written. Each item is `{"ref_id": str, "version": str, "event": str, "modified_at": str}`; `source` comes from the keyword, not the item.
  - `Store.sync_queue_pending(source: str | None = None) -> int`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sync_queue_store.py
"""sync_queue: the durable seam between discovery and work.

The invariant everything rests on: a page's rows and that page's cursor
advance commit TOGETHER, so the cursor can never be ahead of what is recorded.
"""
from mcpbrain.store import Store


def _store(tmp_path):
    s = Store(tmp_path / "q.sqlite3", dim=4)
    s.init()
    return s


def _item(ref, *, version="1", event="upsert", modified_at="2026-09-04T10:00:00"):
    return {"ref_id": ref, "version": version, "event": event,
            "modified_at": modified_at}


def test_enqueue_and_advance_writes_rows_and_cursor(tmp_path):
    s = _store(tmp_path)
    n = s.enqueue_and_advance([_item("f1"), _item("f2")], source="drive", cursor="200")
    assert n == 2
    assert s.sync_queue_pending("drive") == 2
    assert s.get_cursor("drive") == "200"


def test_reenqueue_same_ref_collapses_to_one_row(tmp_path):
    """PRIMARY KEY (source, ref_id): a later event supersedes the earlier one."""
    s = _store(tmp_path)
    s.enqueue_and_advance([_item("f1", event="upsert")], source="drive", cursor="1")
    s.enqueue_and_advance([_item("f1", event="remove", version="2")],
                          source="drive", cursor="2")
    assert s.sync_queue_pending("drive") == 1
    row = s.due_sync_items(limit=10, now="2026-09-04T12:00:00")[0]
    assert row["event"] == "remove"


def test_pending_is_scoped_by_source(tmp_path):
    s = _store(tmp_path)
    s.enqueue_and_advance([_item("f1")], source="drive", cursor="1")
    s.enqueue_and_advance([_item("m1")], source="gmail", cursor="9")
    assert s.sync_queue_pending("drive") == 1
    assert s.sync_queue_pending("gmail") == 1
    assert s.sync_queue_pending() == 2


def test_cursor_and_rows_are_one_transaction(tmp_path):
    """A failure mid-write must leave BOTH unchanged, never a cursor ahead of rows."""
    s = _store(tmp_path)
    s.enqueue_and_advance([_item("f1")], source="drive", cursor="100")
    bad = [_item("f2"), {"ref_id": None, "version": "1", "event": "upsert",
                         "modified_at": "2026-09-04T10:00:00"}]
    try:
        s.enqueue_and_advance(bad, source="drive", cursor="999")
    except Exception:
        pass
    assert s.get_cursor("drive") == "100", "cursor advanced despite a failed write"
    assert s.sync_queue_pending("drive") == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_sync_queue_store.py -q`
Expected: FAIL — `AttributeError: 'Store' object has no attribute 'enqueue_and_advance'`

- [ ] **Step 3: Add the table and index in `init()`**

In `mcpbrain/store.py`, immediately after the `sync_cursors` CREATE TABLE (~line 551):

```python
            # --- sync work queue -------------------------------------------
            # The durable seam between discovery (page the provider delta) and
            # work (fetch/extract/upsert). Presence IS pending; success deletes
            # the row, so `SELECT count(*)` is the backlog as a FACT, not an
            # estimate -- the thing whose absence hid a five-week Drive outage.
            # PRIMARY KEY (source, ref_id) gives per-item event collapse for
            # free: re-discovering a file supersedes its earlier event.
            # modified_at is NOT NULL so the newest-first ORDER BY can use a
            # PLAIN index; discovery falls back to the discovery timestamp for
            # sources (Gmail history) that expose no per-item mtime. An
            # expression index here would reintroduce the 0.7.105 drift class.
            db.execute(f"""CREATE TABLE IF NOT EXISTS sync_queue(
                source          TEXT NOT NULL,
                ref_id          TEXT NOT NULL,
                version         TEXT NOT NULL DEFAULT '',
                event           TEXT NOT NULL,
                modified_at     TEXT NOT NULL,
                discovered_at   TEXT NOT NULL,
                attempts        INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT,
                last_error      TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (source, ref_id)){_S}""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_sync_queue_due "
                       "ON sync_queue(next_attempt_at, modified_at DESC)")
```

- [ ] **Step 4: Add the store methods**

In `mcpbrain/store.py`, after `set_cursor` (~line 2792):

```python
    def enqueue_and_advance(self, items, *, source: str, cursor: str) -> int:
        """UPSERT queue rows and advance this source's cursor in ONE transaction.

        THE invariant of the sync redesign: the cursor can never be ahead of
        what is recorded. Discovery advances per PAGE rather than per completed
        round, which is what makes a budget cutoff free -- the next cycle
        resumes at the last committed page and re-does nothing.

        A differing `version` on an existing row resets attempts/backoff: a
        genuinely new edit is new work, not a continuation of a failing one.
        """
        now = _utc_now_iso()
        with self._connect(write=True) as db:
            for it in items:
                db.execute(
                    "INSERT INTO sync_queue(source, ref_id, version, event, "
                    "  modified_at, discovered_at) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(source, ref_id) DO UPDATE SET "
                    "  event=excluded.event, modified_at=excluded.modified_at, "
                    "  attempts=CASE WHEN sync_queue.version=excluded.version "
                    "                THEN sync_queue.attempts ELSE 0 END, "
                    "  next_attempt_at=CASE WHEN sync_queue.version=excluded.version "
                    "                       THEN sync_queue.next_attempt_at ELSE NULL END, "
                    "  last_error=CASE WHEN sync_queue.version=excluded.version "
                    "                  THEN sync_queue.last_error ELSE '' END, "
                    "  version=excluded.version",
                    (source, it["ref_id"], it.get("version", ""), it["event"],
                     it["modified_at"], now))
            db.execute(
                "INSERT INTO sync_cursors(source, cursor, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(source) DO UPDATE SET cursor=excluded.cursor, "
                "updated_at=excluded.updated_at",
                (source, str(cursor), now))
        return len(items)

    def sync_queue_pending(self, source: str | None = None) -> int:
        """Rows still to be worked. Presence is pending, so this is a count."""
        with self._connect() as db:
            if source is None:
                return db.execute("SELECT count(*) FROM sync_queue").fetchone()[0]
            return db.execute("SELECT count(*) FROM sync_queue WHERE source=?",
                              (source,)).fetchone()[0]
```

If `_utc_now_iso` does not already exist in `store.py`, grep for the module's existing timestamp helper (`datetime('now')` is used inline elsewhere) and use that idiom instead — do not add a second time source.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_sync_queue_store.py -q`
Expected: the three enqueue tests PASS; `test_reenqueue_same_ref_collapses_to_one_row` still FAILS on `due_sync_items` (Task 2). Mark that one `@pytest.mark.xfail(strict=True, reason="due_sync_items lands in Task 2")` and remove the marker in Task 2.

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/store.py tests/test_sync_queue_store.py
git commit -m "feat(sync): add sync_queue table and the transactional discovery write"
```

---

### Task 2: The read path — `due_sync_items`, ordering, and an index-backed plan

**Files:**
- Modify: `mcpbrain/store.py`
- Test: `tests/test_sync_queue_store.py`

**Interfaces:**
- Consumes: Task 1's table and `enqueue_and_advance`.
- Produces: `Store.due_sync_items(*, limit: int, now: str) -> list[dict]` — rows whose `next_attempt_at` is NULL or `<= now`, ordered `modified_at DESC`, capped at `limit`. Each dict has keys `source, ref_id, version, event, modified_at, discovered_at, attempts, next_attempt_at, last_error`. It is a PURE READ — there is no lease.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_sync_queue_store.py
def test_due_items_are_newest_first(tmp_path):
    s = _store(tmp_path)
    s.enqueue_and_advance([
        _item("old", modified_at="2026-07-01T00:00:00"),
        _item("new", modified_at="2026-09-04T00:00:00"),
        _item("mid", modified_at="2026-08-01T00:00:00"),
    ], source="drive", cursor="1")
    got = [r["ref_id"] for r in s.due_sync_items(limit=10, now="2026-09-05T00:00:00")]
    assert got == ["new", "mid", "old"]


def test_due_items_respect_limit(tmp_path):
    s = _store(tmp_path)
    s.enqueue_and_advance([_item(f"f{i}", modified_at=f"2026-09-0{i}T00:00:00")
                           for i in range(1, 6)], source="drive", cursor="1")
    assert len(s.due_sync_items(limit=2, now="2026-09-09T00:00:00")) == 2


def test_backed_off_item_is_not_returned_and_does_not_block(tmp_path):
    """No head-of-line blocking: a failing item must not stall the queue."""
    s = _store(tmp_path)
    s.enqueue_and_advance([
        _item("poison", modified_at="2026-09-04T00:00:00"),
        _item("healthy", modified_at="2026-09-03T00:00:00"),
    ], source="drive", cursor="1")
    s.fail_sync_item("drive", "poison", "export timeout", now="2026-09-04T10:00:00")
    got = [r["ref_id"] for r in s.due_sync_items(limit=10, now="2026-09-04T10:01:00")]
    assert got == ["healthy"], "a backed-off item blocked the newest-first queue"


def test_due_query_is_index_backed_on_an_existing_store(tmp_path):
    """0.7.105 lesson: a fresh store's DDL and query text always agree, so a
    fresh-store test cannot catch drift. Re-init an ALREADY-init'd store and
    assert the plan still uses the index."""
    s = _store(tmp_path)
    s.init()  # second init, as a real upgrade does
    with s._connect() as db:
        plan = " ".join(str(r) for r in db.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM sync_queue "
            "WHERE next_attempt_at IS NULL OR next_attempt_at <= ? "
            "ORDER BY modified_at DESC LIMIT ?", ("2026-09-04", 50)).fetchall())
    assert "SCAN sync_queue" not in plan, f"full scan: {plan}"
    assert "idx_sync_queue_due" in plan, f"index unused: {plan}"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_sync_queue_store.py -q`
Expected: FAIL — `due_sync_items` / `fail_sync_item` do not exist.

- [ ] **Step 3: Implement `due_sync_items`**

```python
    def due_sync_items(self, *, limit: int, now: str) -> list[dict]:
        """Queue rows ready to work, newest-first. A PURE READ -- no lease.

        The daemon is the only worker and holds the single-writer lock, and a
        crash mid-item leaves the row present (deletion happens only on
        success), so a claim/lease would be machinery with no failure to catch.

        Filtering on next_attempt_at is what makes "retry forever" safe: a
        backed-off item is simply not selected, so it can never sit at the head
        of the queue and stall everything behind it.
        """
        with self._connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT source, ref_id, version, event, modified_at, discovered_at, "
                "       attempts, next_attempt_at, last_error "
                "FROM sync_queue "
                "WHERE next_attempt_at IS NULL OR next_attempt_at <= ? "
                "ORDER BY modified_at DESC LIMIT ?", (now, limit)).fetchall()]
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_sync_queue_store.py -q`
Expected: ordering/limit/index tests PASS. The backoff test still fails (Task 3). Remove the Task-1 xfail marker now — `due_sync_items` exists.

If the index test fails, do NOT relax the assertion. Check the index column order matches the query's filter-then-sort shape.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/store.py tests/test_sync_queue_store.py
git commit -m "feat(sync): newest-first due_sync_items with an index-backed plan"
```

---

### Task 3: Completion and retry — atomicity and backoff

**Files:**
- Modify: `mcpbrain/store.py`
- Test: `tests/test_sync_queue_store.py`

**Interfaces:**
- Consumes: Tasks 1-2.
- Produces:
  - `Store.complete_sync_item(source: str, ref_id: str) -> None`
  - `Store.fail_sync_item(source: str, ref_id: str, error: str, *, now: str) -> int` — returns the new attempts count.
  - `Store.sync_queue_stats() -> dict` — `{"pending": int, "oldest_discovered_at": str | None, "failing": list[dict]}` where `failing` holds rows with `attempts >= 3` (`ref_id`, `attempts`, `last_error`), newest-failure-first, capped at 5.
  - Module constant `_SYNC_BACKOFF_S = (120, 600, 3600, 21600, 86400)`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_sync_queue_store.py
def test_complete_removes_the_row(tmp_path):
    s = _store(tmp_path)
    s.enqueue_and_advance([_item("f1")], source="drive", cursor="1")
    s.complete_sync_item("drive", "f1")
    assert s.sync_queue_pending("drive") == 0


def test_failure_backs_off_and_never_drops(tmp_path):
    s = _store(tmp_path)
    s.enqueue_and_advance([_item("f1")], source="drive", cursor="1")
    for expected in (1, 2, 3):
        assert s.fail_sync_item("drive", "f1", "boom",
                                now="2026-09-04T10:00:00") == expected
    assert s.sync_queue_pending("drive") == 1, "an item was dropped"
    row = s.due_sync_items(limit=10, now="2099-01-01T00:00:00")[0]
    assert row["attempts"] == 3
    assert row["last_error"] == "boom"


def test_new_version_resets_attempts(tmp_path):
    """A genuinely new edit is new work, not a continuation of a failing one."""
    s = _store(tmp_path)
    s.enqueue_and_advance([_item("f1", version="1")], source="drive", cursor="1")
    s.fail_sync_item("drive", "f1", "boom", now="2026-09-04T10:00:00")
    s.enqueue_and_advance([_item("f1", version="2")], source="drive", cursor="2")
    row = s.due_sync_items(limit=10, now="2026-09-04T10:00:01")[0]
    assert row["attempts"] == 0
    assert row["next_attempt_at"] is None


def test_stats_surface_pending_age_and_failures(tmp_path):
    s = _store(tmp_path)
    s.enqueue_and_advance([_item("f1"), _item("f2")], source="drive", cursor="1")
    for _ in range(3):
        s.fail_sync_item("drive", "f1", "export timeout", now="2026-09-04T10:00:00")
    st = s.sync_queue_stats()
    assert st["pending"] == 2
    assert st["oldest_discovered_at"] is not None
    assert [f["ref_id"] for f in st["failing"]] == ["f1"]
    assert st["failing"][0]["attempts"] == 3
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_sync_queue_store.py -q`
Expected: FAIL — `complete_sync_item` does not exist.

- [ ] **Step 3: Implement**

Add near the top of `mcpbrain/store.py`, beside the other module constants (~line 296):

```python
# Retry backoff for sync_queue, in seconds by attempt number. The last value is
# the cap and repeats forever: nothing is ever abandoned, so a permanently
# broken file settles at one retry per day -- bounded cost, permanently visible
# (doctor surfaces attempts >= 3). This closes sync/drive.py's KNOWN GAP, where
# a transient export failure silently dropped a file version because the cursor
# moved past it.
_SYNC_BACKOFF_S = (120, 600, 3600, 21600, 86400)
_SYNC_FAILING_ATTEMPTS = 3
```

Then the methods:

```python
    def complete_sync_item(self, source: str, ref_id: str) -> None:
        """Delete the queue row, marking the item done.

        Delivery is AT-LEAST-ONCE, not exactly-once: a crash between the
        handler's chunk write and this call leaves the row queued, so the item
        is re-worked next cycle. That is safe because the handlers are
        idempotent -- upsert_file_chunks keys on positional gdrive-<fid>-<i>
        doc_ids and reconciles orphans, so re-working converges on the same
        state. The crash window resume_ids papered over still exists; it is now
        harmless rather than lossy.
        """
        with self._connect(write=True) as db:
            db.execute("DELETE FROM sync_queue WHERE source=? AND ref_id=?",
                       (source, ref_id))

    def fail_sync_item(self, source: str, ref_id: str, error: str, *,
                       now: str) -> int:
        """Record a failed attempt and schedule the next one. Returns attempts.

        The row is never deleted: retry-forever with a capped backoff. Safe only
        because due_sync_items filters on next_attempt_at, so this row cannot
        block the queue behind it.
        """
        from datetime import datetime, timedelta
        with self._connect(write=True) as db:
            row = db.execute("SELECT attempts FROM sync_queue "
                             "WHERE source=? AND ref_id=?",
                             (source, ref_id)).fetchone()
            if row is None:
                return 0
            attempts = int(row["attempts"]) + 1
            delay = _SYNC_BACKOFF_S[min(attempts, len(_SYNC_BACKOFF_S)) - 1]
            nxt = (datetime.fromisoformat(now) + timedelta(seconds=delay)).isoformat()
            db.execute("UPDATE sync_queue SET attempts=?, next_attempt_at=?, "
                       "last_error=? WHERE source=? AND ref_id=?",
                       (attempts, nxt, str(error)[:200], source, ref_id))
        return attempts

    def sync_queue_stats(self) -> dict:
        """Backlog as a fact, for doctor. `failing` is advisory, not terminal --
        those rows are still retrying."""
        with self._connect() as db:
            pending = db.execute("SELECT count(*) FROM sync_queue").fetchone()[0]
            oldest = db.execute(
                "SELECT min(discovered_at) FROM sync_queue").fetchone()[0]
            failing = [dict(r) for r in db.execute(
                "SELECT source, ref_id, attempts, last_error FROM sync_queue "
                "WHERE attempts >= ? ORDER BY attempts DESC LIMIT 5",
                (_SYNC_FAILING_ATTEMPTS,)).fetchall()]
        return {"pending": pending, "oldest_discovered_at": oldest,
                "failing": failing}
```

- [ ] **Step 4: Run the full file**

Run: `uv run pytest tests/test_sync_queue_store.py -q`
Expected: ALL PASS, including `test_backed_off_item_is_not_returned_and_does_not_block` from Task 2.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/store.py tests/test_sync_queue_store.py
git commit -m "feat(sync): queue completion, capped-backoff retry, and backlog stats"
```

---

### Task 4: The shared work loop

**Files:**
- Create: `mcpbrain/sync/queue.py`
- Test: `tests/test_sync_queue_worker.py` (create)

**Interfaces:**
- Consumes: `Store.due_sync_items`, `complete_sync_item`, `fail_sync_item` (Tasks 2-3).
- Produces: `work_queue(store, *, handlers: dict, limit: int, budget=None, now: str | None = None) -> dict` returning `{"processed": int, "failed": int}`. `handlers` maps a source PREFIX (`"drive"`, `"gmail"`, `"calendar"`) to `callable(item: dict) -> None`; a source of `"drive:<drive_id>"` resolves via `source.split(":", 1)[0]`. A handler that raises is a failure; a handler that returns is a success.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_sync_queue_worker.py
"""The shared work loop: claim newest-first, handle, complete or back off.

A budget cutoff here must be FREE -- whatever was not reached is still queued.
That is the whole point of the redesign, so it is the first thing pinned.
"""
from mcpbrain.store import Store
from mcpbrain.sync.queue import work_queue

NOW = "2026-09-04T10:00:00"


def _store(tmp_path):
    s = Store(tmp_path / "w.sqlite3", dim=4)
    s.init()
    return s


def _seed(s, refs, source="drive"):
    s.enqueue_and_advance(
        [{"ref_id": r, "version": "1", "event": "upsert",
          "modified_at": f"2026-09-{i + 1:02d}T00:00:00"} for i, r in enumerate(refs)],
        source=source, cursor="1")


class _StopBudget:
    def __init__(self, allow): self._left = allow
    def expired(self):
        if self._left > 0:
            self._left -= 1
            return False
        return True


def test_successful_items_leave_the_queue(tmp_path):
    s = _store(tmp_path); _seed(s, ["a", "b"])
    seen = []
    out = work_queue(s, handlers={"drive": seen.append}, limit=10, now=NOW)
    assert out == {"processed": 2, "failed": 0}
    assert s.sync_queue_pending() == 0
    assert [i["ref_id"] for i in seen] == ["b", "a"]      # newest-first


def test_budget_cutoff_leaves_the_rest_queued(tmp_path):
    """The property the whole redesign exists for: a cutoff loses nothing."""
    s = _store(tmp_path); _seed(s, ["a", "b", "c"])
    out = work_queue(s, handlers={"drive": lambda i: None}, limit=10,
                     budget=_StopBudget(1), now=NOW)
    assert out["processed"] == 1
    assert s.sync_queue_pending() == 2


def test_a_failing_item_backs_off_and_the_loop_continues(tmp_path):
    s = _store(tmp_path); _seed(s, ["good", "bad"])

    def handler(item):
        if item["ref_id"] == "bad":
            raise RuntimeError("export timeout")

    out = work_queue(s, handlers={"drive": handler}, limit=10, now=NOW)
    assert out == {"processed": 1, "failed": 1}
    assert s.sync_queue_pending() == 1                    # 'bad' retained
    assert s.due_sync_items(limit=10, now=NOW) == []      # and backed off


def test_shared_drive_source_resolves_to_the_drive_handler(tmp_path):
    s = _store(tmp_path); _seed(s, ["x"], source="drive:0ABC")
    seen = []
    work_queue(s, handlers={"drive": seen.append}, limit=10, now=NOW)
    assert [i["ref_id"] for i in seen] == ["x"]


def test_a_crash_after_the_write_leaves_the_item_queued(tmp_path):
    """At-least-once delivery: the item is re-worked, and that is SAFE because
    the handlers are idempotent. This is the property the spec rests on -- the
    row deletion is NOT in the handler's transaction."""
    s = _store(tmp_path); _seed(s, ["a"])
    writes = []

    def handler(item):
        writes.append(item["ref_id"])       # stands in for the chunk write
        raise RuntimeError("crash after write, before completion")

    work_queue(s, handlers={"drive": handler}, limit=10, now=NOW)
    assert s.sync_queue_pending() == 1, "item lost after a post-write crash"

    # Next cycle (past the backoff) re-works it; a converging handler completes.
    work_queue(s, handlers={"drive": lambda i: writes.append(i["ref_id"])},
               limit=10, now="2026-09-04T10:05:00")
    assert writes == ["a", "a"], "the item was not re-worked"
    assert s.sync_queue_pending() == 0


def test_an_unknown_source_fails_the_item_rather_than_the_loop(tmp_path):
    s = _store(tmp_path); _seed(s, ["x"], source="mystery")
    out = work_queue(s, handlers={"drive": lambda i: None}, limit=10, now=NOW)
    assert out == {"processed": 0, "failed": 1}
    assert s.sync_queue_pending() == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_sync_queue_worker.py -q`
Expected: FAIL — `No module named 'mcpbrain.sync.queue'`

- [ ] **Step 3: Implement `mcpbrain/sync/queue.py`**

```python
"""The shared sync work loop.

Discovery (per source, in drive.py/gmail.py/calendar.py) writes rows; this
drains them. Splitting the two is what removes the livelock class: a round no
longer has to complete for progress to be durable, so a budget cutoff anywhere
costs nothing and repeats nothing.

Deliberately source-agnostic. The only per-source knowledge is the `handlers`
dict the caller passes, which maps a source prefix to the existing
fetch/extract/upsert code.
"""
import logging
from datetime import datetime, timezone

log = logging.getLogger("mcpbrain.sync.queue")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def work_queue(store, *, handlers: dict, limit: int, budget=None,
               now: str | None = None) -> dict:
    """Work up to `limit` queued items, newest-first. Returns counts.

    A handler that returns is a success (its row is deleted); one that raises
    is a failure (attempts+1, backoff, row retained). Nothing is ever dropped.
    """
    now = now or _utc_now_iso()
    processed = failed = 0
    for item in store.due_sync_items(limit=limit, now=now):
        if budget is not None and budget.expired():
            break               # free: every unreached row is still queued
        source = item["source"]
        handler = handlers.get(source.split(":", 1)[0])
        if handler is None:
            # Not a crash: an unknown source is this item's problem, not the
            # loop's. It backs off and stays visible rather than wedging sync.
            store.fail_sync_item(source, item["ref_id"],
                                 f"no handler for source {source!r}", now=now)
            failed += 1
            continue
        try:
            handler(item)
        except Exception as exc:  # noqa: BLE001 — one item must not kill the loop
            attempts = store.fail_sync_item(source, item["ref_id"], str(exc),
                                            now=now)
            log.warning("sync: %s/%s failed (attempt %d), will retry: %s",
                        source, item["ref_id"], attempts, exc)
            failed += 1
            continue
        store.complete_sync_item(source, item["ref_id"])
        processed += 1
    return {"processed": processed, "failed": failed}
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_sync_queue_worker.py -q`
Expected: ALL PASS.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check mcpbrain/
git add mcpbrain/sync/queue.py tests/test_sync_queue_worker.py
git commit -m "feat(sync): shared work loop over sync_queue"
```

---

### Task 5: Drive discovery

**Files:**
- Modify: `mcpbrain/sync/drive.py` — replace the body of `sync_drive` (currently ~line 622-800) and `sync_shared_drive`'s delta loop (~line 860-1065)
- Test: `tests/test_drive_discovery.py` (create)

**Interfaces:**
- Consumes: `Store.enqueue_and_advance` (Task 1).
- Produces:
  - `discover_drive(service, store, source="drive", *, budget=None) -> int` — pages `changes().list`, enqueues, advances the cursor per page. Returns items enqueued.
  - `handle_drive_item(service, store, item, **kw) -> None` — the work-loop handler; fetches/extracts/upserts one file, or deletes its chunks when `item["event"] == "remove"`. Reuses the existing `fetch_content`, `folder_path`, `normalise_drive`, `upsert_file_chunks` unchanged.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_drive_discovery.py
"""Drive discovery enqueues and advances PER PAGE.

The 0.7.123 livelock: newStartPageToken arrives only on the feed's LAST page,
so a round that could not reach it never advanced the cursor and re-walked the
same prefix forever -- five weeks, on the author's store. Advancing per page
removes the need to reach the end at all.
"""
from mcpbrain.store import Store
from mcpbrain.sync.drive import discover_drive

PAGES = 4


class _Req:
    def __init__(self, r): self._r = r
    def execute(self, num_retries=0): return self._r


class _Changes:
    def __init__(self, svc): self._svc = svc

    def list(self, **kw):
        tok = kw.get("pageToken")
        self._svc.pages.append(tok)
        if tok == "DONE":
            return _Req({"changes": [], "newStartPageToken": "DONE"})
        i = int(tok)
        body = {"changes": [{"fileId": f"f{i}", "file": {
            "id": f"f{i}", "name": f"d{i}.pdf", "mimeType": "application/pdf",
            "version": "1", "modifiedTime": f"2026-09-0{i}T00:00:00Z"}}]}
        if i < PAGES:
            body["nextPageToken"] = str(i + 1)
        else:
            body["newStartPageToken"] = "DONE"
        return _Req(body)

    def getStartPageToken(self, **kw): return _Req({"startPageToken": "1"})


class _Service:
    def __init__(self): self.pages = []
    def changes(self): return _Changes(self)


class _OneShotBudget:
    def __init__(self, allow=1): self._left = allow
    def expired(self):
        if self._left > 0:
            self._left -= 1
            return False
        return True


def _store(tmp_path):
    s = Store(tmp_path / "d.sqlite3", dim=4)
    s.init()
    return s


def test_discovery_advances_the_cursor_after_every_page(tmp_path):
    s, svc = _store(tmp_path), _Service()
    s.set_cursor("drive", "1")
    discover_drive(svc, s, budget=_OneShotBudget(1))
    assert s.get_cursor("drive") != "1", "cursor did not advance after one page"
    assert s.sync_queue_pending("drive") == 1


def test_discovery_never_rewalks_a_page(tmp_path):
    """The livelock's signature was page 1, over and over."""
    s, svc = _store(tmp_path), _Service()
    s.set_cursor("drive", "1")
    for _ in range(4):
        discover_drive(svc, s, budget=_OneShotBudget(1))
    assert svc.pages.count("1") == 1, f"page 1 re-walked: {svc.pages}"


def test_discovery_reaches_the_feed_head(tmp_path):
    s, svc = _store(tmp_path), _Service()
    s.set_cursor("drive", "1")
    for _ in range(PAGES + 2):
        discover_drive(svc, s, budget=_OneShotBudget(1))
    assert s.get_cursor("drive") == "DONE"
    assert s.sync_queue_pending("drive") == PAGES


def test_removal_is_enqueued_as_a_remove_event(tmp_path):
    s = _store(tmp_path)

    class _Removed(_Changes):
        def list(self, **kw):
            return _Req({"changes": [{"fileId": "gone", "removed": True}],
                         "newStartPageToken": "DONE"})

    class _S(_Service):
        def changes(self): return _Removed(self)

    s.set_cursor("drive", "1")
    discover_drive(_S(), s)
    row = s.due_sync_items(limit=10, now="2099-01-01T00:00:00")[0]
    assert row["event"] == "remove" and row["ref_id"] == "gone"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_drive_discovery.py -q`
Expected: FAIL — `cannot import name 'discover_drive'`

- [ ] **Step 3: Implement `discover_drive`**

Replace `sync_drive`'s body in `mcpbrain/sync/drive.py`:

```python
def discover_drive(service, store, source: str = "drive", *, budget=None) -> int:
    """Page changes().list, enqueue each change, advance the cursor PER PAGE.

    No fetch, no export, no extraction -- discovery is listing only, which is
    why it is cheap enough to run to completion almost every cycle.

    Advancing per page is what removes the 0.7.123 livelock at the root:
    newStartPageToken arrives only on the feed's last page, so a round that
    could not reach it never advanced. nextPageToken is itself a valid resume
    point, so progress no longer requires finishing the feed.
    """
    cursor = store.get_cursor(source)
    if cursor is None:                      # bootstrap: start from the head
        tok = service.changes().getStartPageToken().execute(
            num_retries=_NUM_RETRIES)["startPageToken"]
        store.set_cursor(source, str(tok))
        return 0

    enqueued = 0
    page_token = cursor
    while True:
        if budget is not None and budget.expired():
            break
        resp = service.changes().list(
            pageToken=page_token, spaces="drive", includeRemoved=True,
            fields=_CHANGES_FIELDS).execute(num_retries=_NUM_RETRIES)

        items = []
        for ch in resp.get("changes", []):
            if ch.get("removed"):
                fid = ch.get("fileId")
                if fid:
                    items.append({"ref_id": fid, "version": "", "event": "remove",
                                  "modified_at": _utc_now_iso()})
                continue
            fmeta = ch.get("file") or {}
            fid = fmeta.get("id")
            if not fid:
                continue
            items.append({
                "ref_id": fid,
                "version": str(fmeta.get("version", "")),
                "event": "upsert",
                # NOT NULL: fall back to discovery time when Drive omits it.
                "modified_at": fmeta.get("modifiedTime") or _utc_now_iso(),
            })

        nxt = resp.get("nextPageToken")
        advance_to = nxt or resp.get("newStartPageToken") or page_token
        # THE invariant: this page's rows and this page's cursor, one commit.
        store.enqueue_and_advance(items, source=source, cursor=advance_to)
        enqueued += len(items)

        if not nxt:
            break
        page_token = nxt
    return enqueued
```

Add a module-level `_utc_now_iso()` helper if `drive.py` lacks one:

```python
def _utc_now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
```

- [ ] **Step 4: Implement `handle_drive_item`**

```python
def handle_drive_item(service, store, item, *, folder_cache=None,
                      bulk_section=None, report=None) -> None:
    """Work one queued Drive item. Raises on failure so the loop backs it off."""
    from contextlib import nullcontext
    bulk_section = bulk_section or nullcontext
    fid = item["ref_id"]
    if item["event"] == "remove":
        with bulk_section():
            doc_ids = store.doc_ids_for_file(fid)
            if doc_ids:
                store.invalidate_local_relations_for_docs(doc_ids)
                store.delete_chunks(doc_ids)
        return
    fmeta = service.files().get(
        fileId=fid, supportsAllDrives=True,
        fields="id,name,mimeType,modifiedTime,version,parents").execute(
            num_retries=_NUM_RETRIES)
    content = fetch_content(service, fmeta, store=store, report=report)
    if content is None or (not content.text and not content.tables):
        return                      # unsupported/empty: done, nothing to write
    folder = folder_path(service, fmeta, folder_cache if folder_cache is not None else {})
    with bulk_section():
        chunks = normalise_drive(fmeta, content.text, tables=content.tables,
                                 folder=folder)
        upsert_file_chunks(store, chunks, file_id=fid, partial=content.partial)
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_drive_discovery.py -q`
Expected: ALL PASS.

- [ ] **Step 6: Commit**

```bash
uv run ruff check mcpbrain/
git add mcpbrain/sync/drive.py tests/test_drive_discovery.py
git commit -m "feat(sync): Drive discovery enqueues and advances per page"
```

---

### Task 6: Gmail discovery

**Files:**
- Modify: `mcpbrain/sync/gmail.py` — replace `sync_gmail`'s body (~line 30-200)
- Test: `tests/test_gmail_discovery.py` (create)

**Interfaces:**
- Consumes: `Store.enqueue_and_advance`.
- Produces:
  - `discover_gmail(service, store, source="gmail", *, budget=None) -> int`
  - `handle_gmail_item(service, store, item, *, bulk_section=None) -> None`

Gmail's `history().list` exposes no per-message mtime, so `modified_at` is the discovery timestamp. This is why the column is NOT NULL with a discovery-time fallback rather than nullable with a `COALESCE` sort — a plain index instead of an expression index.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_gmail_discovery.py
"""Gmail discovery: same per-page cursor advance as Drive.

gmail.py's old docstring described this exact livelock in its own words -- a
budget covering fewer messages than the delta contains meant messages were
"PERMANENTLY never ingested". Gmail stayed healthy only because its daily delta
is small; the shape was identical.
"""
from mcpbrain.store import Store
from mcpbrain.sync.gmail import discover_gmail

PAGES = 3


class _Req:
    def __init__(self, r): self._r = r
    def execute(self, num_retries=0): return self._r


class _History:
    def __init__(self, svc): self._svc = svc

    def list(self, **kw):
        tok = kw.get("pageToken")
        self._svc.pages.append(tok)
        i = 1 if tok is None else int(tok)
        body = {"history": [{"messagesAdded": [{"message": {"id": f"m{i}"}}]}],
                "historyId": str(1000 + i)}
        if i < PAGES:
            body["nextPageToken"] = str(i + 1)
        return _Req(body)


class _Users:
    def __init__(self, svc): self._svc = svc
    def history(self): return _History(self._svc)
    def getProfile(self, userId=None): return _Req({"historyId": "1000"})


class _Service:
    def __init__(self): self.pages = []
    def users(self): return _Users(self)


class _OneShotBudget:
    def __init__(self, allow=1): self._left = allow
    def expired(self):
        if self._left > 0:
            self._left -= 1
            return False
        return True


def _store(tmp_path):
    s = Store(tmp_path / "g.sqlite3", dim=4)
    s.init()
    return s


def test_gmail_discovery_advances_per_page(tmp_path):
    s, svc = _store(tmp_path), _Service()
    s.set_cursor("gmail", "1000")
    discover_gmail(svc, s, budget=_OneShotBudget(1))
    assert s.sync_queue_pending("gmail") == 1
    assert s.get_cursor("gmail") != "1000"


def test_gmail_discovery_does_not_rewalk(tmp_path):
    s, svc = _store(tmp_path), _Service()
    s.set_cursor("gmail", "1000")
    for _ in range(3):
        discover_gmail(svc, s, budget=_OneShotBudget(1))
    assert svc.pages.count(None) == 1, f"first page re-walked: {svc.pages}"


def test_gmail_messages_are_enqueued_as_upserts(tmp_path):
    s, svc = _store(tmp_path), _Service()
    s.set_cursor("gmail", "1000")
    for _ in range(PAGES + 1):
        discover_gmail(svc, s)
    rows = s.due_sync_items(limit=10, now="2099-01-01T00:00:00")
    assert {r["ref_id"] for r in rows} == {"m1", "m2", "m3"}
    assert all(r["event"] == "upsert" for r in rows)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_gmail_discovery.py -q`
Expected: FAIL — `cannot import name 'discover_gmail'`

- [ ] **Step 3: Implement**

```python
def discover_gmail(service, store, source: str = "gmail", *, budget=None) -> int:
    """Page history().list, enqueue message ids, advance the cursor per page.

    modified_at is the DISCOVERY time: the history API exposes no per-message
    mtime, and a messageAdded event is new mail by definition, so discovery
    order is chronological order.
    """
    cursor = store.get_cursor(source)
    if cursor is None:
        hid = service.users().getProfile(
            userId="me").execute(num_retries=_NUM_RETRIES)["historyId"]
        store.set_cursor(source, str(hid))
        return 0

    enqueued = 0
    page_token = None
    latest = cursor
    while True:
        if budget is not None and budget.expired():
            break
        kwargs = {"userId": "me", "startHistoryId": cursor,
                  "historyTypes": ["messageAdded"]}
        if page_token is not None:
            kwargs["pageToken"] = page_token
        try:
            resp = service.users().history().list(
                **kwargs).execute(num_retries=_NUM_RETRIES)
        except HttpError as e:
            if getattr(e, "resp", None) is not None and e.resp.status in (404, 410):
                # historyId too old: reset to head and let backfill cover the gap.
                hid = service.users().getProfile(
                    userId="me").execute(num_retries=_NUM_RETRIES)["historyId"]
                store.set_cursor(source, str(hid))
                return enqueued
            raise

        latest = resp.get("historyId", latest)
        items = []
        for record in resp.get("history", []):
            for added in record.get("messagesAdded", []):
                mid = (added.get("message") or {}).get("id")
                if mid:
                    items.append({"ref_id": mid, "version": "", "event": "upsert",
                                  "modified_at": _utc_now_iso()})

        nxt = resp.get("nextPageToken")
        store.enqueue_and_advance(items, source=source,
                                  cursor=(nxt and cursor) or str(latest))
        enqueued += len(items)
        if nxt is None:
            break
        page_token = nxt
    return enqueued
```

Note the cursor value: Gmail's `startHistoryId` must stay fixed while paging a single delta (unlike Drive's rolling page token), so mid-feed pages re-commit the SAME cursor and only the final page advances it to `latest`. The rows are still committed per page, so a cutoff loses no work — it only re-lists pages, which is cheap.

Add `_utc_now_iso()` to `gmail.py` if absent (same body as Task 5).

- [ ] **Step 4: Implement `handle_gmail_item`**

Reuse the existing per-message fetch/normalise/upsert code that `sync_gmail` used, extracted verbatim into:

```python
def handle_gmail_item(service, store, item, *, fetch_attachments: bool = False,
                      bulk_section=None) -> None:
    """Work one queued Gmail message. Raises on failure so the loop backs off.

    Copied from the real per-message body of the current sync_gmail (its
    `for mid in new_message_ids:` loop) -- verbatim, not invented: normalise_gmail
    takes a `report` dict and writes chunk-by-chunk via store.upsert_chunk
    (singular), and attachments are a SEPARATE fetch (attachments.fetch_and_normalise)
    hoisted OUTSIDE bulk_section because it is network I/O, not a store write.

    A 404 means the message was deleted between discovery and now: that is
    DONE, not a failure -- returning (rather than raising) deletes the row.

    `fetch_attachments` must be read ONCE (config.gmail_attachments(home)) and
    passed in by the Task 8 wiring, not re-read per call -- config.read_config
    does an uncached exists()+read_text()+json.loads() per call, and paying
    that once per message is the exact overhead class the 0.7.105 fix removed
    from metadata queries.
    """
    from contextlib import nullcontext
    from mcpbrain.sync import attachments
    bulk_section = bulk_section or nullcontext
    try:
        raw = service.users().messages().get(
            userId="me", id=item["ref_id"], format="full").execute(
                num_retries=_NUM_RETRIES)
    except HttpError as e:
        if getattr(e, "resp", None) is not None and e.resp.status == 404:
            return
        raise
    skips: dict = {}
    att_chunks = (attachments.fetch_and_normalise(service, raw, store=store)
                  if fetch_attachments else [])
    with bulk_section():
        for chunk in normalise_gmail(raw, report=skips):
            store.upsert_chunk(chunk.doc_id, chunk.text, chunk.content_hash,
                               chunk.metadata)
        for chunk in att_chunks:
            store.upsert_chunk(chunk.doc_id, chunk.text, chunk.content_hash,
                               chunk.metadata)
```

This is copied from `mcpbrain/sync/gmail.py`'s current per-message loop (~line 190-225)
— read it first and confirm the signatures match before writing; the current file is
the source of truth if it has since changed.

- [ ] **Step 5: Run tests and commit**

Run: `uv run pytest tests/test_gmail_discovery.py -q`
Expected: ALL PASS.

```bash
uv run ruff check mcpbrain/
git add mcpbrain/sync/gmail.py tests/test_gmail_discovery.py
git commit -m "feat(sync): Gmail discovery enqueues and advances per page"
```

---

### Task 7: Calendar discovery

**Files:**
- Modify: `mcpbrain/sync/calendar.py` — replace `sync_calendar`'s body (~line 375-479)
- Test: `tests/test_calendar_discovery.py` (create)

**Interfaces:**
- Consumes: `Store.enqueue_and_advance`.
- Produces:
  - `discover_calendar(service, store, source="calendar", calendar_id="primary", time_min=None, time_max=None, *, budget=None, bulk_section=None) -> int`
  - `handle_calendar_item(service, store, item, *, calendar_id="primary", bulk_section=None) -> None`

**Calendar is structurally different from Drive/Gmail — read this before writing code.**
`calendar.py` already has a private helper, `_list_events(service, calendar_id,
sync_token, time_min, time_max, *, budget=None) -> (items, next_sync, interrupted)`,
that pages `events().list()` to completion (or budget expiry) ENTIRELY IN MEMORY — it
performs no store writes. Reuse it; do not reimplement raw pagination. There is no
separate bootstrap helper — `sync_calendar` calls `_list_events(..., None, ...)` for
the initial full fetch and `_list_events(..., cursor, ...)` for a delta, in the same
function.

Because `_list_events` returns only once (after completion or interruption) rather than
yielding per page, the per-page cursor-advance used in Tasks 5-6 does not map onto it
without changing that helper's shape — out of scope for this task. Instead: **advance
the cursor only when `_list_events` reports `interrupted=False`; enqueue whatever it
returned regardless.** This is still strictly better than today (a full ROUND — listing
AND writing AND graph updates — had to complete for the cursor to move; now only
LISTING has to). If `_list_events` is interrupted, its rows are already durably
enqueued and the next discovery call re-lists from the same `syncToken` — a wasted
re-list, never a re-work. Given `DISCOVERY_BUDGET_S` (15s, listing-only) is generous
relative to Calendar's historically small deltas (its own cursor advanced daily with no
livelock reported), this is a deliberate, bounded trade — not a defect.

**`delete_calendar_chunks_after(time_max)` moves into `discover_calendar`**, called
once per discovery pass, still bracketed by `bulk_section()`. This is a chunk-mutating
call, a deliberate exception to "discovery never touches chunks": it is idempotent
window-hygiene (evicting recurring-event expansions past the forward horizon), not
per-item extraction, and was already unconditional in the current code.

**Event identity uses `ev.get("updated", "")` as `version`**, matching the existing
`_event_resume_key(ev)` helper's id+`updated` composite — NOT `etag`. This is not a
style choice: an adversarial review found a Critical bug from keying on bare id (a
rescheduled event's old text survived a round close), and `updated` is the
proven-correct field for that fix.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_calendar_discovery.py
"""Calendar discovery reuses _list_events (which already pages to completion in
memory) and advances the cursor only when it completes uninterrupted. Rows are
enqueued regardless, so an interruption never loses work -- only re-lists."""
from mcpbrain.store import Store
from mcpbrain.sync.calendar import discover_calendar


class _Req:
    def __init__(self, r): self._r = r
    def execute(self, num_retries=0): return self._r


class _Events:
    """One page of 3 events, syncToken on request."""
    def __init__(self, svc): self._svc = svc

    def list(self, **kw):
        self._svc.calls.append(kw.get("pageToken"))
        return _Req({
            "items": [
                {"id": "e1", "status": "confirmed", "updated": "2026-09-01T00:00:00Z"},
                {"id": "e2", "status": "confirmed", "updated": "2026-09-02T00:00:00Z"},
                {"id": "e3", "status": "cancelled", "updated": "2026-09-03T00:00:00Z"},
            ],
            "nextSyncToken": "SYNCED",
        })


class _Service:
    def __init__(self): self.calls = []
    def events(self): return _Events(self)


def _store(tmp_path):
    s = Store(tmp_path / "c.sqlite3", dim=4)
    s.init()
    return s


def test_calendar_discovery_enqueues_and_advances_when_uninterrupted(tmp_path):
    s, svc = _store(tmp_path), _Service()
    s.set_cursor("calendar", "TOK0")
    n = discover_calendar(svc, s)
    assert n == 3
    assert s.sync_queue_pending("calendar") == 3
    assert s.get_cursor("calendar") == "SYNCED"


def test_cancelled_event_is_a_remove(tmp_path):
    s, svc = _store(tmp_path), _Service()
    s.set_cursor("calendar", "TOK0")
    discover_calendar(svc, s)
    rows = {r["ref_id"]: r for r in s.due_sync_items(limit=10, now="2099-01-01T00:00:00")}
    assert rows["e3"]["event"] == "remove"
    assert rows["e1"]["event"] == "upsert"


def test_version_is_the_updated_field_not_etag(tmp_path):
    """The proven-correct dedup key from _event_resume_key: id + updated."""
    s, svc = _store(tmp_path), _Service()
    s.set_cursor("calendar", "TOK0")
    discover_calendar(svc, s)
    rows = {r["ref_id"]: r for r in s.due_sync_items(limit=10, now="2099-01-01T00:00:00")}
    assert rows["e1"]["version"] == "2026-09-01T00:00:00Z"


def test_interrupted_call_enqueues_but_does_not_advance_cursor(tmp_path, monkeypatch):
    import mcpbrain.sync.calendar as cal
    s, svc = _store(tmp_path), _Service()
    s.set_cursor("calendar", "TOK0")

    def fake_list_events(service, calendar_id, sync_token, time_min, time_max, *,
                         budget=None):
        return ([{"id": "e1", "status": "confirmed",
                  "updated": "2026-09-01T00:00:00Z"}], None, True)

    monkeypatch.setattr(cal, "_list_events", fake_list_events)
    discover_calendar(svc, s)
    assert s.sync_queue_pending("calendar") == 1, "interrupted call must still enqueue"
    assert s.get_cursor("calendar") == "TOK0", "cursor must not advance when interrupted"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_calendar_discovery.py -q`
Expected: FAIL — `cannot import name 'discover_calendar'`

- [ ] **Step 3: Implement `discover_calendar`**

```python
def discover_calendar(service, store, source: str = "calendar",
                      calendar_id: str = "primary", time_min: str | None = None,
                      time_max: str | None = None, *, budget=None,
                      bulk_section=None) -> int:
    """List calendar events via _list_events and enqueue them.

    _list_events already pages events().list() to completion (or budget
    expiry) entirely in memory -- reused here rather than reimplemented.
    Rows are enqueued whether or not the list completed; the cursor advances
    to next_sync ONLY when it did (interrupted=False), because Google emits
    nextSyncToken only on the final page. An interrupted call costs a re-list
    next cycle, never a re-work -- what was enqueued here is already durable.
    """
    from contextlib import nullcontext
    bulk_section = bulk_section or nullcontext
    cursor = store.get_cursor(source)
    now = datetime.now(timezone.utc)
    if time_min is None:
        time_min = (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    if time_max is None:
        time_max = (now + timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Window-hygiene eviction: idempotent, not per-item, so it belongs in
    # discovery even though it mutates chunks -- see the task docstring.
    with bulk_section():
        store.delete_calendar_chunks_after(time_max)

    try:
        items, next_sync, interrupted = _list_events(
            service, calendar_id, cursor, time_min, time_max, budget=budget)
    except HttpError as e:
        resp = getattr(e, "resp", None)
        if resp is not None and resp.status == 410:
            items, next_sync, interrupted = _list_events(
                service, calendar_id, None, time_min, time_max, budget=budget)
        else:
            raise

    queue_items = []
    for ev in items:
        eid = ev.get("id")
        if not eid:
            continue
        queue_items.append({
            "ref_id": eid,
            "version": ev.get("updated", ""),
            "event": "remove" if ev.get("status") == "cancelled" else "upsert",
            "modified_at": ev.get("updated") or _utc_now_iso(),
        })

    if next_sync and not interrupted:
        store.enqueue_and_advance(queue_items, source=source, cursor=next_sync)
    else:
        # Enqueue without moving the cursor: durable partial progress, and the
        # next call re-lists from the SAME cursor (cheap: listing only).
        store.enqueue_and_advance(queue_items, source=source, cursor=cursor)
    return len(queue_items)
```

Add `_utc_now_iso()` to `calendar.py` if it lacks one (same body as Task 5).

- [ ] **Step 4: Implement `handle_calendar_item`**

Extracted from the real per-event body of the current `sync_calendar` (its `for ev in
items:` loop) — read that section (`mcpbrain/sync/calendar.py`, inside `sync_calendar`,
after the resume-set setup) before writing this, and copy its calls verbatim:

```python
def handle_calendar_item(service, store, item, *, calendar_id: str = "primary",
                         bulk_section=None) -> None:
    """Work one queued calendar event. Raises on failure so the loop backs it off."""
    from contextlib import nullcontext
    bulk_section = bulk_section or nullcontext
    if item["event"] == "remove":
        with bulk_section():
            store.delete_calendar_chunks_for_event(item["ref_id"])  # verify this
        return                                                       # exact name
    ev = service.events().get(calendarId=calendar_id,
                              eventId=item["ref_id"]).execute(num_retries=_NUM_RETRIES)
    owner = owner_identity_from_config()
    with bulk_section():
        for chunk in normalise_calendar(ev):
            store.upsert_chunk(chunk.doc_id, chunk.text, chunk.content_hash,
                               chunk.metadata)
        _apply_attendees_to_graph(store, ev, owner)
        _annotate_series_from_event(store, ev, owner)
```

Before writing `handle_calendar_item`'s removal branch, grep `calendar.py` and
`store.py` for the actual function that deletes ONE event's chunks — the current
`sync_calendar` handles removal only implicitly via `delete_calendar_chunks_after`'s
window sweep, so there may be no existing per-event delete. If none exists, use
`store.doc_ids_for_event(item["ref_id"])` (or the equivalent lookup — check
`store.py` for what Drive's `doc_ids_for_file` mirrors for calendar) plus
`store.delete_chunks(doc_ids)`, matching Task 5's Drive removal pattern.

- [ ] **Step 5: Run tests and commit**

Run: `uv run pytest tests/test_calendar_discovery.py -q`
Expected: ALL PASS.

```bash
uv run ruff check mcpbrain/
git add mcpbrain/sync/calendar.py tests/test_calendar_discovery.py
git commit -m "feat(sync): Calendar discovery via _list_events, advancing on completion"
```

### Task 8: Wire discovery + work into the cycle

**Files:**
- Modify: `mcpbrain/sync/__init__.py` — `run_sync_cycle` (lines 22-160)
- Modify: `mcpbrain/config.py` — add `sync_work_limit`
- Modify: `mcpbrain/daemon.py` — add `DISCOVERY_BUDGET_S`
- Test: `tests/test_sync_cycle.py` (extend)

**Interfaces:**
- Consumes: `discover_drive`/`discover_gmail`/`discover_calendar`, `handle_*_item`, `work_queue`.
- Produces: `run_sync_cycle` result gains `{"discovered": {source: int}, "worked": {"processed": int, "failed": int}}`.

- [ ] **Step 1: Add the config accessor**

In `mcpbrain/config.py`, beside `spool_thread_cap`:

```python
def sync_work_limit(home) -> int:
    """Items the sync work loop handles per cycle (config 'sync_work_limit',
    default 50).

    This replaces the wall-clock budgets as the real operator knob. "How many
    items" has an obvious meaning; "how many seconds of mixed-cost work" does
    not, when a unit spans a microsecond mime-skip to a 60-second OCR PDF.
    """
    try:
        return max(1, int(read_config(home).get("sync_work_limit", 50)))
    except (TypeError, ValueError):
        return 50
```

- [ ] **Step 2: Add the discovery budget constant**

In `mcpbrain/daemon.py`, beside `CYCLE_BUDGET_S`:

```python
# Discovery's slice of CYCLE_BUDGET_S. Discovery only LISTS (no fetch), so it
# is cheap and normally finishes well inside this; the slice exists so a large
# work queue can never starve discovery of new changes. Unlike the budgets this
# design replaces, it is a fairness knob, not a correctness mechanism -- a
# cutoff on either side of it now costs nothing.
DISCOVERY_BUDGET_S = 15.0
```

- [ ] **Step 3: Write the failing test**

```python
# append to tests/test_sync_cycle.py
def test_cycle_discovers_then_works(tmp_path, monkeypatch):
    """run_sync_cycle must run discovery first, then drain the queue."""
    from mcpbrain import sync as sync_mod
    from mcpbrain.store import Store
    s = Store(tmp_path / "c.sqlite3", dim=4)
    s.init()
    order = []

    def fake_discover(service, store, source="drive", *, budget=None):
        order.append("discover")
        store.enqueue_and_advance(
            [{"ref_id": "f1", "version": "1", "event": "upsert",
              "modified_at": "2026-09-04T00:00:00"}], source="drive", cursor="2")
        return 1

    monkeypatch.setattr(sync_mod, "discover_drive", fake_discover)
    monkeypatch.setattr(sync_mod, "handle_drive_item",
                        lambda *a, **k: order.append("work"))
    out = sync_mod.run_sync_cycle(s, embedder=None, drive_service=object(),
                                  home=str(tmp_path))
    assert order == ["discover", "work"]
    assert out["worked"]["processed"] == 1
    assert s.sync_queue_pending() == 0
```

- [ ] **Step 4: Run to verify it fails**

Run: `uv run pytest tests/test_sync_cycle.py::test_cycle_discovers_then_works -q`
Expected: FAIL — `run_sync_cycle` has no `worked` key.

- [ ] **Step 5: Rewire `run_sync_cycle`**

Replace the three `sync_*` call sites (lines 95, 102, 110) with discovery calls bounded by `DISCOVERY_BUDGET_S`, then a single `work_queue` call after them:

```python
    from mcpbrain.budget import Budget
    from mcpbrain.daemon import DISCOVERY_BUDGET_S
    from mcpbrain.sync.queue import work_queue

    discovered = {}
    disc_budget = Budget(DISCOVERY_BUDGET_S)
    if gmail_service is not None:
        discovered["gmail"] = discover_gmail(gmail_service, store, budget=disc_budget)
    if calendar_service is not None:
        discovered["calendar"] = discover_calendar(calendar_service, store,
                                                   budget=disc_budget)
    if drive_service is not None:
        discovered["drive"] = discover_drive(drive_service, store, budget=disc_budget)
    result["discovered"] = discovered

    # folder_cache and fetch_attachments are hoisted ONCE per cycle and closed
    # over below -- NOT rebuilt per item. folder_path's own docstring says its
    # cache is "owned by the CALLER for a whole sync round" (5,000 files in 40
    # folders costs 40 lookups, not 5,000); gmail's fetch_attachments flag has
    # the identical per-call-config-read cost the 0.7.105 fix removed
    # elsewhere. Building either fresh inside the lambda would silently defeat
    # them -- a lambda called once per item would rebuild the "cache" on every
    # call.
    folder_cache: dict = {}
    fetch_attachments = config.gmail_attachments(home) if home else False

    handlers = {}
    if drive_service is not None:
        handlers["drive"] = lambda it: handle_drive_item(
            drive_service, store, it, folder_cache=folder_cache,
            bulk_section=bulk_section)
    if gmail_service is not None:
        handlers["gmail"] = lambda it: handle_gmail_item(
            gmail_service, store, it, fetch_attachments=fetch_attachments,
            bulk_section=bulk_section)
    if calendar_service is not None:
        handlers["calendar"] = lambda it: handle_calendar_item(
            calendar_service, store, it, bulk_section=bulk_section)
    result["worked"] = work_queue(
        store, handlers=handlers,
        limit=config.sync_work_limit(home) if home else 50,
        budget=budget)
```

Keep the existing `index_pending` call after this block so new chunks are embedded in the same cycle.

- [ ] **Step 6: Run the sync-cycle tests**

Run: `uv run pytest tests/test_sync_cycle.py tests/test_sync_queue_worker.py -q`
Expected: ALL PASS. Fix any test that asserted on the removed `result["drive"]`/`["gmail"]`/`["calendar"]` integer counts — they are now under `result["discovered"]`.

- [ ] **Step 7: Commit**

```bash
uv run ruff check mcpbrain/
git add mcpbrain/sync/__init__.py mcpbrain/config.py mcpbrain/daemon.py tests/test_sync_cycle.py
git commit -m "feat(sync): run discovery then the shared work loop each cycle"
```

---

### Task 9: Delete the dead machinery and surface the backlog

**Files:**
- Modify: `mcpbrain/sync/drive.py`, `mcpbrain/sync/gmail.py`, `mcpbrain/sync/calendar.py` (delete old round code)
- Modify: `mcpbrain/store.py` (one-shot cursor cleanup in `init()`)
- Modify: `mcpbrain/doctor.py` (~line 473, after the re-chunk line)
- Delete: `tests/test_drive_paging_resume.py` (its subject no longer exists)
- Test: `tests/test_sync_queue_cleanup.py` (create), `tests/test_doctor.py` (extend)

**Interfaces:**
- Consumes: `Store.sync_queue_stats` (Task 3).
- Produces: nothing new.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_sync_queue_cleanup.py
"""The obsolete per-round cursor state is removed on upgrade.

resume_ids existed only to remember which files a round had written; rounds no
longer exist. page_token (0.7.123) existed because the cursor could not advance
per page; now it does.
"""
from mcpbrain.store import Store


def test_init_clears_obsolete_round_state(tmp_path):
    s = Store(tmp_path / "m.sqlite3", dim=4)
    s.init()
    s.set_cursor("drive", "336793")
    s.set_cursor("drive:resume_ids", '["a","b"]')
    s.set_cursor("drive:resume_removed_ids", '["c"]')
    s.set_cursor("drive:page_token", "348561")
    s.set_cursor("gmail:resume_ids", '["m1"]')
    s.init()          # upgrade path: init runs again on an existing store
    assert s.get_cursor("drive") == "336793", "the real cursor must survive"
    for dead in ("drive:resume_ids", "drive:resume_removed_ids",
                 "drive:page_token", "gmail:resume_ids"):
        assert s.get_cursor(dead) is None, f"{dead} not cleaned up"
```

```python
# append to tests/test_doctor.py
def test_doctor_reports_sync_backlog_and_failures(tmp_path):
    from mcpbrain.store import Store
    from mcpbrain import doctor
    s = Store(tmp_path / "brain.sqlite3", dim=4)
    s.init()
    s.enqueue_and_advance(
        [{"ref_id": "f1", "version": "1", "event": "upsert",
          "modified_at": "2026-09-04T00:00:00"}], source="drive", cursor="1")
    for _ in range(3):
        s.fail_sync_item("drive", "f1", "export timeout", now="2026-09-04T10:00:00")
    text = "\n".join(doctor.store_lines(str(tmp_path)))
    assert "Sync queue" in text and "1 pending" in text
    assert "Sync failures" in text and "export timeout" in text
```

Read `mcpbrain/doctor.py` for the ACTUAL name of the function that builds these lines (the block near line 454 that opens a read-only `Store`) and call that, not `store_lines`, if it differs.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_sync_queue_cleanup.py -q`
Expected: FAIL — the dead cursors survive `init()`.

- [ ] **Step 3: Add the one-shot cleanup in `init()`**

At the end of `init()`, after the table/index block:

```python
            # One-shot: drop per-round state the queue replaces. resume_ids
            # tracked which files a ROUND had written; rounds no longer exist.
            # page_token (0.7.123) existed because the cursor could not advance
            # per page; it now does. The real cursors are untouched.
            db.execute("DELETE FROM sync_cursors WHERE source LIKE '%:resume_ids' "
                       "OR source LIKE '%:resume_removed_ids' "
                       "OR source LIKE '%:page_token'")
```

- [ ] **Step 4: Add the doctor lines**

In `mcpbrain/doctor.py`, after the "Items awaiting re-chunk" line:

```python
    try:
        q = store.sync_queue_stats()
        if q["pending"]:
            lines.append(f"⚠️ {'Sync queue':<16} {q['pending']:,} pending "
                         f"(oldest discovered {q['oldest_discovered_at']})")
        else:
            lines.append(f"✅ {'Sync queue':<16} 0 pending")
        if q["failing"]:
            detail = ", ".join(f"{f['ref_id']} ({f['attempts']} attempts: "
                               f"{f['last_error']})" for f in q["failing"])
            lines.append(f"⚠️ {'Sync failures':<16} "
                         f"{len(q['failing'])} items retrying — {detail}")
    except Exception as exc:  # noqa: BLE001 — never fatal
        lines.append(f"➖ {'Sync queue':<16} skipped ({exc})")
```

The oldest-`discovered_at` figure is deliberate: it exposes the tail-starvation risk accepted with newest-first ordering. A backlog that is not shrinking shows as an age that climbs.

- [ ] **Step 5: Delete the superseded code**

Remove from `mcpbrain/sync/drive.py`, `gmail.py`, `calendar.py`:
- the old `sync_drive` / `sync_gmail` / `sync_calendar` round bodies and `sync_shared_drive`'s delta loop
- every `resume_ids` / `resume_removed_ids` / `page_token` read and write
- both `"Minimum forward progress"` guards
- the `if not pagination_interrupted:` branch
- the `KNOWN GAP` comment (the gap is closed by Task 3's retry)
- `gmail.py`'s ~30-line docstring paragraph explaining its livelock workaround

Then `git rm tests/test_drive_paging_resume.py` — it tests a mechanism that no longer exists; Tasks 5-7 cover the replacement.

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest tests/ -q`
Expected: ALL PASS. Any failure here is a caller still using a deleted function — fix the caller, do not restore the function.

- [ ] **Step 7: Commit**

```bash
uv run ruff check mcpbrain/
git add -A
git commit -m "refactor(sync): delete per-round state; surface the backlog in doctor"
```

---

### Task 10: Live validation (ATTENDED — do not automate)

**Files:** none modified. This task produces evidence.

**Interfaces:** consumes the whole system.

This task is run by a human at a terminal, in order. Do not skip a step; do not run it unattended.

- [ ] **Step 1: Verify a fresh backup exists BEFORE anything**

```bash
cd /tmp && ~/.local/bin/mcpbrain doctor | grep -i backup
```
Expected: `✅ Backup  On` with a recent verification. If the last backup is stale, run one and wait. **Do not proceed without this** — rollback here is restore-from-backup, not a code revert.

- [ ] **Step 2: Record the gold baseline**

```bash
cd ~/GitHub/mcpbrain && uv run python bin/eval_recall.py --gold
```
Record recall@10 and MRR. The floor is **recall@10 ≥ 0.780, MRR ≥ 0.550**. Check `bin/` for the harness's actual filename if this one does not exist.

- [ ] **Step 3: Dry-run discovery against the real feed in a THROWAWAY store**

```python
# /tmp/verify_discovery.py
import sys; sys.path.insert(0, "/Users/joshkemp/GitHub/mcpbrain")
from mcpbrain import auth, config
from mcpbrain.store import Store
from mcpbrain.sync.drive import discover_drive
from mcpbrain.budget import Budget

svc = auth.build_google_services()["drive_service"]      # NO argument
live = Store(config.store_path(), dim=384, read_only=True)
real = live.get_cursor("drive")
print("live cursor:", real)

s = Store("/tmp/verify-queue.sqlite3", dim=384); s.init()
s.set_cursor("drive", real)
for i in range(1, 9):
    n = discover_drive(svc, s, budget=Budget(15.0))
    print(f"round {i}: +{n} enqueued, cursor={s.get_cursor('drive')}, "
          f"pending={s.sync_queue_pending('drive')}")
```

Run: `cd ~/GitHub/mcpbrain && uv run python /tmp/verify_discovery.py`
Expected: the cursor advances EVERY round and pending climbs into the thousands. If the cursor ever repeats between rounds, stop — discovery is not advancing per page.

- [ ] **Step 4: Clean up the throwaway store**

```bash
rm -f /tmp/verify-queue.sqlite3*
```

- [ ] **Step 5: Install and restart**

```bash
launchctl bootout gui/$(id -u)/com.mcpbrain
launchctl bootout gui/$(id -u)/com.mcpbrain.tray
cd /tmp && uv tool install --python 3.12 --force ~/GitHub/mcpbrain --with-editable ""
```
Use the project's standard local reinstall instead if it differs — it MUST include the `[daemon]` extra (`uv tool install --force ".[daemon]"`), or the embedder breaks and recall returns empty. Then clear stale bytecode and restart:

```bash
SP=$(ls -d ~/.local/share/uv/tools/mcpbrain/lib/python*/site-packages)
find "$SP/mcpbrain" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.mcpbrain.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.mcpbrain.tray.plist
```

- [ ] **Step 6: Verify against the RUNNING process, not the files**

```bash
cd /tmp && ~/.local/bin/mcpbrain doctor
```
Expected: `Sync queue N pending` appears. Confirm the daemon version via the control API, not by importing from the repo — importing from inside `~/GitHub/mcpbrain` loads the working tree, not site-packages, and proves nothing.

- [ ] **Step 7: Watch the queue actually drain**

```bash
cd /tmp && for i in $(seq 1 10); do python3 -c "
import sqlite3
c=sqlite3.connect('file:$HOME/Library/Application Support/mcpbrain/brain.sqlite3?mode=ro',uri=True)
c.execute('pragma busy_timeout=8000')
print(c.execute('select count(*) from sync_queue').fetchone()[0], 'pending')
"; sleep 60; done
```
Expected: pending rises while discovery runs, then falls steadily. The `drive` cursor should reach the feed head within a few cycles.

- [ ] **Step 8: Re-measure gold**

Run the Step 2 command again. **recall@10 ≥ 0.780 and MRR ≥ 0.550.** If either regresses below the floor, stop and investigate before going further.

- [ ] **Step 9: Record the outcome in CLAUDE.md**

Add a "Current state" entry under `## Shipping caveats` covering: what shipped, the before/after gold numbers, the queue depth observed, and any surprise. Follow the existing entries' style.

```bash
git add CLAUDE.md && git commit -m "docs: record the sync-queue migration and its live validation"
```

---

## Notes for the implementer

- **Read the spec first.** `docs/superpowers/specs/2026-09-04-sync-queue-design.md` explains *why* the cursor advances per page and why the queue may never leave the SQLite file. Both are load-bearing.
- **The tests in Tasks 5-7 are the regression suite for a five-week production outage.** If one fails, the livelock is back. Do not weaken an assertion to make it pass.
- **Do not add a lease/claim column** without a second worker actually existing. The spec explains why it would be speculative.
- **`store.py` has no logger.** Return counts; let callers log.
