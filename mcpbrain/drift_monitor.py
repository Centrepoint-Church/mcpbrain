"""Embedding-drift monitor — S4.

Adapted from ops-brain's embedding_rot_monitor.py for our local-only sqlite stack.

Runs the gold retrieval set through hybrid_search, records per-case recall@10
to a `embedding_metrics` table in brain.sqlite3, and fires an advisory alert
when the rolling mean drops more than max(ALERT_THRESHOLD, noise_floor) vs
the 30-day baseline.

The noise floor is 2× the standard deviation of daily mean recall@10 inside the
active baseline window — so a small random fluctuation never triggers a false
positive.

Public API
----------
    from mcpbrain.drift_monitor import run_drift_check
    result = run_drift_check(store, embedder, home)
    # result: {"mean_recall": float, "alert": str|None, "cases": int, "covered": int}

Storage: `embedding_metrics` table in brain.sqlite3 (schema-safe read path).
Flag: `drift_monitor_enabled` (config, default False).
"""
from __future__ import annotations

import logging
import statistics
from datetime import datetime, timezone

# Module-level imports from tests.eval so tests can patch them.
# Try/except so this module still imports cleanly in production (tests/ may
# not be on the path for some install layouts).
try:
    from tests.eval.run_eval import load_gold_cases as _load_gold_cases
    from tests.eval.run_eval import gold_eval as _gold_eval
except ImportError:
    _load_gold_cases = None  # type: ignore
    _gold_eval = None  # type: ignore

log = logging.getLogger("mcpbrain.drift_monitor")

ALERT_THRESHOLD = 0.05   # minimum relative drop to trigger
NOISE_FLOOR_STDEV_MULT = 2.0   # noise floor = 2× stddev of daily means


def init_drift_table(store) -> None:
    """Create embedding_metrics table if not present."""
    try:
        with store._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS embedding_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_date TEXT NOT NULL,
                    query_id TEXT,
                    recall_at_10 REAL,
                    top_score REAL,
                    expected_count INTEGER,
                    found_count INTEGER
                )
            """)
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_emb_metrics_date "
                "ON embedding_metrics(run_date)"
            )
    except Exception as exc:  # noqa: BLE001
        log.debug("drift_monitor: init_drift_table failed: %s", exc)


def _log_metric(store, *, run_date: str, query_id: str,
                recall_at_10: float, top_score: float,
                expected_count: int, found_count: int) -> None:
    try:
        with store._connect() as db:
            db.execute(
                "INSERT INTO embedding_metrics"
                "(run_date, query_id, recall_at_10, top_score, expected_count, found_count) "
                "VALUES(?,?,?,?,?,?)",
                (run_date, query_id, recall_at_10, top_score, expected_count, found_count),
            )
    except Exception as exc:  # noqa: BLE001
        log.debug("drift_monitor: _log_metric failed: %s", exc)


def _get_30day_baseline(store, run_date: str) -> float | None:
    """Return the mean recall@10 over the 30 days before run_date, or None if too few rows."""
    try:
        with store._connect() as db:
            rows = db.execute(
                "SELECT AVG(recall_at_10) FROM embedding_metrics "
                "WHERE run_date < ? ORDER BY run_date DESC LIMIT 30",
                (run_date,),
            ).fetchone()
        if rows and rows[0] is not None:
            return float(rows[0])
    except Exception:  # noqa: BLE001 — schema missing or empty
        pass
    return None


def _noise_floor(store, run_date: str) -> float:
    """2× stddev of daily mean recall@10 inside the last-30-days window."""
    try:
        with store._connect() as db:
            rows = db.execute(
                "SELECT date(run_date) AS d, AVG(recall_at_10) "
                "FROM embedding_metrics "
                "WHERE run_date < ? "
                "GROUP BY d "
                "ORDER BY d DESC "
                "LIMIT 30",
                (run_date,),
            ).fetchall()
        daily = [float(r[1]) for r in rows if r[1] is not None]
        if len(daily) >= 3:
            return max(ALERT_THRESHOLD, NOISE_FLOOR_STDEV_MULT * statistics.pstdev(daily))
    except Exception:  # noqa: BLE001
        pass
    return ALERT_THRESHOLD


def run_drift_check(store, embedder, home: str) -> dict:
    """Run the gold set through hybrid_search, log metrics, return advisory.

    Returns {"mean_recall": float, "alert": str|None, "cases": int, "covered": int}.
    Never raises — all errors return a safe default.
    """
    from mcpbrain import config
    if not config.drift_monitor_enabled(home):
        return {"mean_recall": 0.0, "alert": None, "cases": 0, "covered": 0,
                "skipped": "drift_monitor_enabled=false"}

    try:
        load_gold_cases = _load_gold_cases
        gold_eval = _gold_eval

        if load_gold_cases is None or gold_eval is None:
            return {"mean_recall": 0.0, "alert": None, "cases": 0, "covered": 0,
                    "skipped": "tests.eval not importable"}

        cases = load_gold_cases()
        if not cases:
            return {"mean_recall": 0.0, "alert": None, "cases": 0, "covered": 0,
                    "skipped": "no gold cases"}

        init_drift_table(store)
        run_date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Run gold eval (uses our eval harness — document-level recall@10)
        metrics = gold_eval(store, embedder, k=10)
        # gold_eval returns recall_at_k (generic key); alias as recall_at_10 for this context
        mean_recall = metrics.get("recall_at_10") or metrics.get("recall_at_k") or 0.0

        # Log ONE aggregate row per run. (Previously this fabricated one row per
        # gold case all carrying the same aggregate mean_recall — which both
        # misrepresented per-case recall and corrupted the baseline window, since
        # _get_30day_baseline takes the last 30 *rows*: N rows/run let a single
        # run dominate the baseline. One row/run = one run per baseline slot.)
        covered = metrics.get("covered", 0)
        expected_total = sum(
            len(c.get("expected_chunk_ids") or []) for c in cases[:covered]
        )
        _log_metric(
            store,
            run_date=run_date,
            query_id="__aggregate__",
            recall_at_10=mean_recall,
            top_score=0.0,
            expected_count=expected_total,
            found_count=round(mean_recall * expected_total),
        )

        # Compare to baseline
        baseline = _get_30day_baseline(store, run_date)
        alert: str | None = None
        if baseline is not None and baseline > 0:
            drop = (baseline - mean_recall) / baseline
            threshold = _noise_floor(store, run_date)
            if drop > threshold:
                alert = (
                    f"embedding drift: recall@10 dropped {drop * 100:.1f}% vs "
                    f"30-day baseline (current {mean_recall:.3f}, "
                    f"baseline {baseline:.3f}). Possible model drift or corpus shift."
                )
                log.warning("drift_monitor: %s", alert)

        return {
            "mean_recall": mean_recall,
            "alert": alert,
            "cases": metrics.get("total", len(cases)),
            "covered": metrics.get("covered", 0),
            "baseline": baseline,
        }
    except Exception as exc:  # noqa: BLE001
        log.debug("drift_monitor: run_drift_check failed: %s", exc)
        return {"mean_recall": 0.0, "alert": None, "cases": 0, "covered": 0,
                "error": str(exc)}
