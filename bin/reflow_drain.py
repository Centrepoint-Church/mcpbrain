#!/usr/bin/env python3
"""Attended reflow drain on the LIVE store, with the daemon STOPPED.

  bin/reflow_drain.sh                                   # the way to run it
  python bin/reflow_drain.py                            # plan only, writes nothing
  python bin/reflow_drain.py --yes [--max-owners N] [--source reflow:drive ...]
        [--reset-transient-attempts] [--retry-gave-up]

--reset-transient-attempts / --retry-gave-up repair the damage of a network
outage worked under a classifier that counted it as permanent: run (after
every gate, inside the daemon-stopped window) before the drain loop; without
--yes they only print what they would change. See RELEASE-RUNBOOK §8.

The daemon works the reflow backlog at 10 owners / 15 s per sync cycle, seeded
200 at a time; ~7,000 owners take days that way. This drains it in one attended
sitting through the SAME code path: the daemon's seed logic (workable sources,
drop of permanently-unavailable sources' rows, reflow_candidates, enqueue_items,
REFLOW_WINDOW) and `work_queue(store, handlers={"reflow": ctx.handle})` over a
ReflowContext whose per-cycle caps are lifted. DEFER, the enrich-unit guard,
the give-up stamps, the transient-defer bound, the orphan halt and the
shared-drive pending-publish rows all behave exactly as in the daemon (pending
publishes ARE recorded, so the daemon republishes those artifacts when it
restarts). A changed source takes the source's ordinary handler, as in a sync
cycle -- except a changed SHARED Drive file, which needs that drive's fleet
storage and pin: its row is deferred (no attempt spent) for the daemon.

Why every gate below exists: 2026-09-10, two concurrent writers physically
corrupted this store (CLAUDE.md, "STORE CORRUPTION INCIDENT"). So this:
  * refuses while any daemon is alive (launchd job loaded, or a
    `mcpbrain daemon` process) and re-checks every RECHECK_EVERY owners,
    stopping cleanly (exit 4) if one appears;
  * HOLDS the daemon's single-writer lock for the whole run, so a daemon that
    starts anyway exits at its startup probe instead of writing;
  * refuses unless a backup succeeded within 24 h (--no-backup-check to
    override; discouraged), while reflow is halted, or with the kill switch off;
  * never calls store.init() (no migration of the live store from this tree).

Ctrl-C (or SIGTERM / SIGHUP) finishes the current owner -- each is its own transaction
-- prints the summary and exits 130; a second Ctrl-C aborts that owner (its
transaction rolls back). Safe to re-run: the backlog is level-triggered.

At the end: PRAGMA integrity_check (doctor._run_integrity_check) and
foreign_key_check. `reflow:integrity_checked` is NOT set here -- the daemon's
seed runs its own check when it next finds the backlog empty.

Run it with the INSTALLED tool's interpreter (the wrapper does), never
`uv run`: the imported mcpbrain must be the daemon's own package (refused
otherwise). `--check` runs the refusal gates alone (no daemon detection, writes
nothing) so the wrapper can refuse before it stops anything.

Exit: 0 done; 1 unexpected error; 2 refused / bad input; 3 halted on a
ReflowOrphanError; 4 a daemon appeared mid-run; 5 integrity_check not ok, or
foreign_key_check > 0 on a rebuilt store (the wrapper then does NOT restart
the daemon); 130 interrupted.
"""
import argparse
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

# Deliberately NO sys.path.insert of the repo: the drain must import the
# INSTALLED mcpbrain (the daemon's exact code and dependencies), so the wrapper
# runs it with the uv tool's own interpreter; _check_installed enforces it.
from mcpbrain import config

MARKER_NAME = "reflow_drain.STORE_CHECK_FAILED"
EXIT_STORE_CHECK = 5         # integrity/fk failure: the wrapper must NOT restart the daemon
SIGNAL_DEBOUNCE_S = 1.5      # a second signal this soon is the same keypress

PROGRESS_EVERY = 10          # owners between progress lines
RECHECK_EVERY = 5            # owners between daemon re-checks
_LAUNCHD_LABEL = "com.mcpbrain"
_RESUME_HELP = ("Investigate first: read the failing row's last_error\n"
                "  sqlite3 \"$STORE\" \"SELECT source, ref_id, attempts, last_error FROM "
                "sync_queue WHERE source LIKE 'reflow:%' AND last_error != ''\"\n"
                "and the log, fix the cause, then: uv run python bin/reflow.py resume --yes\n"
                "(docs/RELEASE-RUNBOOK.md §8 step 6)")


