"""Tests for backfill window ordering (Task 2.5).

Recent-first: window[0] is the most recent `recent_days`, then successively
older `window_days`-wide windows back to `horizon_days` ago. Windows are
contiguous with no gaps or overlaps.
"""

from datetime import datetime, timedelta, timezone

import pytest

from mcpbrain.sync import backfill_windows, gmail_query

NOW = datetime(2026, 5, 31, tzinfo=timezone.utc)


def test_first_window_is_recent_30_days():
    windows = backfill_windows(NOW)
    expected_start = NOW - timedelta(days=30)
    assert windows[0] == (expected_start, NOW)
    width = windows[0][1] - windows[0][0]
    assert width == timedelta(days=30)


def test_windows_are_recent_first_monotonic():
    windows = backfill_windows(NOW)
    for i in range(len(windows) - 1):
        # Later window is strictly older: its end <= current window's start
        assert windows[i + 1][1] <= windows[i][0], (
            f"Window {i+1} end {windows[i+1][1]} is not <= window {i} start {windows[i][0]}"
        )
    # Every window has start <= end
    for i, (start, end) in enumerate(windows):
        assert start <= end, f"Window {i} has start > end: {start} > {end}"


def test_windows_are_contiguous():
    windows = backfill_windows(NOW)
    for i in range(len(windows) - 1):
        assert windows[i][0] == windows[i + 1][1], (
            f"Gap or overlap between window {i} and {i+1}: "
            f"{windows[i][0]} != {windows[i+1][1]}"
        )


def test_last_window_reaches_horizon():
    windows = backfill_windows(NOW)
    horizon = NOW - timedelta(days=1825)
    assert windows[-1][0] == horizon


def test_custom_params():
    windows = backfill_windows(NOW, recent_days=7, window_days=30, horizon_days=90)
    # First window is the last 7 days
    assert windows[0] == (NOW - timedelta(days=7), NOW)
    # Last window start == now - 90 days (horizon clamped)
    horizon = NOW - timedelta(days=90)
    assert windows[-1][0] == horizon
    # All windows contiguous
    for i in range(len(windows) - 1):
        assert windows[i][0] == windows[i + 1][1]


def test_backfill_windows_rejects_recent_gt_horizon():
    with pytest.raises(ValueError):
        backfill_windows(recent_days=100, horizon_days=30)


def test_gmail_query_format():
    start = datetime(2026, 4, 1, tzinfo=timezone.utc)
    end = datetime(2026, 5, 1, tzinfo=timezone.utc)
    assert gmail_query(start, end) == "after:2026/04/01 before:2026/05/01"
