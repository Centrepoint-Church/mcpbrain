"""Write transactions take an IMMEDIATE lock so concurrent writers serialise.

A DEFERRED transaction that reads then writes must upgrade its lock, and SQLite
refuses that upgrade instantly (not after busy_timeout) when another writer
holds the lock. BEGIN IMMEDIATE takes the write lock up front so busy_timeout
actually applies.
"""
import sqlite3
import threading
import time

import pytest

from mcpbrain import store as store_module
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
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads)

    assert errors == [], f"concurrent writers raised: {errors}"
    with s._connect() as db:
        assert db.execute("SELECT v FROM t WHERE k=1").fetchone()["v"] == 100


def test_write_txn_rolls_back_on_error(tmp_path):
    """Must be IMMEDIATE-specific: this passed unchanged even with write=True
    reverted to a plain `with db:` wrapper (Python's sqlite3 default deferred
    autocommit also rolls back on exception), so the original version proved
    nothing about THIS change specifically.

    The discriminator: a transaction must already be ACTIVE the instant
    `_connect(write=True)` yields -- before any statement runs -- because
    BEGIN IMMEDIATE fires eagerly, up front. A reverted `with db:` wrapper
    only starts a transaction lazily, on the first DML statement, so
    `db.in_transaction` would read False here instead.
    """
    s = _store(tmp_path)
    with s._connect(write=True) as db:
        db.execute("CREATE TABLE IF NOT EXISTS t2(k INTEGER PRIMARY KEY)")
    try:
        with s._connect(write=True) as db:
            assert db.in_transaction, (
                "a transaction must already be active before any statement "
                "runs here -- proves BEGIN IMMEDIATE fired eagerly, not a "
                "reverted `with db:` wrapper"
            )
            db.execute("INSERT INTO t2(k) VALUES (1)")
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    with s._connect() as db:
        assert db.execute("SELECT COUNT(*) c FROM t2").fetchone()["c"] == 0


# ---------------------------------------------------------------------------
# _BEGIN_RETRIES ceiling + a smaller recall-path budget (Task 6, Step 4)
# ---------------------------------------------------------------------------

def test_begin_retries_lowered_to_three():
    # ~31s worst case at the old 6 was inherited by /api/recall via
    # decay.update_on_recall; lowering the default ceiling roughly halves it.
    assert store_module._BEGIN_RETRIES == 3


def test_recall_path_retry_budget_is_smaller_than_the_default():
    assert store_module.RECALL_PATH_BEGIN_RETRIES < store_module._BEGIN_RETRIES


def test_connect_retries_param_is_forwarded_to_begin_immediate(tmp_path, monkeypatch):
    s = _store(tmp_path)
    seen = {}
    orig = store_module._begin_immediate

    def spy(db, *, retries):
        seen["retries"] = retries
        orig(db, retries=retries)

    monkeypatch.setattr(store_module, "_begin_immediate", spy)
    with s._connect(write=True, retries=1) as db:
        db.execute("SELECT 1").fetchone()
    assert seen["retries"] == 1


# ---------------------------------------------------------------------------
# busy_timeout, not just retries, must bound the recall-path wait
# (post-review fix: retries=1 alone still let busy_timeout dominate, measured
# 5.38s live -- past prompt_recall's 3.0s client timeout).
# ---------------------------------------------------------------------------

def test_recall_path_busy_timeout_is_smaller_than_the_default():
    assert store_module.RECALL_PATH_BUSY_TIMEOUT_MS < store_module.DEFAULT_BUSY_TIMEOUT_MS


def test_connect_busy_timeout_ms_param_is_forwarded_to_open_db(tmp_path, monkeypatch):
    s = _store(tmp_path)
    seen = {}
    orig = store_module._open_db

    def spy(path, read_only, *, busy_timeout_ms):
        seen["busy_timeout_ms"] = busy_timeout_ms
        return orig(path, read_only, busy_timeout_ms=busy_timeout_ms)

    monkeypatch.setattr(store_module, "_open_db", spy)
    with s._connect(write=True, busy_timeout_ms=250) as db:
        db.execute("SELECT 1").fetchone()
    assert seen["busy_timeout_ms"] == 250

    with s._connect() as db:
        actual = db.execute("PRAGMA busy_timeout").fetchone()[0]
    # The PRAGMA actually took effect on the connection (not just recorded).
    with s._connect(busy_timeout_ms=250) as db:
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 250
    assert actual == store_module.DEFAULT_BUSY_TIMEOUT_MS  # untouched default path


