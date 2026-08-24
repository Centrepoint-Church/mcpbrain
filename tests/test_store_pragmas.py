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
