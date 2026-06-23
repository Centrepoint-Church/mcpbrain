"""Tests for mcpbrain.threshold_bandit — S4 Thompson-sampling bandit.

Acceptance criteria (#20):
  - Tuning provably uses an external signal (test shows that when 'used' events
    exist, the bandit biases toward the corresponding arm).
  - Advisory report produced; nothing auto-applies without bandit_auto_apply=true.
  - No-signal path: all arms at prior, report says "no data yet", no auto-apply.
"""
from __future__ import annotations

import json
import math
from unittest.mock import MagicMock, patch

import pytest

from mcpbrain.threshold_bandit import (
    ARMS,
    advisory_report,
    recommend,
    step,
    _read_arms,
    _write_arm,
    _sample_beta,
    init_bandit_table,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_home(tmp_path, **flags):
    (tmp_path / "config.json").write_text(json.dumps(flags))
    return str(tmp_path)


def _make_store(feedback_rows=None):
    store = MagicMock()
    _arms = {}

    def _connect():
        import sqlite3, tempfile
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        return conn

    # Use in-memory store to support real table creation
    store._connect = _connect

    def _all_feedback():
        return feedback_rows or []
    store.all_feedback_rows.side_effect = _all_feedback

    return store


class _InMemoryStore:
    """Minimal store with real SQLite in-memory DB for bandit table tests."""
    import sqlite3

    def __init__(self):
        import sqlite3
        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row

    def _connect(self):
        return self._conn

    def all_feedback_rows(self):
        return []


# ---------------------------------------------------------------------------
# _sample_beta
# ---------------------------------------------------------------------------

def test_sample_beta_mean_is_reasonable():
    # With alpha=10, beta=1 the mean should be ~0.91
    samples = [_sample_beta(10.0, 1.0) for _ in range(100)]
    assert sum(samples) / len(samples) > 0.7


def test_sample_beta_symmetric_prior():
    # Beta(1,1) is uniform; mean should be close to 0.5
    samples = [_sample_beta(1.0, 1.0) for _ in range(200)]
    mean = sum(samples) / len(samples)
    assert 0.3 < mean < 0.7


# ---------------------------------------------------------------------------
# init_bandit_table + read/write arms
# ---------------------------------------------------------------------------

def test_init_and_read_arms():
    store = _InMemoryStore()
    init_bandit_table(store)
    arms = _read_arms(store)
    # All ARMS present at prior (1.0, 1.0)
    for arm in ARMS:
        assert arm in arms
        assert arms[arm] == (1.0, 1.0)


def test_write_and_read_arm():
    store = _InMemoryStore()
    init_bandit_table(store)
    _write_arm(store, 0.75, 5.0, 2.0)
    arms = _read_arms(store)
    assert arms[0.75] == (5.0, 2.0)


# ---------------------------------------------------------------------------
# step() — reward signal
# ---------------------------------------------------------------------------

def test_step_used_increments_alpha():
    store = _InMemoryStore()
    init_bandit_table(store)
    step(store, 0.80, outcome="used")
    arms = _read_arms(store)
    alpha, beta = arms[0.80]
    assert alpha == 2.0   # prior 1.0 + 1 reward
    assert beta == 1.0    # unchanged


def test_step_exposure_increments_beta():
    store = _InMemoryStore()
    init_bandit_table(store)
    step(store, 0.80, outcome="exposure")
    arms = _read_arms(store)
    alpha, beta = arms[0.80]
    assert alpha == 1.0
    assert beta == 2.0


def test_step_multiple_rewards_accumulate():
    store = _InMemoryStore()
    init_bandit_table(store)
    for _ in range(5):
        step(store, 0.70, outcome="used")
    arms = _read_arms(store)
    assert arms[0.70][0] == 6.0   # prior 1 + 5 rewards


def test_step_unknown_arm_ignored():
    store = _InMemoryStore()
    init_bandit_table(store)
    step(store, 0.99, outcome="used")   # not in ARMS
    arms = _read_arms(store)
    # All arms should still be at prior
    for arm in ARMS:
        assert arms[arm] == (1.0, 1.0)


# ---------------------------------------------------------------------------
# recommend()
# ---------------------------------------------------------------------------

def test_recommend_biases_toward_high_alpha_arm():
    """When one arm has many 'used' rewards its probability of being chosen
    is higher than the uniform arms (acceptance: external signal drives tuning)."""
    store = _InMemoryStore()
    init_bandit_table(store)
    # Arm 0.65 has 20 positive, 1 negative → strong bias
    _write_arm(store, 0.65, 20.0, 1.0)
    # All other arms at prior Beta(1,1)

    # Run 50 samples; arm 0.65 should be recommended the most
    wins = {arm: 0 for arm in ARMS}
    for _ in range(50):
        r = recommend(store)
        wins[r] = wins.get(r, 0) + 1
    assert wins.get(0.65, 0) > wins.get(0.80, 0), (
        "Arm 0.65 (high reward) should win more often than arm 0.80 (prior)"
    )


# ---------------------------------------------------------------------------
# advisory_report()
# ---------------------------------------------------------------------------

def test_advisory_no_signal_no_auto_apply(tmp_path):
    """Acceptance: no 'used' signal → advisory says 'no data', no auto-apply."""
    home = _make_home(tmp_path, recall_max_distance=0.80)
    store = _InMemoryStore()
    init_bandit_table(store)
    store.all_feedback_rows = lambda: []

    report = advisory_report(store, home)

    assert report["has_used_signal"] is False
    assert report["auto_apply"] is False
    assert "no" in report["advisory"].lower()
    assert "used" in report["advisory"].lower() and "signal" in report["advisory"].lower()


def test_advisory_with_signal_gives_recommendation(tmp_path):
    """Acceptance: once 'used' events exist, the bandit gives a recommendation."""
    home = _make_home(tmp_path, recall_max_distance=0.80)
    store = _InMemoryStore()
    init_bandit_table(store)
    # 20 'used' events for arm 0.65 — strong signal
    _write_arm(store, 0.65, 20.0, 1.0)
    store.all_feedback_rows = lambda: [{"event_type": "used", "doc_id": "x", "ts": "2026-01-01"}]

    report = advisory_report(store, home)

    assert report["has_used_signal"] is True
    assert isinstance(report["recommended_threshold"], float)
    assert report["recommended_threshold"] in ARMS


def test_advisory_auto_apply_requires_flag(tmp_path):
    """Auto-apply does not happen without bandit_auto_apply=true."""
    home = _make_home(tmp_path, recall_max_distance=0.80, bandit_auto_apply=False)
    store = _InMemoryStore()
    init_bandit_table(store)
    _write_arm(store, 0.65, 20.0, 1.0)
    store.all_feedback_rows = lambda: [{"event_type": "used", "doc_id": "x", "ts": "2026-01-01"}]

    report = advisory_report(store, home)
    assert report["auto_apply"] is False


def test_advisory_report_never_raises(tmp_path):
    """Advisory report must never raise even with a broken store."""
    home = _make_home(tmp_path)
    store = MagicMock()
    store.all_feedback_rows.side_effect = RuntimeError("store broken")
    store._connect.side_effect = RuntimeError("store broken")

    report = advisory_report(store, home)
    assert "recommended_threshold" in report
    assert report["auto_apply"] is False
