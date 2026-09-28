#!/usr/bin/env python3
"""Attended reflow dry run on a STORE COPY (extraction fidelity, plan Task 16).

  python bin/reflow_dryrun.py --store <copy.sqlite3>                    # plan only
  python bin/reflow_dryrun.py --store <copy.sqlite3> --limit 200 --per-mime \\
      --out <summary.json> --yes

Runs the real reflow handler (ReflowContext.handle) over up to --limit
reflow_candidates of the COPY, with the real Google services and embedder
built exactly as bin/repair.py builds them (auth.build_google_services(),
embed.get_embedder()) and the real anarlog database when anarlog is enabled.
Then reports outcomes per class and per MIME, carried vs re-enrich chunk
counts, per-item extraction / embed / apply_reflow seconds (p50/p95/max), the
Gmail/Calendar "source changed" rate, and the store checks: every
_REFLOW_REF_COLUMNS reference with no chunk row (before AND after),
PRAGMA foreign_key_check and PRAGMA integrity_check.

Safety, by construction:
  * refuses the live store (config.store_path(), resolved, or the same file);
  * never publishes: the handler runs with record_publish=False, so no
    shared-drive pending publish is recorded, and a CHANGED Shared Drive file
    (which only the cycle's shared-drive handler -- fleet storage and all --
    could work) is counted as ordinary and NOT run;
  * never touches the daemon (no control API call, no launchctl);
  * writes only to the copy (the embedder's model cache aside). The live
    install's home is only READ: config flags, the enrich queue (in-flight
    units defer an owner, exactly as they would live) and the anarlog db.

Exit status: 0 = clean; 1 = a NEW orphan reference (after > before on any
column; with --strict-orphans, any orphan at all), foreign_key_check > 0 on a
rebuilt store (one whose tables carry REFERENCES clauses), integrity_check
not ok, or the run halted on a ReflowOrphanError; 2 = refused / bad input.

Why orphans gate on NEW ones by default: the live store already holds
references with no chunk row before any reflow touches it (relations whose
source chunk retention deleted; entity_observations.source values that are
not doc ids at all), so an absolute gate would fail every run and teach the
operator to ignore it. The count before and after is always reported.
"""
import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcpbrain import config  # noqa: E402

_SHORT_MIME = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/rtf": "rtf",
    "application/vnd.google-apps.document": "gdoc",
    "application/vnd.google-apps.presentation": "gslides",
}


# -- seams (tests replace these) ------------------------------------------------

def _build_services() -> dict:
    from mcpbrain.auth import build_google_services
    return build_google_services()


def _get_embedder():
    from mcpbrain.embed import get_embedder
    return get_embedder()


# -- helpers --------------------------------------------------------------------

def _is_live(path: Path) -> bool:
    live = config.store_path()
    try:
        if path.resolve() == live.resolve():
            return True
        return live.exists() and path.exists() and path.samefile(live)
    except OSError:
        return False


def _classify(store, src: str, owner: str) -> str:
    """A sampling/reporting class: the Drive MIME, gmail vs gmail attachment
    MIME, calendar, anarlog."""
    from mcpbrain.sync.reflow_handler import ReflowContext
    kind = src.split(":", 1)[1]
    try:
        rows = store.owner_chunks(ReflowContext._prefixes(kind, owner))
    except Exception:  # noqa: BLE001 — classification is cosmetic
        rows = []
    if kind == "drive":
        mime = ((rows[0]["metadata"] or {}).get("mime_type") if rows else "") or ""
        return "drive:" + _SHORT_MIME.get(mime, mime or "unknown")
    if kind == "gmail":
        att = sorted({(r["metadata"] or {}).get("attachment_mime", "") for r in rows
                      if "-att-" in r["doc_id"]} - {""})
        if att:
            return "gmail-att:" + _SHORT_MIME.get(att[0], att[0])
        return "gmail"
    return kind


def _select(store, limit: int, sources, per_mime: bool) -> list[tuple[str, str, str]]:
    if not per_mime:
        return [(s, o, _classify(store, s, o))
                for s, o in store.reflow_candidates(limit, sources=sources)]
    pool = store.reflow_candidates(min(max(limit * 10, limit), 20000), sources=sources)
    by: dict[str, list] = {}
    for s, o in pool:
        by.setdefault(_classify(store, s, o), []).append((s, o))
    out: list[tuple[str, str, str]] = []
    while len(out) < limit and any(by.values()):
        for cls in sorted(by):
            if by[cls] and len(out) < limit:
                s, o = by[cls].pop(0)
                out.append((s, o, cls))
    return out


def _orphans(store) -> dict[str, int]:
    from mcpbrain.store import _REFLOW_REF_COLUMNS
    out = {}
    with store._connect() as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, col in _REFLOW_REF_COLUMNS:
            if table not in tables:
                continue
            out[f"{table}.{col}"] = db.execute(
                f"SELECT count(*) FROM {table} t WHERE t.{col} IS NOT NULL AND t.{col} != '' "
                f"AND NOT EXISTS (SELECT 1 FROM chunks c WHERE c.doc_id = t.{col})"
            ).fetchone()[0]
    return out


