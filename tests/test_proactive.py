"""Tests for mcpbrain/proactive.py — GTD detectors removed (§9E).

Verifies that run() returns zero counts (no-op stub) and that the
detector functions have been removed.
"""

from mcpbrain.store import Store
import mcpbrain.proactive as proactive_mod


def _make_store(tmp_path, name="proactive.sqlite3"):
    s = Store(tmp_path / name, dim=4)
    s.init()
    return s


def test_run_returns_zero_counts(tmp_path):
    """run() always returns zero counts regardless of store contents."""
    store = _make_store(tmp_path)
    result = proactive_mod.run(store, now="2026-06-03T10:00:00+00:00")
    assert result == {"project_no_next_action": 0, "area_overdue": 0}


def test_run_accepts_no_now_kwarg(tmp_path):
    """run() can be called without now= (uses default None)."""
    store = _make_store(tmp_path)
    result = proactive_mod.run(store)
    assert result["project_no_next_action"] == 0
    assert result["area_overdue"] == 0


def test_detect_functions_removed():
    """detect_projects_without_next_action and detect_areas_overdue_for_review
    must not exist — they depended on the now-removed projects/areas tables."""
    assert not hasattr(proactive_mod, "detect_projects_without_next_action")
    assert not hasattr(proactive_mod, "detect_areas_overdue_for_review")
