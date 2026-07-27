"""Write transactions take an IMMEDIATE lock so concurrent writers serialise.

A DEFERRED transaction that reads then writes must upgrade its lock, and SQLite
refuses that upgrade instantly (not after busy_timeout) when another writer
holds the lock. BEGIN IMMEDIATE takes the write lock up front so busy_timeout
actually applies.
"""
import sqlite3
import threading

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

    monkeypatch.setattr(store_module, "_open_db", lambda path, read_only: _FakeConn())
    s = Store(tmp_path / "fake.sqlite3", dim=4)

    with pytest.raises(RuntimeError, match="disk is full"):
        with s._connect(write=True):
            raise RuntimeError("disk is full")