def _checks(store) -> dict:
    with store._connect() as db:
        fk = len(db.execute("PRAGMA foreign_key_check").fetchall())
        rebuilt = bool(db.execute("PRAGMA foreign_key_list(entity_relations)").fetchall())
        integ = [r[0] for r in db.execute("PRAGMA integrity_check").fetchall()]
    return {"foreign_key_check": fk, "rebuilt_store": rebuilt,
            "integrity_check": "ok" if integ == ["ok"] else integ[:20]}


def _dist(xs: list[float]) -> dict:
    if not xs:
        return {"n": 0, "p50": None, "p95": None, "max": None}
    s = sorted(xs)

    def pct(p):
        return round(s[min(len(s) - 1, int(round(p * (len(s) - 1))))], 3)
    return {"n": len(s), "p50": pct(0.5), "p95": pct(0.95), "max": round(s[-1], 3)}


# -- the run --------------------------------------------------------------------

def run(store, *, home, services: dict, embedder, limit: int, per_mime: bool) -> dict:
    from mcpbrain.reflow import REFLOW_SOURCE_SERVICES, workable_reflow_sources
    from mcpbrain.store import ReflowOrphanError
    from mcpbrain.sync import queue
    from mcpbrain.sync.reflow_handler import ReflowContext

    anarlog_db = config.anarlog_db_path(home)
    workable = workable_reflow_sources(home, services)
    now = {s for s, k in REFLOW_SOURCE_SERVICES.items() if services.get(k) is not None}
    sources = (now | {"reflow:anarlog"}) & workable
    orphans_before = _orphans(store)

    shared_not_run: list[str] = []

    def _drive_normal(item):
        if item["source"] != "drive":
            shared_not_run.append(item["ref_id"])   # needs fleet storage: never here
            return
        from mcpbrain.sync import drive
        drive.handle_drive_item(services.get("drive_service"), store, item,
                                folder_cache=ctx._folder_cache)

    ctx = ReflowContext(store, embedder, home,
                        drive_service=services.get("drive_service"),
                        gmail_service=services.get("gmail_service"),
                        calendar_service=services.get("calendar_service"),
                        anarlog_db=anarlog_db, max_items=max(limit, 1),
                        max_seconds=10 ** 9, normal_handlers={"drive": _drive_normal},
                        record_publish=False)

    cur: dict = {}

    def timed(name, fn):
        def w(*a, **k):
            t = time.monotonic()
            try:
                return fn(*a, **k)
            finally:
                cur[name] = cur.get(name, 0.0) + time.monotonic() - t
        return w

    for kind in ("drive", "gmail", "anarlog", "calendar"):
        setattr(ctx, f"_new_{kind}", timed("extract_s", getattr(ctx, f"_new_{kind}")))
    ctx._embed = timed("embed_s", ctx._embed)
    real_apply = store.apply_reflow

    def _apply(*a, **k):
        st = timed("apply_s", real_apply)(*a, **k)
        cur["stats"] = st
        return st
    store.apply_reflow = _apply
    real_record = ctx._record

    def _record(outcome):
        cur["outcome"] = outcome
        return real_record(outcome)
    ctx._record = _record

    items: list[dict] = []
    halted = None
    for src, owner, cls in _select(store, limit, sources, per_mime):
        cur.clear()
        rec = {"source": src, "owner": owner, "class": cls}
        try:
            r = ctx.handle({"source": src, "ref_id": owner, "attempts": 0})
            if r is queue.DEFER:
                rec["outcome"] = "deferred"
            elif "stats" in cur:
                rec["outcome"] = "carried"
                rec["carried"] = cur["stats"].get("carried", 0)
                rec["reenrich"] = cur["stats"].get("reenrich", 0)
            else:
                rec["outcome"] = cur.get("outcome", "noop")
        except ReflowOrphanError as exc:
            rec.update(outcome="failed", error=str(exc)[:300])
            halted = str(exc)[:300]
        except Exception as exc:  # noqa: BLE001 — a dry run reports, never stops
            rec.update(outcome="failed", error=f"{type(exc).__name__}: {exc}"[:300])
        for k in ("extract_s", "embed_s", "apply_s"):
            if k in cur:
                rec[k] = round(cur[k], 3)
        items.append(rec)
        print(f"{rec['outcome']:<12} {cls:<16} {src} {owner}"
              + (f"  {rec.get('error')}" if rec.get("error") else ""), flush=True)
        if halted:
            break
    store.apply_reflow = real_apply

    by_outcome: dict[str, int] = {}
    by_class: dict[str, dict[str, int]] = {}
    for r in items:
        by_outcome[r["outcome"]] = by_outcome.get(r["outcome"], 0) + 1
        c = by_class.setdefault(r["class"], {})
        c[r["outcome"]] = c.get(r["outcome"], 0) + 1
    gc = [r for r in items if r["source"] in ("reflow:gmail", "reflow:calendar")
          and r["outcome"] not in ("deferred", "failed", "noop")]
    orphans_after = _orphans(store)
    new_orphans = {k: orphans_after.get(k, 0) - orphans_before.get(k, 0)
                   for k in orphans_after if orphans_after.get(k, 0) > orphans_before.get(k, 0)}
    return {
        "items": len(items), "limit": limit, "per_mime": per_mime,
        "sources_seeded": sorted(sources), "sources_workable": sorted(workable),
        "by_outcome": by_outcome, "by_class": by_class,
        "chunks_carried": sum(r.get("carried", 0) for r in items),
        "chunks_reenrich": sum(r.get("reenrich", 0) for r in items),
        "extract_s": _dist([r["extract_s"] for r in items if "extract_s" in r]),
        "embed_s": _dist([r["embed_s"] for r in items if "embed_s" in r]),
        "apply_reflow_s": _dist([r["apply_s"] for r in items if "apply_s" in r]),
        "gmail_calendar_source_changed": {
            "ordinary": sum(1 for r in gc if r["outcome"] == "ordinary"), "of": len(gc),
            "rate": round(sum(1 for r in gc if r["outcome"] == "ordinary") / len(gc), 3)
            if gc else None},
        "shared_drive_changed_not_run": shared_not_run,
        "pending_publishes_recorded": _pending_publish_count(store),
        "halted": halted,
        "orphans_before": orphans_before, "orphans_after": orphans_after,
        "orphans_new": new_orphans,
        **_checks(store),
        "failures": [r for r in items if r["outcome"] == "failed"][:50],
    }


