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

import json
import logging
import math
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

log = logging.getLogger("mcpbrain.decay")

_COLD_THRESHOLD = 0.25   # R below this → cold (demote from default recall)
_FLOOR_SALIENCE = 6.0    # chunks with salience >= this are exempt from demotion.
                         # B5 audit (2026-06-24): live corpus max salience = 7.0 but
                         # only 1 chunk reaches it (the first consolidated note); 48
                         # chunks are >= 6.0 (0.06% of 80,705). The original 7.0
                         # floor effectively exempted nothing. 6.0 protects the true
                         # high-salience band (consolidated semantic notes + the
                         # freshest, highest-engagement owner-authored content) while
                         # letting lower-ranked chunks decay normally.
_INITIAL_STRENGTH = 5.0  # strength for a never-recalled chunk


def compute_decay(strength: float, days_since: float) -> float:
    """R = exp(-days_since / strength). Returns 1.0 for 0 days (freshly accessed)."""
    if strength <= 0:
        strength = _INITIAL_STRENGTH
    if days_since <= 0:
        return 1.0
    return math.exp(-days_since / max(strength, 0.1))


_SOURCE_DATE_KEYS = ("modified", "modifiedTime", "date", "start", "created", "timestamp")


def _source_date(metadata) -> datetime | None:
    """Best-effort source timestamp (UTC) from a chunk's metadata.

    Used as the decay age-anchor for chunks that have never been recalled
    (no last_accessed) — otherwise such chunks would never decay. Handles the
    ISO-8601 forms (gdrive `modified`, calendar `start`) and gmail's RFC-2822
    `date`. Returns None when no parseable date is present (enriched/unknown
    sources), in which case the caller stays conservative and skips the chunk.
    """
    if not metadata:
        return None
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (ValueError, TypeError):
            return None
    if not isinstance(metadata, dict):
        return None
    for key in _SOURCE_DATE_KEYS:
        raw = metadata.get(key)
        if not raw or not isinstance(raw, str):
            continue
        dt = None
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            try:
                dt = parsedate_to_datetime(raw)
            except (ValueError, TypeError):
                dt = None
        if dt is None:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    return None


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
                     now: str | None = None, dry_run: bool = False) -> dict:
    """Evaluate decay for all eligible chunks; demote those below threshold.

    Eligible = embedded + not 'core' tier.
    Demoted = R < _COLD_THRESHOLD AND salience < _FLOOR_SALIENCE.
    High-salience chunks are exempt from demotion (their R is still updated
    in memory_strength for future auditing).

    Returns {"evaluated": N, "demoted": M, "exempt": K}.

    dry_run=True: a PROJECTION — bypasses the decay_enabled gate (so the
    auto-enabler can ask "what would decay do here?" while the flag is still off),
    evaluates the whole corpus, writes NOTHING, and returns
    {"evaluated": N, "would_demote": M, "exempt": K, "fraction": M/N}. This is the
    safety check that prevents auto-enabling decay when it would gut recall.
    """
    from mcpbrain import config
    if not dry_run and not config.decay_enabled(home):
        return {"evaluated": 0, "demoted": 0, "exempt": 0}

    ts = now or _now_iso()
    now_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)

    # Projection evaluates the whole corpus; the live pass is bounded per run.
    chunks = store.chunks_for_decay_pass(limit=500000 if dry_run else 5000)
    to_demote = []
    exempt = 0

    for c in chunks:
        doc_id = c["doc_id"]
        strength = float(c.get("memory_strength") or _INITIAL_STRENGTH)
        last_accessed = c.get("last_accessed") or ""
        salience = float(c.get("salience") or 0.0)

        if last_accessed:
            try:
                anchor = datetime.fromisoformat(last_accessed.replace("Z", "+00:00"))
            except ValueError:
                continue
        else:
            # Never recalled: anchor age on the source date (email/file/event
            # time) so old, never-touched memories still decay. If no source
            # date is parseable, stay conservative and skip (don't cold-tier
            # a chunk whose age we can't establish).
            anchor = _source_date(c.get("metadata"))
            if anchor is None:
                continue

        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=timezone.utc)
        days = max(0.0, (now_dt - anchor).total_seconds() / 86400.0)

        r = compute_decay(strength, days)

        if salience >= _FLOOR_SALIENCE:
            exempt += 1
            continue

        if r < _COLD_THRESHOLD:
            to_demote.append(doc_id)

    if dry_run:
        evaluated = len(chunks)
        return {"evaluated": evaluated, "would_demote": len(to_demote),
                "exempt": exempt,
                "fraction": (len(to_demote) / evaluated) if evaluated else 0.0}

    demoted = 0
    if to_demote:
        from mcpbrain.memory_tier import demote_to_cold
        demoted = demote_to_cold(store, to_demote)

    log.info("decay pass: evaluated=%d demoted=%d exempt=%d",
             len(chunks), demoted, exempt)
    return {"evaluated": len(chunks), "demoted": demoted, "exempt": exempt}
