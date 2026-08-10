# Backup Correctness and Size Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `backup.snapshot()` produce a consistent artifact that cannot be blocked by a reader or torn by a writer, and shrink that artifact from a 15.65 GB file copy to ~4.7 GB of live data.

**Architecture:** `snapshot()` replaces `wal_checkpoint(TRUNCATE)` + `shutil.copy2` with `VACUUM INTO`, which is consistent by construction (so causes (R) and (W) and the torn copy all disappear) and excludes free pages. Separately, `enrich_payloads` — 85% of the store — is re-keyed from one row per chunk to one row per Drive file, matching how it is actually read. The freed pages land on the freelist and `VACUUM INTO` then excludes them from every artifact, so no install needs an attended reclaim.

**Tech Stack:** Python 3, SQLite (WAL, sqlite-vec `vec0`, FTS5), pytest.

**Spec:** `docs/superpowers/specs/2026-08-10-backup-correctness-and-size-design.md`

## Global Constraints

- **No pytest-asyncio.** Nothing in this plan needs async tests.
- **TDD.** Every task writes a failing test, runs it to confirm it fails for the stated reason, then implements.
- **Stage files by explicit path.** Never `git add -A`, `git add .`, or `git commit -a` — this working tree is shared with other Claude sessions.
- **Check `git status` before each commit.** Commit only the files your task names.
- **Do not touch version files** (`pyproject.toml`, `mcpbrain/__init__.py`, `plugin/.claude-plugin/plugin.json`, `plugin/.claude-plugin/marketplace.json`, `plugin/mcpb/manifest.json`) and **do not release**.
- **Scope test runs** to the edited and directly impacted files. Josh runs the full suite himself.
- **Parts 1 and 2 ship together.** Either alone still copies 15.65 GB. Do not release after Task 2.
- **Null-instrument rule.** Any test or probe asserting an absence (no busy, no corruption, no loss) must first assert the condition under which it *would* fail. Two null instruments were already caught during design; see the spec's "A note on instruments".
- **Verified facts** (measured 2026-08-10, do not re-derive): `VACUUM INTO` accepts a bound parameter; it raises `sqlite3.OperationalError: output file already exists` if the destination exists; the artifact's `journal_mode` is `delete`, not `wal`.

---

### Task 1: `snapshot()` produces a consistent copy with no checkpoint

**Files:**
- Modify: `mcpbrain/backup.py:1-30` (module docstring), `mcpbrain/backup.py:122-166` (`snapshot`)
- Modify: `mcpbrain/daemon.py:3864-3887` (the `_bulk_lock` rationale comment)
- Modify: `tests/test_daemon.py:1233-1240` (docstring asserting the old rationale)
- Modify: `bin/consolidate.py:28-37`, `bin/repair.py:66-71` (comments describing the mechanism)
- Test: `tests/test_backup.py`

**Interfaces:**
- Consumes: `mcpbrain.store._open_db(path, read_only=False)` — existing.
- Produces: `backup.snapshot(store_path, out_path) -> Path`. Signature unchanged. New behaviour: never executes `wal_checkpoint`; raises `sqlite3.OperationalError` (not `RuntimeError`) if the copy fails; leaves no file at `out_path` on failure.

- [ ] **Step 1: Write the failing test for cause (R)**

Add to `tests/test_backup.py`:

```python
def test_snapshot_succeeds_while_a_read_transaction_is_held(tmp_path):
    """Cause (R) from Finding 3: one open read transaction on an older snapshot
    blocks wal_checkpoint(TRUNCATE) absolutely — busy=1 on 6/6 live attempts,
    checkpointed_frames=0 every time. snapshot() must not depend on that
    checkpoint at all.

    NULL-INSTRUMENT GUARD: the WAL must be non-empty when the snapshot runs.
    With an empty WAL, TRUNCATE returns busy=0 regardless of concurrency, so
    this test could not go red — the exact failure that made the 2026-08-04
    idle measurement worthless.
    """
    from mcpbrain.store import _open_db

    store = Store(tmp_path / "live.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("d1", "before", "h1", {})

    reader = _open_db(store.path, read_only=False)
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM chunks").fetchone()  # pins the snapshot
    try:
        for i in range(50):
            store.upsert_chunk(f"d-{i}", f"body {i}", f"h-{i}", {})

        wal = Path(f"{store.path}-wal")
        assert wal.exists() and wal.stat().st_size > 0, (
            "NULL INSTRUMENT: the WAL is empty, so TRUNCATE would have returned "
            "busy=0 regardless and this test proves nothing")

        out = snapshot(store.path, tmp_path / "snap.sqlite3")
    finally:
        reader.rollback()
        reader.close()

    loaded = Store(out, dim=4)
    assert loaded.get_chunk("d1") is not None
    assert loaded.get_chunk("d-49") is not None
```

`Path` is already imported in this test module; if not, add `from pathlib import Path`.

- [ ] **Step 2: Run it and confirm it fails for the right reason**

Run: `uv run pytest tests/test_backup.py::test_snapshot_succeeds_while_a_read_transaction_is_held -v`

Expected: FAIL with `RuntimeError: wal_checkpoint(TRUNCATE) busy=1; snapshot aborted…`. If it fails on the null-instrument assertion instead, the test is broken, not the code — fix the test first.

- [ ] **Step 3: Replace the body of `snapshot()`**

In `mcpbrain/backup.py`, replace lines 122-166 with:

```python
def snapshot(store_path, out_path) -> Path:
    """Produce a single-file snapshot of the derived store at store_path.

    Uses VACUUM INTO, which builds the output from one consistent read
    transaction. That choice is load-bearing in three ways:

    1. It needs no exclusive checkpoint. PRAGMA wal_checkpoint(TRUNCATE) blocks
       until there is no writer AND every reader is on the newest snapshot, so
       a single held read transaction blocks it absolutely — measured busy=1 on
       6 of 6 attempts with checkpointed_frames=0, against brain_graph reads
       that run 6.3s median on the live store and outlive the 5000ms
       busy_timeout. Removing the need for the checkpoint removes that class.
    2. It cannot be torn. The previous implementation checkpointed and then
       shutil.copy2'd the main DB file, during which any connection's
       wal_autocheckpoint could write pages into that file mid-copy. _bulk_lock
       does not cover the daemon's control-API threads, which is where routed
       tool writes execute. A busy=0 result never made the following copy safe.
    3. It excludes free pages, which is what keeps the artifact small once
       per-file enrich_payloads keying frees ~11.3GB onto the freelist.

    Raises before writing anything if the destination cannot be cleared, and
    unlinks a partial destination if the copy fails, so a returned path always
    reflects a complete artifact. Accepts str or Path for both arguments.
    """
    store_path = Path(store_path)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # VACUUM INTO refuses outright if the output exists ("output file already
    # exists"), so clearing is required, not defensive. The sidecars matter
    # too: a stale -wal left beside a previous artifact would be applied over
    # the fresh one the first time it is opened.
    _clear_artifact(out_path)

    # Write-mode connection. mode=ro would be a better fit for a read-only
    # operation, but a read-only connection cannot create the -shm file when
    # nothing else has the DB open — which is exactly how bin/repair.py and
    # bin/consolidate.py run, with the daemon stopped.
    db = _open_db(store_path, read_only=False)
    try:
        db.execute("VACUUM INTO ?", (str(out_path),))
    except BaseException:
        _clear_artifact(out_path)
        raise
    finally:
        db.close()
    return out_path


def _clear_artifact(out_path: Path) -> None:
    """Remove an artifact path and any sidecars left beside it."""
    for p in (out_path, Path(f"{out_path}-wal"), Path(f"{out_path}-shm")):
        p.unlink(missing_ok=True)
```

- [ ] **Step 4: Run the test and the rest of the backup suite**

Run: `uv run pytest tests/test_backup.py -v`

Expected: the new test PASSES. `test_checkpoint_runs_before_copy` and `test_snapshot_raises_on_busy_checkpoint_no_partial_file` now FAIL — they pin the old mechanism and are replaced in Steps 5-6. The four "snapshot contains the latest committed writes" tests must still pass; if any fails, stop, that is a real regression.

- [ ] **Step 5: Invert the checkpoint test**

Replace `test_checkpoint_runs_before_copy` in `tests/test_backup.py` entirely. Relaxing it is not enough — inverted, it stops anyone reintroducing the exclusive checkpoint later:

```python
def test_snapshot_never_runs_an_exclusive_checkpoint(tmp_path, monkeypatch):
    """The inverse of the old test_checkpoint_runs_before_copy, which pinned
    the defect in place. wal_checkpoint(TRUNCATE) is what cause (R) blocks, so
    the backup path must not issue one at all."""
    import mcpbrain.backup as backup_mod

    store = Store(tmp_path / "live.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("d1", "hello", "h1", {})

    seen = []
    real_open_db = backup_mod._open_db

    def spy_open_db(*args, **kwargs):
        conn = real_open_db(*args, **kwargs)

        class _Proxy:
            def __getattr__(self, name):
                return getattr(conn, name)

            def execute(self, sql, *a, **k):
                seen.append(sql)
                return conn.execute(sql, *a, **k)

        return _Proxy()

    monkeypatch.setattr(backup_mod, "_open_db", spy_open_db)
    snapshot(store.path, tmp_path / "snap.sqlite3")

    assert not any("wal_checkpoint" in s.lower() for s in seen), (
        f"snapshot() issued a checkpoint: {seen}")
    assert any("vacuum into" in s.lower() for s in seen), (
        f"snapshot() did not use VACUUM INTO: {seen}")
```

- [ ] **Step 6: Replace the busy-abort test with a failure-mid-copy test**

Replace `test_snapshot_raises_on_busy_checkpoint_no_partial_file` with:

