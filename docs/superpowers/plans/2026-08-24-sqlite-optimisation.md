# SQLite Optimisation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tune every SQLite pragma, and rebuild the store once with contentless FTS5, JSONB metadata, 8 KiB pages, STRICT tables, enforced foreign keys and a trigram index — taking 2.62 GB to ~1.6 GB with retrieval quality unchanged.

**Architecture:** Two parts. Part A is runtime config in `_open_db`/`_connect` — no data change, immediately reversible. Part B makes `store.init()` emit the optimised schema (so new installs get it for free, one source of schema truth) and adds an attended, backup-gated `bin/optimise_store.py` that rebuilds an existing store out-of-place in a single pass instead of six sequential 2.6 GB rewrites.

**Tech Stack:** Python 3.12, stdlib `sqlite3` (SQLite 3.50.4 under the uv-pinned interpreter), `sqlite_vec`, FTS5, pytest.

**Spec:** `docs/superpowers/specs/2026-08-24-sqlite-optimisation-design.md`

## Global Constraints

- **Feature version floors, guarded at runtime, never assumed:** `contentless_delete` needs SQLite **≥3.43**; JSONB needs **≥3.45**; STRICT needs **≥3.37**. Read `sqlite3.sqlite_version` and fall back to the current form below the floor. The version comes from whichever Python the wheel installs under, and the Windows path pins its own x64 interpreter.
- **Gold bar:** recall@10 **≥ 0.750**, MRR **≥ 0.514**. Contentless FTS5 and JSONB are ranking-neutral by construction, so **any** movement is a defect to investigate, not a result to accept.
- **`init()` uses `CREATE TABLE IF NOT EXISTS`,** so schema changes reach new installs only. Existing stores change *only* via `bin/optimise_store.py`. Never make the rebuild automatic — it is attended and backup-gated, following the `bin/consolidate.py` precedent.
- **Read-only connections must never execute a writing pragma** (`optimize`, `analyze`). Guard on the existing `read_only` flag.
- **Measured baseline to beat:** file 2.62 GB / 639,769 pages @ 4096 B; `fts_chunks_content` 0.78 GB; `fts_chunks_data` 93 MB; `chunks` 170,657 rows; metadata 86 MB; orphans **256 rows total** (`entity_relations.entity_a` 8, `entity_relations.entity_b` 0, `email_entities.entity_id` 146, `entity_observations.entity_id` 96, `entity_communities.entity_id` 6).
- Run only impacted tests plus `uv run ruff check mcpbrain/`. The full suite is Josh's to run.

---

### Task 1: Measurement harness

Baseline first — every later task is judged against its output.

**Files:**
- Create: `bin/measure_store.py`
- Test: `tests/test_measure_store.py`

**Interfaces:**
- Produces: `measure(path) -> dict` with keys `file_bytes`, `page_size`, `page_count`, `freelist_bytes`, `table_bytes` (dict), `row_counts` (dict), `has_stat1` (bool).

- [ ] **Step 1: Write the failing test**

```python
import sqlite3
from bin.measure_store import measure

def test_measure_reports_size_and_rowcounts(tmp_path):
    p = tmp_path / "t.sqlite3"
    db = sqlite3.connect(p)
    db.execute("CREATE TABLE chunks(rowid INTEGER PRIMARY KEY, text TEXT)")
    db.executemany("INSERT INTO chunks(text) VALUES(?)", [("x" * 100,)] * 50)
    db.commit(); db.close()

    m = measure(p)

    assert m["row_counts"]["chunks"] == 50
    assert m["file_bytes"] > 0
    assert m["page_size"] in (4096, 8192)
    assert m["has_stat1"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_measure_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bin.measure_store'`

- [ ] **Step 3: Write minimal implementation**

```python
"""Measure a derived store: size, per-table bytes, row counts, planner stats."""
import sqlite3
from pathlib import Path


def measure(path) -> dict:
    path = Path(path)
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        ps = db.execute("PRAGMA page_size").fetchone()[0]
        pc = db.execute("PRAGMA page_count").fetchone()[0]
        fl = db.execute("PRAGMA freelist_count").fetchone()[0]
        names = [r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        rows = {}
        for t in names:
            try:
                rows[t] = db.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
            except sqlite3.OperationalError:
                continue
        stat1 = bool(db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='sqlite_stat1'").fetchone())
        return {"file_bytes": pc * ps, "page_size": ps, "page_count": pc,
                "freelist_bytes": fl * ps, "table_bytes": {}, "row_counts": rows,
                "has_stat1": stat1}
    finally:
        db.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_measure_store.py -v`
Expected: PASS

- [ ] **Step 5: Add the latency benchmark**

Extend `bin/measure_store.py` with a `--latency` mode timing the four 0.7.105 methods through the real `Store` API (never hand-written SQL, or you measure a different query plan than production uses):

```python
def latency(store) -> dict:
    """Time the 0.7.105 benchmark methods through the real Store API."""
    import time
    out = {}
    probes = {
        "doc_ids_for_messages": lambda: store.doc_ids_for_messages(_sample_msg_ids(store)),
        "thread_chunks":        lambda: store.thread_chunks(_sample_thread_id(store)),
        "chunks_for_file":      lambda: store.chunks_for_file(_sample_file_id(store)),
        "inbound_chunks_since": lambda: store.inbound_chunks_since("2026-01-01"),
    }
    for name, fn in probes.items():
        t0 = time.perf_counter()
        fn()
        out[name] = (time.perf_counter() - t0) * 1000  # ms
    return out
```

