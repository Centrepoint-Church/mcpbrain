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
    (age read from date/date_iso/start/modified/modifiedTime)
  - Drive/calendar items with subject matter: +0.5 (source_type gdrive/drive/calendar)
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
    date_str = (metadata.get("date") or metadata.get("date_iso")
                or metadata.get("start") or metadata.get("modified")
                or metadata.get("modifiedTime") or "")
    if not date_str:
        return None
    now = datetime.now(timezone.utc)
    # ISO-8601 first (calendar `start`, Drive `modified` incl. millis+Z, dates).
    try:
        iso = date_str.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (now - dt).total_seconds() / 86400.0)
    except ValueError:
        pass
    # RFC2822 (Gmail `date`, e.g. "Tue, 02 Jun 2026 16:30:01 +0800")
    try:
        dt = email.utils.parsedate_to_datetime(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (now - dt).total_seconds() / 86400.0)
    except (ValueError, TypeError):
        pass
    # Bare date (YYYY-MM-DD) already handled by fromisoformat.
    return None


def _is_owner_authored(metadata: dict, owner_email: str) -> bool:
    """True when the chunk was authored by the install owner.

    Prefers explicit metadata flags; otherwise derives from the sender address
    matching owner_email, or a Gmail 'SENT' label. Live ingest doesn't set the
    sender_is_owner flag, so this derivation is what actually lights up the
    high-salience band for the owner's own words."""
    if metadata.get("sender_is_owner") or metadata.get("from_owner"):
        return True
    oe = (owner_email or "").strip().lower()
    if oe:
        sender = str(metadata.get("sender") or metadata.get("from") or "").lower()
        if oe in sender:
            return True
    labels = metadata.get("labels") or []
    if isinstance(labels, str):
        labels = labels.lower()
        return "sent" in [p.strip() for p in labels.split(",")]
    return any(str(lb).strip().lower() == "sent" for lb in labels)


def score_structural(metadata: dict, *, owner_email: str = "") -> float:
    """Structural salience in [1.0, 10.0]. No LLM, no I/O.

    owner_email (optional) lets the scorer recognise owner-authored content from
    the sender address / 'SENT' label when the metadata lacks an explicit
    sender_is_owner flag (the live case)."""
    score = _BASELINE

    # Reply depth signal
    reply_depth = int(metadata.get("reply_depth", 0) or 0)
    if reply_depth >= 1:
        score += 1.5
    if reply_depth >= 2:
        score += 0.5

    # Owner sent/replied
    if _is_owner_authored(metadata, owner_email):
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
    if src in ("calendar", "google_drive", "drive", "gdrive"):
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


_LLM_POIGNANCY_PROMPT = (
    "On a scale of 1 to 10, rate the long-term poignancy / importance of the "
    "following email or note for the recipient's personal memory — 1 = purely "
    "mundane/transactional, 10 = a major decision, commitment, or relationship "
    "event worth remembering for years. Reply with ONLY the integer.\n\n{text}"
)

_LLM_BLEND_TOPK = 20   # only the top-K structurally-salient chunks per pass get an LLM score


def score_llm(text: str, *, timeout: int = 20) -> float | None:
    """LLM poignancy score in [1,10] via the claude CLI, or None on failure.

    Subscription-only (no API key). Deliberately NOT called per-chunk over the
    whole corpus — run_salience_pass applies it only to the few highest-value
    items, where an LLM judgement adds the most over the structural heuristic.
    """
    text = (text or "").strip()
    if not text:
        return None
    import subprocess
    from mcpbrain import config
    try:
        claude = config.find_claude()
    except Exception:
        return None
    try:
        proc = subprocess.run(
            [claude, "-p", _LLM_POIGNANCY_PROMPT.format(text=text[:2000]),
             "--output-format", "text"],
            capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            return None
        m = re.search(r"\b(10|[1-9])\b", proc.stdout or "")
        return float(m.group(1)) if m else None
    except Exception:  # noqa: BLE001 — CLI missing/slow/timeout → no LLM score
        return None


def run_salience_pass(store, home: str, *, cap: int = 500) -> dict:
    """Score up to `cap` unscored embedded chunks with structural salience, and —
    when config 'importance_llm' is on — blend an LLM poignancy score into the
    top few highest-salience items.

    Writes scores via store.set_chunk_salience_batch(). Returns
    {"scored": N, "llm_scored": K}. Safe to call repeatedly; only 0.0-salience
    chunks are touched.
    """
    from mcpbrain import config
    chunks = store.chunks_needing_salience(cap)
    if not chunks:
        return {"scored": 0, "llm_scored": 0}

    owner_email = config.owner_email(home)
    scored = []   # (chunk, structural_score)
    for c in chunks:
        try:
            s = score_structural(c.get("metadata") or {}, owner_email=owner_email)
        except Exception as exc:
            log.debug("salience scoring failed for %s: %s", c.get("doc_id"), exc)
            s = _BASELINE
        scored.append((c, s))

    llm_scored = 0
    if config.importance_llm_enabled(home):
        # Blend an LLM poignancy score into only the top-K structural items —
        # bounded cost, maximal signal where it matters most.
        for c, s in sorted(scored, key=lambda x: -x[1])[:_LLM_BLEND_TOPK]:
            llm = score_llm(c.get("text") or "")
            if llm is not None:
                c["_blended"] = max(_MIN, min(_MAX, round(0.6 * s + 0.4 * llm, 2)))
                llm_scored += 1

    pairs = [(c["doc_id"], c.get("_blended", s)) for c, s in scored]
    store.set_chunk_salience_batch(pairs)
    log.info("importance: scored %d chunks (%d LLM-blended)", len(pairs), llm_scored)
    return {"scored": len(pairs), "llm_scored": llm_scored}