# -- seams (tests replace these) ------------------------------------------------

def _build_services() -> dict:
    from mcpbrain.auth import build_google_services
    return build_google_services()


def _get_embedder():
    from mcpbrain.embed import get_embedder
    return get_embedder()


def _daemon_alive() -> str | None:
    """A description of a live (or about-to-be-relaunched) daemon, or None.
    A LOADED launchd job counts even when not running this second: KeepAlive
    relaunches it within seconds (the 2026-09-10 mechanism)."""
    if shutil.which("launchctl"):
        r = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{_LAUNCHD_LABEL}"],
                           capture_output=True, text=True)
        if r.returncode == 0:
            m = re.search(r"^\s*state = (\S+)", r.stdout, re.M)
            return (f"launchd job {_LAUNCHD_LABEL} is loaded (state="
                    f"{m.group(1) if m else 'unknown'}); KeepAlive relaunches it -- "
                    f"`launchctl bootout gui/$(id -u)/{_LAUNCHD_LABEL}` first")
    if shutil.which("pgrep"):
        r = subprocess.run(["pgrep", "-f", "mcpbrain[ .]daemon"],
                           capture_output=True, text=True)
        pids = [p for p in r.stdout.split() if p.isdigit() and int(p) != os.getpid()]
        if pids:
            return f"mcpbrain daemon process(es) alive: pid {', '.join(pids)}"
    return None


def _check_installed() -> tuple[str | None, str]:
    """(refusal or None, version). The imported mcpbrain must be the uv tool's
    installed package, never this repo's working tree: the working tree may be
    ahead of (or behind) the daemon that owns the store, and its venv resolves
    different dependency versions (pymupdf, fastembed) than the fleet runs."""
    import mcpbrain
    version = getattr(mcpbrain, "__version__", "?")
    f = Path(mcpbrain.__file__).resolve()
    repo = Path(__file__).resolve().parents[1]
    if repo in f.parents:
        return (f"mcpbrain was imported from the working tree ({f.parent}); run this "
                "with the installed tool's interpreter (bin/reflow_drain.sh does)", version)
    tools = Path(os.environ.get("UV_TOOL_DIR") or Path.home() / ".local/share/uv/tools")
    try:
        under = tools.resolve() / "mcpbrain" in f.parents
    except OSError:
        under = False
    if not under:
        return (f"mcpbrain at {f.parent} is not the installed uv tool package under "
                f"{tools / 'mcpbrain'}", version)
    return None, version


class _SafeStream:
    """stdout/stderr that never raises: a closed terminal (EIO/EPIPE) must not
    crash the drain between an owner's writes."""

    def __init__(self, inner):
        self._inner = inner

    def write(self, s):
        try:
            return self._inner.write(s)
        except (OSError, ValueError):
            return len(s)

    def flush(self):
        try:
            self._inner.flush()
        except (OSError, ValueError):
            pass

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _make_signal_handler(clock=time.monotonic):
    """First SIGINT/SIGTERM/SIGHUP: finish the current owner, then stop. A
    second within SIGNAL_DEBOUNCE_S is the same keypress delivered twice (tty +
    a forwarding parent) and is ignored; a later one aborts the owner."""
    first = {"at": None}

    def handler(signum, frame):
        now = clock()
        if _STOP.reason == "interrupt" and first["at"] is not None:
            if now - first["at"] < SIGNAL_DEBOUNCE_S:
                return
            raise KeyboardInterrupt
        first["at"] = now
        _STOP.request("interrupt")
        print("\ninterrupt: finishing the current owner (Ctrl-C again to abort it)...",
              flush=True)
    return handler


# -- stop flag --------------------------------------------------------------------

class _Stop:
    """Why the run should stop before its next owner (None = keep going).
    Doubles as work_queue's `budget`: an expired budget breaks the loop
    before the next item and leaves every unreached row queued."""

    def __init__(self):
        self.reason = None

    def request(self, reason: str) -> None:
        if self.reason is None:
            self.reason = reason

    def reset(self) -> None:
        self.reason = None

    def expired(self) -> bool:
        return self.reason is not None


_STOP = _Stop()


class _SharedDriveChanged(Exception):
    """A changed Shared Drive file: only the daemon's cycle has its fleet
    storage and pin, so the row is deferred for it."""


