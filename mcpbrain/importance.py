"""B3 — Salience scoring for chunks.

Structural scorer (deterministic, no LLM) converts chunk metadata into a
1.0–10.0 importance score. Signals (all additive, capped at 10):

  - Baseline: 3.0 (all content starts here)
  - reply_depth >= 1: +1.5 (a reply happened — conversation value)
  - reply_depth >= 2: +0.5 more (deep thread)
  - sender_is_owner: +1.5 (Josh authored it — high recall value)
  - label_starred or importance_high: +1.0 (explicit user signal)
  - known entity count (entities in metadata): +0.3 per entity, cap +1.5
  - Gmail category IMPORTANT: +0.5; PROMOTIONS/UPDATES: -1.5
  - Recency bonus: 0–2.0 based on age (< 7 days: +2, <30d: +1, <90d: +0.5, else 0)
  - Drive/calendar items with subject matter: +0.5
  - no-reply / auto-send: -2.0

Public API:
  score_structural(metadata) -> float        # synchronous, in [1.0, 10.0]
  run_salience_pass(store, home, *, cap) -> dict
"""

from __future__ import annotations

import email.utils
import json
import logging
import math
import re
from datetime import datetime, timezone

log = logging.getLogger("mcpbrain.importance")

_NOREPLY_RE = re.compile(r"\bno.?reply\b|\bnoreply\b|\bmailer.daemon\b|\bautomated?\b",
                         re.IGNORECASE)

_BASELINE = 3.0
_MAX = 10.0
_MIN = 1.0


def _parse_age_days(metadata: dict) -> float | None:
    """Days since the chunk's date. Returns None if unparseable."""
    date_str = metadata.get("date") or metadata.get("date_iso") or metadata.get("start") or ""
    if not date_str:
        return None
    now = datetime.now(timezone.utc)
    try:
        # RFC2822 (Gmail)
        dt = email.utils.parsedate_to_datetime(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (now - dt).total_seconds() / 86400.0)
    except Exception:
        pass
    # ISO-8601 (calendar, Drive)
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S+00:00", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(date_str[:19], fmt[:len(date_str[:19])])
            dt = dt.replace(tzinfo=timezone.utc)
            return max(0.0, (now - dt).total_seconds() / 86400.0)
        except Exception:
            continue
    return None


def score_structural(metadata: dict) -> float:
    """Structural salience in [1.0, 10.0]. No LLM, no I/O."""
    score = _BASELINE

    # Reply depth signal
    reply_depth = int(metadata.get("reply_depth", 0) or 0)
    if reply_depth >= 1:
        score += 1.5
    if reply_depth >= 2:
        score += 0.5

    # Owner sent/replied
    if metadata.get("sender_is_owner") or metadata.get("from_owner"):
        score += 1.5

    # Explicit user signals
    labels = metadata.get("labels") or []
    if isinstance(labels, str):
        try:
            labels = json.loads(labels)
        except Exception:
            labels = labels.split(",")
    labels_lower = [str(lb).lower() for lb in labels]
    if "starred" in labels_lower or metadata.get("starred"):
        score += 1.0
    if "important" in labels_lower or metadata.get("importance") == "high":
        score += 0.5
    if any("promotions" in lb or "updates" in lb for lb in labels_lower):
        score -= 1.5

    # Known-entity density
    entities = metadata.get("entities") or metadata.get("known_entities") or []
    if isinstance(entities, str):
        try:
            entities = json.loads(entities)
        except Exception:
            entities = []
    entity_boost = min(len(entities) * 0.3, 1.5)
    score += entity_boost

    # Source-type bonus for structured content
    src = metadata.get("source_type", "")
    if src in ("calendar", "google_drive", "drive"):
        score += 0.5

    # No-reply / automated sender penalty
    sender = metadata.get("sender") or metadata.get("from") or ""
    if _NOREPLY_RE.search(sender):
        score -= 2.0

    # Recency bonus
    age = _parse_age_days(metadata)
    if age is not None:
        if age < 7:
            score += 2.0
        elif age < 30:
            score += 1.0
        elif age < 90:
            score += 0.5

    return max(_MIN, min(_MAX, round(score, 2)))


def recency_decay(metadata: dict, *, alpha: float = 0.01) -> float:
    """Exponential recency weight for the three-axis ranker.

    Returns a value in (0, 1]: 1.0 for today's content, falling with age.
    alpha controls decay rate: 0.01 → half-life ~69 days.
    """
    age = _parse_age_days(metadata)
    if age is None:
        return 0.5  # unknown age: neutral
    return math.exp(-alpha * age)


def run_salience_pass(store, home: str, *, cap: int = 500) -> dict:
    """Score up to `cap` unscored embedded chunks with structural salience.

    Writes scores back via store.set_chunk_salience_batch(). Returns
    {"scored": N, "skipped": 0} — skipped is always 0 for the structural scorer
    (no LLM call to fail). Safe to call repeatedly; only 0.0-salience chunks
    are touched.
    """
    chunks = store.chunks_needing_salience(cap)
    if not chunks:
        return {"scored": 0, "skipped": 0}

    pairs = []
    for c in chunks:
        try:
            s = score_structural(c.get("metadata") or {})
        except Exception as exc:
            log.debug("salience scoring failed for %s: %s", c.get("doc_id"), exc)
            s = _BASELINE
        pairs.append((c["doc_id"], s))

    store.set_chunk_salience_batch(pairs)
    log.info("importance: scored %d chunks", len(pairs))
    return {"scored": len(pairs), "skipped": 0}
