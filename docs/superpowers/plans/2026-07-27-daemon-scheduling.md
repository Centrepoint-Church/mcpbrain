# Daemon Scheduling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop periodic maintenance from being starved behind unbounded bulk sync, and make a stalled daemon recover itself.

**Architecture:** Two independent timers in one process. The existing cycle loop keeps doing bulk work but every phase is deadline-bounded and resumable; a new scheduler thread owns the ~20 cadence passes and runs them on their own schedule. Three shared-state hazards that were safe only by accident of single-threading are closed first. A per-phase progress heartbeat drives a watchdog that self-heals via platform-appropriate restart.

**Tech Stack:** Python 3.12, SQLite (WAL) via stdlib `sqlite3`, `threading`, fastembed/onnxruntime, launchd (macOS) / schtasks (Windows).

**Spec:** `docs/superpowers/specs/2026-07-27-daemon-scheduling-design.md`

## Global Constraints

- Python 3.12; no new third-party dependencies. APScheduler is explicitly rejected (see spec § Rejected alternatives).
- Preserve the existing `_is_due` / injectable `_clock` cadence machinery — every cadence test drives it.
- All store writes stay reversible in style: no destructive migration in this plan.
- Never break the pause guarantee: when `_pause` is set, nothing writes to the store.
- Run tests scoped to edited + directly impacted files. Josh runs the full suite himself.
- `uv run pytest <paths> -q` to test; `uv run ruff check mcpbrain/` must be clean before each commit.
- Work directly on `main`, commit per task. Do not release (that is a separate explicit step).

---

### Task 1: SQLite write-transaction correctness

Foundation for everything else: with two writer threads, DEFERRED transactions
that read-then-write fail *immediately* with `SQLITE_BUSY` regardless of
`busy_timeout`, because the lock upgrade cannot be granted without breaking
serializability. There are currently zero uses of `BEGIN IMMEDIATE` in the
codebase. This task is independently valuable even without the threading work.

**Files:**
- Modify: `mcpbrain/store.py:76-94` (`_open_db`), `mcpbrain/store.py:121-138` (`_connect`)
- Modify: every `Store` method that writes (see Step 5)
- Test: `tests/test_store_write_txn.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: `Store._connect(write: bool = False)` — a write-mode context manager
  that issues `BEGIN IMMEDIATE` with bounded retry. Tasks 3 and 4 rely on this
  being safe under concurrent writers.

- [ ] **Step 1: Write the failing test**

Create `tests/test_store_write_txn.py`:

```python
"""Write transactions take an IMMEDIATE lock so concurrent writers serialise.

A DEFERRED transaction that reads then writes must upgrade its lock, and SQLite
refuses that upgrade instantly (not after busy_timeout) when another writer
holds the lock. BEGIN IMMEDIATE takes the write lock up front so busy_timeout
actually applies.
"""
import threading

from mcpbrain.store import Store


def _store(tmp_path, name="w.sqlite3"):
    s = Store(tmp_path / name, dim=4)
    s.init()
    return s


def test_write_connect_uses_immediate(tmp_path):
    s = _store(tmp_path)
    with s._connect(write=True) as db:
        # An IMMEDIATE txn is already active, so SQLite reports we are in a txn.
        assert db.in_transaction


def test_read_connect_does_not_hold_write_lock(tmp_path):
    s = _store(tmp_path)
    with s._connect() as db:
        db.execute("SELECT 1").fetchone()
        # A plain read must not have taken a write lock.
        assert not db.in_transaction


def test_concurrent_writers_both_succeed(tmp_path):
    """Two threads doing read-then-write must both complete, not raise."""
    s = _store(tmp_path)
    with s._connect(write=True) as db:
        db.execute("CREATE TABLE IF NOT EXISTS t(k INTEGER PRIMARY KEY, v INTEGER)")
        db.execute("INSERT INTO t(k, v) VALUES (1, 0)")

    errors = []

    def bump():
        try:
            for _ in range(25):
                with s._connect(write=True) as db:
                    cur = db.execute("SELECT v FROM t WHERE k=1").fetchone()
                    db.execute("UPDATE t SET v=? WHERE k=1", (cur["v"] + 1,))
        except Exception as exc:  # noqa: BLE001 — the assertion is "no exception"
            errors.append(exc)

    threads = [threading.Thread(target=bump) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"concurrent writers raised: {errors}"
    with s._connect() as db:
        assert db.execute("SELECT v FROM t WHERE k=1").fetchone()["v"] == 100


def test_write_txn_rolls_back_on_error(tmp_path):
    s = _store(tmp_path)
    with s._connect(write=True) as db:
        db.execute("CREATE TABLE IF NOT EXISTS t2(k INTEGER PRIMARY KEY)")
    try:
        with s._connect(write=True) as db:
            db.execute("INSERT INTO t2(k) VALUES (1)")
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    with s._connect() as db:
        assert db.execute("SELECT COUNT(*) c FROM t2").fetchone()["c"] == 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_store_write_txn.py -q`
Expected: FAIL — `_connect()` takes no `write` keyword (`TypeError`).

- [ ] **Step 3: Add the pragma and the retry helper**

In `mcpbrain/store.py`, inside `_open_db` after the existing `busy_timeout`
line (currently line 90), add:

```python
    db.execute("PRAGMA busy_timeout=5000")
    # Cap WAL growth. A long-lived reader can otherwise prevent checkpointing
    # and the WAL grows without bound; this makes SQLite truncate it back after
    # a checkpoint instead. Chosen well above normal transaction size.
    db.execute("PRAGMA journal_size_limit=67108864")  # 64 MiB
```

Then add, immediately above `class Store` (after `_base_cal_event_id`):

```python
_BEGIN_RETRIES = 6
_BEGIN_BASE_SLEEP_S = 0.05


def _begin_immediate(db, *, retries: int = _BEGIN_RETRIES) -> None:
    """Start a write transaction, retrying with jittered backoff on lock contention.

    BEGIN IMMEDIATE acquires the write lock up front, so a later read-then-write
    never has to upgrade — the upgrade is what SQLite refuses instantly, ignoring
    busy_timeout. busy_timeout still covers most contention; this retry loop is
    the backstop for the window where it does not.
    """
    import random
    import time as _time
    for attempt in range(retries):
        try:
            db.execute("BEGIN IMMEDIATE")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) and "busy" not in str(exc):
                raise
            if attempt == retries - 1:
                raise
            _time.sleep(_BEGIN_BASE_SLEEP_S * (2 ** attempt) * (0.5 + random.random()))
```

- [ ] **Step 4: Add the `write` mode to `_connect`**

Replace the body of `Store._connect` (currently lines 133-138), keeping the
existing docstring above it:

```python
        db = _open_db(self.path, self.read_only)
        try:
            if write and not self.read_only:
                # Manual transaction control: take the write lock up front.
                db.isolation_level = None
                _begin_immediate(db)
                try:
                    yield db
                except BaseException:
                    db.execute("ROLLBACK")
                    raise
                else:
                    db.execute("COMMIT")
            else:
                with db:
                    yield db
        finally:
            db.close()
```

and change the signature to:

```python
    @contextmanager
    def _connect(self, *, write: bool = False):
