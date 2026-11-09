"""Numbers, freshness and health for the local console.

Pure functions over a sqlite3 connection or plain values, so each definition
lives in exactly one place and is testable without a daemon. `stats()` at the
bottom assembles the payload behind GET /api/dashboard/stats.

Definitions (the single source of truth):
  - eligible          chunks whose enrich_state is NULL, '' or 'duplicate'
                      (duplicates are included: all are enriched, so counting
                      them does not move the percentage)
  - enriched_eligible eligible chunks with enriched=1
  - waiting           eligible - enriched_eligible
  - cold              enrich_state = 'cold' (low-signal, deliberately skipped)
  - relations_live    entity_relations with invalidated_at IS NULL
  - unlinked          entities with no live relation on either side
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

# One place for the freshness threshold: 6h covers an overnight gap in the
# hourly enrichment task.
STALE_HOURS = 6.0

SOURCE_KEYS = ("gmail", "drive", "calendar", "anarlog")
_SOURCE_LABELS = {"gmail": "Gmail", "drive": "Drive", "calendar": "Calendar",
                  "anarlog": "Meetings"}
_CONNECTION_LABELS = {
    "google": "Google", "claude": "Claude plugin", "backup": "Backup",
    "records": "Records", "enrichment": "Enrichment",
}
_TYPE_KEYS = ("person", "org", "project", "topic", "meeting", "event",
              "document", "thread", "unknown")


# --- helpers ---------------------------------------------------------------

def _tables(db) -> set[str]:
    return {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def _columns(db, table: str) -> set[str]:
    return {r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()}


def pct_of(done: int, eligible: int) -> int:
    """Whole-number percentage, floored. Never 100 until everything is done,
    never above 100, and 0 when there is nothing eligible."""
    if eligible <= 0:
        return 0
    if done >= eligible:
        return 100
    return max(0, min(99, (max(0, done) * 100) // eligible))


def parse_utc(value) -> datetime | None:
    """Parse the two timestamp shapes the store writes ('YYYY-MM-DD HH:MM:SS'
    from datetime('now'), and ISO strings with an offset) into an aware UTC
    datetime. Anything unparseable gives None."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


# --- index -----------------------------------------------------------------

def index_counts(db) -> dict:
    out = {"total": 0, "eligible": 0, "cold": 0, "duplicate": 0,
           "enriched_eligible": 0, "waiting": 0, "pct": 0}
    if "chunks" not in _tables(db):
        return out
    cols = _columns(db, "chunks")
    st = "COALESCE(enrich_state,'')" if "enrich_state" in cols else "''"
    enr = "COALESCE(enriched,0)" if "enriched" in cols else "0"
    row = db.execute(
        f"SELECT COUNT(*),"
        f" SUM(CASE WHEN {st}='cold' THEN 1 ELSE 0 END),"
        f" SUM(CASE WHEN {st}='duplicate' THEN 1 ELSE 0 END),"
        f" SUM(CASE WHEN {st} IN ('','duplicate') THEN 1 ELSE 0 END),"
        f" SUM(CASE WHEN {st} IN ('','duplicate') AND {enr}=1 THEN 1 ELSE 0 END)"
        f" FROM chunks").fetchone()
    total, cold, dup, eligible, done = (int(v or 0) for v in row)
    out.update(total=total, cold=cold, duplicate=dup, eligible=eligible,
               enriched_eligible=done, waiting=max(0, eligible - done),
               pct=pct_of(done, eligible))
    return out


# --- graph -----------------------------------------------------------------

def graph_counts(db) -> dict:
    by_type = {k: 0 for k in _TYPE_KEYS}
    out = {"by_type": by_type, "relations_live": 0, "observations_live": 0,
           "unlinked": 0}
    tables = _tables(db)
    if "entities" in tables:
        type_expr = "type" if "type" in _columns(db, "entities") else "NULL"
        for typ, n in db.execute(
                f"SELECT {type_expr}, COUNT(*) FROM entities GROUP BY 1").fetchall():
            key = typ if typ in by_type else "unknown"
            by_type[key] += int(n)

    live_rel = ""
    if "entity_relations" in tables:
        rel_cols = _columns(db, "entity_relations")
        live_rel = " WHERE invalidated_at IS NULL" if "invalidated_at" in rel_cols else ""
        out["relations_live"] = int(db.execute(
            f"SELECT COUNT(*) FROM entity_relations{live_rel}").fetchone()[0])
    if "entity_observations" in tables:
        obs_cols = _columns(db, "entity_observations")
        live = " WHERE invalidated_at IS NULL" if "invalidated_at" in obs_cols else ""
        out["observations_live"] = int(db.execute(
            f"SELECT COUNT(*) FROM entity_observations{live}").fetchone()[0])

    if "entities" in tables:
        if "entity_relations" in tables and {"entity_a", "entity_b"} <= _columns(db, "entity_relations"):
            cond = "AND invalidated_at IS NULL" if live_rel else ""
            out["unlinked"] = int(db.execute(
                "SELECT COUNT(*) FROM entities e WHERE "
                f"NOT EXISTS (SELECT 1 FROM entity_relations r WHERE r.entity_a=e.id {cond}) "
                f"AND NOT EXISTS (SELECT 1 FROM entity_relations r WHERE r.entity_b=e.id {cond})"
            ).fetchone()[0])
        else:
            out["unlinked"] = sum(by_type.values())
    return out