class _DrainStore:
    """The live Store, with two narrowings: due_sync_items returns only the
    reflow rows this run may work (work_queue fails a row with no handler, so
    an ordinary sync row must never reach it), and apply_reflow reports its
    stats to the progress accounting."""

    def __init__(self, store, allowed: set[str], on_apply):
        self._s, self._allowed, self._on_apply = store, sorted(allowed), on_apply

    def __getattr__(self, name):
        return getattr(self._s, name)

    def due_sync_items(self, *, limit: int, now: str) -> list[dict]:
        if not self._allowed:
            return []
        ph = ",".join("?" * len(self._allowed))
        with self._s._connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT source, ref_id, version, event, modified_at, discovered_at, "
                "       attempts, next_attempt_at, last_error, transient_defers "
                f"FROM sync_queue WHERE source IN ({ph}) "
                "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
                "ORDER BY modified_at DESC LIMIT ?",
                (*self._allowed, now, limit)).fetchall()]

    def due_count(self) -> int:
        from mcpbrain.sync.queue import _utc_now_iso
        return len(self.due_sync_items(limit=10 ** 9, now=_utc_now_iso()))

    def queued_count(self) -> int:
        if not self._allowed:
            return 0
        ph = ",".join("?" * len(self._allowed))
        with self._s._connect() as db:
            return db.execute(f"SELECT count(*) FROM sync_queue WHERE source IN ({ph})",
                              self._allowed).fetchone()[0]

    def apply_reflow(self, *a, **k):
        st = self._s.apply_reflow(*a, **k)
        self._on_apply(st)
        return st


# -- the drain --------------------------------------------------------------------

def _fmt_h(hours: float | None) -> str:
    if hours is None:
        return "?"
    m = int(round(hours * 60))
    return f"{m // 60}h{m % 60:02d}m"