```python
def test_snapshot_leaves_no_partial_artifact_when_the_copy_fails(tmp_path, monkeypatch):
    """The old busy-abort contract, preserved under the new mechanism: a
    failure must leave nothing that looks like a successful artifact to
    make_encrypted_snapshot."""
    import sqlite3

    import pytest

    import mcpbrain.backup as backup_mod

    store = Store(tmp_path / "live.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("d1", "hello", "h1", {})

    out = tmp_path / "snap.sqlite3"
    real_open_db = backup_mod._open_db

    def spy_open_db(*args, **kwargs):
        conn = real_open_db(*args, **kwargs)

        class _Proxy:
            def __getattr__(self, name):
                return getattr(conn, name)

            def execute(self, sql, *a, **k):
                if "vacuum into" in sql.lower():
                    out.write_bytes(b"partial")   # a half-written artifact
                    raise sqlite3.OperationalError("disk I/O error")
                return conn.execute(sql, *a, **k)

        return _Proxy()

    monkeypatch.setattr(backup_mod, "_open_db", spy_open_db)

    with pytest.raises(sqlite3.OperationalError):
        snapshot(store.path, out)

    assert not out.exists(), "a partial artifact was left behind"


def test_snapshot_clears_a_pre_existing_destination_and_its_sidecars(tmp_path):
    """VACUUM INTO refuses to overwrite, and a stale -wal beside a previous
    artifact would be applied over the fresh one on first open."""
    store = Store(tmp_path / "live.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("d1", "hello", "h1", {})

    out = tmp_path / "snap.sqlite3"
    out.write_bytes(b"stale artifact")
    Path(f"{out}-wal").write_bytes(b"stale wal")
    Path(f"{out}-shm").write_bytes(b"stale shm")

    snapshot(store.path, out)

    assert Store(out, dim=4).get_chunk("d1") is not None
    assert not Path(f"{out}-wal").exists()
    assert not Path(f"{out}-shm").exists()


def test_snapshot_artifact_opens_and_ends_in_wal_mode(tmp_path):
    """VACUUM INTO writes a fresh DB whose header says rollback-journal, where
    copy2 preserved WAL. init() converts it on open; this pins that the restore
    path is unaffected."""
    from mcpbrain.store import _open_db

    store = Store(tmp_path / "live.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("d1", "hello", "h1", {})

    out = snapshot(store.path, tmp_path / "snap.sqlite3")

    db = _open_db(out, read_only=False)
    try:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        db.close()

    restored = Store(out, dim=4)
    restored.init()
    db = _open_db(out, read_only=False)
    try:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        db.close()
    assert restored.get_chunk("d1") is not None
```

- [ ] **Step 7: Add the concurrent-writer consistency test**

Spec test #2. This is the torn-copy half, and the spec is explicit about how to handle it honestly: attempt a true RED against the old implementation, and if that proves flaky, keep it as a positive guard and **say so in the commit message** rather than claim a reproduction you did not get.

```python
def test_snapshot_is_consistent_under_a_concurrent_writer(tmp_path):
    """The torn-copy half. The old implementation checkpointed and then copied
    the DB file for minutes, during which any connection's wal_autocheckpoint
    could write pages into that file mid-copy. VACUUM INTO builds from one
    consistent read transaction and cannot be torn.

    The artifact is a point-in-time snapshot, so rows committed DURING it may
    or may not appear — what must hold is that everything committed BEFORE the
    call is present and the result is a valid database.
    """
    import threading

    from mcpbrain.store import _open_db

    store = Store(tmp_path / "live.sqlite3", dim=4)
    store.init()
    for i in range(200):
        store.upsert_chunk(f"pre-{i}", f"before {i}", f"hp{i}", {})

    stop = threading.Event()
    written = []

    def writer():
        i = 0
        while not stop.is_set():
            store.upsert_chunk(f"dur-{i}", f"during {i}", f"hd{i}", {})
            written.append(i)
            i += 1

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    try:
        while len(written) < 20:          # ensure the writer is genuinely live
            pass
        out = snapshot(store.path, tmp_path / "snap.sqlite3")
    finally:
        stop.set()
        t.join(timeout=10)

    assert written, "NULL INSTRUMENT: the writer never committed anything"

    db = _open_db(out, read_only=False)
    try:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        missing = [i for i in range(200) if db.execute(
            "SELECT 1 FROM chunks WHERE doc_id=?", (f"pre-{i}",)).fetchone() is None]
    finally:
        db.close()
    assert not missing, f"pre-snapshot rows missing from the artifact: {missing[:5]}"
```

- [ ] **Step 8: Run the full backup suite**

Run: `uv run pytest tests/test_backup.py -v`
Expected: all PASS.

- [ ] **Step 9: Correct every comment that states the old rationale**

These assert things that are now false. The behaviour they guard stays; only the reason changes.

`mcpbrain/backup.py:1-8` — replace the module docstring's opening paragraph:

```python
"""Store snapshot (Phase 5, Task 5.1).

Produces a single-file snapshot of the derived store. The store runs with
journal_mode=WAL, so committed writes can live in the `-wal` sidecar and a bare
copy of the main `.sqlite3` file alone can MISS them. snapshot() therefore uses
VACUUM INTO, which reads through the WAL under one consistent read transaction
and needs no exclusive checkpoint — see snapshot()'s own docstring for why the
previous checkpoint-then-copy approach was both blockable and tearable.
```

`mcpbrain/daemon.py:3864-3887` — replace the `_bulk_lock` justification. The lock stays; its reason changes:

```python
                # Backup self-gates on configured + due; harmless when paused
                # (a snapshot of current state). Runs in this loop thread, so it
                # shares the single-writer lock the daemon already holds.
                #
                # Held under _bulk_lock, but NOT for the reason this comment
                # used to give. snapshot() no longer runs wal_checkpoint and no
                # longer copies the DB file, so neither the busy-abort nor the
                # torn-copy hazard exists any more. What remains is that
                # VACUUM INTO pins a read transaction for the whole multi-minute
                # rebuild: no checkpoint can advance past it, so the WAL grows
                # for that window. Holding the four chunk-writing passes off
                # bounds that growth and the I/O contention alongside it.
                #
                # The acquire itself is BOUNDED (_backup_under_bulk_lock), the
                # same shape as the gated passes' own acquire on the other side
                # of this lock: a gated pass's execution time is not bounded by
                # this plan, only its acquire is, so it can legitimately hold
                # _bulk_lock past one tick -- an unbounded `with` here would
                # park this cycle thread for that pass's whole duration.
```

`tests/test_daemon.py:1233-1240` — replace the docstring of `test_run_loop_holds_the_bulk_lock_across_the_backup` (the assertions are unchanged and must keep passing):

```python
    """maybe_backup must run under _bulk_lock, exactly as run_one() does.

    The original reason (a busy wal_checkpoint aborting, or a racing write
    tripping wal_autocheckpoint mid-copy2 and tearing the snapshot) no longer
    applies — snapshot() uses VACUUM INTO and does neither. The lock is still
    required because that rebuild pins a read transaction for minutes, during
    which no checkpoint can advance and the WAL grows; holding the
    chunk-writing passes off bounds it.
    """
```

`bin/consolidate.py:28-37` — replace the comment inside `_backup_db`:

```python
def _backup_db(db_path: Path) -> Path:
    # WAL-safe backup: the store runs journal_mode=WAL, so committed writes can
    # live in the -wal sidecar and a bare shutil.copy2 of the main .sqlite3 file
    # can silently MISS them. backup.snapshot() uses VACUUM INTO, which reads
    # through the WAL under one consistent read transaction — so the .bak is a
    # complete, restorable snapshot even while the daemon is writing. This is
    # the reversibility guarantee for the destructive migration.
```

`bin/repair.py:66-68` — the existing comment already claims "the SQLite backup API", which was never true. Make it accurate:

```python
def _backup(db_path: Path) -> Path:
    # WAL-safe: the store runs journal_mode=WAL, so a plain file copy can MISS
    # committed transactions. backup.snapshot() uses VACUUM INTO, which is
    # consistent by construction and cannot be blocked by a held reader.
```

`tests/test_backup.py:1-8` — replace the module docstring:

```python
"""Tests for mcpbrain.backup — store snapshot via VACUUM INTO.

The store runs journal_mode=WAL. Committed writes can live in the -wal sidecar,
so a bare copy of the main .sqlite3 file can MISS the latest writes. snapshot()
uses VACUUM INTO, which reads through the WAL under one consistent read
transaction: it needs no exclusive checkpoint (which a single held reader
blocks absolutely) and cannot be torn by a concurrent autocheckpoint. The
latest-writes roundtrip test below is the behavioural proof: a freshly
committed row must survive the snapshot.
"""
```

- [ ] **Step 10: Run the impacted tests**

Run: `uv run pytest tests/test_backup.py tests/test_daemon.py tests/test_consolidate.py -q`
Expected: all PASS.

- [ ] **Step 11: Commit**

```bash
git status --short
git add mcpbrain/backup.py mcpbrain/daemon.py bin/consolidate.py bin/repair.py \
        tests/test_backup.py tests/test_daemon.py
git commit -m "fix(backup): snapshot via VACUUM INTO, so a held reader cannot block it

wal_checkpoint(TRUNCATE) needs no writer AND every reader on the newest
snapshot, so one held read transaction blocks it absolutely: busy=1 on 6/6
measured attempts with checkpointed_frames=0, against brain_graph reads that
run 6.3s median and outlive the 5000ms busy_timeout. The copy2 that followed a
clean checkpoint was never safe either -- a concurrent autocheckpoint can write
into the main file mid-copy, and _bulk_lock does not cover the control-API
threads where routed tool writes now run.

VACUUM INTO builds the artifact from one consistent read transaction, so it
needs no checkpoint and cannot be torn. Comments and test docstrings asserting
the old rationale are corrected rather than left: the behaviour they guard is
still right, the reason they give is not."
```

---

### Task 2: The artifact carries an intact vector index

**Files:**
- Modify: `mcpbrain/backup.py` (`snapshot`, plus a new `_verify_artifact`)
- Test: `tests/test_backup.py`

**Interfaces:**
- Consumes: `backup.snapshot` from Task 1, `backup._clear_artifact` from Task 1.
- Produces: `backup._verify_artifact(out_path) -> None`, raising `RuntimeError` on a failed probe. Called at the end of `snapshot()` before returning.