```

- [ ] **Step 5: Convert the write methods**

Find every method whose `_connect()` block mutates:

```bash
uv run python - <<'PY'
import re, pathlib
src = pathlib.Path("mcpbrain/store.py").read_text().splitlines()
cur = None
hits = set()
for i, line in enumerate(src, 1):
    m = re.match(r"\s*def (\w+)\(", line)
    if m:
        cur = (m.group(1), i)
    if re.search(r"\b(INSERT|UPDATE|DELETE|REPLACE)\b", line) and cur:
        hits.add(cur)
for name, ln in sorted(hits, key=lambda x: x[1]):
    print(f"{ln}\t{name}")
PY
```

For each reported method, change its `with self._connect() as db:` to
`with self._connect(write=True) as db:`. Do **not** change read-only methods —
taking a write lock on reads would serialise the control API behind ingest,
which is the opposite of this plan's goal.

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_store_write_txn.py -q`
Expected: PASS (4 tests).

- [ ] **Step 7: Run the impacted suites and lint**

Run: `uv run pytest tests/ -q -k "store or action or dashboard or consolidate" && uv run ruff check mcpbrain/`
Expected: all pass, ruff clean. These exercise the converted write paths.

- [ ] **Step 8: Commit**

```bash
git add mcpbrain/store.py tests/test_store_write_txn.py
git commit -m "fix(store): BEGIN IMMEDIATE for write transactions

Python's sqlite3 uses DEFERRED, so a read-then-write transaction must upgrade
its lock — and SQLite refuses that upgrade instantly, regardless of
busy_timeout. With a second writer thread arriving in a later task that becomes
reachable. Adds a write-mode _connect that takes the write lock up front with
jittered retry, plus journal_size_limit to cap WAL growth."
```

---

### Task 2: Bound the cycle

Make `run_one()` return promptly. `index_pending` currently fetches the entire
unembedded set with no LIMIT and embeds all of it; a 61,580-chunk backlog took
~1.9 h, and `run_sync_cycle` calls it six times.

**Files:**
- Create: `mcpbrain/budget.py`
- Modify: `mcpbrain/store.py:1236-1249` (`unembedded_chunks`)
- Modify: `mcpbrain/index.py:9-43` (`index_pending`)
- Modify: `mcpbrain/sync/__init__.py:22-188` (`run_sync_cycle`)
- Modify: `mcpbrain/daemon.py` (`run_cycle` ~338-414, `run_one` ~1203-1273, loop ~2304-2329)
- Test: `tests/test_budget.py` (create), `tests/test_index_bounded.py` (create)

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces:
  - `mcpbrain.budget.Budget(deadline_s: float | None, clock=time.monotonic)` with
    `.expired() -> bool` and `.remaining() -> float`.
  - `store.unembedded_chunks(limit: int | None = None) -> list[dict]`
  - `index_pending(store, embedder, batch_size=32, *, home=None, budget=None, max_items: int | None = None) -> int`
  - `drain.drain(store, *, home=None, apply=None, embedder=None, budget=None) -> dict`
  - `run_cycle(...)` return dict gains key `more_work: bool`. Task 5 reads it.

- [ ] **Step 1: Write the failing Budget test**

Create `tests/test_budget.py`:

```python
from mcpbrain.budget import Budget


def test_not_expired_before_deadline():
    now = [100.0]
    b = Budget(deadline_s=10.0, clock=lambda: now[0])
    assert not b.expired()
    assert b.remaining() == 10.0


def test_expired_after_deadline():
    now = [100.0]
    b = Budget(deadline_s=10.0, clock=lambda: now[0])
    now[0] = 111.0
    assert b.expired()
    assert b.remaining() == 0.0


def test_zero_budget_is_immediately_expired():
    now = [5.0]
    b = Budget(deadline_s=0.0, clock=lambda: now[0])
    assert b.expired()


def test_none_budget_never_expires():
    """A None deadline means unbounded — used by tests and one-shot CLI paths."""
    b = Budget(deadline_s=None, clock=lambda: 0.0)
    assert not b.expired()
    assert b.remaining() == float("inf")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_budget.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcpbrain.budget'`.

- [ ] **Step 3: Implement Budget**

Create `mcpbrain/budget.py`:

```python
"""A wall-clock budget for one pass through the daemon's bulk-work cycle.

The cycle loop used to run every phase to completion, so a large ingest or a
stalled socket held the loop for hours and everything scheduled after it —
notably the ~20 maintenance passes — never ran. Phases now take a Budget,
check it between units of work, and yield when it expires. Work is resumed on
the next tick: the bulk phases are driven by DB predicates (embedded=0,
enriched=0) and delta tokens, so they are naturally resumable and need no
explicit cursor.
"""
from __future__ import annotations

import time


class Budget:
    """Expires `deadline_s` seconds after construction. `deadline_s=None` is unbounded."""

    def __init__(self, deadline_s: float | None, clock=time.monotonic):
        self._clock = clock
        self._deadline_s = deadline_s
        self._start = clock()

    def expired(self) -> bool:
        if self._deadline_s is None:
            return False
        return (self._clock() - self._start) >= self._deadline_s

    def remaining(self) -> float:
        if self._deadline_s is None:
            return float("inf")
        return max(0.0, self._deadline_s - (self._clock() - self._start))
```

- [ ] **Step 4: Run the Budget tests**

Run: `uv run pytest tests/test_budget.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Write the failing bounded-embed test**

Create `tests/test_index_bounded.py`:

```python
"""index_pending must respect a limit and a budget, and resume cleanly."""
import json

from mcpbrain.budget import Budget
from mcpbrain.index import index_pending
from mcpbrain.store import Store


class _FakeEmbedder:
    dim = 4

    def embed_passages(self, texts):
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    def embed_query(self, text):
        return [0.1, 0.2, 0.3, 0.4]


def _store_with_pending(tmp_path, n):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    with s._connect(write=True) as db:
        for i in range(n):
            db.execute(
                "INSERT INTO chunks(doc_id,text,content_hash,metadata,embedded,enriched) "
                "VALUES (?,?,?,?,0,0)",
                (f"d-{i}", f"text {i}", f"h{i}", json.dumps({})),
            )
    return s


def test_unembedded_chunks_respects_limit(tmp_path):
    s = _store_with_pending(tmp_path, 50)
    assert len(s.unembedded_chunks(limit=10)) == 10
    assert len(s.unembedded_chunks()) == 50


def test_index_pending_stops_at_expired_budget(tmp_path):
    s = _store_with_pending(tmp_path, 200)
    spent = Budget(deadline_s=0.0)          # already expired
    done = index_pending(s, _FakeEmbedder(), batch_size=32, home=str(tmp_path),
                         budget=spent)
    assert done == 0, "an expired budget must embed nothing"


def test_index_pending_resumes_and_processes_every_item_exactly_once(tmp_path):
    """N bounded slices over a K-item backlog process exactly K items."""
    s = _store_with_pending(tmp_path, 100)
    total = 0
    for _ in range(20):                      # generous slice count
        done = index_pending(s, _FakeEmbedder(), batch_size=10,
                             home=str(tmp_path), max_items=10)
        total += done
        if done == 0:
            break
    assert total == 100
    assert s.unembedded_chunks() == []