def drain(store, *, home, services: dict, embedder, sources_filter=None,
          max_owners: int | None = None, out=print) -> dict:
    from mcpbrain.daemon import REFLOW_REMAINING_CAP, REFLOW_WINDOW
    from mcpbrain.reflow import (REFLOW_SOURCE_SERVICES, REFLOW_SOURCES,
                                 workable_reflow_sources)
    from mcpbrain.store import ReflowOrphanError
    from mcpbrain.sync import queue
    from mcpbrain.sync.reflow_handler import ReflowContext

    t0 = time.monotonic()
    # The daemon's source sets (daemon._reflow_source_sets), then the filter.
    workable = workable_reflow_sources(home, services)
    now = ({s for s, k in REFLOW_SOURCE_SERVICES.items() if services.get(k) is not None}
           | {"reflow:anarlog"}) & workable
    allowed = set(REFLOW_SOURCES)
    if sources_filter:
        allowed &= set(sources_filter)
    seed_sources = now & allowed

    tally = {"outcomes": {}, "carried": 0, "uncovered": 0, "inherited_unenriched": 0,
             "failures": [], "worked": 0, "shared_changed_deferred": 0, "reselected": 0}
    cur: dict = {}

    def on_apply(st):
        cur["stats"] = st

    ds = _DrainStore(store, allowed, on_apply)

    # The cycle's ordinary handlers (sync.run_sync_cycle), for a changed source.
    from mcpbrain.sync.anarlog import handle_anarlog_item
    from mcpbrain.sync.calendar import handle_calendar_item
    from mcpbrain.sync.drive import flush_skip_report, handle_drive_item
    from mcpbrain.sync.gmail import handle_gmail_item
    drive_svc, gmail_svc, cal_svc = (services.get("drive_service"),
                                     services.get("gmail_service"),
                                     services.get("calendar_service"))
    anarlog_db = config.anarlog_db_path(home)
    folder_cache: dict = {}
    skip_report: dict = {}
    fetch_attachments = config.gmail_attachments(home)
    normal: dict = {}
    if drive_svc is not None:
        def _drive(it):
            if ":" in it["source"]:
                raise _SharedDriveChanged(it["source"])
            return handle_drive_item(drive_svc, ds, it, folder_cache=folder_cache,
                                     report=skip_report)
        normal["drive"] = _drive
    if gmail_svc is not None:
        normal["gmail"] = lambda it: handle_gmail_item(
            gmail_svc, ds, it, fetch_attachments=fetch_attachments)
    if cal_svc is not None:
        normal["calendar"] = lambda it: handle_calendar_item(cal_svc, ds, it)
    if anarlog_db:
        normal["anarlog"] = lambda it: handle_anarlog_item(ds, it, db_path=anarlog_db)

    ctx = ReflowContext(ds, embedder, home, drive_service=drive_svc,
                        gmail_service=gmail_svc, calendar_service=cal_svc,
                        anarlog_db=anarlog_db, max_items=10 ** 12,
                        max_seconds=float("inf"), normal_handlers=normal,
                        record_publish=True)
    real_record = ctx._record

    def _record(outcome):
        cur["outcome"] = outcome
        return real_record(outcome)
    ctx._record = _record

    handled: set[tuple[str, str]] = set()
    est = {"total": None}

    def progress(final=False):
        w = tally["worked"]
        hours = (time.monotonic() - t0) / 3600
        rate = w / hours if hours > 0 else 0.0
        total = est["total"]
        left = max(0, total - w) if total is not None else None
        eta = (left / rate) if (rate and left is not None) else None
        oc = " ".join(f"{k}={v}" for k, v in sorted(tally["outcomes"].items()))
        out(f"{'done' if final else 'progress'}: {w}/{total if total is not None else '?'} "
            f"owners  [{oc}]  {rate:.0f}/h  ETA {_fmt_h(eta) if not final else '-'}",
            flush=True)

    def handle(item):
        key = (item["source"], item["ref_id"])
        if tally["worked"] and tally["worked"] % RECHECK_EVERY == 0 \
                and cur.get("checked_at") != tally["worked"]:
            cur["checked_at"] = tally["worked"]
            alive = _daemon_alive()
            if alive:
                _STOP.request("daemon")
                cur["daemon"] = alive
        if _STOP.reason is not None or (max_owners and tally["worked"] >= max_owners):
            if max_owners and tally["worked"] >= max_owners:
                _STOP.request("max_owners")
            return queue.DEFER            # untouched: no write, row stays queued
        for k in ("outcome", "stats"):
            cur.pop(k, None)
        handled.add(key)
        outcome = None
        try:
            r = ctx.handle(item)
        except _SharedDriveChanged:
            ctx._defer_later(item)
            tally["shared_changed_deferred"] += 1
            outcome, r = "deferred", queue.DEFER
        except ReflowOrphanError as exc:
            _STOP.request("halt")
            cur["halt"] = str(exc)[:500]
            outcome = "failed"
            tally["failures"].append({"source": key[0], "ref_id": key[1],
                                      "error": str(exc)[:300]})
            raise                          # work_queue records it, as in the daemon
        except Exception as exc:
            outcome = "failed"
            tally["failures"].append({"source": key[0], "ref_id": key[1],
                                      "error": f"{type(exc).__name__}: {exc}"[:300]})
            raise
        finally:
            if outcome is None:
                if r is queue.DEFER:
                    outcome = "deferred"
                elif "stats" in cur:
                    outcome = "carried"
                    for k in ("carried", "uncovered", "inherited_unenriched"):
                        tally[k] += int(cur["stats"].get(k, 0) or 0)
                else:
                    outcome = cur.get("outcome", "noop")
            tally["outcomes"][outcome] = tally["outcomes"].get(outcome, 0) + 1
            tally["worked"] += 1
            if tally["worked"] % PROGRESS_EVERY == 0:
                progress()
        return r

    gone = set(REFLOW_SOURCES) - workable
    dropped = store.drop_queued_reflow_rows(gone) if gone else 0
    if dropped:
        out(f"freed {dropped} queued row(s) of permanently unavailable source(s) "
            f"{sorted(gone)}", flush=True)

    while _STOP.reason is None:
        if max_owners and tally["worked"] >= max_owners:
            break
        # -- seed (daemon._reflow_seed_once) --
        room = REFLOW_WINDOW - store.reflow_due_count()
        n = 0
        if room > 0 and seed_sources:
            by_src: dict[str, list[dict]] = {}
            for src, owner in store.reflow_candidates(room + len(handled),
                                                      sources=seed_sources):
                if (src, owner) in handled:
                    continue                       # worked this run already
                if sum(len(v) for v in by_src.values()) >= room:
                    break
                by_src.setdefault(src, []).append(
                    {"ref_id": owner, "event": "reflow", "modified_at": "1970-01-01T00:00:00"})
            n = sum(store.enqueue_items(items, source=src) for src, items in by_src.items())
            if n:
                # As the daemon's seed does: new work means the backlog's end
                # must be integrity-checked again (by the daemon, later).
                store.set_cursor("reflow:integrity_checked", "")
        due = ds.due_count()
        remaining = len(store.reflow_candidates(REFLOW_REMAINING_CAP, sources=seed_sources)) \
            if seed_sources else 0
        est["total"] = tally["worked"] + due + remaining
        if max_owners:
            est["total"] = min(est["total"], max_owners)
        if due == 0:
            break                          # done, or only deferred rows left
        limit = due if not max_owners else max(0, min(due, max_owners - tally["worked"]))
        before = tally["worked"]
        queue.work_queue(ds, handlers={"reflow": handle}, limit=limit, budget=_STOP)
        if tally["worked"] == before:
            break                          # no progress possible this pass

    if skip_report:
        flush_skip_report(store, skip_report, source="drive")
    if seed_sources:
        tally["reselected"] = sum(1 for k in store.reflow_candidates(
            REFLOW_REMAINING_CAP, sources=seed_sources) if k in handled)
    progress(final=True)
    return {
        "elapsed_s": round(time.monotonic() - t0, 1),
        "owners_worked": tally["worked"],
        "outcomes": tally["outcomes"],
        "chunks_carried": tally["carried"], "chunks_uncovered": tally["uncovered"],
        "chunks_inherited_unenriched": tally["inherited_unenriched"],
        "deferred_left": ds.queued_count() - ds.due_count(),
        "queued_left": ds.queued_count(),
        "shared_changed_deferred": tally["shared_changed_deferred"],
        "reselected_after_done": tally["reselected"],
        "failures": tally["failures"],
        "sources_seeded": sorted(seed_sources),
        "stop": _STOP.reason, "halt": cur.get("halt"), "daemon": cur.get("daemon"),
    }