Why this exists: Task 1 swapped a page copy for a **logical rebuild**, and the hazard it introduces is that a vec0 shadow table does not survive it. `vec_chunks_vector_chunks00` is declared `rowid PRIMARY KEY` with no type — not an `INTEGER PRIMARY KEY` alias by the strict rule — and on the live store holds 274 rows spanning rowids 1–450, i.e. 176 gaps for a renumbering VACUUM to close. A design probe measured that rowids **are** preserved and KNN is identical; this task is the regression guard, because a silently repointed vector index would be discovered only at restore.

- [ ] **Step 1: Write the failing fidelity test**

Add to `tests/test_backup.py`:

```python
def test_snapshot_preserves_the_vector_index_across_the_rebuild(tmp_path):
    """VACUUM may renumber rowids of tables without an INTEGER PRIMARY KEY, and
    vec_chunks_vector_chunks00 is declared `rowid PRIMARY KEY` untyped. If it
    renumbered while vec_chunks_chunks.chunk_id (INTEGER PRIMARY KEY
    AUTOINCREMENT) did not, KNN would silently return the wrong chunks.

    NULL-INSTRUMENT GUARD: gaps must exist in that table before the snapshot.
    With contiguous rowids a renumbering VACUUM is the identity map and this
    test could not go red — the first design probe passed for exactly that
    reason and proved nothing.
    """
    import struct

    from mcpbrain.store import _open_db

    dim = 8
    store = Store(tmp_path / "live.sqlite3", dim=dim)
    store.init()

    def vec(i):
        return [((i * 7919 + j * 104729) % 1000) / 1000.0 for j in range(dim)]

    def ser(v):
        return struct.pack(f"{len(v)}f", *v)

    db = _open_db(store.path, read_only=False)
    db.execute("BEGIN")
    for i in range(6000):          # > 5 vec0 chunks at the 1024 default
        db.execute("INSERT INTO chunks(rowid,doc_id,text,content_hash,metadata,embedded) "
                   "VALUES(?,?,?,?,'{}',1)", (i + 1, f"doc-{i:05d}", f"text {i}", f"h{i}"))
        db.execute("INSERT INTO vec_chunks(rowid,embedding) VALUES(?,?)", (i + 1, ser(vec(i))))
    db.execute("COMMIT")
    db.execute("BEGIN")            # free whole vector chunks -> rowid gaps
    db.execute("DELETE FROM vec_chunks WHERE rowid BETWEEN 1100 AND 4200")
    db.execute("DELETE FROM chunks WHERE rowid BETWEEN 1100 AND 4200")
    db.execute("COMMIT")

    cnt, lo, hi = db.execute(
        "SELECT count(*), min(rowid), max(rowid) FROM vec_chunks_vector_chunks00").fetchone()
    assert (hi - lo + 1) - cnt > 0, (
        "NULL INSTRUMENT: no rowid gaps, a renumbering VACUUM would be the "
        "identity map and this test could not fail")

    def knn(path, q, k=10):
        d = _open_db(path, read_only=False)
        try:
            return [(r["doc_id"], round(r["distance"], 6)) for r in d.execute(
                "SELECT c.doc_id, v.distance FROM vec_chunks v "
                "JOIN chunks c ON c.rowid = v.rowid "
                "WHERE v.embedding MATCH ? AND k = ? ORDER BY v.distance", (ser(q), k))]
        finally:
            d.close()

    queries = [vec(i * 137 + 3) for i in range(4)]
    before = [knn(store.path, q) for q in queries]
    fts_before = db.execute(
        "SELECT count(*) FROM fts_chunks WHERE fts_chunks MATCH 'text'").fetchone()[0]
    db.close()

    out = snapshot(store.path, tmp_path / "snap.sqlite3")

    assert [knn(out, q) for q in queries] == before, "KNN differs after the rebuild"
    d = _open_db(out, read_only=False)
    try:
        assert d.execute("SELECT count(*) FROM fts_chunks "
                         "WHERE fts_chunks MATCH 'text'").fetchone()[0] == fts_before
    finally:
        d.close()
```

- [ ] **Step 2: Run it**

Run: `uv run pytest tests/test_backup.py::test_snapshot_preserves_the_vector_index_across_the_rebuild -v`

Expected: PASS immediately — the design probe already measured that rowids survive. This is a guard, not a red-green cycle. If it FAILS, stop and report: the mechanism chosen in the spec is invalid and Task 1 must be reverted in favour of the `Connection.backup(pages=-1)` fallback named in the spec's Rejected alternatives.

- [ ] **Step 3: Write the failing test for the runtime smoke check**

```python
def test_snapshot_rejects_an_artifact_whose_vectors_do_not_resolve(tmp_path, monkeypatch):
    """The runtime guard: snapshot() must not hand back an artifact whose
    vector index does not resolve. Simulated by corrupting the artifact between
    the copy and the check."""
    import pytest

    import mcpbrain.backup as backup_mod
    from mcpbrain.store import _open_db

    store = Store(tmp_path / "live.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("d1", "hello", "h1", {})
    with store._connect(write=True) as db:
        rid = db.execute("SELECT rowid FROM chunks WHERE doc_id='d1'").fetchone()["rowid"]
    store.write_embedding(rid, [0.1, 0.2, 0.3, 0.4])

    real_verify = backup_mod._verify_artifact

    def corrupt_then_verify(out_path):
        d = _open_db(out_path, read_only=False)
        try:
            d.execute("DELETE FROM vec_chunks")   # chunks still claim embedded=1
            d.commit()
        finally:
            d.close()
        return real_verify(out_path)

    monkeypatch.setattr(backup_mod, "_verify_artifact", corrupt_then_verify)

    out = tmp_path / "snap.sqlite3"
    with pytest.raises(RuntimeError, match="vector"):
        snapshot(store.path, out)
    assert not out.exists(), "a failed artifact must not be left behind"
```

- [ ] **Step 4: Run it to verify it fails**

Run: `uv run pytest tests/test_backup.py::test_snapshot_rejects_an_artifact_whose_vectors_do_not_resolve -v`
Expected: FAIL with `AttributeError: module 'mcpbrain.backup' has no attribute '_verify_artifact'`.

- [ ] **Step 5: Implement `_verify_artifact` and call it**

Add to `mcpbrain/backup.py`, after `_clear_artifact`:

```python
_VERIFY_SAMPLE = 20


def _verify_artifact(out_path) -> None:
    """Check that the rebuilt artifact's vector index still resolves.

    Deliberately narrow. It probes the ARTIFACT ALONE and asserts an internal
    invariant — every sampled embedded chunk's rowid resolves to a vector of
    uniform, non-zero length. It does NOT compare against the source: the
    daemon writes throughout a multi-minute rebuild, so any source-vs-artifact
    count or KNN comparison would be legitimately unequal and flaky. The
    stronger source-equality check belongs in the test suite and the live gate,
    where the store is quiescent.

    It is also NOT an integrity_check: that re-reads the whole artifact and is
    not the best detector of the hazard this mechanism actually introduces,
    which is a vec0 shadow table not surviving the rebuild.

    Silent no-op when the store has no embedded chunks or no vec0 table — the
    probe has nothing to say, and bin/repair.py snapshots stores that may
    already be broken. A probe that raised there would block the very safety
    copy it exists to protect.
    """
    db = _open_db(out_path, read_only=False)
    try:
        try:
            rowids = [r[0] for r in db.execute(
                "SELECT rowid FROM chunks WHERE embedded=1 "
                "ORDER BY rowid LIMIT ?", (_VERIFY_SAMPLE,))]
        except sqlite3.DatabaseError:
            return                      # no chunks table: nothing to verify
        if not rowids:
            return

        lengths = set()
        for rid in rowids:
            try:
                row = db.execute(
                    "SELECT embedding FROM vec_chunks WHERE rowid=?", (rid,)).fetchone()
            except sqlite3.DatabaseError as exc:
                raise RuntimeError(
                    f"snapshot artifact {out_path}: vector lookup failed for "
                    f"chunk rowid {rid} ({exc}); the rebuild did not preserve "
                    "the vec0 index") from exc
            if row is None or not row[0]:
                raise RuntimeError(
                    f"snapshot artifact {out_path}: chunk rowid {rid} is marked "
                    "embedded but its vector does not resolve; the rebuild did "
                    "not preserve the vec0 index")
            lengths.add(len(bytes(row[0])))

        if len(lengths) != 1:
            raise RuntimeError(
                f"snapshot artifact {out_path}: sampled vectors have differing "
                f"lengths {sorted(lengths)}; the rebuild did not preserve the "
                "vec0 index")
    finally:
        db.close()
```

Add `import sqlite3` to `mcpbrain/backup.py`'s imports if absent.

Then in `snapshot()`, replace the `return out_path` at the end with:

```python
    try:
        _verify_artifact(out_path)
    except BaseException:
        _clear_artifact(out_path)
        raise
    return out_path
```

- [ ] **Step 6: Run the suite**

Run: `uv run pytest tests/test_backup.py -v`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git status --short
git add mcpbrain/backup.py tests/test_backup.py
git commit -m "fix(backup): verify the rebuilt artifact's vector index resolves

VACUUM INTO is a logical rebuild, and the hazard it introduces is a vec0 shadow
table not surviving it: vec_chunks_vector_chunks00 is declared untyped
\`rowid PRIMARY KEY\`, and on the live store holds 274 rows across rowids 1-450
-- 176 gaps for a renumbering VACUUM to close, while vec_chunks_chunks.chunk_id
is INTEGER PRIMARY KEY and would not move. Measured: rowids ARE preserved and
KNN is identical. Both guards are regression protection, since a repointed
index would otherwise surface only at restore.

The runtime check probes the artifact alone -- the daemon writes throughout a
multi-minute rebuild, so source-vs-artifact comparison would be legitimately
unequal. It no-ops on a store with no embedded chunks so bin/repair.py can
still snapshot an already-broken store."
```

---

### Task 3: `enrich_payloads` gains a file-keyed table, with the legacy one set aside

**Files:**
- Modify: `mcpbrain/store.py:964-970` (schema block inside `init()`)
- Test: `tests/test_store.py`