```

- [ ] **Step 6: Run it to verify it fails**

Run: `uv run pytest tests/test_index_bounded.py -q`
Expected: FAIL — `unembedded_chunks()` takes no `limit`, `index_pending()` takes
no `budget`/`max_items`.

- [ ] **Step 7: Add the limit to `unembedded_chunks`**

Replace `mcpbrain/store.py:1236-1249` with:

```python
    def unembedded_chunks(self, limit: int | None = None) -> list[dict]:
        """Chunks awaiting embedding. `limit` bounds one cycle's slice of work.

        Unbounded by default for callers that genuinely want the whole set
        (tests, one-shot CLI). The daemon always passes a limit: embedding the
        entire backlog in one call is what used to hold the cycle for hours.
        """
        sql = "SELECT rowid,doc_id,text,metadata FROM chunks WHERE embedded=0"
        params: tuple = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (int(limit),)
        with self._connect() as db:
            cur = db.execute(sql, params)
            return [
                {
                    "rowid": r["rowid"],
                    "doc_id": r["doc_id"],
                    "text": r["text"],
                    "metadata": json.loads(r["metadata"]),
                }
                for r in cur.fetchall()
            ]
```

- [ ] **Step 8: Bound `index_pending`**

Replace `mcpbrain/index.py:9-43` with:

```python
def index_pending(store, embedder, batch_size: int = 32, *, home: str | None = None,
                  budget=None, max_items: int | None = None) -> int:
    """Embed pending chunks, prepending the Q6 contextual-retrieval prefix to each
    passage when enabled.

    Contextual retrieval is ON by default — validated on the live gold set to lift
    recall@10 +0.10 / MRR +0.175 (A/B 2026-06-24). It is gated by the
    `contextual_retrieval` config flag so it can be rolled back; the prefix is
    PASSAGE-ONLY (embed.contextual_prefix), never applied to the query side. `home`
    selects which config to read (defaults to the app dir).

    Bounded: `max_items` caps how many chunks one call fetches, and `budget`
    stops the loop between batches once the cycle's wall-clock slice is spent.
    Remaining chunks keep embedded=0 and are picked up next cycle — the work is
    resumable because it is driven by that predicate, not an in-memory cursor.
    """
    from mcpbrain import config
    _home = home or str(config.app_dir())
    if budget is not None and budget.expired():
        return 0
    pending = store.unembedded_chunks(limit=max_items)
    done = 0
    if pending:
        use_prefix = config.contextual_retrieval_enabled(_home)
        for i in range(0, len(pending), batch_size):
            if budget is not None and budget.expired():
                log.info("index_pending: budget spent after %d chunks", done)
                break
            batch = pending[i:i + batch_size]
            texts = [
                (contextual_prefix(c["metadata"]) + c["text"]) if use_prefix else c["text"]
                for c in batch
            ]
            vectors = embedder.embed_passages(texts)
            for c, v in zip(batch, vectors):
                store.write_embedding(c["rowid"], v, home=_home)
                done += 1
    # Phase C: drain the contextual-BM25 FTS re-index backfill in bounded
    # batches (no re-embed) so existing chunks pick up the C1 contextual
    # prefix. Runs every cycle — including when nothing is pending — so it
    # actually converges once the corpus is fully embedded.
    try:
        store.reindex_fts_batch(cap=5000)
    except Exception:  # noqa: BLE001
        log.warning("reindex_fts_batch failed; FTS contextual backfill deferred", exc_info=True)
    return done
```

- [ ] **Step 9: Run the bounded tests**

Run: `uv run pytest tests/test_index_bounded.py tests/test_budget.py -q`
Expected: PASS (7 tests).

- [ ] **Step 10: Thread the budget through sync and the cycle**

In `mcpbrain/sync/__init__.py`, add `budget=None` and
`embed_max_items: int = 2000` to `run_sync_cycle`'s signature, pass both to all
six `index_pending(...)` calls (lines 60, 63, 67, 102, 147, 187) as
`index_pending(store, embedder, home=home, budget=budget, max_items=embed_max_items)`,
and after each source's block add:

```python
        if budget is not None and budget.expired():
            result["budget_spent"] = True
            return result
```

In `mcpbrain/daemon.py`, add a module constant near the other cycle constants:

```python
# Wall-clock slice for one bulk-work cycle. The loop must always reach the
# bottom: maintenance, the enrichment producer and the heartbeat all live after
# run_one(), and an unbounded cycle starved them for four days (2026-07-23..27).
CYCLE_BUDGET_S = 60.0
```

In `mcpbrain/drain.py`, add `budget=None` to `drain()`'s signature (line 278)
and, inside the per-file loop that increments `summary["files"]`, add at the top
of each iteration:

```python
        if budget is not None and budget.expired():
            summary["budget_spent"] = True
            break
```

Enrichment drain is unbounded per cycle for the same reason embedding was: it
processes every inbox file before returning.

In `run_cycle`, accept `budget=None`, pass it to both `run_sync_cycle` and
`drain.drain(...)` (line 408), and include
`"more_work": bool(budget is not None and budget.expired())` in the returned
dict. In `run_one`, construct `budget = Budget(CYCLE_BUDGET_S, clock=self._clock)`
and pass it to `run_cycle`; return the dict unchanged otherwise.

In the `run()` loop (currently line ~2329), replace the unconditional wait:

```python
                # Re-wake promptly when the cycle yielded mid-work, so a large
                # backlog still drains at close to full speed while the loop
                # keeps reaching the maintenance/heartbeat section every minute.
                more = bool((cycle_result or {}).get("more_work"))
                self._wake.wait(timeout=1.0 if more else self._interval_s)
```

capturing `cycle_result = self.run_one()` at the top of the try block.

- [ ] **Step 11: Run the impacted suites and lint**

Run: `uv run pytest tests/ -q -k "index or sync or daemon or budget" && uv run ruff check mcpbrain/`
Expected: all pass, ruff clean.

- [ ] **Step 12: Commit**

```bash
git add mcpbrain/budget.py mcpbrain/index.py mcpbrain/store.py mcpbrain/sync/__init__.py mcpbrain/daemon.py tests/test_budget.py tests/test_index_bounded.py
git commit -m "feat(daemon): bound the bulk-work cycle with a wall-clock budget

index_pending fetched the entire unembedded set with no LIMIT and embedded all
of it, six times per cycle; a 61,580-chunk backlog took ~1.9h and the loop never
reached the maintenance passes below it. Phases now take a Budget and yield when
the slice is spent; the loop re-wakes promptly when work remains, so throughput
is preserved."
```

---

### Task 3: Close the shared-state hazards

Three pieces of state are currently safe only because one thread touches them.
This must land **before** Task 4 introduces the second thread.

**Files:**
- Modify: `mcpbrain/daemon.py` (`__init__` ~531-670, `run_one` ~1238-1272, the
  pass methods that write `_pending_*`: lines ~1863, 1973, 2029, 2135, 2180,
  and the `_embedder` property ~432-447)
- Modify: `mcpbrain/embed.py:117-128` (`_LocalEmbedder`)
- Test: `tests/test_daemon_thread_safety.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: `Daemon._stash_lock`, `Daemon._embedder_lock`, `Daemon._bulk_lock`
  (all `threading.Lock`), and `Daemon._stash_take() -> dict`. Task 4 acquires
  `_bulk_lock` for the four chunk-contending passes.