# --- freshness -------------------------------------------------------------

def _cursor_rows(db) -> list[tuple[str, str | None]]:
    if "sync_cursors" not in _tables(db):
        return []
    return [(r[0], r[1]) for r in db.execute(
        "SELECT source, updated_at FROM sync_cursors").fetchall()]


def source_times(db) -> dict:
    """Last-synced time per base source (ISO, UTC) or None if never synced.
    Shared-drive cursors (`drive:<id>`) and backfill bookkeeping keys are not
    base sources and are ignored."""
    out = {k: None for k in SOURCE_KEYS}
    for source, updated in _cursor_rows(db):
        if source in out:
            out[source] = _iso(parse_utc(updated))
    return out


def shared_drive_health(db, now: datetime) -> dict:
    total = stale = 0
    cutoff = now - timedelta(hours=STALE_HOURS)
    for source, updated in _cursor_rows(db):
        if not source.startswith("drive:"):
            continue
        if source.endswith(("_backfill_until", "_backfill_empty")):
            continue
        total += 1
        when = parse_utc(updated)
        if when is None or when < cutoff:
            stale += 1
    return {"total": total, "stale": stale}


def enrich_activity(db, now: datetime) -> dict:
    """Last enrichment push and pushes in the last 24h. enrich_payloads.at
    mixes formats, so times are parsed here rather than compared in SQL."""
    out = {"last_push_at": None, "pushes_24h": 0}
    if "enrich_payloads" not in _tables(db) or "at" not in _columns(db, "enrich_payloads"):
        return out
    cutoff = now - timedelta(hours=24)
    last = None
    n = 0
    for (raw,) in db.execute("SELECT at FROM enrich_payloads").fetchall():
        when = parse_utc(raw)
        if when is None:
            continue
        if last is None or when > last:
            last = when
        if when >= cutoff:
            n += 1
    out["last_push_at"] = _iso(last)
    out["pushes_24h"] = n
    return out


def _is_stale(iso, now: datetime) -> bool:
    when = parse_utc(iso)
    return when is not None and now - when > timedelta(hours=STALE_HOURS)


# --- health ----------------------------------------------------------------

