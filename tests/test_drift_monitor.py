"""Tests for mcpbrain.drift_monitor — S4 embedding-drift monitor.

Acceptance criteria (#20):
  - Drift alert fires on gold-set regression with a noise floor (drift > threshold).
  - No alert when recall is stable.
  - Alert is advisory only — no config is mutated.
  - Monitor skips cleanly when flag is off or store is unavailable.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

from mcpbrain.drift_monitor import (
    init_drift_table,
    _log_metric,
    _get_30day_baseline,
    _noise_floor,
    run_drift_check,
    ALERT_THRESHOLD,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_home(tmp_path, **flags):
    (tmp_path / "config.json").write_text(json.dumps(flags))
    return str(tmp_path)


class _InMemoryStore:
    """Minimal store backed by in-memory SQLite for drift table tests."""
    def __init__(self):
        self._db = sqlite3.connect(":memory:")
        self._db.row_factory = sqlite3.Row

    def _connect(self):
        return self._db


def _past_date(days_ago: int) -> str:
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# init_drift_table
# ---------------------------------------------------------------------------

def test_init_drift_table_creates_schema():
    store = _InMemoryStore()
    init_drift_table(store)
    rows = store._db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='embedding_metrics'"
    ).fetchall()
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# _log_metric + _get_30day_baseline
# ---------------------------------------------------------------------------

def test_get_30day_baseline_empty():
    store = _InMemoryStore()
    init_drift_table(store)
    result = _get_30day_baseline(store, _past_date(0))
    assert result is None


def test_get_30day_baseline_returns_mean():
    store = _InMemoryStore()
    init_drift_table(store)
    for i in range(5):
        _log_metric(store, run_date=_past_date(i + 1), query_id=f"q{i}",
                    recall_at_10=0.60, top_score=0.9, expected_count=1, found_count=1)
    baseline = _get_30day_baseline(store, _past_date(0))
    assert baseline is not None
    assert abs(baseline - 0.60) < 0.01


# ---------------------------------------------------------------------------
# Alert logic
# ---------------------------------------------------------------------------

def test_alert_fires_on_significant_drop():
    """Drift alert fires when recall drops significantly below baseline."""
    store = _InMemoryStore()
    init_drift_table(store)
    # 10 days of stable 0.70 baseline
    for i in range(10):
        _log_metric(store, run_date=_past_date(i + 1), query_id=f"q{i}",
                    recall_at_10=0.70, top_score=0.9, expected_count=2, found_count=1)

    # Current run drops to 0.40 → 43% drop, well above noise floor
    current_recall = 0.40
    baseline = _get_30day_baseline(store, _past_date(0))
    assert baseline is not None
    noise = _noise_floor(store, _past_date(0))
    drop = (baseline - current_recall) / baseline
    assert drop > noise, "Drop should exceed noise floor for alert to fire"


def test_no_alert_on_stable_recall():
    """No alert when recall is stable (small fluctuation stays within noise floor)."""
    store = _InMemoryStore()
    init_drift_table(store)
    # 10 days of stable 0.70 baseline
    for i in range(10):
        _log_metric(store, run_date=_past_date(i + 1), query_id=f"q{i}",
                    recall_at_10=0.70, top_score=0.9, expected_count=2, found_count=1)

    # Current run at 0.68 — tiny fluctuation, well within noise
    current_recall = 0.68
    baseline = _get_30day_baseline(store, _past_date(0))
    noise = _noise_floor(store, _past_date(0))
    drop = (baseline - current_recall) / baseline
    # Should not exceed the noise floor (0.68 vs 0.70 is only 2.8%)
    # noise_floor with all same values = 0.0 stddev → ALERT_THRESHOLD = 0.05
    assert drop < ALERT_THRESHOLD


# ---------------------------------------------------------------------------
# run_drift_check() — integration
# ---------------------------------------------------------------------------

def test_run_drift_check_flag_off(tmp_path):
    """Monitor skips when flag is off."""
    home = _make_home(tmp_path, drift_monitor=False)
    store = MagicMock()
    embedder = MagicMock()
    result = run_drift_check(store, embedder, home)
    assert result.get("skipped") is not None
    store._connect.assert_not_called()


def test_run_drift_check_no_gold_cases_skips(tmp_path):
    """Monitor skips gracefully when gold set is unavailable."""
    home = _make_home(tmp_path, drift_monitor=True)
    store = _InMemoryStore()
    embedder = MagicMock()
    with patch("mcpbrain.drift_monitor._load_gold_cases", return_value=[]):
        result = run_drift_check(store, embedder, home)
    assert result.get("skipped") is not None


def test_run_drift_check_no_alert_when_stable(tmp_path):
    """Monitor produces no alert when recall is within noise floor of baseline."""
    home = _make_home(tmp_path, drift_monitor=True)
    store = _InMemoryStore()
    init_drift_table(store)
    embedder = MagicMock()

    # Seed 10 days of baseline at 0.60
    for i in range(10):
        _log_metric(store, run_date=_past_date(i + 1), query_id=f"q{i}",
                    recall_at_10=0.60, top_score=0.9, expected_count=2, found_count=1)

    mock_cases = [{"id": "q1", "query": "test", "expected_chunk_ids": ["c1"]}]
    mock_metrics = {"recall_at_10": 0.59, "total": 1, "covered": 1, "mrr": 0.5, "k": 10, "missing": 0}
    with patch("mcpbrain.drift_monitor._load_gold_cases", return_value=mock_cases), \
         patch("mcpbrain.drift_monitor._gold_eval", return_value=mock_metrics):
        result = run_drift_check(store, embedder, home)

    # 0.59 vs baseline 0.60 = 1.7% drop — within ALERT_THRESHOLD of 5%
    assert result["alert"] is None


def test_run_drift_check_alert_fires_on_drop(tmp_path):
    """Acceptance: drift alert fires when recall drops more than noise floor."""
    home = _make_home(tmp_path, drift_monitor=True)
    store = _InMemoryStore()
    init_drift_table(store)
    embedder = MagicMock()

    # Seed 10 days of baseline at 0.70
    for i in range(10):
        _log_metric(store, run_date=_past_date(i + 1), query_id=f"q{i}",
                    recall_at_10=0.70, top_score=0.9, expected_count=2, found_count=1)

    mock_cases = [{"id": "q1", "query": "test", "expected_chunk_ids": ["c1"]}]
    # Current recall drops to 0.40 — 43% below baseline
    mock_metrics = {"recall_at_10": 0.40, "total": 1, "covered": 1, "mrr": 0.3, "k": 10, "missing": 0}
    with patch("mcpbrain.drift_monitor._load_gold_cases", return_value=mock_cases), \
         patch("mcpbrain.drift_monitor._gold_eval", return_value=mock_metrics):
        result = run_drift_check(store, embedder, home)

    # Alert should fire
    assert result["alert"] is not None
    assert "drift" in result["alert"].lower() or "dropped" in result["alert"].lower()


def test_run_drift_check_no_baseline_no_alert(tmp_path):
    """First run (no historical data) should not alert — no baseline to compare against."""
    home = _make_home(tmp_path, drift_monitor=True)
    store = _InMemoryStore()
    init_drift_table(store)
    embedder = MagicMock()

    mock_cases = [{"id": "q1", "query": "test", "expected_chunk_ids": ["c1"]}]
    mock_metrics = {"recall_at_10": 0.40, "total": 1, "covered": 1, "mrr": 0.3, "k": 10, "missing": 0}
    with patch("mcpbrain.drift_monitor._load_gold_cases", return_value=mock_cases), \
         patch("mcpbrain.drift_monitor._gold_eval", return_value=mock_metrics):
        result = run_drift_check(store, embedder, home)

    # No baseline → no alert possible
    assert result["alert"] is None
    assert result.get("baseline") is None


def test_run_drift_check_never_raises(tmp_path):
    """run_drift_check must never raise even with broken store/embedder."""
    home = _make_home(tmp_path, drift_monitor=True)
    store = MagicMock()
    store._connect.side_effect = RuntimeError("broken")
    embedder = MagicMock()

    result = run_drift_check(store, embedder, home)
    assert isinstance(result, dict)
