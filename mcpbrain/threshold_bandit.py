"""Thompson-sampling bandit for self-tuning recall_max_distance — S4.

ADVISORY MODE: auto-applies ONLY when bandit_auto_apply=true in config AND a
real 'used' accept signal exists.  The default is ADVISORY (no auto-apply).

Background
----------
Thompson sampling (Beta-Bernoulli model) maintains one Beta(alpha, beta) per
threshold arm.  On each round we sample θ ~ Beta(α, β) per arm and pick the
arm with the highest θ.  After a recall session where 'used' or 'edited' events
are logged by feedback.py, alpha is incremented for the winning arm; 'ignored'
events (or no event) increment beta.

Current status (S2 partial): feedback.py logs EXPOSURE only; 'used'/'edited'
events are not yet captured from the recall hot path.  The bandit therefore:
  - reads zero positive events → all arms stay at Beta(1,1) (uniform prior)
  - reports "no data yet; retain current config value"
  - does NOT auto-apply anything

Once S2 starts capturing 'used'/'edited' events, the bandit activates without
any further code changes.

Storage
-------
Arm stats live in the main brain.sqlite3 (a new `bandit_arms` table added by
`init_bandit_table()`).  Reads are schema-safe: any SELECT wraps in try/except
so a missing table never crashes the recall path.

Public API
----------
    from mcpbrain.threshold_bandit import advisory_report, step
    report = advisory_report(store, home)   # returns a dict, never raises
    step(store, arm_value, outcome)         # record a reward signal
"""
from __future__ import annotations

import logging
import random
from datetime import datetime, timezone

log = logging.getLogger("mcpbrain.bandit")

# Candidate threshold arms — covering the calibrated on-topic gap
# (see config.recall_max_distance docstring: on-topic ~0.62–0.73, off-topic ~0.84–0.88)
ARMS = [0.65, 0.70, 0.75, 0.80, 0.85]

_PRIOR_ALPHA = 1.0   # uninformative Beta(1,1) prior
_PRIOR_BETA = 1.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Schema init (safe: no-op if already present)
# ---------------------------------------------------------------------------

def init_bandit_table(store) -> None:
    """Create bandit_arms table in the main brain.sqlite3 if it doesn't exist."""
    try:
        with store._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS bandit_arms (
                    arm_value REAL PRIMARY KEY,
                    alpha REAL NOT NULL DEFAULT 1.0,
                    beta REAL NOT NULL DEFAULT 1.0,
                    updated_at TEXT
                )
            """)
    except Exception as exc:  # noqa: BLE001
        log.debug("bandit: could not create bandit_arms table: %s", exc)


# ---------------------------------------------------------------------------
# Arm reads / writes (schema-safe)
# ---------------------------------------------------------------------------

def _read_arms(store) -> dict[float, tuple[float, float]]:
    """Return {arm_value: (alpha, beta)} for all registered arms.

    Falls back to prior Beta(1,1) for any arm not in the table.
    """
    result: dict[float, tuple[float, float]] = {}
    try:
        with store._connect() as db:
            rows = db.execute(
                "SELECT arm_value, alpha, beta FROM bandit_arms"
            ).fetchall()
        for row in rows:
            result[float(row[0])] = (float(row[1]), float(row[2]))
    except Exception:  # noqa: BLE001 — missing table → prior for all
        pass
    # Fill in any arm missing from the table with the prior
    for arm in ARMS:
        if arm not in result:
            result[arm] = (_PRIOR_ALPHA, _PRIOR_BETA)
    return result


def _write_arm(store, arm_value: float, alpha: float, beta: float) -> None:
    """Upsert one arm's stats."""
    try:
        with store._connect() as db:
            db.execute(
                "INSERT INTO bandit_arms(arm_value, alpha, beta, updated_at) "
                "VALUES(?,?,?,?) ON CONFLICT(arm_value) DO UPDATE SET "
                "alpha=excluded.alpha, beta=excluded.beta, updated_at=excluded.updated_at",
                (arm_value, alpha, beta, _now_iso()),
            )
    except Exception as exc:  # noqa: BLE001
        log.debug("bandit: _write_arm failed: %s", exc)


# ---------------------------------------------------------------------------
# Thompson sampling
# ---------------------------------------------------------------------------

def _sample_beta(alpha: float, beta: float) -> float:
    """Draw one sample from Beta(alpha, beta) using the standard library."""
    try:
        # Python 3.9+ has random.betavariate
        return random.betavariate(max(alpha, 1e-6), max(beta, 1e-6))
    except Exception:
        return alpha / (alpha + beta)  # fallback: mean