- [ ] **Step 1: Write the failing test**

Create `tests/test_daemon_thread_safety.py`:

```python
"""State shared between the cycle loop and the maintenance thread.

These are real-thread tests on purpose: the whole bug class lives in the
interleaving, and a mocked lock proves nothing.
"""
import threading

from mcpbrain import daemon as d


def _bare_daemon():
    dm = d.Daemon.__new__(d.Daemon)
    dm._stash_lock = threading.Lock()
    dm._embedder_lock = threading.Lock()
    dm._bulk_lock = threading.Lock()
    dm._pending_blocks = {}
    dm._pending_audit = {}
    dm._pending_synthesis = {}
    return dm


def test_locks_exist_on_a_real_daemon(tmp_path):
    """The real constructor must create all three locks."""
    for name in ("_stash_lock", "_embedder_lock", "_bulk_lock"):
        assert name in d.Daemon.__init__.__code__.co_names, f"{name} not set in __init__"


def test_stash_take_is_atomic_under_concurrent_writers():
    """No update is lost and no key is read-then-dropped mid-write."""
    dm = _bare_daemon()
    stop = threading.Event()

    def writer(n):
        i = 0
        while not stop.is_set():
            with dm._stash_lock:
                dm._pending_blocks[f"w{n}-{i}"] = [i]
            i += 1

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(3)]
    for t in threads:
        t.start()

    taken = []
    for _ in range(200):
        taken.append(dm._stash_take())

    stop.set()
    for t in threads:
        t.join()

    # Every take returns a plain dict snapshot and leaves the stash cleared;
    # crucially nothing raises "dictionary changed size during iteration".
    assert all(isinstance(x, dict) for x in taken)


def test_stash_take_clears_and_returns_contents():
    dm = _bare_daemon()
    dm._pending_blocks = {"a": [1]}
    dm._pending_audit = {"b": [2]}
    dm._pending_synthesis = {"c": [3]}
    got = dm._stash_take()
    assert got == {"blocks": {"a": [1]}, "audit": {"b": [2]}, "synthesis": {"c": [3]}}
    assert dm._pending_blocks == {} and dm._pending_audit == {}
    assert dm._pending_synthesis == {}


def test_embedder_lock_serialises_model_access():
    """Two threads embedding concurrently must not overlap inside the model."""
    overlaps = []
    inside = threading.Lock()
    active = [0]

    class _Model:
        def embed(self, texts):
            with inside:
                active[0] += 1
                if active[0] > 1:
                    overlaps.append(True)
            try:
                return [[0.0] * 4 for _ in texts]
            finally:
                with inside:
                    active[0] -= 1

    from mcpbrain.embed import _LocalEmbedder
    emb = _LocalEmbedder.__new__(_LocalEmbedder)
    emb._model = _Model()
    emb.dim = 4
    emb._qp = ""
    emb._lock = threading.Lock()

    threads = [threading.Thread(target=lambda: emb.embed_passages(["x"] * 50))
               for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert overlaps == [], "embedder model was entered concurrently"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_daemon_thread_safety.py -q`
Expected: FAIL — locks not created, `_stash_take` missing, `_LocalEmbedder` has
no `_lock`.

- [ ] **Step 3: Add the locks and the atomic stash take**

In `Daemon.__init__` (near the existing `_backfill_lock` at ~line 655) add:

```python
        # Guards the _pending_* stashes. They are written by cadence passes and
        # read-and-cleared by run_one; once those run on different threads that
        # is a genuine read-delete race.
        self._stash_lock = threading.Lock()
        # Guards the lazily-built embedder. index_pending (cycle thread) and
        # consolidation/self_improve (maintenance thread) share one ONNX model.
        self._embedder_lock = threading.Lock()
        # Coarse advisory lock. Held by the cycle around chunk-mutating phases
        # and acquired by the four cadence passes that also write `chunks`.
        self._bulk_lock = threading.Lock()
```

Add the method (near `run_one`):

```python
    def _stash_take(self) -> dict:
        """Atomically snapshot and clear the three request stashes.

        run_one previously read these dicts and deleted keys from them in
        separate statements; with a maintenance thread writing concurrently that
        loses requests and can raise "dictionary changed size during iteration".
        """
        with self._stash_lock:
            got = {
                "blocks": dict(self._pending_blocks),
                "audit": dict(self._pending_audit),
                "synthesis": dict(self._pending_synthesis),
            }
            self._pending_blocks = {}
            self._pending_audit = {}
            self._pending_synthesis = {}
            return got
```

- [ ] **Step 4: Route the stash writers and reader through the lock**

Every cadence pass that assigns to `self._pending_blocks`,
`self._pending_audit` or `self._pending_synthesis` (lines ~1863, 1973, 2029,
2135, 2180) must wrap the assignment:

```python
        with self._stash_lock:
            self._pending_blocks[key] = value
```

In `run_one` (~1238-1240), replace the direct reads and the later per-key
deletions with a single `_stash_take()` call, using the returned snapshot for
`synthesis_requests` and `extra_blocks`.

- [ ] **Step 5: Lock the embedder**

In `mcpbrain/embed.py`, in `_LocalEmbedder.__init__` add `self._lock = threading.Lock()`
(and `import threading` at the top), then guard both methods:

```python
    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        with self._lock:
            return [list(map(float, v)) for v in self._model.embed(list(texts))]

    def embed_query(self, text: str) -> list[float]:
        with self._lock:
            return list(map(float, next(self._model.query_embed([self._qp + text]))))
```

In `Daemon._embedder` (~432-447), wrap the lazy build in `self._embedder_lock`
so two threads cannot construct the model simultaneously.

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_daemon_thread_safety.py -q`
Expected: PASS (4 tests).

- [ ] **Step 7: Run the impacted suites and lint**

Run: `uv run pytest tests/ -q -k "daemon or embed or cadence" && uv run ruff check mcpbrain/`
Expected: all pass, ruff clean.

- [ ] **Step 8: Commit**

```bash
git add mcpbrain/daemon.py mcpbrain/embed.py tests/test_daemon_thread_safety.py
git commit -m "fix(daemon): guard state shared with the coming maintenance thread

_pending_* stashes are written by cadence passes and read-and-cleared by
run_one; the embedder model and its lazy build are shared by index_pending and
consolidation/self_improve. All were safe only because one thread touched them.
Adds _stash_lock/_embedder_lock/_bulk_lock and an atomic _stash_take."
```

---

### Task 4: Move maintenance onto its own thread

The fix for the reported bug. Safe because `SingleWriterLock` is a
process-scoped file lock, not a thread lock, and concurrent store writers
already exist (see spec § Why a second thread is safe).

**Files:**
- Modify: `mcpbrain/daemon.py` (`_CADENCE_PASSES` ~156-215, `_run_periodic_passes`
  ~2201-2221, `run()` ~2304-2329, `__init__`, `stop()`)
- Modify: `tests/test_cadence_dispatch.py:63`
- Test: `tests/test_maintenance_scheduler.py` (create)

**Interfaces:**
- Consumes: `Daemon._bulk_lock` (Task 3).
- Produces: `Daemon._start_maintenance_thread()`, `Daemon._maintenance_loop()`,
  `MAINTENANCE_TICK_S`, and `CadencePass.needs_bulk_lock: bool`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_maintenance_scheduler.py`:

```python
"""Maintenance runs on its own thread, independent of the bulk cycle.

The regression this locks in: passes must fire even while run_one() is blocked.
Nothing in the suite covered that, which is why a four-day starvation went
unnoticed.
"""
import threading
import time

from mcpbrain import daemon as d


def test_four_chunk_writers_need_the_bulk_lock():
    need = {cp.name for cp in d._CADENCE_PASSES if cp.needs_bulk_lock}
    assert need == {"stale_reextract", "salience_score", "decay_pass", "consolidation"}


def test_passes_run_while_the_cycle_thread_is_blocked():
    """The bug: maintenance was starved behind an unbounded run_one()."""
    dm = d.Daemon.__new__(d.Daemon)
    dm._stop = threading.Event()
    dm._pause = threading.Event()
    dm._bulk_lock = threading.Lock()
    dm._maintenance_interval_s = 0.01
    ran = []

    def _fake_passes():
        ran.append(1)

    dm._run_periodic_passes = _fake_passes
    dm._note_progress = lambda phase: None

    # Simulate the cycle loop wedged inside run_one(): it holds nothing the
    # scheduler needs, so maintenance must keep ticking.
    wedged = threading.Event()
    threading.Thread(target=wedged.wait, daemon=True).start()

    t = threading.Thread(target=dm._maintenance_loop, daemon=True)
    t.start()
    deadline = time.monotonic() + 2.0
    while len(ran) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    dm._stop.set()
    t.join(timeout=2.0)
    wedged.set()

    assert len(ran) >= 3, f"scheduler only ticked {len(ran)} times"


def test_maintenance_loop_exits_on_stop():
    dm = d.Daemon.__new__(d.Daemon)
    dm._stop = threading.Event()
    dm._pause = threading.Event()
    dm._bulk_lock = threading.Lock()
    dm._maintenance_interval_s = 0.01
    dm._run_periodic_passes = lambda: None
    dm._note_progress = lambda phase: None

    t = threading.Thread(target=dm._maintenance_loop, daemon=True)
    t.start()
    dm._stop.set()
    t.join(timeout=2.0)
    assert not t.is_alive()


def test_maintenance_loop_survives_a_raising_pass():
    dm = d.Daemon.__new__(d.Daemon)
    dm._stop = threading.Event()
    dm._pause = threading.Event()
    dm._bulk_lock = threading.Lock()
    dm._maintenance_interval_s = 0.01
    calls = []

    def _boom():
        calls.append(1)
        raise RuntimeError("pass exploded")

    dm._run_periodic_passes = _boom
    dm._note_progress = lambda phase: None

    t = threading.Thread(target=dm._maintenance_loop, daemon=True)
    t.start()
    deadline = time.monotonic() + 2.0
    while len(calls) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    dm._stop.set()
    t.join(timeout=2.0)

    assert len(calls) >= 3, "a raising pass must not kill the scheduler"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_maintenance_scheduler.py -q`
Expected: FAIL — `CadencePass` has no `needs_bulk_lock`, `_maintenance_loop`
missing.

- [ ] **Step 3: Tag the contending passes**

In the `CadencePass` dataclass (~line 145) add:

```python
    needs_bulk_lock: bool = False
```

Set `needs_bulk_lock=True` on exactly these four entries in `_CADENCE_PASSES`:
`stale_reextract`, `salience_score`, `decay_pass`, `consolidation`.

- [ ] **Step 4: Add the scheduler loop**

Add near `_run_periodic_passes`:

```python
MAINTENANCE_TICK_S = 60.0


    def _maintenance_loop(self) -> None:
        """Run due cadence passes on our own clock, independent of the bulk cycle.

        Each pass still self-gates via _is_due, so a tick is cheap. This thread
        exists because the passes used to run only after run_one() returned, and
        an unbounded cycle therefore starved every one of them.
        """
        while not self._stop.is_set():
            if not self._pause.is_set():
                try:
                    self._run_periodic_passes()
                    self._note_progress("maintenance")
                except Exception:  # noqa: BLE001 — a bad pass must not kill the thread
                    log.warning("maintenance loop iteration failed", exc_info=True)
            self._stop.wait(timeout=self._maintenance_interval_s)

    def _start_maintenance_thread(self) -> None:
        self._maintenance_thread = threading.Thread(
            target=self._maintenance_loop, name="mcpbrain-maintenance", daemon=True)
        self._maintenance_thread.start()
```

In `__init__` add `self._maintenance_interval_s = MAINTENANCE_TICK_S` and
`self._maintenance_thread = None`.

- [ ] **Step 5: Acquire the bulk lock per pass and drop the dead guard**

In `_run_periodic_passes`, delete the dead `_backfill_active` early return
(lines 2212-2213 — the backfill thread cannot start because
`mcpbrain/enrich_backfill.py` does not exist), and wrap the dispatch:

```python
        for cp in _CADENCE_PASSES:
            if cp.needs_configured and not configured:
                continue
            try:
                if cp.needs_bulk_lock:
                    with self._bulk_lock:
                        getattr(self, cp.fn_name)()
                else:
                    getattr(self, cp.fn_name)()
            except Exception as exc:  # noqa: BLE001
                log.warning("periodic pass %s failed: %s", cp.name, exc, exc_info=True)
```

Also correct the inaccurate comment above the loop: `lint_graph.py` never reads
`entity_communities`; the consumer of `communities` is the `blocks` pass via
`community_synth.py:54`.

- [ ] **Step 6: Start the thread and stop calling passes inline**

In `run()`, start the thread before the `while` loop and **remove** the
`self._run_periodic_passes()` call from the loop body (~line 2322). Hold the
bulk lock around the cycle:

```python
            self._start_maintenance_thread()
            while not self._stop.is_set():
                ...
                with self._bulk_lock:
                    cycle_result = self.run_one()
```

- [ ] **Step 7: Update the test that encodes the removed behaviour**

`tests/test_cadence_dispatch.py:63` asserts `_backfill_active` suppresses all
graph passes. That guard is dead code being removed; delete the assertion and
its setup, leaving the rest of the test intact.

- [ ] **Step 8: Run the tests**

Run: `uv run pytest tests/test_maintenance_scheduler.py tests/test_cadence_dispatch.py tests/test_cadence_gate.py -q`
Expected: PASS.

- [ ] **Step 9: Run the impacted suites and lint**

Run: `uv run pytest tests/ -q -k "daemon or cadence or maintenance or action" && uv run ruff check mcpbrain/`
Expected: all pass, ruff clean.

- [ ] **Step 10: Commit**

