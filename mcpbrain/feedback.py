"""Recall-acceptance feedback: exposure logging + Bayesian-smoothed quality.

Ported from ops-brain feedback.py + feedback_aggregator.py (adapted for sqlite).

Signals:
  exposure  — chunk was injected into a UserPromptSubmit context block
  (used / edited / ignored are not yet capturable via hooks; tracked as future work)

Aggregation (nightly, via daemon cadence):
  Bayesian CTR with a 90-day half-life decay.  With only exposure data and no
  click signals, the smoothed CTR stays at the uninformative prior (1.0 neutral).
  The multiplier hook is wired but identity until click data accrues.

Storage:
  recall_feedback  — raw event log (one row per exposure)
  chunk_quality    — per-chunk quality float (default 1.0 = neutral)

Configuration:
  feedback_enabled (config.json, default True) — master switch to disable all
  feedback I/O without touching code paths. Safe default: on.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

log = logging.getLogger("mcpbrain.feedback")

# Bayesian prior: α + β = total prior weight; α/total = prior mean (0.5).
# With no click data the posterior stays at ~prior = 1.0 (neutral).
_ALPHA = 1.0   # pseudo-successes (used)
_BETA = 1.0    # pseudo-failures (not used)

_HALF_LIFE_DAYS = 90.0   # exponential decay half-life for age-weighting


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _decay(age_days: float) -> float:
    """Exponential decay weight: 1.0 at age=0, 0.5 at age=half_life_days."""
    return math.exp(-math.log(2) * age_days / _HALF_LIFE_DAYS)


# ---------------------------------------------------------------------------
# Raw event logging
# ---------------------------------------------------------------------------

def record_feedback(store, doc_id: str, session_id: str, event_type: str) -> None:
    """Fire-and-forget: log one recall feedback event.

    event_type is one of: 'exposure' | 'used' | 'edited' | 'ignored'.
    Silently swallows all errors — feedback must never disrupt a recall.
    """
    try:
        store.record_recall_feedback(doc_id, session_id, event_type, _now_iso())
    except Exception as exc:  # noqa: BLE001
        log.debug("feedback.record_feedback error (swallowed): %s", exc)


def record_exposures(store, doc_ids: list[str], session_id: str) -> None:
    """Log exposure events for a batch of doc_ids injected in one recall pass."""
    for doc_id in (doc_ids or []):
        record_feedback(store, doc_id, session_id, "exposure")


# ---------------------------------------------------------------------------
# Nightly aggregation
# ---------------------------------------------------------------------------

def aggregate_feedback(store, *, now: datetime | None = None) -> dict:
    """Compute Bayesian-smoothed CTR per chunk and write chunk_quality.

    Returns {"updated": int, "skipped": int} summary.

    With only exposure data (no click signals yet), quality remains close to 1.0
    (the uninformative prior). This wires the plumbing so quality updates
    automatically once 'used' / 'edited' signals are added.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    rows = store.all_feedback_rows()  # [{doc_id, event_type, ts}]
    if not rows:
        return {"updated": 0, "skipped": 0}

    # Group by doc_id, accumulate decay-weighted exposures and uses.
    stats: dict[str, dict] = {}
    for row in rows:
        doc_id = row["doc_id"]
        if doc_id not in stats:
            stats[doc_id] = {"exposures": 0.0, "uses": 0.0}
        try:
            ts = datetime.fromisoformat(row["ts"].replace("Z", "+00:00"))
            age_days = max(0.0, (now - ts).total_seconds() / 86400.0)
        except Exception:
            age_days = 0.0
        w = _decay(age_days)
        if row["event_type"] == "exposure":
            stats[doc_id]["exposures"] += w
        elif row["event_type"] in ("used", "edited"):
            stats[doc_id]["uses"] += w

    updated = skipped = 0
    for doc_id, s in stats.items():
        exp = s["exposures"]
        uses = s["uses"]
        # Bayesian posterior mean: (α + uses) / (α + β + exposures)
        quality = (_ALPHA + uses) / (_ALPHA + _BETA + exp)
        try:
            store.update_chunk_quality(doc_id, round(quality, 4), int(exp), int(uses))
            updated += 1
        except Exception as exc:  # noqa: BLE001
            log.debug("aggregate_feedback: update_chunk_quality failed for %s: %s", doc_id, exc)
            skipped += 1

    return {"updated": updated, "skipped": skipped}


# ---------------------------------------------------------------------------
# Ranking multiplier hook (neutral until data accrues)
# ---------------------------------------------------------------------------

def apply_quality_multiplier(results: list[dict], store,
                             *, weight: float = 0.0) -> list[dict]:
    """Multiply each result's score by chunk_quality, weighted by `weight`.

    weight=0.0 (default) means identity: score unchanged regardless of quality.
    weight=1.0 means full quality weighting: score *= quality.

    The multiplier is neutral until data accrues (chunk_quality defaults to
    1.0 for all chunks, so identity even at weight=1.0 early on).
    This hook is the insertion point; the daemon wires it once weight is tuned.
    """
    if weight == 0.0 or not results:
        return results

    out = []
    for r in results:
        quality = store.get_chunk_quality(r.get("doc_id") or "")
        adjusted_score = float(r.get("score") or 0.0) * (1.0 + weight * (quality - 1.0))
        out.append({**r, "score": round(adjusted_score, 4)})
    return out
