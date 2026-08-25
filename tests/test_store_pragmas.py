import sqlite3

from mcpbrain.store import _open_db, Store


def test_open_db_sets_tuned_pragmas(tmp_path):
    p = tmp_path / "b.sqlite3"
    Store(str(p), dim=4).init()
    db = _open_db(str(p))
    try:
        assert db.execute("PRAGMA cache_size").fetchone()[0] == -65536
        assert db.execute("PRAGMA mmap_size").fetchone()[0] == 268435456
        assert db.execute("PRAGMA temp_store").fetchone()[0] == 2
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 1
    finally:
        db.close()


def test_read_only_connection_also_gets_read_pragmas(tmp_path):
    p = tmp_path / "b.sqlite3"
    Store(str(p), dim=4).init()
    db = _open_db(str(p), read_only=True)
    try:
        assert db.execute("PRAGMA cache_size").fetchone()[0] == -65536
        assert db.execute("PRAGMA mmap_size").fetchone()[0] == 268435456
    finally:
        db.close()


def test_write_connection_creates_planner_stats(tmp_path):
    p = tmp_path / "b.sqlite3"
    s = Store(str(p), dim=4)
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
    Store(str(p), dim=4).init()
    ro = Store(str(p), dim=4, read_only=True)
    with ro._connect() as db:            # must not raise "attempt to write a readonly database"
        db.execute("SELECT count(*) FROM chunks").fetchone()


def test_a_read_only_use_of_a_writable_store_does_not_attempt_optimize(tmp_path, monkeypatch):
    """PRAGMA optimize WRITES (it can create/update sqlite_stat1). Gating it on
    `not self.read_only` alone fires it on every write=False close too --
    including daemon.search's read path -- so it acquires the write lock
    AFTER the caller's read-only transaction is done, contending with a
    drain's writes and reintroducing the 0.7.105 recall-starvation class this
    whole plan exists to fix. graph_write.upsert_relation opens one
    connection per relation via store._connect(), so a drain pays this cost
    tens of thousands of times per cycle. Must gate on `write and not
    self.read_only` -- only an actual write transaction should pay for it."""
    p = tmp_path / "b.sqlite3"
    s = Store(str(p), dim=4)
    s.init()

    # sqlite3.Connection is an immutable C type -- neither the class method nor
    # an instance attribute can be monkeypatched directly. A Connection
    # SUBCLASS can override execute() though, so inject one via a spy on
    # sqlite3.connect() (the real connect still runs; only the returned type
    # differs), which _open_db calls with no `factory=` of its own.
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