def recommend(store) -> float:
    """Pick the arm with the highest Thompson sample.

    Returns the recommended threshold value.  With a uniform prior (no data)
    this is random among the arms; with data it biases toward arms that received
    more positive reward.
    """
    arms = _read_arms(store)
    best_arm = ARMS[2]   # default: middle arm (0.75) if sampling fails
    best_sample = -1.0
    for arm, (alpha, beta) in arms.items():
        s = _sample_beta(alpha, beta)
        if s > best_sample:
            best_sample = s
            best_arm = arm
    return best_arm


# ---------------------------------------------------------------------------
# Reward signal
# ---------------------------------------------------------------------------

def step(store, arm_value: float, *, outcome: str) -> None:
    """Update arm stats with a reward signal.

    outcome: 'used' | 'edited' → positive reward (alpha += 1)
             'ignored' | 'exposure' → negative signal (beta += 1)
    """
    if arm_value not in ARMS:
        log.debug("bandit: unknown arm %.2f — skipping", arm_value)
        return
    arms = _read_arms(store)
    alpha, beta = arms.get(arm_value, (_PRIOR_ALPHA, _PRIOR_BETA))
    if outcome in ("used", "edited"):
        alpha += 1
    else:
        beta += 1
    _write_arm(store, arm_value, alpha, beta)


# ---------------------------------------------------------------------------
# Advisory report
# ---------------------------------------------------------------------------

def advisory_report(store, home: str) -> dict:
    """Generate an advisory report on the current bandit state.

    Returns:
        {
          "recommended_threshold": float,
          "current_threshold": float,
          "arms": {arm_value: {"alpha": ..., "beta": ..., "mean": ...}},
          "has_used_signal": bool,
          "advisory": str,   # human-readable one-liner
          "auto_apply": bool, # True only when config says so AND signal exists
        }

    Never raises — all errors produce a safe default report.
    """
    from mcpbrain import config
    try:
        current = config.recall_max_distance(home)
        arms = _read_arms(store)

        # Check whether any positive reward signal exists in feedback
        try:
            all_rows = store.all_feedback_rows()
            has_signal = any(
                r.get("event_type") in ("used", "edited") for r in (all_rows or [])
            )
        except Exception:
            has_signal = False

        # Compute arm means
        arm_stats = {
            arm: {
                "alpha": round(a, 2),
                "beta": round(b, 2),
                "mean": round(a / (a + b), 4),
                "observations": round(a + b - 2, 0),  # subtract prior
            }
            for arm, (a, b) in arms.items()
        }

        if not has_signal:
            advisory = (
                "No 'used'/'edited' signal yet — all arms at prior Beta(1,1). "
                "Retaining current recall_max_distance. "
                "Signal will start arriving once S2 captures accept events."
            )
            recommended = current
        else:
            recommended = recommend(store)
            if abs(recommended - current) < 0.01:
                advisory = (
                    f"Bandit recommendation matches current threshold {current:.2f}. "
                    "No change needed."
                )
            else:
                advisory = (
                    f"Bandit recommends threshold {recommended:.2f} "
                    f"(current {current:.2f}) based on reward signal. "
                    "Set bandit_auto_apply=true in config to auto-apply."
                )

        # Auto-apply only when explicitly enabled AND signal exists
        auto_apply = bool(
            has_signal
            and config.bandit_auto_apply_enabled(home)
            and abs(recommended - current) >= 0.01
        )
        if auto_apply:
            try:
                config.write_config(home, {"recall_max_distance": recommended})
                log.info("bandit: auto-applied threshold %.2f → %.2f", current, recommended)
                advisory += f" [AUTO-APPLIED: {current:.2f} → {recommended:.2f}]"
            except Exception as exc:
                log.warning("bandit: auto-apply failed: %s", exc)
                auto_apply = False

        return {
            "recommended_threshold": recommended,
            "current_threshold": current,
            "arms": arm_stats,
            "has_used_signal": has_signal,
            "advisory": advisory,
            "auto_apply": auto_apply,
        }
    except Exception as exc:  # noqa: BLE001
        log.debug("bandit: advisory_report failed: %s", exc)
        return {
            "recommended_threshold": 0.80,
            "current_threshold": 0.80,
            "arms": {},
            "has_used_signal": False,
            "advisory": f"Bandit unavailable: {exc}",
            "auto_apply": False,
        }
