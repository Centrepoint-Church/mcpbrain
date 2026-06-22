"""Recall-acceptance feedback: exposure logging + Bayesian-smoothed quality.

Ported from ops-brain feedback.py + feedback_aggregator.py (adapted for sqlite).

Signals:
  exposure  — chunk was injected into a UserPromptSubmit context block (the only
              signal currently capturable from the auto-recall hook)
  used / edited / ignored — NOT yet captured anywhere. There is no reliable
              automatic "the recall was useful" signal from the UserPromptSubmit
              path, so a positive signal is deferred (depends on S1/S4 work). The
              event API and aggregation below already handle these the moment a
              capture mechanism exists — see record_feedback.

Aggregation (nightly, via daemon cadence):
  A BOOST-ONLY quality multiplier centred on 1.0. A chunk with no positive
  ('used'/'edited') signal stays at exactly 1.0 (neutral) — exposure alone NEVER
  lowers a chunk's quality (exposure ~= relevance, so penalising it would be
  backwards). A chunk that accrues positive signal rises above 1.0 (up to
  1 + _USE_BOOST). Down-weighting would require an explicit negative ('ignored')
  signal and is intentionally not done until that signal exists.

Storage:
  recall_feedback  — raw event log (one row per event)
  chunk_quality    — per-chunk quality float; ONLY written for chunks with a
                     positive signal. Absent row → get_chunk_quality returns the
                     1.0 neutral default, so exposure-only chunks are untouched.

Configuration:
  feedback_enabled (config.json, default True) — master switch to disable all
  feedback I/O without touching code paths. Safe default: on.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

log = logging.getLogger("mcpbrain.feedback")

# Boost-only model: positive signal raises quality above the 1.0 neutral baseline;
# nothing lowers it. _USE_BOOST is the max multiplier gain; _PRIOR smooths so a
# single 'used' event doesn't jump a chunk straight to the cap.
_USE_BOOST = 0.5    # a heavily-used chunk reaches up to 1.5x
_PRIOR = 3.0        # decay-weighted uses needed to reach ~half the boost

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
    """Log exposure events for a batch of doc_ids injected in one recall pass.

    Single batched write (one transaction), not one connection per doc — keeps the
    recall hot path cheap. Errors are swallowed so feedback never disrupts a recall.
    """
    if not doc_ids:
        return
    try:
        ts = _now_iso()
        store.record_recall_feedback_batch(
            [(doc_id, session_id, "exposure", ts) for doc_id in doc_ids])
    except Exception as exc:  # noqa: BLE001
        log.debug("feedback.record_exposures error (swallowed): %s", exc)


# ---------------------------------------------------------------------------
# Nightly aggregation
# ---------------------------------------------------------------------------

def aggregate_feedback(store, *, now: datetime | None = None) -> dict:
    """Compute a boost-only quality multiplier per chunk and write chunk_quality.

    Returns {"updated": int, "skipped": int, "neutral": int} where `neutral` is
    the number of chunks that had only exposure (no positive signal) and were
    therefore left at the 1.0 default (no row written).

    Quality for a chunk with decay-weighted positive uses `u`:
        quality = 1 + _USE_BOOST * u / (u + _PRIOR)   (in [1.0, 1+_USE_BOOST))
    A chunk with u == 0 stays at exactly 1.0 — exposure never lowers quality.
    Today no 'used'/'edited' events are captured, so every chunk is neutral and
    nothing is written; the plumbing activates automatically once a positive
    signal is recorded.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    rows = store.all_feedback_rows()  # [{doc_id, event_type, ts}]
    if not rows:
        return {"updated": 0, "skipped": 0, "neutral": 0}

    # Group by doc_id, accumulate decay-weighted exposures and positive uses.
    stats: dict[str, dict] = {}
    for row in rows:
        doc_id = row["doc_id"]
        s = stats.setdefault(doc_id, {"exposures": 0.0, "uses": 0.0})
        try:
            ts = datetime.fromisoformat(row["ts"].replace("Z", "+00:00"))
            age_days = max(0.0, (now - ts).total_seconds() / 86400.0)
        except Exception:
            age_days = 0.0
        w = _decay(age_days)
        if row["event_type"] == "exposure":
            s["exposures"] += w
        elif row["event_type"] in ("used", "edited"):
            s["uses"] += w

    updated = skipped = neutral = 0
    for doc_id, s in stats.items():
        u = s["uses"]
        if u <= 0.0:
            neutral += 1   # exposure-only → leave at 1.0 default, write nothing
            continue
        quality = 1.0 + _USE_BOOST * (u / (u + _PRIOR))
        try:
            store.update_chunk_quality(
                doc_id, round(quality, 4), round(s["exposures"], 2), round(u, 2))
            updated += 1
        except Exception as exc:  # noqa: BLE001
            log.debug("aggregate_feedback: update_chunk_quality failed for %s: %s", doc_id, exc)
            skipped += 1

    return {"updated": updated, "skipped": skipped, "neutral": neutral}


# ---------------------------------------------------------------------------
# Ranking multiplier hook (neutral until data accrues)
# ---------------------------------------------------------------------------

def apply_quality_multiplier(results: list[dict], store,
                             *, weight: float = 0.0) -> list[dict]:
    """Boost results by chunk_quality, scaled by `weight`. Boost-only: quality is
    in [1.0, 1+_USE_BOOST], so this never lowers a score.

    weight=0.0 (default) means identity: score unchanged regardless of quality.
    weight=1.0 applies the full boost: score *= quality.

    Neutral until positive signal accrues (chunk_quality defaults to 1.0 for every
    chunk and is only written above 1.0 for chunks with 'used'/'edited' events).
    This hook is the insertion point; the ranker wires it once weight is tuned (S4).
    """
    if weight == 0.0 or not results:
        return results

    out = []
    for r in results:
        quality = store.get_chunk_quality(r.get("doc_id") or "")
        adjusted_score = float(r.get("score") or 0.0) * (1.0 + weight * (quality - 1.0))
        out.append({**r, "score": round(adjusted_score, 4)})
    return out