**Interfaces:**
- Produces: table `enrich_payloads(file_id TEXT PRIMARY KEY, payload TEXT NOT NULL, logic_version INTEGER DEFAULT 0, at TEXT DEFAULT CURRENT_TIMESTAMP)`; table `enrich_payloads_legacy` present only on a store that predates this change. Also `store._file_key_from_doc_id(doc_id) -> str | None`, used by Tasks 4 and 5.

Why a rename-aside rather than an in-place migration: collapsing 41,916 rows means freeing their overflow pages, which is minutes of I/O on 13.5 GB. That cannot run on `init()`, which blocks daemon startup. Renaming the old table is metadata-only and instant; Task 5 drains it in the background. Renaming rather than dropping keeps the cached payloads, which are worth real Haiku tokens to peers.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_store.py`:

```python
def test_init_creates_the_file_keyed_enrich_payloads_table(tmp_path):
    store = Store(tmp_path / "s.sqlite3", dim=4)
    store.init()
    with store._connect() as db:
        cols = {r["name"] for r in db.execute("PRAGMA table_info(enrich_payloads)")}
    assert "file_id" in cols and "doc_id" not in cols


def test_init_sets_a_legacy_doc_keyed_table_aside(tmp_path):
    """A store written before this change is keyed per chunk doc_id. init()
    must move it aside instantly (metadata-only) rather than collapse it —
    freeing 41,916 rows' overflow pages is minutes of I/O and init() blocks
    daemon startup."""
    path = tmp_path / "s.sqlite3"
    store = Store(path, dim=4)
    store.init()
    with store._connect(write=True) as db:            # recreate the OLD shape
        db.execute("DROP TABLE enrich_payloads")
        db.execute("CREATE TABLE enrich_payloads(doc_id TEXT PRIMARY KEY, "
                   "payload TEXT NOT NULL, logic_version INTEGER DEFAULT 0, "
                   "at TEXT DEFAULT CURRENT_TIMESTAMP)")
        db.executemany("INSERT INTO enrich_payloads(doc_id,payload,logic_version) "
                       "VALUES(?,?,?)",
                       [(f"gdrive-FILE1-{i}", '{"x":1}', 3) for i in range(5)])

    Store(path, dim=4).init()

    with store._connect() as db:
        cols = {r["name"] for r in db.execute("PRAGMA table_info(enrich_payloads)")}
        assert "file_id" in cols and "doc_id" not in cols
        assert db.execute("SELECT count(*) FROM enrich_payloads").fetchone()[0] == 0
        assert db.execute(
            "SELECT count(*) FROM enrich_payloads_legacy").fetchone()[0] == 5


def test_init_is_idempotent_over_the_legacy_rename(tmp_path):
    """Restoring a pre-fix artifact into a post-fix build must migrate it, and
    running init() again must not clobber a legacy table still draining."""
    path = tmp_path / "s.sqlite3"
    store = Store(path, dim=4)
    store.init()
    with store._connect(write=True) as db:
        db.execute("DROP TABLE enrich_payloads")
        db.execute("CREATE TABLE enrich_payloads(doc_id TEXT PRIMARY KEY, "
                   "payload TEXT NOT NULL, logic_version INTEGER DEFAULT 0, "
                   "at TEXT DEFAULT CURRENT_TIMESTAMP)")
        db.execute("INSERT INTO enrich_payloads(doc_id,payload) VALUES('gdrive-A-0','{}')")

    Store(path, dim=4).init()
    Store(path, dim=4).init()          # second run must be a no-op

    with store._connect() as db:
        assert db.execute(
            "SELECT count(*) FROM enrich_payloads_legacy").fetchone()[0] == 1


@pytest.mark.parametrize("doc_id,expected", [
    ("gdrive-1AbC-0", "1AbC"),
    ("gdrive-1AbC-12", "1AbC"),
    ("gdrive-a-b-c-7", "a-b-c"),          # file_id containing hyphens
    ("gdrive-file-2024-3", "file-2024"),  # file_id ending in digits
    ("gdrive-noindex", None),
    ("cal-event-1", None),
    ("", None),
])
def test_file_key_from_doc_id(doc_id, expected):
    """Drive file ids are base64url and can contain '-', so only a trailing
    '-<digits>' may be stripped. Splitting on the first or every hyphen would
    merge distinct files."""
    from mcpbrain.store import _file_key_from_doc_id
    assert _file_key_from_doc_id(doc_id) == expected
```

`pytest` must be imported in `tests/test_store.py`; add `import pytest` if absent.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_store.py -k "enrich_payloads or file_key" -v`
Expected: FAIL — `doc_id` still present, `enrich_payloads_legacy` missing, `_file_key_from_doc_id` undefined.

- [ ] **Step 3: Add the key helper**

Add near the top of `mcpbrain/store.py`, beside the other module-level regexes (around `_CAL_INSTANCE_SUFFIX`, line 138):

```python
# Drive chunk doc_ids are `gdrive-<file_id>-<idx>`. A Drive file_id uses the
# base64url alphabet and can contain '-', and can end in digits, so ONLY a
# trailing '-<digits>' may be stripped — the greedy `.+` leaves exactly the
# last such group. Splitting on the first or every hyphen would merge the
# payloads of distinct files.
_DRIVE_DOC_ID = re.compile(r"^gdrive-(.+)-\d+$")


def _file_key_from_doc_id(doc_id: str) -> str | None:
    """The Drive file_id a chunk doc_id belongs to, or None if it is not a
    Drive chunk doc_id. `enrich_payloads` is keyed on the bare file_id — what
    ingest_cache.publish_file already holds and chunks.metadata.$.file_id
    stores."""
    m = _DRIVE_DOC_ID.match(doc_id or "")
    return m.group(1) if m else None
```

- [ ] **Step 4: Change the schema block in `init()`**

Replace `mcpbrain/store.py:964-970` with:

```python
            # --- A4, Task 1: enrich_payloads (cache enrichment payloads) ----
            # Carries the extraction so importers skip re-enrich on shared-drive
            # artifacts. Keyed on the Drive FILE, one row per file: drain used
            # to write the whole-unit payload once per chunk doc_id while
            # ingest_cache.publish_file reads exactly one per file, which on the
            # live store was 50,099 rows for 8,183 files -- 13.5GB of a 15.65GB
            # store, and the reason the plaintext backup copy stopped fitting on
            # disk.
            db.execute("""CREATE TABLE IF NOT EXISTS enrich_payloads(
                file_id       TEXT PRIMARY KEY,
                payload       TEXT NOT NULL,
                logic_version INTEGER DEFAULT 0,
                at            TEXT DEFAULT CURRENT_TIMESTAMP)""")
            # A store written before the re-key is keyed per chunk doc_id. Move
            # it aside -- metadata-only, so init() stays instant -- and let the
            # enrich_payload_migration cadence drain it in the background.
            # Collapsing here would free 41,916 rows' overflow pages on the
            # startup path, minutes of I/O.
            #
            # Driven from init() on whatever store it finds, NOT a one-shot
            # behind a version marker: a backup artifact captured before this
            # change carries the old schema, and restoring it into a post-fix
            # build has to migrate it too.
            _ep_cols = {row["name"] for row in
                        db.execute("PRAGMA table_info(enrich_payloads)").fetchall()}
            if "doc_id" in _ep_cols:
                db.execute("ALTER TABLE enrich_payloads RENAME TO enrich_payloads_legacy")
                db.execute("""CREATE TABLE enrich_payloads(
                    file_id       TEXT PRIMARY KEY,
                    payload       TEXT NOT NULL,
                    logic_version INTEGER DEFAULT 0,
                    at            TEXT DEFAULT CURRENT_TIMESTAMP)""")
```

Note the ordering: the `CREATE TABLE IF NOT EXISTS` runs first so a fresh store gets the new shape; the `PRAGMA table_info` then sees `doc_id` only on a store that already had the old table, because `IF NOT EXISTS` left it untouched.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_store.py -k "enrich_payloads or file_key" -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git status --short
git add mcpbrain/store.py tests/test_store.py
git commit -m "feat(store): key enrich_payloads by Drive file, legacy table aside

drain wrote the whole-unit payload once per chunk doc_id while
ingest_cache.publish_file reads exactly one per file: 50,099 rows for 8,183
files, 13,587MB of a 15.65GB store (85%), which is why the plaintext backup
copy stopped fitting on disk.

init() creates the file-keyed table and renames an old doc_id-keyed one aside
rather than collapsing it -- freeing 41,916 rows' overflow pages is minutes of
I/O and init() blocks daemon startup. Driven from init() on whatever store it
finds rather than a one-shot version marker, so a backup artifact captured
before this change still migrates when restored."
```

---

### Task 4: Writers, readers and deleters use the file key

**Files:**
- Modify: `mcpbrain/store.py:2688-2701` (`set_enrich_payload`, `get_enrich_payload`), `mcpbrain/store.py:1437-1444` (`delete_chunks`)
- Modify: `mcpbrain/drain.py:531-536`
- Modify: `mcpbrain/ingest_cache.py:205-211`, `mcpbrain/ingest_cache.py:356-388` (`publish_file`)
- Test: `tests/test_store.py`, `tests/test_drain.py`, `tests/test_ingest_cache.py`

**Interfaces:**
- Consumes: `store._file_key_from_doc_id` (Task 3).
- Produces: `store.set_enrich_payload(file_id, payload, logic_version)`, `store.get_enrich_payload(file_id) -> dict | None`. Both take a bare Drive `file_id`, not a chunk `doc_id`.

The delete semantics need care and are **not** a mechanical rename. Today, deleting one chunk removes that chunk's payload row while the file's other rows survive, and `publish_file` stops at the first hit — so deleting some chunks of a file does not, in effect, drop the file's cached payload. With one row per file, deleting it whenever any chunk goes would be a behaviour change. The faithful rule: **drop the file's payload only when no chunks of that file remain.** That matters most at `ingest_cache.py:205-211`, whose whole purpose is deleting the stale chunks of a file that *shrank* — the file is still there and must keep its payload.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_store.py`:

```python
def test_enrich_payload_roundtrips_on_the_file_key(tmp_path):
    store = Store(tmp_path / "s.sqlite3", dim=4)
    store.init()
    store.set_enrich_payload("FILE1", '{"entities":[]}', 3)
    got = store.get_enrich_payload("FILE1")
    assert got == {"payload": '{"entities":[]}', "logic_version": 3}
    assert store.get_enrich_payload("NOPE") is None


def test_delete_chunks_keeps_the_payload_while_any_chunk_of_the_file_remains(tmp_path):
    """A Drive file that SHRANK still has chunks and must keep its cached
    payload. Deleting it here would be a behaviour change: before the re-key,
    the file's other per-chunk rows survived and publish_file found one."""
    store = Store(tmp_path / "s.sqlite3", dim=4)
    store.init()
    for i in range(3):
        store.upsert_chunk(f"gdrive-FILE1-{i}", f"t{i}", f"h{i}", {"file_id": "FILE1"})
    store.set_enrich_payload("FILE1", "{}", 1)

    store.delete_chunks(["gdrive-FILE1-2"])

    assert store.get_enrich_payload("FILE1") is not None


def test_delete_chunks_drops_the_payload_when_the_last_chunk_goes(tmp_path):
    store = Store(tmp_path / "s.sqlite3", dim=4)
    store.init()
    for i in range(2):
        store.upsert_chunk(f"gdrive-FILE1-{i}", f"t{i}", f"h{i}", {"file_id": "FILE1"})
    store.set_enrich_payload("FILE1", "{}", 1)

    store.delete_chunks(["gdrive-FILE1-0", "gdrive-FILE1-1"])

    assert store.get_enrich_payload("FILE1") is None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_store.py -k "enrich_payload or delete_chunks" -v`
Expected: FAIL — `set_enrich_payload` still writes a `doc_id` column that no longer exists.

- [ ] **Step 3: Update the store accessors**

Replace `mcpbrain/store.py:2688-2701`:

```python
    def set_enrich_payload(self, file_id: str, payload: str, logic_version: int) -> None:
        """Persist the validated extraction (JSON string) a Drive FILE produced,
        so its shared-drive cache artifact can carry it and importers skip
        re-enrich. One row per file: every chunk of a Drive doc shares the one
        unit extraction, and publish_file reads exactly one."""
        with self._connect(write=True) as db:
            db.execute("INSERT OR REPLACE INTO enrich_payloads"
                       "(file_id, payload, logic_version) VALUES(?,?,?)",
                       (file_id, payload, int(logic_version)))

    def get_enrich_payload(self, file_id: str) -> dict | None:
        with self._connect() as db:
            r = db.execute("SELECT payload, logic_version FROM enrich_payloads "
                           "WHERE file_id=?", (file_id,)).fetchone()
        return {"payload": r["payload"], "logic_version": r["logic_version"]} if r else None
```

- [ ] **Step 4: Update `delete_chunks`**

In `mcpbrain/store.py`, replace the `enrich_payloads` cleanup at lines 1437-1444 (keep the rest of the method as it is) so it runs **after** the chunk deletion:

```python
        with self._connect(write=True) as db:
            qs = ",".join("?" * len(doc_ids))
            rowids = [r["rowid"] for r in db.execute(
                f"SELECT rowid FROM chunks WHERE doc_id IN ({qs})", doc_ids).fetchall()]
            # Which Drive files these doc_ids belong to, resolved BEFORE the
            # delete so the payload cleanup below can ask whether anything is
            # left. Derived from the doc_id rather than metadata so an orphaned
            # payload (no chunk row at all) is still cleaned up.
            file_ids = {k for k in (_file_key_from_doc_id(d) for d in doc_ids) if k}
            if rowids:
                ph = ",".join("?" * len(rowids))
                db.execute(f"DELETE FROM vec_chunks WHERE rowid IN ({ph})", rowids)
                db.execute(f"DELETE FROM fts_chunks WHERE rowid IN ({ph})", rowids)
                db.execute(f"DELETE FROM chunks WHERE rowid IN ({ph})", rowids)
            # A file's cached payload dies only with its LAST chunk. Before the
            # re-key each chunk had its own row and the file's others survived a
            # partial delete, so dropping it on any delete would be a behaviour
            # change — and ingest_cache's shrink path deletes stale chunks of a
            # file that is still very much present.
            for fid in file_ids:
                remaining = db.execute(
                    "SELECT 1 FROM chunks WHERE json_extract(metadata,'$.file_id')=? "
                    "LIMIT 1", (fid,)).fetchone()
                if remaining is None:
                    db.execute("DELETE FROM enrich_payloads WHERE file_id=?", (fid,))
            return len(rowids)
```

Note this changes `delete_chunks` to no longer early-`return 0` before the payload cleanup — an orphaned payload with no chunk rows must still be removable, which the old unconditional delete achieved and this ordering preserves.

- [ ] **Step 5: Run the store tests**

Run: `uv run pytest tests/test_store.py -k "enrich_payload or delete_chunks" -v`
Expected: PASS.

- [ ] **Step 6: Update the existing drain test and add the duplication test**

`tests/test_drain.py:336-357` already has `test_drain_persists_enrich_payload_for_drive_docs_only`, which asserts on the chunk doc_id. Change its two assertions to the file key (the rest of the test is unchanged):

```python
    assert summary["applied"] == 1
    assert store.get_enrich_payload("F1") is not None       # the FILE key
    assert store.get_enrich_payload("gdrive-F1-0") is None  # not the chunk key
```

Then add, directly beneath it, using this module's existing `_seed_chunk` / `_envelope` / `_batch` / `_write_inbox` / `RecordingApply` helpers:

```python
def test_drain_writes_one_payload_per_drive_file_not_per_chunk(store, home):
    """drain looped every gdrive- doc_id writing the same whole-unit payload
    while ingest_cache.publish_file reads exactly one per file: 50,099 rows for
    8,183 files on the live store, 13.5GB of a 15.65GB store."""
    thread_id = "t-multi"
    for i in range(4):
        _seed_chunk(store, f"gdrive-F9-{i}", thread_id, message_id="msg-drive")
    env = _envelope(thread_id, messages=[
        {"message_id": "msg-drive", "sender": "Joel <joel@example.org>",
         "date": "2026-04-18", "labels": "INBOX", "subject": "Subject"},
    ])
    _write_inbox(home, "batch.json", _batch("batch-multi", [env]))

    summary = drain.drain(store, home=home, apply=RecordingApply())

    assert summary["applied"] == 1
    with store._connect() as db:
        rows = [r["file_id"] for r in
                db.execute("SELECT file_id FROM enrich_payloads").fetchall()]
    assert rows == ["F9"], f"expected one row for the file, got {rows}"
```

- [ ] **Step 7: Update drain**

Replace `mcpbrain/drain.py:531-536`:

```python
                # A#4: persist the validated extraction for shared-drive docs so
                # its cache artifact can carry it (importers then skip Haiku).
                # ONE row per file, not per chunk: every chunk of a Drive doc
                # shares this unit's extraction and ingest_cache.publish_file
                # reads exactly one. Writing per doc_id duplicated the whole
                # payload 6.1x on the live store. Drive-only: email payloads
                # never enter a shared cache. `extraction` here has already
                # passed sanitize_batch + validate_extraction + grounding.
                _file_ids = {k for k in (_file_key_from_doc_id(d) for d in doc_ids) if k}
                if _file_ids:
                    _payload = json.dumps(extraction, sort_keys=True)
                    for _f in _file_ids:
                        store.set_enrich_payload(_f, _payload, ENRICH_LOGIC_VERSION)
```

Add `_file_key_from_doc_id` to drain's import from `mcpbrain.store`.

- [ ] **Step 8: Update `publish_file` and the ingest-cache shrink path**

In `mcpbrain/ingest_cache.py`, replace the lookup loop inside `publish_file` (lines 375-384):

```python
    if enrich is None:
        floor = max(int(pin.enrich_logic_floor), int(ENRICH_LOGIC_VERSION))
        row = store.get_enrich_payload(file_id)
        if row and int(row["logic_version"]) >= floor:
            enrich = {"logic_version": int(row["logic_version"]),
                      "extraction": json.loads(row["payload"])}
```

and update its docstring (lines 359-366):

```python
    When `enrich` is not explicitly passed, looks up the file's validated
    extraction payload (`store.get_enrich_payload(file_id)`); if it exists at
    or above the fleet floor (max of `pin.enrich_logic_floor` and this
    install's `ENRICH_LOGIC_VERSION`), it's attached as
    `{"logic_version": N, "extraction": <dict>}` so importers can skip
    re-enrichment. One row per file — chunks share the unit's extraction.
    Falls back to unchanged behaviour (no payload) when nothing qualifies.
```

At lines 209-211, the shrink path deletes stale chunks of a file that still exists, so it must **stop** touching the payload. Delete these two lines:

```python
                db.executemany("DELETE FROM enrich_payloads WHERE doc_id=?",
                               [(d,) for d in stale])
```

and add above the remaining deletes:

```python
                # NOT the payload: this file shrank, it did not go away, and
                # its cached extraction still describes it. (Before the re-key
                # this deleted the stale chunks' own rows while the file's
                # others survived, so the payload effectively stayed anyway.)
```

- [ ] **Step 9: Write the failing ingest-cache test and run the suites**

Add to `tests/test_ingest_cache.py`:

```python
def test_shrinking_a_file_keeps_its_cached_payload(tmp_path):
    """The shrink path deletes a file's stale chunks; the file still exists and
    must keep its cached extraction."""
    store = Store(tmp_path / "s.sqlite3", dim=4)
    store.init()
    for i in range(3):
        store.upsert_chunk(f"gdrive-FILE1-{i}", f"t{i}", f"h{i}", {"file_id": "FILE1"})
    store.set_enrich_payload("FILE1", "{}", 1)

    store.delete_chunks(["gdrive-FILE1-2"])       # what the shrink path does

    assert store.get_enrich_payload("FILE1") is not None
```

Run: `uv run pytest tests/test_store.py tests/test_drain.py tests/test_ingest_cache.py -q`
Expected: all PASS. Any test elsewhere still calling `set_enrich_payload`/`get_enrich_payload` with a chunk `doc_id` must be updated to a file_id; find them with `grep -rn "enrich_payload" tests/`.