# -- checks / reporting -------------------------------------------------------------

def _checks(store, home) -> dict:
    from mcpbrain.doctor import _run_integrity_check
    problems = _run_integrity_check(home)
    with store._connect() as db:
        fk = len(db.execute("PRAGMA foreign_key_check").fetchall())
        rebuilt = bool(db.execute("PRAGMA foreign_key_list(entity_relations)").fetchall())
    return {"integrity_check": "ok" if not problems else problems[:20],
            "foreign_key_check": fk, "rebuilt_store": rebuilt}


def _print_summary(s: dict, checks: dict | None) -> None:
    print("\n== reflow drain summary ==")
    print(f"elapsed:            {s['elapsed_s']} s")
    print(f"owners worked:      {s['owners_worked']}")
    print(f"outcomes:           {s['outcomes']}")
    print(f"chunks:             carried={s['chunks_carried']} "
          f"uncovered={s['chunks_uncovered']} "
          f"inherited_unenriched={s['chunks_inherited_unenriched']}")
    print(f"deferred left:      {s['deferred_left']} (queued left: {s['queued_left']}; "
          f"changed shared-drive files left for the daemon: {s['shared_changed_deferred']})")
    if s["reselected_after_done"]:
        print(f"reselected:         {s['reselected_after_done']} owner(s) completed but "
              "still match the selector (not re-queued this run)")
    for line in _recovery_lines(s.get("recovery") or {}, apply=True):
        print(line)
    print(f"failures:           {len(s['failures'])}")
    for f in s["failures"][:20]:
        print(f"  {f['source']} {f['ref_id']}: {f['error']}")
    if checks is not None:
        print(f"integrity_check: {checks['integrity_check']}")
        fk = checks["foreign_key_check"]
        print(f"foreign_key_check: {fk}"
              + ("" if checks["rebuilt_store"] else " (store not rebuilt: FKs not declared)")
              + (" -- WARNING: investigate" if fk and checks["rebuilt_store"] else ""))


def _backup_age(home) -> float | None:
    from mcpbrain.probes import last_backup_success
    last = last_backup_success(home)
    return None if last is None else time.time() - last


