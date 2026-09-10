"""doctor must run PRAGMA integrity_check — nothing else did.

On 2026-09-10 `sync_cursors` was physically corrupted mid-repair (a NULL in a
STRICT TEXT PRIMARY KEY, and foreign pages linked into its b-tree). It went
unnoticed for hours: the daemon raised `database disk image is malformed` only
when something happened to read a cursor, and `doctor` reported
`foreign_key_check` but never `integrity_check`. Content was fine; the silence
was the problem.
"""
import re

from mcpbrain.doctor import integrity_line


def test_reports_ok_when_the_store_is_clean():
    assert "✅" in integrity_line("/nonexistent", check=lambda home: [])


def test_reports_the_problems_when_the_store_is_not_clean():
    problems = ["wrong # of entries in index sqlite_autoindex_sync_cursors_1",
                "NULL value in sync_cursors.source"]
    line = integrity_line("/nonexistent", check=lambda home: problems)
    assert "❌" in line
    assert "sync_cursors" in line, "must name what is broken, not just that it is"


def test_caps_how_many_problems_it_prints():
    """A badly corrupted store can report thousands; doctor's output stays legible."""
    line = integrity_line("/nonexistent", check=lambda home: [f"problem {i}" for i in range(50)])
    assert line.count("problem") <= 6
    assert "50" in line, "but it must still say how many there were"


def test_never_raises_when_the_check_itself_fails():
    def _boom(home):
        raise OSError("disk gone")
    line = integrity_line("/nonexistent", check=_boom)
    assert "➖" in line and "skipped" in line


def test_uses_the_FULL_integrity_check_not_quick_check():
    """THE point of this module. `quick_check` is ~2x faster (9s vs 19s on the
    author's 1.9 GB store) and would have MISSED the 2026-09-10 corruption
    entirely: per SQLite's docs it "does not verify UNIQUE constraints and does
    not verify that index content matches table content" — which is exactly the
    two error classes that fired (`wrong # of entries in index ...`, `NULL value
    in sync_cursors.source`). Do not swap it for quick_check to save 10 seconds
    in an on-demand diagnostic.
    """
    import inspect

    from mcpbrain import doctor
    src = inspect.getsource(doctor._run_integrity_check)
    # Pin the EXECUTED pragma, not the word: the docstring names quick_check on
    # purpose, to record why it was rejected.
    assert 'PRAGMA integrity_check' in src
    assert 'PRAGMA quick_check' not in src, \
        "quick_check cannot catch what this exists to catch"


def test_the_real_check_runs_against_an_actual_sqlite_file(tmp_path):
    """Exercises the default path, not just the injected one — the store-opening
    code is where a signature mistake would hide (it has hidden there before)."""
    import sqlite3

    from mcpbrain import doctor
    db = tmp_path / "brain.sqlite3"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE t(a TEXT PRIMARY KEY)")
    con.execute("INSERT INTO t VALUES ('x')")
    con.commit(); con.close()
    assert doctor._run_integrity_check(str(tmp_path)) == []
    line = integrity_line(str(tmp_path))
    assert re.search(r"✅.*[Ii]ntegrity", line)