def test_recall_path_write_stays_well_under_prompt_recall_budget_under_contention(tmp_path):
    """Reproduces the reviewer's measurement directly: hold the write lock open
    in one thread (simulating a bulk/ingest writer) while a SECOND thread does
    a recall-path write (RECALL_PATH_BEGIN_RETRIES + RECALL_PATH_BUSY_TIMEOUT_MS)
    against the same store. Before this fix, retries=1 alone still measured
    5.38s (dominated by the default 5000ms busy_timeout). With both the smaller
    retries AND the smaller busy_timeout, the contending write must fail (or
    succeed) in well under prompt_recall's 3.0s client budget -- asserted here
    at a generous 2.0s ceiling (the actual measured value is printed so a
    flaky/slow CI box's real number is visible if this ever fails).

    hold_s MUST outlast the DEFAULT busy_timeout's worst case (~5.36-5.4s
    measured) or this test cannot discriminate a working fix from a reverted
    one: a too-short hold (an earlier version used 2.0s) lets the holder
    finish waiting on its own timeout before a reverted (5000ms) contending
    write would fail, so the contender just succeeds late (~1.86s measured)
    and still passes `elapsed < 2.0` even with the fix reverted. Reproduced
    directly: with hold_s=6.0 and RECALL_PATH_BUSY_TIMEOUT_MS monkeypatched
    back to DEFAULT_BUSY_TIMEOUT_MS, this test genuinely FAILS at ~5.36s (see
    task-6-report.md for the measured before/after).
    """
    import time

    s = _store(tmp_path, name="contend.sqlite3")
    with s._connect(write=True) as db:
        db.execute("CREATE TABLE IF NOT EXISTS t3(k INTEGER PRIMARY KEY, v INTEGER)")
        db.execute("INSERT INTO t3(k, v) VALUES (1, 0)")

    hold_s = 6.0  # must outlast the default 5000ms busy_timeout's worst case
    release = threading.Event()

    def hold_writer():
        with s._connect(write=True) as db:
            db.execute("UPDATE t3 SET v = 1 WHERE k = 1")
            release.wait(timeout=hold_s)

    holder = threading.Thread(target=hold_writer, daemon=True)
    holder.start()
    time.sleep(0.1)  # let the holder actually acquire BEGIN IMMEDIATE first

    start = time.monotonic()
    try:
        with s._connect(write=True,
                        retries=store_module.RECALL_PATH_BEGIN_RETRIES,
                        busy_timeout_ms=store_module.RECALL_PATH_BUSY_TIMEOUT_MS) as db:
            db.execute("UPDATE t3 SET v = 2 WHERE k = 1")
    except sqlite3.OperationalError:
        pass  # expected outcome under contention -- the point is HOW LONG it took
    elapsed = time.monotonic() - start

    release.set()
    holder.join(timeout=hold_s + 5)
    assert not holder.is_alive()

    assert elapsed < 2.0, (
        f"recall-path write took {elapsed:.2f}s under contention -- "
        f"prompt_recall's client budget is 3.0s"
    )


# ---------------------------------------------------------------------------
# Rollback must not mask the original error (Task 6, Step 5)
# ---------------------------------------------------------------------------

def test_rollback_failure_does_not_mask_the_original_error(tmp_path, monkeypatch):
    """SQLite can auto-rollback on its own (SQLITE_FULL/SQLITE_IOERR) before our
    exception handler runs, so the explicit `db.execute("ROLLBACK")` then fails
    with 'cannot rollback - no transaction is active'. That secondary failure
    must never replace the real exception that triggered the rollback attempt.
    """

    class _FakeConn:
        row_factory = None
        isolation_level = None

        def execute(self, sql, *a, **kw):
            if sql == "ROLLBACK":
                raise sqlite3.OperationalError(
                    "cannot rollback - no transaction is active")
            return None

        def close(self):
            pass

    monkeypatch.setattr(store_module, "_open_db",
                        lambda path, read_only, **kw: _FakeConn())
    s = Store(tmp_path / "fake.sqlite3", dim=4)

    with pytest.raises(RuntimeError, match="disk is full"):
        with s._connect(write=True):
            raise RuntimeError("disk is full")