def _gates(store, home, ns) -> str | None:
    """The refusal gates shared by --check and --yes (daemon detection aside):
    None to proceed, else the refusal message."""
    from mcpbrain.daemon import REFLOW_BACKUP_MAX_AGE_S
    from mcpbrain.store import REFLOW_HALT_CURSOR
    halted = store.get_cursor(REFLOW_HALT_CURSOR)
    if halted:
        return f"refusing: reflow is halted: {halted}\n{_RESUME_HELP}"
    if not config.reflow_enabled(home):
        return "refusing: reflow is disabled (reflow_enabled kill switch)."
    if not ns.no_backup_check:
        age = _backup_age(home)
        if age is None or age > REFLOW_BACKUP_MAX_AGE_S:
            return ("refusing: no backup succeeded in the last 24 h "
                    f"({'never' if age is None else f'{age / 3600:.1f} h ago'}). Let the "
                    "daemon back up first (mcpbrain doctor shows it).")
    unsupported = _recovery_unsupported(ns)
    if unsupported:
        return unsupported
    return None


def _recovering(ns) -> bool:
    return bool(ns.reset_transient_attempts or ns.retry_gave_up)


def _recovery_unsupported(ns) -> str | None:
    """The recovery flags need the installed package's network classifier and
    store methods (shipped together with the _is_transient fix): resetting
    attempts under the OLD classifier would just spend them on the next outage."""
    if not _recovering(ns):
        return None
    from mcpbrain.store import Store
    from mcpbrain.sync import reflow_handler
    if not (hasattr(reflow_handler, "_is_transient_message")
            and hasattr(Store, "reset_reflow_transient_attempts")
            and hasattr(Store, "retry_reflow_gave_up")):
        return ("refusing: --reset-transient-attempts / --retry-gave-up need an installed "
                "mcpbrain that classifies network failures as transient (upgrade first).")
    return None


def _recover(store, ns, *, apply: bool) -> dict:
    """--reset-transient-attempts / --retry-gave-up. Runs only after every
    gate, and with --yes only inside the daemon-stopped window (main's daemon
    detection + single-writer lock come first)."""
    from mcpbrain.sync.reflow_handler import _is_transient_message
    r: dict = {}
    if ns.reset_transient_attempts:
        r["reset"] = store.reset_reflow_transient_attempts(
            _is_transient_message, sources=ns.sources, apply=apply)
    if ns.retry_gave_up:
        r["retry"] = store.retry_reflow_gave_up(sources=ns.sources, apply=apply)
    return r