```bash
git add mcpbrain/daemon.py tests/test_maintenance_scheduler.py tests/test_cadence_dispatch.py
git commit -m "feat(daemon): run maintenance passes on their own thread

_run_periodic_passes was called after run_one() on the same thread, so an
unbounded cycle starved every cadence pass — four days with none running on the
live store. Maintenance now ticks on its own schedule; only the four passes that
write chunks take the coarse bulk lock. Also removes the dead _backfill_active
guard (enrich_backfill.py does not exist, so the thread can never start)."
```

---

### Task 5: Progress heartbeat, watchdog and self-healing

The daemon must notice it is wedged and recover. The current heartbeat is
written *after* the passes, so by construction it cannot detect a mid-cycle
stall — it was 35.9 h stale while the process ran.

**Files:**
- Modify: `mcpbrain/daemon.py` (heartbeat ~2324, `run_one`, `run_cycle`)
- Modify: `mcpbrain/agents.py` (`_schtasks_args` ~168, add XML registration)
- Modify: `mcpbrain/control_api.py` (`/api/status`)
- Test: `tests/test_daemon_watchdog.py` (create)

**Interfaces:**
- Consumes: `Daemon._maintenance_loop` (Task 4).
- Produces: `Daemon._note_progress(phase: str)`, `Daemon._stalled_phase() -> tuple[str, float] | None`,
  `Daemon._recover_from_stall()`, `STALL_S`, `WATCHDOG_MAX_EXITS`, `WATCHDOG_WINDOW_S`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_daemon_watchdog.py`:

```python
"""Stall detection and platform-aware self-healing."""
import json
import threading

from mcpbrain import daemon as d


def _wd_daemon(tmp_path, monkeypatch=None, now=1000.0):
    dm = d.Daemon.__new__(d.Daemon)
    dm._clock = lambda: now
    dm._progress = {}
    dm._progress_lock = threading.Lock()
    # Daemon has no _home attribute; it resolves the app dir on demand.
    if monkeypatch is not None:
        monkeypatch.setattr(d, "app_dir", lambda: tmp_path)
    return dm


def test_note_progress_records_a_timestamp(tmp_path):
    dm = _wd_daemon(tmp_path)
    dm._note_progress("sync")
    assert dm._progress["sync"] == 1000.0


def test_no_stall_when_progress_is_fresh(tmp_path):
    dm = _wd_daemon(tmp_path)
    dm._note_progress("sync")
    assert dm._stalled_phase() is None


def test_stall_detected_after_threshold(tmp_path):
    dm = _wd_daemon(tmp_path)
    dm._note_progress("sync")
    dm._clock = lambda: 1000.0 + d.STALL_S + 1.0
    stalled = dm._stalled_phase()
    assert stalled is not None
    assert stalled[0] == "sync"


def test_no_stall_before_any_progress_recorded(tmp_path):
    """A daemon that has not started work yet is not stalled."""
    dm = _wd_daemon(tmp_path)
    assert dm._stalled_phase() is None


def test_exit_limiter_stops_after_three_in_window(tmp_path, monkeypatch):
    dm = _wd_daemon(tmp_path, monkeypatch)
    (tmp_path / "watchdog_exits.json").write_text(
        json.dumps([900.0, 950.0, 990.0]))          # 3 recent exits
    assert dm._watchdog_may_exit() is False


def test_exit_limiter_allows_when_window_has_aged_out(tmp_path, monkeypatch):
    dm = _wd_daemon(tmp_path, monkeypatch)
    old = 1000.0 - d.WATCHDOG_WINDOW_S - 10.0
    (tmp_path / "watchdog_exits.json").write_text(
        json.dumps([old, old - 1, old - 2]))
    assert dm._watchdog_may_exit() is True


def test_exit_limiter_allows_when_no_history(tmp_path, monkeypatch):
    dm = _wd_daemon(tmp_path, monkeypatch)
    assert dm._watchdog_may_exit() is True


def test_recovery_exits_on_macos(tmp_path, monkeypatch):
    """launchd KeepAlive=True restarts us, so a plain exit is correct."""
    dm = _wd_daemon(tmp_path)
    monkeypatch.setattr(d.sys, "platform", "darwin")
    called = {}
    monkeypatch.setattr(dm, "_exit_for_restart", lambda: called.setdefault("exit", True))
    monkeypatch.setattr(dm, "_spawn_replacement", lambda: called.setdefault("spawn", True))
    dm._recover_from_stall()
    assert called == {"exit": True}


def test_recovery_spawns_replacement_on_unsupervised_windows(tmp_path, monkeypatch):
    """Startup-folder fallback has no supervisor, so exiting alone would kill us."""
    dm = _wd_daemon(tmp_path)
    monkeypatch.setattr(d.sys, "platform", "win32")
    monkeypatch.setattr(d, "win_persistence_mechanism", lambda: "startup")
    called = {}
    monkeypatch.setattr(dm, "_exit_for_restart", lambda: called.setdefault("exit", True))
    monkeypatch.setattr(dm, "_spawn_replacement", lambda: called.setdefault("spawn", True))
    dm._recover_from_stall()
    assert called.get("spawn") is True


def test_recovery_exits_on_supervised_windows(tmp_path, monkeypatch):
    """With a schtasks RestartOnFailure task, exit is supervised."""
    dm = _wd_daemon(tmp_path)
    monkeypatch.setattr(d.sys, "platform", "win32")
    monkeypatch.setattr(d, "win_persistence_mechanism", lambda: "schtasks")
    called = {}
    monkeypatch.setattr(dm, "_exit_for_restart", lambda: called.setdefault("exit", True))
    monkeypatch.setattr(dm, "_spawn_replacement", lambda: called.setdefault("spawn", True))
    dm._recover_from_stall()
    assert called.get("exit") is True
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_daemon_watchdog.py -q`
Expected: FAIL — `_note_progress`, `STALL_S` etc. do not exist.

- [ ] **Step 3: Implement progress tracking and stall detection**

Add to `mcpbrain/daemon.py` (module level):

```python
# Zero progress for this long means the cycle is wedged, not merely slow. A
# sampled main thread once sat in _ssl__SSLSocket_read at 0% CPU for 1h44m.
STALL_S = 1800.0
WATCHDOG_MAX_EXITS = 3
WATCHDOG_WINDOW_S = 6 * 3600.0
```

Add to `Daemon.__init__`: `self._progress = {}` and
`self._progress_lock = threading.Lock()`.

Add the methods:

```python
    def _note_progress(self, phase: str) -> None:
        """Record that `phase` advanced. The old heartbeat was written only after
        the cadence passes, so a mid-cycle stall was invisible by construction."""
        with self._progress_lock:
            self._progress[phase] = self._clock()

    def _stalled_phase(self) -> tuple[str, float] | None:
        """(phase, seconds_since) for the most recent progress, if it is too old."""
        with self._progress_lock:
            if not self._progress:
                return None            # nothing started yet is not a stall
            phase, ts = max(self._progress.items(), key=lambda kv: kv[1])
        age = self._clock() - ts
        return (phase, age) if age > STALL_S else None