Write `_sample_msg_ids`, `_sample_thread_id` and `_sample_file_id` to read real ids out of `chunks` metadata, so the probe exercises rows that exist.

- [ ] **Step 6: Capture the baseline**

Run: `uv run python bin/measure_store.py --latency > /tmp/baseline.json`
Record the numbers in the commit message. Expect `has_stat1: false` and `page_size: 4096`.

- [ ] **Step 7: Commit**

```bash
git add bin/measure_store.py tests/test_measure_store.py
git commit -m "feat(bin): add store measurement + latency harness for the optimisation pass"
```

---

### Task 2: Runtime pragmas in `_open_db`

**Files:**
- Modify: `mcpbrain/store.py:102-135` (`_open_db`)
- Test: `tests/test_store_pragmas.py`

**Interfaces:**
- Consumes: nothing.
- Produces: no signature change. `_open_db(path, read_only=False, *, busy_timeout_ms=...)` keeps its contract; only the pragmas it sets change.

**CORRECTED 2026-08-25** (PR #25 finding 5): `cache_size`/`mmap_size` are set
PER CONNECTION, and the daemon opens roughly one connection per call across
~21 threads (e.g. `graph_write.upsert_relation` opens one per relation). The
64 MiB/256 MiB values below, applied unconditionally to EVERY connection,
could add on the order of a gigabyte of RSS between concurrent callers —
this project gates releases on peak RSS (226 MB verified in 0.7.113), so
this risked failing that gate. The tuned values are reserved for `bulk=True`
connections only (the rebuild tool, `Store.reindex_fts_batch`, backup's
`VACUUM INTO` snapshot) — genuinely long-lived, throughput-sensitive
callers; the common per-call connection gets a smaller default (16 MiB
cache / 64 MiB mmap — bigger than SQLite's own 2 MiB default, which this
task's own rationale called too small, but not the full tuned value). Kept
below for its historical record; the code and tests now reflect the
corrected scoping.

- [ ] **Step 1: Write the failing test**

```python
from mcpbrain.store import _open_db, Store

def test_open_db_sets_tuned_pragmas(tmp_path):
    p = tmp_path / "b.sqlite3"
    Store(str(p)).init()
    db = _open_db(str(p))
    try:
        assert db.execute("PRAGMA cache_size").fetchone()[0] == -16384
        assert db.execute("PRAGMA mmap_size").fetchone()[0] == 67108864
        assert db.execute("PRAGMA temp_store").fetchone()[0] == 2
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 1
    finally:
        db.close()


def test_read_only_connection_also_gets_read_pragmas(tmp_path):
    p = tmp_path / "b.sqlite3"
    Store(str(p)).init()
    db = _open_db(str(p), read_only=True)
    try:
        assert db.execute("PRAGMA cache_size").fetchone()[0] == -16384
        assert db.execute("PRAGMA mmap_size").fetchone()[0] == 67108864
    finally:
        db.close()


def test_bulk_connection_gets_the_larger_tuned_pragmas(tmp_path):
    """CORRECTED per PR #25 finding 5: the original tuned values are
    reserved for bulk=True (rebuild / reindex_fts_batch / backup) --
    long-lived, throughput-sensitive callers, not the common per-call case."""
    p = tmp_path / "b.sqlite3"
    Store(str(p)).init()
    db = _open_db(str(p), bulk=True)
    try:
        assert db.execute("PRAGMA cache_size").fetchone()[0] == -65536
        assert db.execute("PRAGMA mmap_size").fetchone()[0] == 268435456
    finally:
        db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_store_pragmas.py -v`
Expected: FAIL — `assert -2000 == -16384`

- [ ] **Step 3: Write minimal implementation**

In `_open_db`, after the existing `journal_size_limit` line and **before** the extension load:

```python
    # Tuned for a multi-GB, read-heavy derived store, but SCOPED to genuinely
    # long-lived, throughput-sensitive connections (bulk=True) -- unconditional
    # 64 MiB/256 MiB per connection could add ~1 GB of RSS across the daemon's
    # ~21 concurrent per-call threads. Everyday connections get a smaller
    # default -- still bigger than SQLite's own 2 MiB default on a 2.6 GB
    # database, just not the full tuned value.
    cache_kib = 65536 if bulk else 16384
    mmap_bytes = 268435456 if bulk else 67108864
    db.execute(f"PRAGMA cache_size=-{cache_kib}")
    db.execute(f"PRAGMA mmap_size={mmap_bytes}")
    db.execute("PRAGMA temp_store=2")         # MEMORY: FTS5/ORDER BY temp b-trees
    db.execute("PRAGMA threads=4")            # parallel sort
    db.execute("PRAGMA analysis_limit=400")   # bounds PRAGMA optimize (Task 3)
    if not read_only:
        # NORMAL is corruption-safe under WAL and this store is DERIVED —
        # fully re-ingestable from Google. FULL fsyncs every commit for a
        # durability guarantee we do not need.
        db.execute("PRAGMA synchronous=1")
```

`_open_db` gains a `bulk: bool = False` keyword-only parameter (and
`Store._connect` forwards its own `bulk: bool = False` through to it); pass
`bulk=True` only from the rebuild tool, `reindex_fts_batch`, and backup's
`VACUUM INTO` snapshot.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_store_pragmas.py -v`
Expected: PASS

- [ ] **Step 5: Run impacted tests**

Run: `uv run pytest tests/test_store.py tests/test_store_schema.py tests/test_store_schema_p3.py tests/test_store_write_txn.py -q`
Expected: all pass. If a test asserted default pragma values, update the assertion — do not weaken the pragma.

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/store.py tests/test_store_pragmas.py
git commit -m "perf(store): tune connection pragmas (cache 64MiB, mmap, temp_store, synchronous=NORMAL)"
```

---

### Task 3: `PRAGMA optimize` on connection close

`sqlite_stat1` has never existed in this database — the planner has always guessed.

**Files:**
- Modify: `mcpbrain/store.py:285-317` (`_connect` finally block)
- Test: `tests/test_store_pragmas.py` (extend)

**Interfaces:**
- Consumes: `_open_db` from Task 2 (which sets `analysis_limit=400`).
- Produces: no signature change.

**CORRECTED 2026-08-25** (post-merge human review on PR #25, finding 1): the
original Step 1/3 below gated `PRAGMA optimize` on `if not self.read_only`
alone — that fires it on every `write=False` close too, not just after an
actual write. `PRAGMA optimize` WRITES, so a read-only USE of a writable
`Store` (e.g. `daemon.search`'s read path, or `graph_write.upsert_relation`,
which opens one connection per relation) would acquire the write lock
*after* the caller's read-only transaction had already finished — right
into contention with a concurrent drain's writes, at exactly the latency
budget `RECALL_PATH_BUSY_TIMEOUT_MS`/`RECALL_PATH_BEGIN_RETRIES` exist to
protect. That reintroduces the 0.7.105 recall-starvation class this very
pragma was meant to help fix. The gate must be `write and not self.read_only`
— only an actual write transaction pays for the stats refresh. Kept below
for its historical record; the code and tests now reflect the corrected gate.

- [ ] **Step 1: Write the failing test**

```python
def test_write_connection_creates_planner_stats(tmp_path):
    p = tmp_path / "b.sqlite3"
    s = Store(str(p))
    s.init()
    with s._connect(write=True) as db:
        db.execute("INSERT INTO chunks(doc_id, text) VALUES('d1','hello world')")
    db = _open_db(str(p), read_only=True)
    try:
        assert db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='sqlite_stat1'").fetchone()
    finally:
        db.close()


def test_read_only_connection_does_not_attempt_optimize(tmp_path):
    p = tmp_path / "b.sqlite3"
    Store(str(p)).init()
    ro = Store(str(p), read_only=True)
    with ro._connect() as db:            # must not raise "attempt to write a readonly database"
        db.execute("SELECT count(*) FROM chunks").fetchone()


def test_a_read_only_use_of_a_writable_store_does_not_attempt_optimize(tmp_path, monkeypatch):
    """CORRECTED per PR #25 finding 1. A write=False use of a WRITABLE Store
    must not attempt PRAGMA optimize either -- only self.read_only alone is
    not the right gate."""
    p = tmp_path / "b.sqlite3"
    s = Store(str(p))
    s.init()
    calls = []

    class _Spy(sqlite3.Connection):
        def execute(self, sql, *a, **kw):
            calls.append(sql)
            return super().execute(sql, *a, **kw)

    real_connect = sqlite3.connect

    def spy_connect(*a, **kw):
        kw.setdefault("factory", _Spy)
        return real_connect(*a, **kw)

    monkeypatch.setattr(sqlite3, "connect", spy_connect)
    with s._connect() as db:   # write=False (the default) on a WRITABLE Store
        db.execute("SELECT count(*) FROM chunks")
    assert "PRAGMA optimize" not in calls
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_store_pragmas.py::test_write_connection_creates_planner_stats -v`
Expected: FAIL — assertion is None, `sqlite_stat1` absent.

- [ ] **Step 3: Write minimal implementation**

Replace the `finally` block of `_connect`:

```python
        finally:
            # PRAGMA optimize is the documented way to keep planner statistics
            # current; analysis_limit (set in _open_db) bounds its cost so it
            # cannot stall a close. It WRITES, so never on a read-only handle
            # -- AND never on a write=False connection either, even on a
            # writable Store: it would contend with a drain's writes at
            # exactly the recall path's latency budget. Only an actual write
            # transaction should pay for a stats refresh.
            if write and not self.read_only:
                try:
                    db.execute("PRAGMA optimize")
                except sqlite3.Error:
                    # Best-effort: a stats refresh must never fail a caller's
                    # otherwise-successful transaction.
                    pass
            db.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_store_pragmas.py -v`
Expected: PASS (all three)

- [ ] **Step 5: Re-measure and compare**

Run: `uv run python bin/measure_store.py --latency > /tmp/after-partA.json`
Diff against `/tmp/baseline.json`. Record both in the commit message.

**Re-measure the 0.7.105 benchmark methods specifically.** Those plans were hand-tuned against a planner with no statistics. If any is now materially faster, note it — but **do not revert any 0.7.105 rewrite**; they stand on their own measurements.

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/store.py tests/test_store_pragmas.py
git commit -m "perf(store): run PRAGMA optimize on close so the planner finally has statistics"
```

---

### Task 4: Orphan report — read-only, deletes nothing

**Files:**
- Create: `bin/optimise_store.py`
- Test: `tests/test_optimise_store.py`

**Interfaces:**
- Produces: `report_orphans(path) -> dict[str, int]` keyed by `"<table>.<column>"`.

- [ ] **Step 1: Write the failing test**

```python
import sqlite3
from bin.optimise_store import report_orphans

def test_report_orphans_counts_dangling_entity_refs(tmp_path):
    p = tmp_path / "b.sqlite3"
    db = sqlite3.connect(p)
    db.execute("CREATE TABLE entities(id TEXT PRIMARY KEY, name TEXT)")
    db.execute("CREATE TABLE email_entities(message_id TEXT, entity_id TEXT)")
    db.execute("INSERT INTO entities VALUES('e1','Real')")
    db.executemany("INSERT INTO email_entities VALUES(?,?)",
                   [("m1", "e1"), ("m2", "GONE"), ("m3", "GONE")])
    db.commit(); db.close()

    r = report_orphans(p)

    assert r["email_entities.entity_id"] == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_optimise_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bin.optimise_store'`

- [ ] **Step 3: Write minimal implementation**

```python
"""Attended, backup-gated store rebuild. NEVER run automatically."""
import sqlite3
from pathlib import Path

# (child table, child column) -> parent table. entity_observations already
# DECLARES REFERENCES entities(id), but foreign_keys was OFF so it never
# enforced anything.
_REFS = [
    ("entity_relations", "entity_a", "entities", "id"),
    ("entity_relations", "entity_b", "entities", "id"),
    ("email_entities", "entity_id", "entities", "id"),
    ("entity_observations", "entity_id", "entities", "id"),
    ("entity_communities", "entity_id", "entities", "id"),
]


def report_orphans(path) -> dict[str, int]:
    db = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True)
    try:
        out = {}
        for child, col, parent, pcol in _REFS:
            try:
                n = db.execute(
                    f'SELECT count(*) FROM "{child}" c '
                    f'LEFT JOIN "{parent}" p ON p."{pcol}" = c."{col}" '
                    f'WHERE p."{pcol}" IS NULL'
                ).fetchone()[0]
            except sqlite3.OperationalError:
                continue
            out[f"{child}.{col}"] = n
        return out
    finally:
        db.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_optimise_store.py -v`
Expected: PASS

- [ ] **Step 5: Run it against the live store and compare to the spec baseline**

Run: `uv run python -c "from bin.optimise_store import report_orphans; import mcpbrain.config as c; print(report_orphans(c.app_dir()/'brain.sqlite3'))"`

Expected, from the spec: `entity_relations.entity_a` 8, `entity_relations.entity_b` 0, `email_entities.entity_id` 146, `entity_observations.entity_id` 96, `entity_communities.entity_id` 6 — **256 total, 0.1% of ~250k graph rows.**

**If the count is materially higher than this, STOP and report.** An unexpected orphan population is a bug signal, not cleanup work.

- [ ] **Step 6: Commit**

```bash
git add bin/optimise_store.py tests/test_optimise_store.py
git commit -m "feat(bin): report store orphans (read-only) ahead of enabling foreign keys"
```

---

### Task 5: Contentless FTS5 in `init()`, with a version guard

**Files:**
- Modify: `mcpbrain/store.py:365` (the `fts_chunks` CREATE) and `init()`
- Test: `tests/test_fts_contentless.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `store.fts5_supports_contentless() -> bool` — a module-level helper both `init()` and the tests read, so the floor is expressed once.

- [ ] **Step 1: Write the failing test**

```python
import sqlite3
import pytest
from mcpbrain.store import Store, fts5_supports_contentless

def test_fts_table_is_contentless_when_sqlite_supports_it(tmp_path):
    if not fts5_supports_contentless():
        pytest.skip("SQLite < 3.43")
    p = tmp_path / "b.sqlite3"
    Store(str(p)).init()
    db = sqlite3.connect(p)
    sql = db.execute(
        "SELECT sql FROM sqlite_master WHERE name='fts_chunks'").fetchone()[0]
    db.close()
    assert "content=''" in sql
    assert "contentless_delete=1" in sql


def test_contentless_fts_still_ranks_with_bm25(tmp_path):
    if not fts5_supports_contentless():
        pytest.skip("SQLite < 3.43")
    p = tmp_path / "b.sqlite3"
    s = Store(str(p)); s.init()
    with s._connect(write=True) as db:
        for i, txt in enumerate(["septic tank byford", "unrelated roster"]):
            db.execute("INSERT INTO chunks(doc_id, text, embedded) VALUES(?,?,1)",
                       (f"d{i}", txt))
            db.execute("INSERT INTO fts_chunks(rowid, text) VALUES(?,?)",
                       (db.execute("SELECT last_insert_rowid()").fetchone()[0], txt))
        rows = db.execute(
            "SELECT rowid, bm25(fts_chunks) FROM fts_chunks "
            "WHERE fts_chunks MATCH 'septic' ORDER BY 2"
        ).fetchall()
    assert len(rows) == 1


def test_deleting_a_chunk_does_not_error_on_contentless_fts(tmp_path):
    """contentless_delete=1 is what makes this legal; without it FTS5 raises."""
    if not fts5_supports_contentless():
        pytest.skip("SQLite < 3.43")
    p = tmp_path / "b.sqlite3"
    s = Store(str(p)); s.init()
    with s._connect(write=True) as db:
        db.execute("INSERT INTO chunks(doc_id, text, embedded) VALUES('d1','hello',1)")
        rid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.execute("INSERT INTO fts_chunks(rowid, text) VALUES(?,'hello')", (rid,))
        db.execute("DELETE FROM fts_chunks WHERE rowid=?", (rid,))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_fts_contentless.py -v`
Expected: FAIL — `ImportError: cannot import name 'fts5_supports_contentless'`

- [ ] **Step 3: Write minimal implementation**

Module level in `store.py`:

```python
def fts5_supports_contentless() -> bool:
    """True when SQLite is new enough for contentless FTS5 WITH deletes.

    contentless_delete=1 lands in 3.43. Without it a contentless table cannot
    service DELETE, which mcpbrain does on every retention and GC sweep — so
    below the floor we keep the content-storing form. The version comes from
    whichever Python the wheel installed under, and the Windows path pins its
    own x64 interpreter, so this MUST be checked and never assumed.
    """
    parts = tuple(int(x) for x in sqlite3.sqlite_version.split(".")[:2])
    return parts >= (3, 43)
```

In `init()`, replace the `fts_chunks` creation:

```python
            if fts5_supports_contentless():
                # content='' drops FTS5's own duplicate copy of the indexed
                # text -- 0.78 GB of the 2.62 GB live store. External content
                # (content='chunks') is NOT usable: _fts_text indexes the
                # contextual prefix + body while chunks.text stays raw, so the
                # content table could not reproduce the indexed string.
                db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS fts_chunks "
                           "USING fts5(text, content='', contentless_delete=1)")
            else:
                db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS fts_chunks "
                           "USING fts5(text)")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_fts_contentless.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Run every FTS-touching test**

Run: `uv run pytest tests/ -q -k "fts or search or recall or retriev"`
Expected: all pass. A failure here means something *does* read text back out of `fts_chunks` — find it and route it to `chunks.text` before continuing.

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/store.py tests/test_fts_contentless.py
git commit -m "perf(store): contentless FTS5 (drops 780MB duplicate text) behind a 3.43 guard"
```

---

### Task 6: JSONB metadata

**Files:**
- Modify: `mcpbrain/store.py` — `init()` expression indexes, and every `json_extract(metadata` read site
- Test: `tests/test_metadata_jsonb.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `store.jsonb_supported() -> bool`; `store._meta_extract(path: str) -> str` returning the SQL fragment (`jsonb_extract(metadata,'$.x')` or `json_extract(metadata,'$.x')`) so index and query text can never disagree.

- [ ] **Step 1: Write the failing test**

```python
import sqlite3, pytest
from mcpbrain.store import Store, jsonb_supported, _meta_extract

def test_meta_extract_matches_index_expression(tmp_path):
    """The index and the query MUST use the same function or the index is dead."""
    p = tmp_path / "b.sqlite3"
    Store(str(p)).init()
    db = sqlite3.connect(p)
    idx = db.execute("SELECT sql FROM sqlite_master "
                     "WHERE name='idx_chunks_msgid'").fetchone()[0]
    db.close()
    assert _meta_extract("$.message_id") in idx


def test_message_id_lookup_uses_the_index(tmp_path):
    p = tmp_path / "b.sqlite3"
    s = Store(str(p)); s.init()
    with s._connect(write=True) as db:
        db.execute("INSERT INTO chunks(doc_id, text, metadata) VALUES(?,?,?)",
                   ("d1", "t", '{"message_id":"m1"}'))
    with s._connect() as db:
        plan = db.execute(
            f"EXPLAIN QUERY PLAN SELECT doc_id FROM chunks "
            f"WHERE {_meta_extract('$.message_id')} = 'm1'").fetchall()
    assert any("idx_chunks_msgid" in str(r) for r in plan), plan
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_metadata_jsonb.py -v`
Expected: FAIL — `ImportError: cannot import name 'jsonb_supported'`

- [ ] **Step 3: Write minimal implementation**

```python
def jsonb_supported() -> bool:
    """JSONB (binary JSON) lands in SQLite 3.45. Same guard rationale as
    fts5_supports_contentless()."""
    parts = tuple(int(x) for x in sqlite3.sqlite_version.split(".")[:2])
    return parts >= (3, 45)


def _meta_extract(path: str) -> str:
    """SQL fragment reading `path` out of chunks.metadata.

    Single source of truth so an expression INDEX and the QUERY that should use
    it can never drift apart — a mismatch silently produces a full table scan,
    which is exactly the 0.7.105 failure mode.
    """
    fn = "jsonb_extract" if jsonb_supported() else "json_extract"
    return f"{fn}(metadata,'{path}')"
```

Then rewrite the five `init()` expression indexes and every read site to build their SQL from `_meta_extract(...)`. Concretely, each index becomes:

```python
            for name, path in (
                ("idx_chunks_msgid",   "$.message_id"),
                ("idx_chunks_fileid",  "$.file_id"),
                ("idx_chunks_threadid", "$.thread_id"),
                ("idx_chunks_eventid", "$.event_id"),
            ):
                db.execute(f"CREATE INDEX IF NOT EXISTS {name} "
                           f"ON chunks({_meta_extract(path)})")
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_chunks_inbound_date ON chunks("
                f"COALESCE({_meta_extract('$.date')},"
                f"{_meta_extract('$.date_iso')}))")
```
 Preserve the 0.7.105 rewrites exactly: `doc_ids_for_messages` keeps its `UNION` form (an `OR` will not use expression indexes), and `chunks_for_file` keeps matching on `$.file_id` rather than `doc_id LIKE ... ESCAPE`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_metadata_jsonb.py -v`
Expected: PASS

- [ ] **Step 5: Run the 0.7.105 regression tests**

Run: `uv run pytest tests/ -q -k "index or metadata or doc_ids or chunks_for_file or inbound"`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/store.py tests/test_metadata_jsonb.py
git commit -m "perf(store): JSONB metadata via a single _meta_extract source of truth"
```

---

### Task 7: FK constraints, STRICT tables, trigram index in `init()`

**REVERTED 2026-08-25** (PR #25 finding 6, spec/plan defect): this task's
trigram index shipped with zero readers (`email_mentions` still scans
`chunks.text` directly) and populating it on the live store measured
+1.089 GB — erasing most of this whole plan's 57% storage saving. The plan
asked for the index without a task to wire a reader and without pricing the
populate cost. Rather than defer it (which is what actually shipped first,
behind an opt-in `--populate-trigram` flag on `bin/optimise_store.py`), it
was removed entirely: the `fts_chunks_trigram` schema, `_populate_trigram`,
`--populate-trigram`, and their tests are all gone. `email_mentions`'s
`text LIKE` remains genuinely unindexable, same as CLAUDE.md recorded
before this task — a future reader would likely want different tokenizer
settings anyway, so this is left as a clean slate for whoever actually
wires up a reader, rather than half-built infrastructure nothing uses. The
FK constraints and STRICT tables parts of this task stand; only the
trigram index was reverted. Kept below for its historical record.

**Files:**
- Modify: `mcpbrain/store.py` — `init()` table DDL, `_open_db` (`foreign_keys=ON`)
- Test: `tests/test_store_constraints.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `store.strict_supported() -> bool`.

- [ ] **Step 1: Write the failing test**

```python
import sqlite3, pytest
from mcpbrain.store import Store, strict_supported

def test_foreign_keys_are_enforced(tmp_path):
    p = tmp_path / "b.sqlite3"
    s = Store(str(p)); s.init()
    with pytest.raises(sqlite3.IntegrityError):
        with s._connect(write=True) as db:
            db.execute("INSERT INTO email_entities(message_id, entity_id) "
                       "VALUES('m1','NO_SUCH_ENTITY')")


def test_strict_tables_reject_wrong_types(tmp_path):
    if not strict_supported():
        pytest.skip("SQLite < 3.37")
    p = tmp_path / "b.sqlite3"
    s = Store(str(p)); s.init()
    with pytest.raises(sqlite3.IntegrityError):
        with s._connect(write=True) as db:
            db.execute("INSERT INTO chunks(doc_id, text, embedded) "
                       "VALUES('d1','t','not-an-integer')")


def test_email_mentions_like_is_index_backed(tmp_path):
    """CLAUDE.md records this LIKE as unindexable; a trigram index fixes that."""
    p = tmp_path / "b.sqlite3"
    s = Store(str(p)); s.init()
    with s._connect() as db:
        plan = db.execute(
            "EXPLAIN QUERY PLAN SELECT rowid FROM fts_chunks_trigram "
            "WHERE fts_chunks_trigram MATCH 'byford'").fetchall()
    assert plan
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_store_constraints.py -v`
Expected: FAIL — the insert succeeds (FKs off), so `pytest.raises` reports `DID NOT RAISE`.

- [ ] **Step 3: Write minimal implementation**

In `_open_db`, alongside the Task 2 pragmas:

```python
    db.execute("PRAGMA foreign_keys=ON")
```

In `init()`, declare `REFERENCES entities(id) ON DELETE CASCADE` on `entity_relations.entity_a`/`entity_b`, `email_entities.entity_id`, `entity_observations.entity_id`, `entity_communities.entity_id`. Add `STRICT` to each table when `strict_supported()`, mapping the **two `DATETIME` columns to `TEXT`** — STRICT permits only INT, INTEGER, REAL, TEXT, BLOB and ANY, so `DATETIME` is a hard error.

Add the trigram index:

```python
            # unicode61 cannot index a LIKE; trigram can. This is the fix for
            # the email_mentions `text LIKE` that CLAUDE.md records as
            # unindexable.
            db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS fts_chunks_trigram "
                       "USING fts5(text, content='', tokenize='trigram')")
```

Also add partial indexes for the `embedded=1` / `enriched=0` filters, matching the existing partial-index style already used by `idx_actions_waiting`, `idx_areas_org`, `idx_eo_entity_attr` and `idx_er_valid_now`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_store_constraints.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Run the graph and enrichment tests**

Run: `uv run pytest tests/ -q -k "entit or graph or merge or enrich or gardener or review"`
Expected: all pass. **A new `IntegrityError` here is a real finding, not a test to relax** — it means that code path was writing an orphan. Fix the writer.

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/store.py tests/test_store_constraints.py
git commit -m "feat(store): enforce foreign keys, STRICT tables, trigram index for LIKE"
```

---

### Task 8: The single out-of-place rebuild

**Files:**
- Modify: `bin/optimise_store.py`
- Test: `tests/test_optimise_store.py` (extend)

**Interfaces:**
- Consumes: `report_orphans` (Task 4); `Store.init()` (Tasks 5–7); `mcpbrain.backup.make_encrypted_snapshot`.
- Produces: `rebuild(src, dst, *, page_size=8192) -> dict` returning `{"copied": {...}, "dropped": {...}, "src_bytes": int, "dst_bytes": int}`.

- [ ] **Step 1: Write the failing test**

```python
from bin.optimise_store import rebuild

def test_rebuild_preserves_rows_and_drops_only_orphans(tmp_path):
    src, dst = tmp_path / "src.sqlite3", tmp_path / "dst.sqlite3"
    s = Store(str(src)); s.init()
    with s._connect(write=True) as db:
        db.execute("INSERT INTO entities(id,name,type) VALUES('e1','Real','person')")
        db.execute("INSERT INTO email_entities(message_id,entity_id) VALUES('m1','e1')")
        db.execute("INSERT INTO email_entities(message_id,entity_id) VALUES('m2','GONE')")
        db.execute("INSERT INTO chunks(doc_id,text,metadata,embedded) "
                   "VALUES('d1','hello',' {\"message_id\":\"m1\"}',1)")

    r = rebuild(src, dst)

    assert r["copied"]["chunks"] == 1
    assert r["copied"]["email_entities"] == 1
    assert r["dropped"]["email_entities.entity_id"] == 1


def test_rebuild_sets_the_larger_page_size(tmp_path):
    src, dst = tmp_path / "s.sqlite3", tmp_path / "d.sqlite3"
    Store(str(src)).init()
    rebuild(src, dst)
    import sqlite3
    db = sqlite3.connect(dst)
    assert db.execute("PRAGMA page_size").fetchone()[0] == 8192
    db.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_optimise_store.py -v`
Expected: FAIL — `ImportError: cannot import name 'rebuild'`

- [ ] **Step 3: Write minimal implementation**

```python
def rebuild(src, dst, *, page_size: int = 8192) -> dict:
    """Rebuild `src` into a fresh `dst` in ONE pass.

    Six of the target changes each require rewriting the whole file, so they
    are applied together: page_size, STRICT, JSONB, contentless FTS5, partial
    and trigram indexes, and FK constraints. Six sequential migrations would
    mean six full rewrites of a 2.6 GB file.

    page_size MUST be set before any table exists, so it comes first.
    """
    from mcpbrain.store import Store
    src, dst = Path(src), Path(dst)
    if dst.exists():
        raise FileExistsError(dst)

    db = sqlite3.connect(dst)
    db.execute(f"PRAGMA page_size={int(page_size)}")
    db.close()

    Store(str(dst)).init()   # single source of schema truth -- Tasks 5-7

    dropped = report_orphans(src)
    copied = _copy_all(src, dst)
    _rederive_fts(dst)
    d = sqlite3.connect(dst)
    d.execute("ANALYZE"); d.commit(); d.close()
    return {"copied": copied, "dropped": dropped,
            "src_bytes": src.stat().st_size, "dst_bytes": dst.stat().st_size}


# FTS5 and vec0 virtual tables plus their shadow tables are DERIVED. Copying
# shadow rows straight across would corrupt them (and would defeat the whole
# point of the contentless rebuild), so they are skipped and re-derived.
_SKIP_PREFIXES = ("fts_chunks", "vec_chunks", "sqlite_stat")

# Referential filter per table -- keep only rows whose parent still exists.
_KEEP = {
    "entity_relations": "entity_a IN (SELECT id FROM entities) "
                        "AND entity_b IN (SELECT id FROM entities)",
    "email_entities": "entity_id IN (SELECT id FROM entities)",
    "entity_observations": "entity_id IN (SELECT id FROM entities)",
    "entity_communities": "entity_id IN (SELECT id FROM entities)",
}


def _copy_all(src, dst, *, batch: int = 5000) -> dict:
    s_db = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    d_db = sqlite3.connect(dst)
    d_db.execute("PRAGMA foreign_keys=OFF")  # parents may land after children
    copied = {}
    try:
        tables = [r[0] for r in s_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        # entities first so the FK targets exist before dependents.
        tables.sort(key=lambda t: 0 if t == "entities" else 1)
        for t in tables:
            if t.startswith(_SKIP_PREFIXES):
                continue
            cols = [r[1] for r in s_db.execute(f'PRAGMA table_info("{t}")')]
            if not cols:
                continue
            collist = ",".join(f'"{c}"' for c in cols)
            ph = ",".join("?" * len(cols))
            where = f" WHERE {_KEEP[t]}" if t in _KEEP else ""
            cur = s_db.execute(f'SELECT {collist} FROM "{t}"{where}')
            n = 0
            while True:
                rows = cur.fetchmany(batch)
                if not rows:
                    break
                d_db.executemany(
                    f'INSERT INTO "{t}"({collist}) VALUES({ph})', rows)
                d_db.commit()
                n += len(rows)
            copied[t] = n
        # Force the FTS rebuild to actually consider every row. Copying
        # fts_context_version across verbatim would make reindex_fts_batch
        # think the work was already done and leave the index EMPTY.
        d_db.execute("UPDATE chunks SET fts_context_version=0")
        d_db.commit()
    finally:
        s_db.close(); d_db.close()
    return copied


def _rederive_fts(dst) -> None:
    """Rebuild FTS from chunks via the existing batch reindexer.

    Reuses Store.reindex_fts_batch so the contextual prefix is applied by the
    same _fts_text the write path uses -- never a second copy of that logic.
    """
    from mcpbrain.store import Store
    store = Store(str(dst))
    while store.reindex_fts_batch(limit=2000):
        pass
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_optimise_store.py -v`
Expected: PASS

- [ ] **Step 5: Add the CLI with its safety gates**

`main()` must, in order: refuse to run unless the daemon is stopped or the bulk lock is held; take an encrypted snapshot via `backup.make_encrypted_snapshot`; require `--yes` to proceed past the orphan report; rebuild to `<store>.new`; run `PRAGMA integrity_check` and `PRAGMA foreign_key_check`; compare row counts against `dropped`; and **leave the swap to a separate explicit `--swap` invocation**, retaining the old file.

- [ ] **Step 6: Dry run against a COPY of the live store**

```bash
cp ~/Library/Application\ Support/mcpbrain/brain.sqlite3 /tmp/live-copy.sqlite3
uv run python bin/optimise_store.py --src /tmp/live-copy.sqlite3 --dst /tmp/rebuilt.sqlite3 --yes
uv run python bin/measure_store.py --path /tmp/rebuilt.sqlite3
```

Expected: ≈2.62 GB → ≈1.6 GB; `dropped` totals **256**; `integrity_check` and `foreign_key_check` both `ok`.

- [ ] **Step 7: Commit**

```bash
git add bin/optimise_store.py tests/test_optimise_store.py
git commit -m "feat(bin): single-pass out-of-place store rebuild with orphan sweep + gates"
```

---

### Task 9: Gates and the runbook entry

**Files:**
- Modify: `docs/RELEASE-RUNBOOK.md` (a maintenance section for `bin/optimise_store.py`)
- Modify: `CLAUDE.md` (record the pragma baseline and the rebuild's attended, non-automatic status)

- [ ] **Step 1: Run the gold harness against the rebuilt copy**

Run the `--gold` harness against `/tmp/rebuilt.sqlite3`.
Expected: recall@10 **≥ 0.750**, MRR **≥ 0.514**.

**Contentless FTS5 and JSONB are ranking-neutral by construction, so any movement at all is a defect.** Investigate rather than accept. Note the known blind spot: the harness calls `hybrid_search` directly and so does not exercise `recall_max_distance` — check the injection path separately via `prompt_recall`.

- [ ] **Step 2: Record before/after**

Compare `/tmp/baseline.json` with a fresh run against the rebuilt copy: file size, the four benchmark method latencies, `has_stat1`.

- [ ] **Step 3: Document**

Add a runbook section covering: stop the daemon, snapshot, report orphans, rebuild, verify, `--swap`, restart, keep the old file until the next successful run. State plainly that this is **attended and never automatic**, following the `bin/consolidate.py` precedent.

- [ ] **Step 4: Run impacted tests and lint**

Run: `uv run pytest tests/test_store_pragmas.py tests/test_fts_contentless.py tests/test_metadata_jsonb.py tests/test_store_constraints.py tests/test_optimise_store.py tests/test_measure_store.py tests/test_store.py tests/test_store_schema.py tests/test_store_schema_p3.py tests/test_store_write_txn.py -q && uv run ruff check mcpbrain/ bin/`
Expected: all pass, ruff clean. Ask Josh to run the full suite.

- [ ] **Step 5: Commit**

```bash
git add docs/RELEASE-RUNBOOK.md CLAUDE.md
git commit -m "docs: record SQLite optimisation gates and the attended rebuild procedure"
```

---

### Task 10: Prove rollback works before trusting the swap

The spec's rollback path is the existing encrypted-snapshot restore. Untested rollback is not rollback.

**Files:**
- Test: `tests/test_optimise_store.py` (extend)

- [ ] **Step 1: Write the failing test**

```python
def test_snapshot_taken_before_rebuild_restores_the_original(tmp_path):
    from mcpbrain import backup
    src = tmp_path / "src.sqlite3"
    s = Store(str(src)); s.init()
    with s._connect(write=True) as db:
        db.execute("INSERT INTO chunks(doc_id,text,embedded) VALUES('d1','keep me',1)")

    key = backup.generate_escrow_key()
    enc = tmp_path / "snap.enc"
    backup.make_encrypted_snapshot(src, enc, key)

    src.unlink()                      # simulate a failed rebuild + lost original
    restored = tmp_path / "restored.sqlite3"
    backup.restore(enc, restored, key)

    import sqlite3
    db = sqlite3.connect(restored)
    assert db.execute("SELECT text FROM chunks WHERE doc_id='d1'").fetchone()[0] == "keep me"
    db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_optimise_store.py::test_snapshot_taken_before_rebuild_restores_the_original -v`
Expected: FAIL until the rebuild CLI actually takes the snapshot in the right order.

- [ ] **Step 3: Wire the snapshot into `main()` as the first action**

The snapshot must be taken and **verified** before a single byte is written to `<store>.new`. Verify by decrypting to a temp path and running `PRAGMA integrity_check` on the result — an unverified snapshot is not a rollback.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_optimise_store.py -v`
Expected: PASS

- [ ] **Step 5: Rehearse the real rollback on the live copy**

Rebuild `/tmp/live-copy.sqlite3`, then restore from the snapshot taken in step 3 and confirm row counts match the pre-rebuild baseline from Task 1.

- [ ] **Step 6: Commit**

```bash
git add bin/optimise_store.py tests/test_optimise_store.py
git commit -m "test(bin): prove the pre-rebuild snapshot actually restores"
```
