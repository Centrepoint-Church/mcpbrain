"""Auto-graduation — enable data-gated brain-layer flags once their readiness
condition is genuinely met (not merely "a week passed").

Governing rule respected: every gate is a deterministic check on an EXTERNAL
signal (the recall accept signal) or a safety projection (the decay dry-run) —
never the model grading its own output. The mechanism is:

  - CONSERVATIVE: thresholds require real accumulated data + a time span.
  - SAFE: decay only graduates if a dry-run shows it would not gut recall.
  - REVERSIBLE + RESPECTFUL: it only flips a flag that is ABSENT from config.json
    (a held default). If the user has explicitly set the flag — true OR false —
    it is left alone forever. A graduated flag is persisted to config.json and a
    change is recorded, so the flip is visible and the user can turn it back off.

Gated flags (chosen because their blocker is *data*, not algorithm or review):
  bandit_auto_apply, lessons — need real accept-signal volume to tune/learn from.
  decay                      — needs enough recall-access history that "stale"
                               is informative, AND must not cold-tier the corpus.

NOT gated here (their blocker is not data): Q6 rerank/routing/crag (algorithm —
needs a cross-encoder), procedural_memory (needs human review of suggestions),
schema_grounding / write_time_dedup / importance_llm (need a one-off validation).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

log = logging.getLogger("mcpbrain.auto_enable")

# Conservative defaults; each is overridable via config.json.
_ACCEPT_MIN = 50           # used/edited accept events before bandit/lessons graduate
_ACCEPT_MIN_DAYS = 7       # ...spanning at least this many days
_DECAY_ACCESS_MIN = 1000   # chunks with last_accessed before decay graduates
_DECAY_MIN_DAYS = 14       # ...recall activity spanning at least this many days
_DECAY_MAX_FRACTION = 0.40 # ...and a dry-run must cold-tier <= this fraction

# flag name -> readiness predicate name (resolved below)
_GATED = ("bandit_auto_apply", "lessons", "decay")


def _thresholds(home: str) -> dict:
    from mcpbrain import config
    cfg = config.read_config(home).get("auto_enable_thresholds") or {}
    return {
        "accept_min":        int(cfg.get("accept_min", _ACCEPT_MIN)),
        "accept_min_days":   float(cfg.get("accept_min_days", _ACCEPT_MIN_DAYS)),
        "decay_access_min":  int(cfg.get("decay_access_min", _DECAY_ACCESS_MIN)),
        "decay_min_days":    float(cfg.get("decay_min_days", _DECAY_MIN_DAYS)),
        "decay_max_fraction": float(cfg.get("decay_max_fraction", _DECAY_MAX_FRACTION)),
    }


def _span_days(ts_values: list[str]) -> float:
    """Days between the earliest and latest parseable ISO timestamp (0 if <2)."""
    dts = []
    for t in ts_values:
        if not t:
            continue
        try:
            dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dts.append(dt)
        except ValueError:
            continue
    if len(dts) < 2:
        return 0.0
    return (max(dts) - min(dts)).total_seconds() / 86400.0


def _accept_stats(store) -> tuple[int, float]:
    """(count, span_days) of real accept events (used/edited) in recall_feedback."""
    try:
        with store._connect() as db:
            rows = db.execute(
                "SELECT ts FROM recall_feedback "
                "WHERE event_type IN ('used','edited')").fetchall()
    except Exception:  # noqa: BLE001 — table may not exist yet
        return 0, 0.0
    ts = [r[0] for r in rows]
    return len(ts), _span_days(ts)


def _access_stats(store) -> tuple[int, float]:
    """(count, span_days) of chunks with a last_accessed stamp in chunk_quality."""
    try:
        with store._connect() as db:
            rows = db.execute(
                "SELECT last_accessed FROM chunk_quality "
                "WHERE last_accessed IS NOT NULL AND last_accessed != ''").fetchall()
    except Exception:  # noqa: BLE001 — column/table may not exist yet
        return 0, 0.0
    ts = [r[0] for r in rows]
    return len(ts), _span_days(ts)


def _readiness(store, home: str) -> dict:
    """Per-flag readiness: {flag: (ready_bool, reason_str)}. No side effects."""
    th = _thresholds(home)
    out: dict = {}

    n_accept, accept_span = _accept_stats(store)
    accept_ready = n_accept >= th["accept_min"] and accept_span >= th["accept_min_days"]
    accept_reason = (f"{n_accept}/{th['accept_min']} accept events over "
                     f"{accept_span:.1f}/{th['accept_min_days']:.0f}d")
    out["bandit_auto_apply"] = (accept_ready, accept_reason)
    out["lessons"] = (accept_ready, accept_reason)

    n_access, access_span = _access_stats(store)
    if n_access < th["decay_access_min"] or access_span < th["decay_min_days"]:
        out["decay"] = (False, f"{n_access}/{th['decay_access_min']} accessed over "
                               f"{access_span:.1f}/{th['decay_min_days']:.0f}d")
    else:
        # Safety projection: only graduate decay if it would not gut recall.
        from mcpbrain.decay import apply_decay_pass
        proj = apply_decay_pass(store, home, dry_run=True)
        frac = proj.get("fraction", 1.0)
        ready = frac <= th["decay_max_fraction"]
        out["decay"] = (ready, f"dry-run would cold-tier {frac*100:.0f}% "
                               f"(cap {th['decay_max_fraction']*100:.0f}%)")
    return out


def auto_enable_pass(store, home: str) -> dict:
    """Graduate any ready, un-overridden gated flag. Returns a summary dict.

    Never raises (best-effort background pass). Only flips flags ABSENT from
    config.json; persists each flip and records a change.
    """
    from mcpbrain import config
    if not config.auto_enable_enabled(home):
        return {"enabled": [], "skipped": "auto_enable disabled"}

    cfg = config.read_config(home)
    readiness = _readiness(store, home)
    enabled: list[str] = []
    for flag in _GATED:
        if flag in cfg:
            continue  # explicit user setting (true or false) — never override
        ready, reason = readiness.get(flag, (False, "unknown"))
        if not ready:
            log.debug("auto_enable: %s not ready (%s)", flag, reason)
            continue
        try:
            config.write_config(home, {flag: True})
        except Exception as exc:  # noqa: BLE001
            log.warning("auto_enable: failed to persist %s: %s", flag, exc)
            continue
        enabled.append(flag)
        log.info("auto_enable: graduated %s ON (%s)", flag, reason)
        try:
            store.record_change(
                "auto_enable",
                summary=f"auto-enabled {flag} ({reason})",
                source="auto_enable")
        except Exception:  # noqa: BLE001 — recording is best-effort
            pass
    return {"enabled": enabled,
            "readiness": {k: {"ready": v[0], "reason": v[1]} for k, v in readiness.items()}}
