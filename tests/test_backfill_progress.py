from mcpbrain.store import Store
from mcpbrain.sync import backfill_progress, _STOP_AFTER_EMPTY_WINDOWS


def _store(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4, read_only=False); s.init(); return s


def test_progress_defaults_when_no_cursors(tmp_path):
    p = backfill_progress(_store(tmp_path))
    assert set(p) == {"gmail", "drive", "calendar"}
    assert p["gmail"] == {"reached": None, "done": False}


def test_progress_reads_floor_and_done(tmp_path):
    s = _store(tmp_path)
    s.set_cursor("gmail_backfill_until", "2019-03-01T00:00:00+00:00")
    s.set_cursor("gmail_backfill_empty", str(_STOP_AFTER_EMPTY_WINDOWS))
    p = backfill_progress(s)
    assert p["gmail"]["reached"].startswith("2019-03-01")
    assert p["gmail"]["done"] is True
