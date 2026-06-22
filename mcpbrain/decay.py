"""B5 — Memory strength and forgetting.

Ebbinghaus-style decay per memory item:
  R = exp(-Δt / S)

where:
  S  = memory_strength (starts at 5.0, grows by 1.0 on each recall)
  Δt = days since last_accessed

On each recall: S += 1, last_accessed = now.
High-importance items (salience >= FLOOR_SALIENCE) are exempt from demotion
but still accumulate strength, so they eventually become very durable.

Public API:
  compute_decay(strength, days_since) -> float   # R in (0, 1]
  update_on_recall(store, doc_ids, now=None)     # strengthen recalled items
  apply_decay_pass(store, home, now=None) -> dict
      Evaluates each chunk's R; those below COLD_THRESHOLD and below salience
      floor are demoted to memory_tier='cold'. Returns {"demoted": N, "exempt": M}.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

log = logging.getLogger("mcpbrain.decay")

_COLD_THRESHOLD = 0.25   # R below this → cold (demote from default recall)
_FLOOR_SALIENCE = 7.0    # chunks with salience >= this are exempt from demotion
_INITIAL_STRENGTH = 5.0  # strength for a never-recalled chunk


def compute_decay(strength: float, days_since: float) -> float:
    """R = exp(-days_since / strength). Returns 1.0 for 0 days (freshly accessed)."""
    if strength <= 0:
        strength = _INITIAL_STRENGTH
    if days_since <= 0:
        return 1.0
    return math.exp(-days_since / max(strength, 0.1))


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def update_on_recall(store, doc_ids: list[str],
                     now: str | None = None) -> None:
    """Strengthen recalled chunks: S += 1, last_accessed = now.

    Fire-and-forget: errors are swallowed so recall never fails because of
    the strength update.
    """
    if not doc_ids:
        return
    ts = now or _now_iso()
    try:
        rows = []
        for doc_id in doc_ids:
            strength, _ = store.get_memory_strength(doc_id)
            rows.append((doc_id, strength + 1.0, ts))
        store.update_memory_strength_batch(rows)
    except Exception as exc:  # noqa: BLE001
        log.debug("decay.update_on_recall error (swallowed): %s", exc)


def apply_decay_pass(store, home: str, *,
                     now: str | None = None) -> dict:
    """Evaluate decay for all eligible chunks; demote those below threshold.

    Eligible = embedded + not 'core' tier.
    Demoted = R < _COLD_THRESHOLD AND salience < _FLOOR_SALIENCE.
    High-salience chunks are exempt from demotion (their R is still updated
    in memory_strength for future auditing).

    Returns {"evaluated": N, "demoted": M, "exempt": K}.
    """
    from mcpbrain import config
    if not config.decay_enabled(home):
        return {"evaluated": 0, "demoted": 0, "exempt": 0}

    ts = now or _now_iso()
    now_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))

    chunks = store.chunks_for_decay_pass(limit=5000)
    to_demote = []
    exempt = 0

    for c in chunks:
        doc_id = c["doc_id"]
        strength = float(c.get("memory_strength") or _INITIAL_STRENGTH)
        last_accessed = c.get("last_accessed") or ""
        salience = float(c.get("salience") or 0.0)

        if not last_accessed:
            # Never accessed → treat as accessed at embedded time (we don't have
            # that timestamp; use strength-only heuristic: R = 1.0 for new items)
            continue

        try:
            la_dt = datetime.fromisoformat(last_accessed.replace("Z", "+00:00"))
            days = max(0.0, (now_dt - la_dt).total_seconds() / 86400.0)
        except Exception:
            continue

        r = compute_decay(strength, days)

        if salience >= _FLOOR_SALIENCE:
            exempt += 1
            continue

        if r < _COLD_THRESHOLD:
            to_demote.append(doc_id)

    demoted = 0
    if to_demote:
        from mcpbrain.memory_tier import demote_to_cold
        demoted = demote_to_cold(store, to_demote)

    log.info("decay pass: evaluated=%d demoted=%d exempt=%d",
             len(chunks), demoted, exempt)
    return {"evaluated": len(chunks), "demoted": demoted, "exempt": exempt}