def _recovery_lines(r: dict, *, apply: bool) -> list[str]:
    w = "" if apply else "would "
    out = []
    if "reset" in r:
        out.append(f"recovery: {w}reset {r['reset']} network-failed queued reflow row(s) "
                   "(attempts, transient_defers, backoff, last_error cleared)")
    if "retry" in r:
        out.append(f"recovery: {w}retry {r['retry']['owners']} gave-up owner(s) "
                   f"({r['retry']['chunks']} chunk(s); reflow_skipped/split_version/"
                   "extraction_version removed)")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="reflow_drain", description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--yes", action="store_true", help="actually run (default: plan only)")
    mode.add_argument("--check", action="store_true",
                      help="run the refusal gates only (no daemon detection); write "
                           "nothing; exit 0 = would proceed, 2 = refused")
    ap.add_argument("--max-owners", type=int, default=None)
    ap.add_argument("--source", action="append", dest="sources", default=None,
                    help="restrict to this reflow source, e.g. reflow:drive (repeatable)")
    ap.add_argument("--no-backup-check", action="store_true",
                    help="skip the 24 h backup gate (discouraged)")
    ap.add_argument("--reset-transient-attempts", action="store_true",
                    help="before draining: clear attempts/backoff of queued reflow rows "
                         "whose last_error is a network failure (DNS, reset, timeout)")
    ap.add_argument("--retry-gave-up", action="store_true",
                    help="before draining: un-stamp reflow_skipped=gave_up owners so the "
                         "selector matches them again (other stamps untouched)")
    ns = ap.parse_args(argv)
    _STOP.reset()

    from mcpbrain.daemon import REFLOW_BACKUP_MAX_AGE_S, REFLOW_REMAINING_CAP
    from mcpbrain.reflow import REFLOW_SOURCES
    from mcpbrain.store import REFLOW_HALT_CURSOR, Store

    if ns.max_owners is not None and ns.max_owners < 1:
        print("--max-owners must be >= 1", file=sys.stderr)
        return 2
    bad = sorted(set(ns.sources or ()) - set(REFLOW_SOURCES))
    if bad:
        print(f"unknown --source {bad}; choose from {list(REFLOW_SOURCES)}", file=sys.stderr)
        return 2
    err, version = _check_installed()
    if err and (ns.yes or ns.check):
        print(f"refusing: {err}", file=sys.stderr)
        return 2
    home = str(config.app_dir())
    path = config.store_path()
    if not path.is_file():
        print(f"no store at {path}", file=sys.stderr)
        return 2
    if (ns.yes or ns.check) and _marker_path(home).exists():
        print(f"refusing: {_marker_path(home)} exists -- an earlier run's store check "
              f"FAILED:\n{_marker_path(home).read_text(errors='replace').strip()}\n"
              "The store is suspect: investigate (integrity_check, the snapshot), then "
              "delete the marker.", file=sys.stderr)
        return 2
    dim = Store.stored_dim(path)
    if dim is None:
        if ns.yes:     # --check passed a moment ago: unreadable now means damaged
            return _store_check_failed(home, f"cannot read the store's meta at {path}")
        print(f"{path} has no recorded embedding dim (never initialised by the daemon?)",
              file=sys.stderr)
        return 2

    if ns.check:
        refusal = _gates(Store(path, dim=dim, read_only=True), home, ns)
        if refusal:
            print(refusal, file=sys.stderr)
            return 2
        print(f"check ok: mcpbrain {version}; gates pass"
              f"{' (backup check SKIPPED)' if ns.no_backup_check else ''}.")
        return 0

    if not ns.yes:
        store = Store(path, dim=dim, read_only=True)
        halted = store.get_cursor(REFLOW_HALT_CURSOR) or None
        age = _backup_age(home)
        st = store.reflow_stats()
        left = len(store.reflow_candidates(REFLOW_REMAINING_CAP, sources=ns.sources))
        alive = _daemon_alive()
        if _recovering(ns):
            unsupported = _recovery_unsupported(ns)
            for line in ([unsupported] if unsupported
                         else _recovery_lines(_recover(store, ns, apply=False), apply=False)):
                print(line)
        print(f"plan: drain the reflow backlog of {path} (live store) through the "
              f"daemon's own seed + work_queue path, caps lifted"
              f"{f', at most {ns.max_owners} owner(s)' if ns.max_owners else ''}"
              f"{f', sources {ns.sources}' if ns.sources else ''}.\n"
              f"  code: {err or f'installed mcpbrain {version}'}\n"
              f"  queued reflow rows: {st['queued']}; unqueued candidates: "
              f"{left}{'+' if left >= REFLOW_REMAINING_CAP else ''}; "
              f"owners done so far: {st['owners_done']}\n"
              f"  halted: {halted or 'no'}\n"
              f"  last backup: {f'{age / 3600:.1f} h ago' if age is not None else 'none'}"
              f" (gate: {REFLOW_BACKUP_MAX_AGE_S / 3600:.0f} h)\n"
              f"  daemon: {alive or 'not detected'}\n"
              "Run it via bin/reflow_drain.sh (boots the daemon out, snapshots, "
              "and always bootstraps it back). Nothing was written.")
        return 0

    alive = _daemon_alive()
    if alive:
        print(f"refusing: a daemon is alive -- {alive}. Two writers corrupted this "
              "store on 2026-09-10; use bin/reflow_drain.sh.", file=sys.stderr)
        return 2
    from mcpbrain.daemon import AlreadyRunningError, SingleWriterLock
    lock = SingleWriterLock()
    try:
        lock.acquire()
    except AlreadyRunningError as exc:
        print(f"refusing: the single-writer lock is held ({exc}) -- a daemon or another "
              "drain is running.", file=sys.stderr)
        return 2
    prev: dict = {}
    streams = (sys.stdout, sys.stderr)

    def restore_signals():
        for sig, h in prev.items():
            signal.signal(sig, h)
        prev.clear()

    try:
        # A closed terminal must not crash the run mid-owner: output becomes
        # best-effort, and SIGHUP stops cleanly like SIGINT/SIGTERM.
        sys.stdout, sys.stderr = _SafeStream(sys.stdout), _SafeStream(sys.stderr)
        print(f"mcpbrain {version} (installed package)", flush=True)
        try:
            return _run_yes(ns, store_path=path, dim=dim, home=home, prev=prev,
                            restore_signals=restore_signals)
        except sqlite3.DatabaseError as exc:
            # "database disk image is malformed" is exactly the 2026-09-10
            # symptom: never an ordinary error the wrapper restarts through.
            return _store_check_failed(home, f"{type(exc).__name__}: {exc}")
    finally:
        restore_signals()
        sys.stdout, sys.stderr = (_live_or_devnull(streams[0]),
                                  _live_or_devnull(streams[1]))
        lock.release()


