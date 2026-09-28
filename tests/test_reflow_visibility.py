"""Reflow progress and halt state surfaced in doctor, /api/status and the
dashboard payload."""
from mcpbrain import dashboard
from mcpbrain.doctor import reflow_line
from mcpbrain.store import Store


def _store(tmp_path):
    s = Store(tmp_path / "a.sqlite3", dim=4)
    s.init()
    return s


def test_reflow_line_idle_progress_and_halt(tmp_path):
    s = _store(tmp_path)
    assert reflow_line(s).startswith("✅")
    s.enqueue_items([{"ref_id": "F", "event": "reflow", "modified_at": "1970-01-01T00:00:00"}],
                    source="reflow:drive")
    assert "1 queued" in reflow_line(s)
    s.set_cursor("reflow:halted", "reflow F: 1 dangling reference(s)")
    line = reflow_line(s)
    assert line.startswith("❌") and "bin/reflow.py" in line


def test_reflow_line_skips_on_store_failure():
    class _Broken:
        def reflow_stats(self):
            raise RuntimeError("boom")

    line = reflow_line(_Broken())
    assert line.startswith("➖")


def test_dashboard_stats_passes_through_reflow_block(tmp_path):
    import sqlite3

    db_path = tmp_path / "a.sqlite3"
    with sqlite3.connect(str(db_path)) as db:
        db.execute("CREATE TABLE entities(id INTEGER PRIMARY KEY)")
        db.execute("CREATE TABLE entity_relations(id INTEGER PRIMARY KEY)")
        db.execute("CREATE TABLE entity_observations(id INTEGER PRIMARY KEY)")
        db.execute("CREATE TABLE chunks(rowid INTEGER PRIMARY KEY, enrich_state TEXT)")

    class _Store:
        _path = str(db_path)

        def list_communities(self):
            return []

    status = {
        "chunk_count": 0, "enriched_count": 0, "spool": {"pending": 0, "inbox": 0},
        "backfill": {}, "connections": {}, "reflow": {"queued": 3},
    }
    out = dashboard.stats(_Store(), str(tmp_path), status)
    assert out["reflow"] == {"queued": 3}