- [ ] **Step 10: Commit**

```bash
git status --short
git add mcpbrain/store.py mcpbrain/drain.py mcpbrain/ingest_cache.py \
        tests/test_store.py tests/test_drain.py tests/test_ingest_cache.py
git commit -m "fix(enrich): write one cached payload per Drive file, not per chunk

drain looped every gdrive- doc_id writing the same whole-unit payload while
publish_file reads exactly one per file. Storage now matches the access
pattern.

The delete semantics are not a mechanical rename: a file's payload dies only
with its LAST chunk. Before the re-key, deleting some of a file's chunks left
its other per-chunk rows behind and publish_file still found one, so dropping
the row on any delete would be a behaviour change -- and ingest_cache's shrink
path exists precisely to delete stale chunks of a file that is still there."
```

---

### Task 5: A background pass drains the legacy table

**Files:**
- Modify: `mcpbrain/store.py` (add `migrate_enrich_payloads_batch`)
- Modify: `mcpbrain/daemon.py:252+` (`_CADENCE_PASSES`), plus the interval attribute and `_run_enrich_payload_migration`
- Test: `tests/test_store.py`, `tests/test_daemon.py`

**Interfaces:**
- Consumes: `store._file_key_from_doc_id` (Task 3), the `enrich_payloads_legacy` table (Task 3).
- Produces: `store.migrate_enrich_payloads_batch(limit: int = 200) -> dict` returning `{"migrated": int, "deleted": int, "done": bool}`. `done=True` means the legacy table is gone.

The keeper rule is deliberately **the highest chunk index per file**, not the highest `logic_version`. Reading `logic_version` means reading past the `payload` column, whose overflow chain is the 13.5 GB this task exists to reclaim; ordering on `doc_id` alone is served by the primary-key index. All rows for a file are written in one loop and share a `logic_version` in the normal case; where they differ (the file grew between enrichments) the highest chunk index is the newer one. Worst case is a cache miss and one re-enrichment. Record this reasoning in the docstring — it is the kind of choice a later reader will otherwise "fix".

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_store.py`:

```python
def test_migrate_enrich_payloads_batch_collapses_to_one_row_per_file(tmp_path):
    path = tmp_path / "s.sqlite3"
    store = Store(path, dim=4)
    store.init()
    with store._connect(write=True) as db:
        db.execute("DROP TABLE enrich_payloads")
        db.execute("CREATE TABLE enrich_payloads(doc_id TEXT PRIMARY KEY, "
                   "payload TEXT NOT NULL, logic_version INTEGER DEFAULT 0, "
                   "at TEXT DEFAULT CURRENT_TIMESTAMP)")
        db.executemany(
            "INSERT INTO enrich_payloads(doc_id,payload,logic_version) VALUES(?,?,?)",
            [(f"gdrive-F{f}-{i}", '{"n":%d}' % f, 2) for f in range(3) for i in range(4)])
    store = Store(path, dim=4)
    store.init()

    res = store.migrate_enrich_payloads_batch(limit=2)
    assert res["migrated"] == 2 and res["done"] is False

    while not store.migrate_enrich_payloads_batch(limit=2)["done"]:
        pass

    with store._connect() as db:
        rows = db.execute("SELECT file_id, payload FROM enrich_payloads "
                          "ORDER BY file_id").fetchall()
        assert [r["file_id"] for r in rows] == ["F0", "F1", "F2"]
        assert db.execute("SELECT count(*) FROM sqlite_schema WHERE name="
                          "'enrich_payloads_legacy'").fetchone()[0] == 0


def test_migrate_enrich_payloads_batch_is_a_noop_without_a_legacy_table(tmp_path):
    store = Store(tmp_path / "s.sqlite3", dim=4)
    store.init()
    assert store.migrate_enrich_payloads_batch() == {
        "migrated": 0, "deleted": 0, "done": True}


def test_migrate_enrich_payloads_batch_never_overwrites_a_fresh_row(tmp_path):
    """A payload written since the rename is current; a legacy row for the same
    file is stale and must not clobber it."""
    path = tmp_path / "s.sqlite3"
    store = Store(path, dim=4)
    store.init()
    with store._connect(write=True) as db:
        db.execute("CREATE TABLE enrich_payloads_legacy(doc_id TEXT PRIMARY KEY, "
                   "payload TEXT NOT NULL, logic_version INTEGER DEFAULT 0, "
                   "at TEXT DEFAULT CURRENT_TIMESTAMP)")
        db.execute("INSERT INTO enrich_payloads_legacy(doc_id,payload) "
                   "VALUES('gdrive-F1-0','\"stale\"')")
    store.set_enrich_payload("F1", '"fresh"', 5)

    while not store.migrate_enrich_payloads_batch()["done"]:
        pass

    assert store.get_enrich_payload("F1")["payload"] == '"fresh"'


def test_migrate_enrich_payloads_batch_discards_unrecognised_keys(tmp_path):
    """The table has only ever been written by drain's gdrive- filter, so a key
    that is not a Drive chunk doc_id means an assumption broke. It is dropped,
    never guessed at — the payload is a regenerable cache."""
    path = tmp_path / "s.sqlite3"
    store = Store(path, dim=4)
    store.init()
    with store._connect(write=True) as db:
        db.execute("CREATE TABLE enrich_payloads_legacy(doc_id TEXT PRIMARY KEY, "
                   "payload TEXT NOT NULL, logic_version INTEGER DEFAULT 0, "
                   "at TEXT DEFAULT CURRENT_TIMESTAMP)")
        db.execute("INSERT INTO enrich_payloads_legacy(doc_id,payload) "
                   "VALUES('weird-key','{}')")

    while not store.migrate_enrich_payloads_batch()["done"]:
        pass

    with store._connect() as db:
        assert db.execute("SELECT count(*) FROM enrich_payloads").fetchone()[0] == 0
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_store.py -k migrate_enrich_payloads -v`
Expected: FAIL with `AttributeError: 'Store' object has no attribute 'migrate_enrich_payloads_batch'`.

- [ ] **Step 3: Implement the batch migrator**

Add to `mcpbrain/store.py`, beside the other `enrich_payloads` accessors:

```python
    def migrate_enrich_payloads_batch(self, limit: int = 200) -> dict:
        """Move up to `limit` Drive files' cached payloads out of the legacy
        doc_id-keyed table, deleting the legacy rows as it goes. Drops the
        legacy table once empty. Returns {"migrated", "deleted", "done"}.

        Incremental because the work is genuinely large: on the live store the
        legacy table is 13.5GB, and deleting a row means freeing its overflow
        chain. Runs on the daemon's maintenance cadence, never on init() and
        never inside the backup path.

        THE KEEPER IS THE HIGHEST CHUNK INDEX PER FILE, NOT THE HIGHEST
        logic_version, and that is deliberate. Reading logic_version means
        reading past the `payload` column and traversing the very overflow
        pages this exists to reclaim, whereas ordering on doc_id alone is
        served by the primary-key index. Every row for a file is written in one
        loop and shares a logic_version in the normal case; where they differ
        (the file grew between enrichments) the highest chunk index is the
        newer. Worst case is one cache miss and one re-enrichment — the table
        is a regenerable cache, not user data.
        """
        with self._connect(write=True) as db:
            if db.execute("SELECT count(*) FROM sqlite_schema WHERE type='table' "
                          "AND name='enrich_payloads_legacy'").fetchone()[0] == 0:
                return {"migrated": 0, "deleted": 0, "done": True}

            # doc_id only: covered by the primary-key index, so this does not
            # touch the payload overflow pages.
            doc_ids = [r["doc_id"] for r in db.execute(
                "SELECT doc_id FROM enrich_payloads_legacy ORDER BY doc_id").fetchall()]
            if not doc_ids:
                db.execute("DROP TABLE enrich_payloads_legacy")
                return {"migrated": 0, "deleted": 0, "done": True}

            keeper: dict[str, str] = {}
            unkeyed: list[str] = []
            for d in doc_ids:
                fid = _file_key_from_doc_id(d)
                if fid is None:
                    unkeyed.append(d)
                elif fid not in keeper or d > keeper[fid]:
                    keeper[fid] = d

            batch = sorted(keeper.items())[:limit]
            migrated = 0
            for fid, doc_id in batch:
                row = db.execute("SELECT payload, logic_version FROM "
                                 "enrich_payloads_legacy WHERE doc_id=?",
                                 (doc_id,)).fetchone()
                if row is None:
                    continue
                # INSERT OR IGNORE, not REPLACE: a row written since the rename
                # is current and a legacy row for the same file is stale.
                db.execute("INSERT OR IGNORE INTO enrich_payloads"
                           "(file_id, payload, logic_version) VALUES(?,?,?)",
                           (fid, row["payload"], row["logic_version"]))
                migrated += 1

            done_keys = {fid for fid, _ in batch}
            drop = [d for d in doc_ids
                    if (_file_key_from_doc_id(d) in done_keys) or d in unkeyed]
            db.executemany("DELETE FROM enrich_payloads_legacy WHERE doc_id=?",
                           [(d,) for d in drop])

            remaining = db.execute(
                "SELECT count(*) FROM enrich_payloads_legacy").fetchone()[0]
            if remaining == 0:
                db.execute("DROP TABLE enrich_payloads_legacy")
            return {"migrated": migrated, "deleted": len(drop),
                    "done": remaining == 0}
```

- [ ] **Step 4: Run the store tests**

Run: `uv run pytest tests/test_store.py -k migrate_enrich_payloads -v`
Expected: PASS.

- [ ] **Step 5: Write the failing daemon test**

Add to `tests/test_daemon.py`:

```python
def test_enrich_payload_migration_is_registered_as_a_cadence_pass():
    from mcpbrain.daemon import _CADENCE_PASSES
    names = {p.name for p in _CADENCE_PASSES}
    assert "enrich_payload_migration" in names
    p = next(p for p in _CADENCE_PASSES if p.name == "enrich_payload_migration")
    # Draining our own cache table needs no Google identity, and it must not
    # run against the store while a backfill holds it.
    assert p.needs_configured is False
    assert p.needs_bulk_lock is True