def _pending_publish_count(store) -> int:
    try:
        with store._connect() as db:
            return db.execute("SELECT count(*) FROM shared_drive_pending_publish").fetchone()[0]
    except sqlite3.OperationalError:
        return 0


def verdict(summary: dict, *, strict_orphans: bool = False) -> list[str]:
    problems = []
    if summary["orphans_new"]:
        problems.append(f"new orphan references: {summary['orphans_new']}")
    if strict_orphans and any(summary["orphans_after"].values()):
        problems.append(f"orphan references: {summary['orphans_after']}")
    if summary["rebuilt_store"] and summary["foreign_key_check"]:
        problems.append(f"foreign_key_check: {summary['foreign_key_check']} violation(s)")
    if summary["integrity_check"] != "ok":
        problems.append(f"integrity_check: {summary['integrity_check']}")
    if summary["halted"]:
        problems.append(f"halted: {summary['halted']}")
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="reflow_dryrun", description=__doc__.splitlines()[0])
    ap.add_argument("--store", required=True, help="path to a store COPY (never the live one)")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--per-mime", action="store_true",
                    help="sample round-robin across MIME/source classes")
    ap.add_argument("--out", help="write the JSON summary here")
    ap.add_argument("--strict-orphans", action="store_true",
                    help="fail on ANY orphan reference, not only new ones")
    ap.add_argument("--yes", action="store_true", help="actually run (default: plan only)")
    ns = ap.parse_args(argv)

    path = Path(ns.store).expanduser()
    if _is_live(path):
        print(f"refusing: {path} is the LIVE store ({config.store_path()}). Copy it first "
              "(daemon booted out, VACUUM INTO), then point --store at the copy.",
              file=sys.stderr)
        return 2
    if not path.is_file():
        print(f"no store copy at {path}", file=sys.stderr)
        return 2
    if ns.limit < 1:
        print("--limit must be >= 1", file=sys.stderr)
        return 2
    if not ns.yes:
        print(f"dry-run plan: would reflow up to {ns.limit} candidate owner(s) of {path}"
              f"{' sampled per MIME' if ns.per_mime else ''}, with the real Google "
              f"services and embedder; never publishing, never touching the daemon; "
              f"then report orphans, foreign_key_check and integrity_check"
              f"{' to ' + ns.out if ns.out else ''}. Pass --yes to run.")
        return 0

    from mcpbrain.store import Store
    embedder = _get_embedder()
    store = Store(path, dim=embedder.dim)
    store.init()                    # the copy gets exactly the migrations a release runs
    home = str(config.app_dir())
    summary = run(store, home=home, services=_build_services() or {}, embedder=embedder,
                  limit=ns.limit, per_mime=ns.per_mime)
    summary["store"] = str(path)
    problems = verdict(summary, strict_orphans=ns.strict_orphans)
    summary["problems"] = problems
    text = json.dumps(summary, indent=2, sort_keys=True, default=str)
    if ns.out:
        Path(ns.out).write_text(text + "\n")
    print(text)
    if problems:
        print("DRY RUN FAILED: " + "; ".join(problems), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