def test_rollback_failure_of_any_exception_type_does_not_mask_the_original_error(
        tmp_path, monkeypatch):
    """The rollback-cleanup catch was broadened from sqlite3.OperationalError
    to Exception: the unconditional `raise` right after it always re-raises
    the ORIGINAL error regardless of what this cleanup-only ROLLBACK does, so
    there is no signal lost by catching any Exception subclass here — this
    pins that down with a non-sqlite3 exception type.
    """

    class _FakeConn:
        row_factory = None
        isolation_level = None

        def execute(self, sql, *a, **kw):
            if sql == "ROLLBACK":
                raise ValueError("some unrelated cleanup failure")
            return None

        def close(self):
            pass

    monkeypatch.setattr(store_module, "_open_db",
                        lambda path, read_only, **kw: _FakeConn())
    s = Store(tmp_path / "fake2.sqlite3", dim=4)

    with pytest.raises(RuntimeError, match="disk is full"):
        with s._connect(write=True):
            raise RuntimeError("disk is full")


# ---------------------------------------------------------------------------
# _begin_immediate's retry/backoff must actually retry, not just be reachable
# (Task 8, Step 2)
# ---------------------------------------------------------------------------

def test_begin_immediate_actually_retries_under_real_contention(tmp_path, monkeypatch):
    """Forces a REAL held write lock (a second, genuine connection holding
    BEGIN IMMEDIATE) so the first one or more attempts inside _begin_immediate
    genuinely fail with "database is locked" and the loop's own jittered
    time.sleep backoff runs, before a later attempt succeeds once the holder
    releases. A small busy_timeout_ms means SQLite's own busy handler can't
    quietly cover the whole wait -- _begin_immediate's retry loop has to.
    Existing tests only prove the retries/busy_timeout_ms PARAMETERS are
    forwarded (test_connect_retries_param_is_forwarded_to_begin_immediate);
    none of them force a real contended lock and prove the retry loop
    actually iterates more than once.
    """
    s = _store(tmp_path, name="retry.sqlite3")
    with s._connect(write=True) as db:
        db.execute("CREATE TABLE IF NOT EXISTS t4(k INTEGER PRIMARY KEY)")

    # sqlite3 connections are thread-affine (check_same_thread defaults True),
    # so the holder connection must be created AND released on the SAME
    # thread -- the contending _connect(write=True) attempt runs on a
    # separate thread instead, so the main thread is free to hold, sleep,
    # then release without ever touching the holder from another thread.
    holder = store_module._open_db(s.path, False)
    holder.isolation_level = None
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO t4(k) VALUES (1)")

    sleep_calls = []
    orig_sleep = time.sleep

    def _spy_sleep(seconds):
        sleep_calls.append(seconds)
        orig_sleep(seconds)

    monkeypatch.setattr(time, "sleep", _spy_sleep)

    hold_s = 0.3
    errors = []

    def _contend():
        try:
            # busy_timeout_ms deliberately small: SQLite's own busy handler
            # must NOT be able to quietly wait out the whole hold_s window on
            # a single attempt -- _begin_immediate's own retry loop has to
            # bridge the gap.
            with s._connect(write=True, retries=8, busy_timeout_ms=20) as db2:
                db2.execute("SELECT 1")
        except Exception as exc:  # noqa: BLE001 — surfaced via `errors`
            errors.append(exc)

    contender = threading.Thread(target=_contend, daemon=True)
    contender.start()
    try:
        orig_sleep(hold_s)
        holder.execute("COMMIT")
    finally:
        holder.close()
    contender.join(timeout=10)
    assert not contender.is_alive()
    assert errors == [], f"contending write failed: {errors}"

    assert sleep_calls, (
        "expected _begin_immediate's backoff to sleep at least once -- the "
        "held lock should have forced a real retry, not a first-attempt success"
    )
    with s._connect() as db3:
        assert db3.execute("SELECT COUNT(*) c FROM t4").fetchone()["c"] == 1


# ---------------------------------------------------------------------------
# PRAGMA journal_size_limit must actually apply to the connection
# (Task 8, Step 2)
# ---------------------------------------------------------------------------

def test_journal_size_limit_pragma_is_applied(tmp_path):
    s = _store(tmp_path, name="jsl.sqlite3")
    with s._connect() as db:
        limit = db.execute("PRAGMA journal_size_limit").fetchone()[0]
    assert limit == 67108864