```

- [ ] **Step 6: Register the pass**

In `mcpbrain/daemon.py`, add to `_CADENCE_PASSES` beside `action_hygiene`:

```python
    # One-time-ish: drain the legacy doc_id-keyed enrich_payloads table left
    # aside by store.init(). Batched because the live table is 13.5GB and
    # deleting a row frees its overflow chain. needs_configured=False: draining
    # our own cache table needs no Google identity.
    CadencePass("enrich_payload_migration", "_enrich_payload_migration_interval_s",
                "_last_enrich_payload_migration", "_run_enrich_payload_migration",
                needs_configured=False, needs_bulk_lock=True),
```

Add the interval attribute beside the other `_*_interval_s` defaults in `Daemon.__init__` (follow the surrounding style; hourly is right — the pass self-terminates once the table is gone):

```python
        self._enrich_payload_migration_interval_s = 3600.0
        self._last_enrich_payload_migration = None
```

And the runner, beside the other `_run_*` methods:

```python
    def _run_enrich_payload_migration(self) -> None:
        """Drain one batch of the legacy doc_id-keyed enrich_payloads table.

        Self-terminating: once store.init() has renamed the old table aside and
        this has emptied and dropped it, every subsequent call is a cheap
        no-op returning done=True.
        """
        res = self._store.migrate_enrich_payloads_batch()
        if res["migrated"] or res["deleted"]:
            log.info("enrich_payload_migration: migrated %d file(s), "
                     "deleted %d legacy row(s), done=%s",
                     res["migrated"], res["deleted"], res["done"])
```

- [ ] **Step 7: Run the daemon tests**

Run: `uv run pytest tests/test_daemon.py -k "enrich_payload or cadence" -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git status --short
git add mcpbrain/store.py mcpbrain/daemon.py tests/test_store.py tests/test_daemon.py
git commit -m "feat(store): drain the legacy enrich_payloads table on a cadence

init() renames the old doc_id-keyed table aside instantly; this empties it in
batches and drops it. Batched because deleting a row means freeing its overflow
chain and the live table is 13.5GB -- work that cannot sit on the startup path.

The keeper is the highest chunk index per file rather than the highest
logic_version, deliberately: reading logic_version means reading past the
payload column and traversing the overflow pages this reclaims, while ordering
on doc_id is served by the primary-key index. Worst case is one cache miss."
```

---

### Task 6: The free-space preflight measures live data, not file size

**Files:**
- Modify: `mcpbrain/backup.py:300-342` (`_require_free_space`)
- Test: `tests/test_backup.py`

**Interfaces:**
- Produces: `backup._live_bytes(store_path) -> int`.

Without this the whole size half delivers nothing. `_require_free_space` sizes the temp need from `Path(store_path).stat().st_size`, which stays 15.65 GB after Task 5 frees 11.3 GB onto the freelist — so the preflight would keep refusing a backup whose actual artifact is ~4.7 GB. `VACUUM INTO`'s output tracks live pages, so the estimate must too.

- [ ] **Step 1: Write the failing test**

```python
def test_free_space_preflight_sizes_from_live_pages_not_file_size(tmp_path, monkeypatch):
    """After the enrich_payloads re-key the store file stays large while most
    of it is freelist. VACUUM INTO's output tracks LIVE pages, so an estimate
    based on stat().st_size would keep refusing a backup that now fits."""
    import mcpbrain.backup as backup_mod

    store = Store(tmp_path / "live.sqlite3", dim=4)
    store.init()
    for i in range(4000):
        store.upsert_chunk(f"d-{i}", "x" * 2000, f"h{i}", {})
    store.delete_chunks([f"d-{i}" for i in range(3600)])   # big freelist, no VACUUM

    file_bytes = Path(store.path).stat().st_size
    live_bytes = backup_mod._live_bytes(store.path)
    assert live_bytes < file_bytes * 0.7, (
        f"NULL INSTRUMENT: freelist too small to tell the two apart "
        f"(live={live_bytes} file={file_bytes})")

    class _Usage:
        free = int(live_bytes * 2.0)       # room for live data, not for the file

    monkeypatch.setattr(backup_mod.shutil, "disk_usage", lambda p: _Usage)
    backup_mod._require_free_space(tmp_path, tmp_path / "out.enc", store.path)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_backup.py::test_free_space_preflight_sizes_from_live_pages_not_file_size -v`
Expected: FAIL with `AttributeError: … has no attribute '_live_bytes'`.

- [ ] **Step 3: Implement**

Add above `_require_free_space` in `mcpbrain/backup.py`:

```python
def _live_bytes(store_path) -> int:
    """Bytes of LIVE data in the store — page_count minus freelist, times
    page_size.

    snapshot() uses VACUUM INTO, whose output contains only live pages, so this
    is what the temp copy will cost. The file's own size is the wrong number:
    re-keying enrich_payloads frees ~11.3GB onto the freelist without shrinking
    the file, and sizing from stat() would keep refusing backups that now fit.
    Falls back to the file size if the PRAGMAs cannot be read — refusing to
    back up is worse than over-estimating.
    """
    try:
        db = _open_db(store_path, read_only=False)
    except sqlite3.DatabaseError:
        return Path(store_path).stat().st_size
    try:
        page_size = db.execute("PRAGMA page_size").fetchone()[0]
        page_count = db.execute("PRAGMA page_count").fetchone()[0]
        freelist = db.execute("PRAGMA freelist_count").fetchone()[0]
    except sqlite3.DatabaseError:
        return Path(store_path).stat().st_size
    finally:
        db.close()
    return max(0, page_count - freelist) * page_size
```

In `_require_free_space`, replace line 315 and update the docstring paragraph that quotes the old figures:

```python
    store_bytes = _live_bytes(store_path)
```

```python
    make_encrypted_snapshot's dominant cost is one snapshot of the store in the
    temp dir; the encrypted artifact then lands at out_path. Neither is small.
    Both are sized from LIVE data (see _live_bytes), not the store file: after
    the enrich_payloads re-key the file keeps ~11.3GB of freelist that
    VACUUM INTO never copies. Running the system disk to zero takes down far
    more than the backup (2026-08-03: 57 failures in a day, each an ENOSPC
    part-way through, each leaving an orphaned work dir). Checking up front
    converts that into one clean, logged, backed-off failure per interval.
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_backup.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git status --short
git add mcpbrain/backup.py tests/test_backup.py
git commit -m "fix(backup): size the free-space preflight from live pages

_require_free_space sized the temp need from stat().st_size, which stays
15.65GB after the enrich_payloads re-key frees ~11.3GB onto the freelist --
so the preflight would have kept refusing a backup whose artifact is now
~4.7GB, and the size work would have delivered nothing. VACUUM INTO's output
tracks live pages, so the estimate does too."
```

---

### Task 7: The live gate

**Files:**
- Create: `bin/probe_backup_snapshot.py`
- Create: `docs/superpowers/specs/2026-08-10-backup-verification-record.md`

This is the gate the spec makes the ship decision on, in the shape of 0.7.113's `2026-08-04-release-verification-record.md`. **Nothing ships until this passes.** It runs against the real store on this machine, with the daemon running.

- [ ] **Step 1: Write the probe script**

Create `bin/probe_backup_snapshot.py`. Structure follows `bin/probe_wal_contention.py`: a module docstring stating the question, the arms, and the safety argument, then the arms.

```python
#!/usr/bin/env python3
"""Is VACUUM INTO a viable backup mechanism on the real store?

THE QUESTION
------------
backup.snapshot() no longer runs wal_checkpoint(TRUNCATE) and no longer copies
the DB file; it builds the artifact with VACUUM INTO. Two things that can only
be answered against the real 15.65GB store decide whether it ships:

  1. DURATION. A logical rebuild is slower per byte than a file copy. The
     backup runs on the daemon's cycle thread, so a rebuild approaching
     STALL_S (1800s, daemon.py:169) means redesign, not ship.
  2. FIDELITY. VACUUM rebuilds every b-tree, including vec0's shadow tables.
     vec_chunks_vector_chunks00 is declared untyped `rowid PRIMARY KEY` and
     holds 274 rows across rowids 1..450 -- 176 gaps a renumbering VACUUM
     could close, while vec_chunks_chunks.chunk_id is INTEGER PRIMARY KEY and
     would not move. A synthetic probe measured rowids preserved and KNN
     identical; this repeats it at real scale (167,992 vectors, dim 384).

The pinned_reader arm is the direct proof that cause (R) is closed: before this
work, snapshot() inside that window raised
`RuntimeError: wal_checkpoint(TRUNCATE) busy=1`.

SAFETY
------
Read-only against the live store apart from the artifact it writes into a temp
dir and deletes. It never moves, replaces or deletes brain.sqlite3, and it
holds no write lock. The pinned reader is a mode=ro connection, exactly as
bin/probe_wal_contention.py's arm of the same name -- a read-only connection
cannot write, cannot checkpoint, and in WAL mode does not block writers, so it
is safe to hold against the real store while the daemon is up.
"""
import argparse
import json
import resource
import shutil
import struct
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcpbrain import backup                      # noqa: E402
from mcpbrain.config import app_dir              # noqa: E402
from mcpbrain.store import _open_db              # noqa: E402

STORE = Path(app_dir()) / "brain.sqlite3"
QUERY_SEEDS = (11, 2731, 90210)