```

- [ ] **Step 4: Implement the exit limiter and recovery**

```python
    def _watchdog_exits_path(self):
        # Daemon has no _home attribute — the app dir is resolved on demand,
        # as everywhere else in this module.
        return app_dir() / "watchdog_exits.json"

    def _watchdog_may_exit(self) -> bool:
        """False once WATCHDOG_MAX_EXITS restarts have happened in the window.

        A persistently broken install should end up visibly stuck rather than
        restarting forever.
        """
        import json as _json
        path = self._watchdog_exits_path()
        try:
            recent = [float(t) for t in _json.loads(path.read_text())]
        except (OSError, ValueError):
            recent = []
        cutoff = self._clock() - WATCHDOG_WINDOW_S
        recent = [t for t in recent if t >= cutoff]
        return len(recent) < WATCHDOG_MAX_EXITS

    def _record_watchdog_exit(self) -> None:
        import json as _json
        path = self._watchdog_exits_path()
        try:
            recent = [float(t) for t in _json.loads(path.read_text())]
        except (OSError, ValueError):
            recent = []
        cutoff = self._clock() - WATCHDOG_WINDOW_S
        recent = [t for t in recent if t >= cutoff] + [self._clock()]
        try:
            path.write_text(_json.dumps(recent))
        except OSError:
            pass

    def _exit_for_restart(self) -> None:
        os._exit(1)   # noqa: SLF001 — bypass atexit; the supervisor restarts us

    def _spawn_replacement(self) -> None:
        """Start a detached successor before exiting (unsupervised Windows only)."""
        import subprocess
        subprocess.Popen([sys.executable, "-m", "mcpbrain.daemon"],  # noqa: S603
                         close_fds=True)
        os._exit(1)  # noqa: SLF001

    def _recover_from_stall(self) -> None:
        supervised = True
        if sys.platform == "win32":
            supervised = win_persistence_mechanism() == "schtasks"
        self._record_watchdog_exit()
        if supervised:
            self._exit_for_restart()
        else:
            self._spawn_replacement()
```

Import `win_persistence_mechanism` from `mcpbrain.agents` at module level, and
ensure `os` and `sys` are imported.

- [ ] **Step 5: Wire the watchdog into the maintenance loop**

In `_maintenance_loop`, after `self._note_progress("maintenance")`, add:

```python
                stalled = self._stalled_phase()
                if stalled is not None:
                    phase, age = stalled
                    if self._watchdog_may_exit():
                        log.error("watchdog: no progress in %.0fs (last phase=%s) "
                                  "— restarting", age, phase)
                        self._recover_from_stall()
                    else:
                        log.error("watchdog: no progress in %.0fs (last phase=%s); "
                                  "restart limit reached, staying up for diagnosis",
                                  age, phase)
```

Call `self._note_progress("cycle")` at the end of `run_one`, and
`self._note_progress("sync")` after `run_sync_cycle` returns inside `run_cycle`.

- [ ] **Step 6: Add Windows XML task registration**

In `mcpbrain/agents.py`, add a generator beside `_schtasks_args`:

```python
def schtasks_xml(*, shim_path, home: str) -> str:
    """On-logon task XML with RestartOnFailure.

    The CLI's /RI flag cannot express restart-on-failure for an on-logon task,
    so registration goes through /XML instead. Without this, a watchdog exit on
    Windows would kill the daemon until the next logon — strictly worse than the
    stall it is recovering from.
    """
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        "  <Triggers><LogonTrigger><Enabled>true</Enabled></LogonTrigger></Triggers>\n"
        "  <Settings>\n"
        "    <RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\n"
        "  </Settings>\n"
        "  <Actions Context=\"Author\">\n"
        f"    <Exec><Command>{shim_path}</Command></Exec>\n"
        "  </Actions>\n"
        "</Task>\n"
    )
```

Change the schtasks registration path to write this XML to a temp file and call
`schtasks /create /TN <name> /XML <file> /F`.

- [ ] **Step 7: Surface watchdog state on /api/status**

In `control_api.py`'s `/api/status` handler, include:

```python
                    "progress": dict(server.daemon._progress),
                    "stalled": server.daemon._stalled_phase(),
```

- [ ] **Step 8: Run the tests**

Run: `uv run pytest tests/test_daemon_watchdog.py -q`
Expected: PASS (10 tests).

- [ ] **Step 9: Run the impacted suites and lint**

Run: `uv run pytest tests/ -q -k "daemon or watchdog or agents or control or status" && uv run ruff check mcpbrain/`
Expected: all pass, ruff clean.

- [ ] **Step 10: Commit**

```bash
git add mcpbrain/daemon.py mcpbrain/agents.py mcpbrain/control_api.py tests/test_daemon_watchdog.py
git commit -m "feat(daemon): per-phase progress heartbeat, watchdog and self-heal

The heartbeat was written only after the cadence passes, so a mid-cycle stall
was invisible — it read 35.9h stale while the process was running. Progress is
now recorded per phase; 30min of none triggers recovery. Recovery is
platform-aware: macOS exits to launchd KeepAlive, Windows registers the task
from XML with RestartOnFailure so exit is supervised, and the unsupervisable
Startup-folder fallback spawns a replacement first. Bounded to 3 restarts / 6h."
```

---

### Task 6: Recall latency and the misleading queue indicator

Two independent user-visible fixes that need no threading work.

**Files:**
- Modify: `mcpbrain/auth.py:28-33`, `mcpbrain/auth.py:243-260`
- Modify: `mcpbrain/embed.py` (`_LocalEmbedder.__init__`)
- Modify: `mcpbrain/dashboard.py` (~438-455)
- Test: `tests/test_auth_timeouts.py` (create), `tests/test_dashboard_queue.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: `auth.DEFAULT_READ_TIMEOUT_S`, `auth.build_service(..., timeout_s=...)`
  behaviour unchanged for the backup caller.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_auth_timeouts.py`:

```python
"""Routine API reads must not inherit the 600s backup-upload timeout."""
from mcpbrain import auth


def test_read_timeout_is_much_smaller_than_the_upload_timeout():
    assert auth.DEFAULT_READ_TIMEOUT_S < auth.DEFAULT_HTTP_TIMEOUT_S
    assert auth.DEFAULT_READ_TIMEOUT_S <= 120


def test_build_service_defaults_to_the_read_timeout(monkeypatch):
    seen = {}

    class _Http:
        def __init__(self, timeout=None):
            seen["timeout"] = timeout

    monkeypatch.setattr(auth.httplib2, "Http", _Http)
    monkeypatch.setattr(auth, "AuthorizedHttp", lambda creds, http: http)
    monkeypatch.setattr(auth, "build", lambda api, version, http: ("svc", api))
    auth.build_service("drive", "v3", object())
    assert seen["timeout"] == auth.DEFAULT_READ_TIMEOUT_S


def test_backup_can_still_request_the_long_timeout(monkeypatch):
    seen = {}

    class _Http:
        def __init__(self, timeout=None):
            seen["timeout"] = timeout

    monkeypatch.setattr(auth.httplib2, "Http", _Http)
    monkeypatch.setattr(auth, "AuthorizedHttp", lambda creds, http: http)
    monkeypatch.setattr(auth, "build", lambda api, version, http: ("svc", api))
    auth.build_service("drive", "v3", object(), timeout_s=auth.DEFAULT_HTTP_TIMEOUT_S)
    assert seen["timeout"] == auth.DEFAULT_HTTP_TIMEOUT_S