def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def health(*, now: datetime, paused: bool, stalled, watchdog_limit_reached: bool,
           connections: list, open_findings: int, pending_reviews: int,
           sources: list, waiting: int, last_push_at) -> dict:
    """Verdict for the health strip. Order: paused, stalled, needs_you, behind,
    healthy. `sources` is a list of {label, last_synced_at}; a source that has
    never synced is not stale. `reasons` lists the facts behind the winning
    state."""
    if paused:
        return {"state": "paused", "reasons": ["Paused"]}

    stalled_reasons: list[str] = []
    if stalled:
        stalled_reasons.append("The sync cycle has stopped making progress")
    if watchdog_limit_reached:
        stalled_reasons.append("The watchdog has restarted the daemon too many times")
    limit = timedelta(hours=STALE_HOURS)
    for s in sources:
        when = parse_utc(s.get("last_synced_at"))
        if when is not None and now - when > limit:
            hours = int((now - when).total_seconds() // 3600)
            stalled_reasons.append(f"{s.get('label') or s.get('name')} has not synced "
                                   f"for {_plural(hours, 'hour')}")
    if stalled_reasons:
        return {"state": "stalled", "reasons": stalled_reasons}

    needs: list[str] = []
    for c in connections or []:
        if c.get("state") == "needs_action":
            needs.append(f"{c.get('label') or c.get('name')} needs attention")
    if open_findings:
        needs.append(f"{_plural(int(open_findings), 'open finding')}")
    if pending_reviews:
        needs.append(f"{_plural(int(pending_reviews), 'change')} waiting for your review")
    if needs:
        return {"state": "needs_you", "reasons": needs}

    if waiting > 0:
        pushed = parse_utc(last_push_at)
        if pushed is None:
            return {"state": "behind", "reasons": [
                f"{waiting:,} chunks waiting and no enrichment push yet"]}
        if now - pushed > limit:
            hours = int((now - pushed).total_seconds() // 3600)
            return {"state": "behind", "reasons": [
                f"{waiting:,} chunks waiting, last push {_plural(hours, 'hour')} ago"]}

    return {"state": "healthy", "reasons": []}


# --- payload ---------------------------------------------------------------

def _reflow_active(r) -> bool:
    """Re-chunking is shown only while halted, blocked or still has work."""
    if not r:
        return False
    if r.get("halted"):
        return True
    pending = (r.get("queued") or 0) + (r.get("remaining") or 0)
    if pending:
        return True
    last = (r.get("last_seed") or {}).get("status")
    gated = last in ("no_recent_backup", "disabled", "error")
    return bool(gated and r.get("remaining") is None)


def stats(store, home, status: dict, *, now: datetime | None = None) -> dict:
    """Assemble the dashboard payload. Every count comes from one read-only
    connection so the figures agree with each other. `status` is the daemon's
    status() snapshot (passed in so this stays testable without a daemon)."""
    from mcpbrain.dashboard import _open_ro, queue_state  # noqa: PLC0415

    now = now or datetime.now(timezone.utc)
    path = store._path if hasattr(store, "_path") else store.path
    try:
        db = _open_ro(path)
        try:
            idx = index_counts(db)
            gc = graph_counts(db)
            times = source_times(db)
            shared = shared_drive_health(db, now)
            activity = enrich_activity(db, now)
        finally:
            db.close()
    except sqlite3.Error as exc:
        # A WAL checkpoint or lock must not blank the dashboard: zero sections.
        log.warning("stats: cannot read the store: %s", exc)
        empty = sqlite3.connect(":memory:")
        try:
            idx = index_counts(empty)
            gc = graph_counts(empty)
            times = source_times(empty)
            shared = shared_drive_health(empty, now)
            activity = enrich_activity(empty, now)
        finally:
            empty.close()

    try:
        circles = len(store.list_communities())
    except Exception as exc:  # noqa: BLE001 — degrade, never break the payload
        log.warning("stats: list_communities failed: %s", exc)
        circles = 0
    # Reviews waiting in Needs you: proposed graph corrections plus automatic
    # changes held under "ask me first" (auto_changes).
    try:
        reviews = len(store.pending_corrections())
    except Exception as exc:  # noqa: BLE001
        log.warning("stats: pending_corrections failed: %s", exc)
        reviews = 0
    try:
        reviews += int(store.pending_changes_count())
    except Exception as exc:  # noqa: BLE001
        log.warning("stats: pending_changes_count failed: %s", exc)

    backfill = status.get("backfill") or {}
    sources = []
    for key in SOURCE_KEYS:
        bf = backfill.get(key) or {}
        entry = {
            "name": key, "label": _SOURCE_LABELS[key],
            "last_synced_at": times[key],
            "stale": _is_stale(times[key], now),
            # Meetings has no backfill: it reads the local database in full.
            "history": {"done": True if key == "anarlog" else bool(bf.get("done")),
                        "reached": bf.get("reached")},
        }
        if key == "drive":
            entry["shared"] = shared
        sources.append(entry)

    conns = status.get("connections") or {}
    connections = [
        {"name": k, "label": _CONNECTION_LABELS.get(k, k.title()),
         "state": (conns.get(k) or {}).get("state", "not_started"),
         "detail": (conns.get(k) or {}).get("detail", "")}
        for k in ("google", "claude", "backup", "records", "enrichment") if k in conns
    ]

    spool = status.get("spool") or {}
    units_pending = int(spool.get("pending") or 0)
    open_findings = int(status.get("open_findings") or 0)
    # Each Needs-you item shows once: a pending change or proposed correction
    # also carries a finding, but it is counted (and listed) as a review.
    try:
        from mcpbrain.dashboard import REVIEW_FINDING_TYPES  # noqa: PLC0415
        open_findings = int(store.open_findings_count(exclude_types=REVIEW_FINDING_TYPES))
    except Exception:  # noqa: BLE001 — fall back to the status snapshot's total
        pass
    paused = bool(status.get("paused"))

    verdict = health(
        now=now, paused=paused, stalled=status.get("stalled"),
        watchdog_limit_reached=bool(status.get("watchdog_limit_reached")),
        connections=connections, open_findings=open_findings,
        pending_reviews=reviews, sources=sources, waiting=idx["waiting"],
        last_push_at=activity["last_push_at"])

    reflow = status.get("reflow")
    return {
        "as_of": now.isoformat(),
        "index": idx,
        "enrich": {
            "queue_state": queue_state(queued=units_pending,
                                       unenriched_eligible=idx["waiting"]),
            "units_pending": units_pending,
            "last_push_at": activity["last_push_at"],
            "pushes_24h": activity["pushes_24h"],
        },
        "graph": {**gc, "circles": circles},
        "sources": sources,
        "health": verdict,
        "needs_you": {
            "connections": [c for c in connections if c["state"] == "needs_action"],
            "findings": open_findings,
            "reviews": reviews,
        },
        "connections": connections,
        "system": {
            "version": status.get("version") or "",
            "paused": paused,
            "watchdog_exits": int(status.get("watchdog_exits") or 0),
            "reflow": reflow if _reflow_active(reflow) else None,
        },
    }
