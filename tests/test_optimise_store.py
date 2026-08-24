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