def _sizes():
    wal = Path(f"{STORE}-wal")
    return {"file_mb": STORE.stat().st_size // 2**20,
            "live_mb": backup._live_bytes(STORE) // 2**20,
            "wal_mb": (wal.stat().st_size // 2**20) if wal.exists() else 0}


def _peak_rss_mb():
    # macOS reports ru_maxrss in bytes, Linux in KiB.
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return raw // 2**20 if sys.platform == "darwin" else raw // 1024


def _dim(db):
    row = db.execute("SELECT embedding FROM vec_chunks LIMIT 1").fetchone()
    return len(bytes(row[0])) // 4 if row else 0


def _query(seed, dim):
    return struct.pack(f"{dim}f", *[((seed * 7919 + j * 104729) % 1000) / 1000.0
                                    for j in range(dim)])


def _fingerprint(path):
    """Everything a broken rebuild would change, read from one database."""
    db = _open_db(path, read_only=False)
    try:
        dim = _dim(db)
        out = {"chunks": db.execute("SELECT count(*) FROM chunks").fetchone()[0],
               "vec_rowids": db.execute(
                   "SELECT count(*) FROM vec_chunks_rowids").fetchone()[0],
               "dim": dim,
               "fts": db.execute("SELECT count(*) FROM fts_chunks "
                                 "WHERE fts_chunks MATCH 'the'").fetchone()[0],
               "knn": {}}
        for seed in QUERY_SEEDS:
            rows = db.execute(
                "SELECT c.doc_id, v.distance FROM vec_chunks v "
                "JOIN chunks c ON c.rowid = v.rowid "
                "WHERE v.embedding MATCH ? AND k = 10 ORDER BY v.distance",
                (_query(seed, dim),)).fetchall()
            out["knn"][seed] = [(r["doc_id"], round(r["distance"], 5)) for r in rows]
        return out
    finally:
        db.close()


def arm_baseline(work):
    """The mechanism being replaced, sized WITHOUT running it.

    shutil.copy2 of the store needs file_mb of free space. On this box that is
    15.65GB against 13.19GB free, so it cannot run at all -- which is not a
    limitation of this probe, it is the finding: the old mechanism is
    unrunnable here, and that is what the ENOSPC preflight has been reporting.
    The freelist does NOT change this; freeing pages leaves the file the same
    size on disk.

    So this measures read throughput over the store and reports what a copy
    would have required, rather than pretending to a number it cannot obtain.
    """
    free_mb = shutil.disk_usage(str(work)).free // 2**20
    file_mb = STORE.stat().st_size // 2**20
    t0 = time.monotonic()
    read = 0
    with STORE.open("rb") as fh:
        while chunk := fh.read(8 * 1024 * 1024):
            read += len(chunk)
    elapsed = time.monotonic() - t0
    return {"arm": "baseline_copy2",
            "read_seconds": round(elapsed, 1),
            "read_mb_per_s": round((read / 2**20) / max(elapsed, 0.001), 1),
            "copy2_would_need_mb": file_mb, "free_mb": free_mb,
            "runnable": file_mb < free_mb,
            "note": ("copy2 cannot run here: the store file exceeds free disk. "
                     "A copy writes as well as reads, so its wall time is at "
                     "least the read time above.")}


def arm_snapshot(work, keep=False):
    dest = work / "snapshot.sqlite3"
    before = _sizes()
    t0 = time.monotonic()
    backup.snapshot(STORE, dest)
    elapsed = time.monotonic() - t0
    res = {"arm": "snapshot_vacuum_into", "seconds": round(elapsed, 1),
           "artifact_mb": dest.stat().st_size // 2**20,
           "peak_rss_mb": _peak_rss_mb(),
           "wal_mb_before": before["wal_mb"], "wal_mb_after": _sizes()["wal_mb"],
           "stall_s_budget": 1800.0}
    res["gate"] = "PASS" if elapsed < 900 else "REVIEW — over half of STALL_S"
    if not keep:
        dest.unlink(missing_ok=True)
    return res


def arm_pinned_reader(work):
    """Cause (R): one held read transaction. This raised busy=1 before."""
    dest = work / "pinned.sqlite3"
    reader = _open_db(STORE, read_only=True)
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM chunks").fetchone()
    try:
        wal = Path(f"{STORE}-wal")
        wal_mb = (wal.stat().st_size // 2**20) if wal.exists() else 0
        t0 = time.monotonic()
        try:
            backup.snapshot(STORE, dest)
            outcome = "SUCCESS — cause (R) is closed"
        except Exception as exc:                      # noqa: BLE001
            outcome = f"FAILED — {type(exc).__name__}: {exc}"
        return {"arm": "pinned_reader", "seconds": round(time.monotonic() - t0, 1),
                "wal_mb_at_start": wal_mb, "outcome": outcome}
    finally:
        reader.rollback()
        reader.close()
        dest.unlink(missing_ok=True)


def arm_fidelity(work):
    dest = work / "fidelity.sqlite3"
    src_before = _fingerprint(STORE)
    backup.snapshot(STORE, dest)
    art = _fingerprint(dest)
    src_after = _fingerprint(STORE)
    dest.unlink(missing_ok=True)
    # The daemon may write during the rebuild, so counts can legitimately move.
    # KNN over the top of a 167k-vector index should not.
    knn_match = all(art["knn"][s] == src_before["knn"][s] for s in QUERY_SEEDS)
    return {"arm": "fidelity", "source_before": src_before["chunks"],
            "source_after": src_after["chunks"], "artifact": art["chunks"],
            "dim": art["dim"], "fts_source": src_before["fts"],
            "fts_artifact": art["fts"],
            "knn_identical": knn_match,
            "gate": "PASS" if knn_match else "FAIL — artifact KNN differs"}


ARMS = {"baseline": arm_baseline, "snapshot": arm_snapshot,
        "pinned_reader": arm_pinned_reader, "fidelity": arm_fidelity}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arms", nargs="*", default=["baseline", "snapshot",
                                                  "pinned_reader", "fidelity"])
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    work = Path(tempfile.mkdtemp(prefix="mcpbrain-probe-"))
    print(json.dumps({"store": _sizes()}, indent=2))
    try:
        for name in (ARMS if args.all else args.arms):
            print(json.dumps(ARMS[name](work), indent=2), flush=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
```

Before running, note the disk arithmetic. The `snapshot`, `pinned_reader` and `fidelity` arms each write one artifact sized by *live* data, so they need ~4.7 GB free once Task 5 has drained the legacy table and ~15.7 GB before that. **Run this probe after Task 5's migration has completed**, or the arms themselves will hit the very ENOSPC condition the work exists to fix. Check first:

```bash
uv run python -c "
from pathlib import Path; import shutil
from mcpbrain import backup
from mcpbrain.config import app_dir
s = Path(app_dir()) / 'brain.sqlite3'
print('file  %5d MB' % (s.stat().st_size // 2**20))
print('live  %5d MB' % (backup._live_bytes(s) // 2**20))
print('free  %5d MB' % (shutil.disk_usage('/tmp').free // 2**20))"
```

- [ ] **Step 2: Install this build locally and let the migration drain**

The probe's arms each write an artifact, and until the legacy table is drained that artifact is ~15.7 GB against 13.19 GB free. The migration has to run first.

```bash
launchctl bootout gui/$(id -u)/com.mcpbrain 2>/dev/null || true
uv tool install --force ".[daemon]"     # the [daemon] extra is REQUIRED —
                                        # a bare `.` drops fastembed and recall
                                        # silently returns empty
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.mcpbrain.plist
```

The `enrich_payload_migration` cadence is hourly and batched, so this takes several firings. Watch it:

```bash
grep enrich_payload_migration "$HOME/Library/Application Support/mcpbrain/com.mcpbrain.err" | tail -5
sqlite3 -readonly "$HOME/Library/Application Support/mcpbrain/brain.sqlite3" \
  "SELECT count(*) FROM enrich_payloads;
   SELECT count(*) FROM sqlite_schema WHERE name='enrich_payloads_legacy';
   PRAGMA freelist_count;"
```

Expected when drained: `enrich_payloads` ≈ 8 183 rows, no `enrich_payloads_legacy`, and a freelist of roughly 2.7M pages. Record the before/after in the verification record.

- [ ] **Step 3: Run the probe and record the numbers**

Run: `uv run python bin/probe_backup_snapshot.py --all`

Record: store file size, live bytes, artifact size, the snapshot wall time, peak RSS, WAL growth, the `pinned_reader` outcome, and the fidelity comparison.

- [ ] **Step 4: Apply the gate**

Compare the `snapshot` arm's wall time against `STALL_S = 1800.0` (`daemon.py:169`). The backup runs on the cycle thread, so a rebuild approaching that window means **stop and report** — the spec's named fallback is `Connection.backup(pages=-1)` plus a one-shot reclaim script, which is a decision for Josh, not a change to make unilaterally.

Any fidelity mismatch is also a **stop**: report it rather than working around it.

- [ ] **Step 5: Confirm the preflight now passes**

The whole point of the size half. With the migration drained:

```bash
uv run python -c "
from pathlib import Path
from mcpbrain import backup
from mcpbrain.config import app_dir
s = Path(app_dir()) / 'brain.sqlite3'
backup._require_free_space('/tmp', Path('/tmp/x.enc'), s)
print('preflight PASSES')"
```

Before this work it raised `OSError: [Errno 28] snapshot needs ~18763MB free … but only 13188MB is available`. Record both numbers.

- [ ] **Step 6: One full encrypted cycle**

Run one `make_encrypted_snapshot` against the real store with a throwaway key, in the 0.7.113 verification shape: record artifact size, peak temp usage, and that `restore()` decrypts it and the restored store opens with matching chunk counts and a working KNN query. Delete the artifact and key afterwards.

- [ ] **Step 7: Commit the record**

```bash
git status --short
git add bin/probe_backup_snapshot.py docs/superpowers/specs/2026-08-10-backup-verification-record.md
git commit -m "docs(spec): record the backup snapshot verification on the live store"
```

---

## Notes for the implementer

- **`grep -rn "enrich_payload" tests/ mcpbrain/ bin/` before finishing Task 4.** Any caller still passing a chunk `doc_id` is a silent cache miss, not a crash — the kind of bug tests do not catch unless you go looking.
- **Do not add a feature flag.** A single path was chosen deliberately over a kill switch in the spec; the live gate is the safety net. Adding one is a design change, not an implementation detail.
- **If Task 2 Step 2 fails**, the mechanism is invalid — stop and report rather than patching around it. The whole plan rests on that measurement.
- The spec records three things found while measuring but deliberately **not** fixed here: the backup bundle omits `google_token.json` and the enrich spool, already-published fleet cache artifacts keep their bloated payloads, and `com.mcpbrain.err` is 334 MB and unrotated. Leave them alone.