def _run_yes(ns, *, store_path, dim, home, prev, restore_signals) -> int:
    from mcpbrain.store import Store
    store = Store(store_path, dim=dim)   # writable; NO init(): never migrate from here
    refusal = _gates(store, home, ns)
    if refusal:
        print(refusal, file=sys.stderr)
        return 2
    if ns.no_backup_check:
        print("WARNING: --no-backup-check: running with no verified recent backup; "
              "the snapshot the wrapper takes is the only recovery point.", flush=True)
    embedder = _get_embedder()
    if getattr(embedder, "dim", dim) != dim:
        print(f"refusing: embedder dim {embedder.dim} != store dim {dim}", file=sys.stderr)
        return 2
    services = _build_services() or {}
    recovery = _recover(store, ns, apply=True)
    for line in _recovery_lines(recovery, apply=True):
        print(line, flush=True)

    handler = _make_signal_handler()
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            prev[sig] = signal.signal(sig, handler)
        except ValueError:                # not the main thread (never in the CLI)
            pass
    summary = None
    try:
        summary = drain(store, home=home, services=services, embedder=embedder,
                        sources_filter=ns.sources, max_owners=ns.max_owners)
    except KeyboardInterrupt:
        _STOP.request("interrupt")
        print("aborted: the in-flight owner's transaction rolled back.", flush=True)
    finally:
        restore_signals()                 # the checks below run with default handlers
    checks = _checks(store, home)
    if summary is not None:
        summary["recovery"] = recovery
        _print_summary(summary, checks)
    else:
        print(f"integrity_check: {checks['integrity_check']}; "
              f"foreign_key_check: {checks['foreign_key_check']}")
    reason = _STOP.reason
    if checks["integrity_check"] != "ok":
        return _store_check_failed(home, f"integrity_check: {checks['integrity_check']}")
    if checks["rebuilt_store"] and checks["foreign_key_check"]:
        return _store_check_failed(
            home, f"foreign_key_check: {checks['foreign_key_check']} violation(s)")
    if reason == "halt":
        print(f"\nREFLOW HALTED: {(summary or {}).get('halt')}\n{_RESUME_HELP}",
              file=sys.stderr)
        return 3
    if reason == "daemon":
        print(f"\nSTOPPED: a daemon appeared mid-run -- {(summary or {}).get('daemon')}",
              file=sys.stderr)
        return 4
    if reason == "interrupt":
        return 130
    return 0


def _marker_path(home) -> Path:
    return Path(home) / MARKER_NAME


def _store_check_failed(home, reason: str) -> int:
    """Record a failed store check durably BEFORE returning: the wrapper
    reads this marker (not only the exit code, which a dead terminal can
    rewrite at interpreter shutdown) and then does NOT restart the daemon."""
    import datetime as _dt
    stamp = _dt.datetime.now(_dt.timezone.utc).isoformat()
    try:
        _marker_path(home).write_text(f"{stamp}\n{reason}\n")
    except OSError as exc:
        print(f"!! could not write {_marker_path(home)}: {exc}", file=sys.stderr)
    print(f"\nSTORE CHECK FAILED ({reason}) -- do NOT restart the daemon on this store "
          "before investigating (CLAUDE.md, 2026-09-10 incident rules); the wrapper's "
          f"snapshot is the recovery point. Marker: {_marker_path(home)} (delete it "
          "after investigating).", file=sys.stderr)
    return EXIT_STORE_CHECK


def _live_or_devnull(stream):
    """The original stream if it still flushes, else /dev/null: restoring a
    dead terminal would make CPython's shutdown flush fail and rewrite the
    exit status (to 120)."""
    try:
        stream.flush()
        return stream
    except (OSError, ValueError):
        return open(os.devnull, "w")


def _run_cli(argv=None) -> None:
    """The CLI entry: main(), a guarded flush, then os._exit so nothing at
    interpreter shutdown can change the exit status the wrapper acts on."""
    rc = main(argv)
    for st in (sys.stdout, sys.stderr):
        try:
            st.flush()
        except (OSError, ValueError):
            pass
    os._exit(rc)


if __name__ == "__main__":
    _run_cli()
