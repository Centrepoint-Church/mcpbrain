import sqlite3
from bin.measure_store import measure


def test_measure_reports_size_and_rowcounts(tmp_path):
    p = tmp_path / "t.sqlite3"
    db = sqlite3.connect(p)
    db.execute("CREATE TABLE chunks(rowid INTEGER PRIMARY KEY, text TEXT)")
    db.executemany("INSERT INTO chunks(text) VALUES(?)", [("x" * 100,)] * 50)
    db.commit()
    db.close()

    m = measure(p)

    assert m["row_counts"]["chunks"] == 50
    assert m["file_bytes"] > 0
    assert m["page_size"] in (4096, 8192)
    assert m["has_stat1"] is False