```

Create `tests/test_dashboard_queue.py`:

```python
"""'Queue clear' must not be reported when the producer is simply starved."""
from mcpbrain import dashboard


def test_queue_state_idle_when_nothing_to_do():
    assert dashboard.queue_state(queued=0, unenriched_eligible=0) == "idle"


def test_queue_state_working_when_units_are_queued():
    assert dashboard.queue_state(queued=5, unenriched_eligible=1000) == "working"


def test_queue_state_starved_when_backlog_exists_but_nothing_queued():
    """The live failure: 0 units queued while 64,340 chunks awaited enrichment."""
    assert dashboard.queue_state(queued=0, unenriched_eligible=64340) == "starved"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_auth_timeouts.py tests/test_dashboard_queue.py -q`
Expected: FAIL — `DEFAULT_READ_TIMEOUT_S` and `queue_state` do not exist.

- [ ] **Step 3: Split the timeouts**

In `mcpbrain/auth.py`, below `DEFAULT_HTTP_TIMEOUT_S` add:

```python
# Routine Gmail/Drive/Calendar reads. The 600s figure above exists for ~750MB
# resumable backup uploads; applying it to every request means one stalled call
# holds the daemon's cycle for ten minutes.
DEFAULT_READ_TIMEOUT_S = 60
```

Change `build_service`'s default from `timeout_s: float = DEFAULT_HTTP_TIMEOUT_S`
to `timeout_s: float = DEFAULT_READ_TIMEOUT_S`, then pass
`timeout_s=DEFAULT_HTTP_TIMEOUT_S` explicitly at the backup upload call sites in
`mcpbrain/backup.py` (see the comment at `backup.py:214`).

- [ ] **Step 4: Cap onnxruntime threads**

In `_LocalEmbedder.__init__`, before constructing `TextEmbedding`:

```python
        import os as _os
        # Leave the control plane schedulable. Unconfigured, ORT affinitises
        # intra-op threads across every physical core (measured 425% CPU on a
        # 10-core box) and /api/recall starves behind embedding.
        if not _os.environ.get("OMP_NUM_THREADS"):
            _os.environ["OMP_NUM_THREADS"] = "1"
        cpu = _os.cpu_count() or 4
        threads = max(1, cpu - 2)
```

and pass `threads=threads` to `TextEmbedding(...)` (fastembed forwards it to the
ORT session options).

- [ ] **Step 5: Add the queue-state helper and use it**

In `mcpbrain/dashboard.py`:

```python
def queue_state(*, queued: int, unenriched_eligible: int) -> str:
    """Distinguish an empty queue from a starved producer.

    prepare_units runs inside run_cycle; when the cycle wedged, no units were
    produced and the dashboard reported 'queue clear' while 64,340 chunks were
    waiting. 'Nothing queued' is not 'nothing to do'.
    """
    if queued > 0:
        return "working"
    return "idle" if unenriched_eligible == 0 else "starved"
```

Include its result in the dashboard payload beside the existing index counters.

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_auth_timeouts.py tests/test_dashboard_queue.py -q`
Expected: PASS (6 tests).

- [ ] **Step 7: Run the impacted suites and lint**

Run: `uv run pytest tests/ -q -k "auth or dashboard or embed or backup" && uv run ruff check mcpbrain/`
Expected: all pass, ruff clean.

- [ ] **Step 8: Commit**

```bash
git add mcpbrain/auth.py mcpbrain/embed.py mcpbrain/dashboard.py mcpbrain/backup.py tests/test_auth_timeouts.py tests/test_dashboard_queue.py
git commit -m "fix: split API read timeout from backup upload, cap ORT threads

A single 600s socket timeout (sized for 750MB backup uploads) applied to every
Google request, so one stalled read held the cycle for ten minutes. Routine
reads now use 60s. onnxruntime was unconfigured and took every core, starving
/api/recall. Dashboard now distinguishes an idle queue from a starved producer."
```

---

### Task 7: Live-store acceptance

Unit tests cannot prove the bug is fixed; these are the spec's acceptance
criteria. No code unless something fails.

**Files:** none (verification only).

- [ ] **Step 1: Reinstall and restart**

```bash
cd /Users/joshkemp/GitHub/mcpbrain
uv tool install --reinstall --no-cache ".[daemon]"
launchctl kickstart -k gui/$(id -u)/com.mcpbrain
```

Note: plain `--force` has served a stale wheel before; `--reinstall --no-cache`
is required.

- [ ] **Step 2: Confirm cycles complete**

```bash
SRC="$HOME/Library/Application Support/mcpbrain"
for i in 1 2 3 4 5 6; do sleep 30; cat "$SRC/daemon_heartbeat.json"; echo; done
```

Expected: `last_cycle` advances at least twice within 3 minutes. Before this
work it was 35.9 h stale.

- [ ] **Step 3: Confirm maintenance runs during active ingest**

```bash
grep -E "resolve_entities:|action_hygiene:|salience_score:|decay_pass:" \
  "$HOME/Library/Application Support/mcpbrain/com.mcpbrain.err" | tail -5
```

Expected: entries dated today. Before this work the last were 2026-07-23.

- [ ] **Step 4: Confirm the enrichment producer refills**

```bash
ls "$HOME/Library/Application Support/mcpbrain/enrich_queue/units/" | wc -l
```

Expected: non-zero and changing across cycles while a backlog exists.

- [ ] **Step 5: Measure recall latency during ingest**

```bash
SRC="$HOME/Library/Application Support/mcpbrain"
PORT=$(cat "$SRC/control_port"); TOKEN=$(cat "$SRC/control_token")
for i in $(seq 1 20); do
  curl -s -o /dev/null -w "%{time_total}\n" -X POST "http://127.0.0.1:${PORT}/api/recall" \
    -H "Authorization: Bearer ${TOKEN}" -H "Content-Type: application/json" \
    -d '{"query":"what decisions did we make about governance","limit":4}'
done | sort -n | tail -2
```

Expected: p95 under ~3 s with no `BrokenPipeError` in the log. If it is not,
that is the signal for the process-split lever noted in the spec — record the
number, do not build it here.

- [ ] **Step 6: Gold eval must hold**

```bash
uv run python tests/eval/run_eval.py --gold --k 10
```

Expected: recall@10 and MRR at or above the pre-change baseline (0.700 / 0.511
measured 2026-07-27). A regression blocks the work.

- [ ] **Step 7: Record the results**

Append the measured numbers to the spec's acceptance section and commit:

```bash
git add docs/superpowers/specs/2026-07-27-daemon-scheduling-design.md
git commit -m "docs: record daemon scheduling acceptance results"
```

---

## Notes for the implementer

- **Do not** attempt the ingestion fixes described in
  `docs/superpowers/specs/2026-07-27-ingestion-defects-findings.md`. They are
  specs 2 and 3 and are deliberately out of scope.
- The `_backfill_active` guards are dead code throughout `daemon.py` (the module
  they depend on does not exist). Task 4 removes the one in the dispatch path;
  leave the others alone rather than expanding scope.
- If a task's tests pass but you are unsure a change is safe under concurrency,
  say so in the handoff rather than proceeding — the failure mode here is
  silent, and that is exactly how it went unnoticed for four days.
