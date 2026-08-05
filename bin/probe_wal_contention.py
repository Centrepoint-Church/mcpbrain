#!/usr/bin/env python3
"""Why does `wal_checkpoint(TRUNCATE)` return busy=1 and abort real backups?

THE QUESTION
------------
Backups aborted for real -- 97 failures vs 52 successes between 2026-06-25 and
2026-08-04, and `backup_state.json` on this machine still carries
`"last_error": "wal_checkpoint(TRUNCATE) busy=1"` -- at `backup.snapshot()`'s
very first statement:

    row = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    busy = row[0]; if busy != 0: raise RuntimeError(...)

The 0.7.113 investigation looked for a second WRITER and found none: `lsof`
showed one holder and there was no `-wal` file. But that was measured with the
system IDLE. The MCP server holds a **writable** `draft_store` handle for the
whole life of a client connection and Claude Desktop keeps two servers open, so
"a concurrently-used MCP server blocks the checkpoint" was never tested under
load. Phase 4 removes those writable handles, which would make the question
permanently unanswerable -- so it gets answered now.

WHAT `busy` ACTUALLY MEANS (this drives the whole design)
---------------------------------------------------------
`SQLITE_CHECKPOINT_TRUNCATE` needs more than "no other writer". Per SQLite, it
blocks (invoking the busy handler) until there is no writer **and every reader
is reading the most recent snapshot**, then resets the WAL to zero length. Two
independent causes, then:

  (W) another connection holds the write lock, or
  (R) another connection has an open READ transaction on an OLDER snapshot.

And one precondition that is easy to miss: if the `-wal` is **empty**, TRUNCATE
has nothing to wait for and returns busy=0 no matter how much concurrency is
running. A first version of this probe fired checkpoints into two MCP sessions
issuing ~330 writes/second and got busy=0 three times out of three -- because
every one of those checkpoints reported `log_frames=0`. That was not a
refutation, it was a **null instrument**. Hence:

ARMS
----
`mechanism` -- a POSITIVE CONTROL on a throwaway scratch database. Reproduces
    (W) and (R) deterministically, and then clears each and re-checkpoints. If
    this arm cannot produce busy=1, the instrument is broken and every busy=0
    below is meaningless. Runs on scratch precisely so that proving (W)
    requires holding a write lock -- which on the live store would block the
    daemon's own writer.

`idle` -- the 0.7.113 baseline, for comparison.

`mcp_writes` -- the literal hypothesis: two of this script's own MCP sessions,
    each looping a genuinely Store-writing tool, checkpoints fired into it.

`mcp_reads` -- two sessions looping `brain_graph`. The Task 6 baseline measured
    it at a 6.3 s median / 8.3 s p95 on this store: a read transaction that
    outlives the 5000 ms busy_timeout `_open_db` sets, i.e. cause (R) occurring
    naturally in ordinary use.

`mcp_mixed` -- one session writing while the other runs those multi-second
    `brain_graph` reads. This is the realistic Claude Desktop shape: two
    connected servers, one of them mid-recall while the other saves something.

`pinned_reader` -- (R) demonstrated on the LIVE store, safely: a **read-only**
    connection holds one open read transaction while an MCP session appends WAL
    frames behind it. A read-only connection cannot write, cannot checkpoint,
    and in WAL mode does not block writers, so this is safe to run against the
    real 11.9 GB store while the daemon and the user's Desktop servers are up.

THE THIRD CONNECTION
--------------------
Checkpoints are issued through `mcpbrain.store._open_db(path, read_only=False)`
and the identical PRAGMA -- `backup.snapshot()`'s own code path, same 5000 ms
busy_timeout, same `journal_size_limit`, same sqlite-vec load. Going via the
daemon/ControlClient would be wrong here: the daemon runs its backup under the
BULK lock, which suppresses part of the very contention being probed, and it
exposes no checkpoint endpoint. Nothing is copied and nothing is deleted -- a
TRUNCATE checkpoint is routine maintenance the daemon performs anyway.

SAFETY
------
* The loaded sessions are this script's OWN subprocesses. It never touches the
  `mcpbrain mcp-server` processes Claude Desktop has connected -- those belong
  to the user's live app.
* No write lock is ever held on the live store; that is what the scratch arm is
  for.
* Every row written is tagged `sdd-probe-wal-`, so it is unmistakably synthetic.

USAGE
-----
    .venv/bin/python bin/probe_wal_contention.py --out /tmp/wal.json
    .venv/bin/python bin/probe_wal_contention.py --arms mechanism pinned_reader
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SYNTH = "sdd-probe-wal"
ARMS = ["mechanism", "idle", "mcp_writes", "mcp_reads", "mcp_mixed",
        "pinned_reader"]


# --------------------------------------------------------------------------- #
# observations
# --------------------------------------------------------------------------- #

def lsof_holders(store_path: str) -> dict:
    """Which processes hold the store file open, per `lsof`.

    The same instrument the 0.7.113 investigation used ("lsof showed one
    holder"), so the comparison is apples to apples. Returns who, not just how
    many -- a count is not attributable.

    A count of 0 is a real and informative answer here, not a failure: every
    `Store` operation opens its own connection and closes it (`Store._connect`
    -> `_open_db` per call), so between operations the file genuinely has no
    holders at all and SQLite deletes the `-wal`/`-shm` sidecars.
    """
    try:
        out = subprocess.run(["lsof", "--", store_path], capture_output=True,
                             text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return {"error": str(exc)}
    lines = [ln for ln in out.splitlines()[1:] if ln.strip()]
    holders: dict[str, str] = {}
    for ln in lines:
        parts = ln.split()
        if len(parts) >= 2:
            holders[parts[1]] = parts[0]  # pid -> command
    return {"pids": holders, "holder_count": len(holders)}


def sidecars(store_path: str) -> dict:
    out = {}
    for suffix in ("-wal", "-shm"):
        p = Path(store_path + suffix)
        out[suffix] = ({"exists": True, "bytes": p.stat().st_size}
                       if p.exists() else {"exists": False})
    return out


def checkpoint_truncate(store_path: str) -> dict:
    """Run backup.snapshot()'s exact first statement.

    `log_frames` is as important as `busy`: log_frames == 0 means the WAL was
    empty and busy=0 proves nothing.
    """
    from mcpbrain.store import _open_db

    t0 = time.perf_counter()
    db = _open_db(store_path, read_only=False)
    try:
        row = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    except Exception as exc:  # noqa: BLE001 -- a locked DB must be data, not a crash
        return {"error": f"{type(exc).__name__}: {exc}",
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1)}
    finally:
        db.close()
    busy, log, ckpt = (row[0], row[1], row[2]) if row else (None, None, None)
    return {"busy": busy, "log_frames": log, "checkpointed_frames": ckpt,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1)}


# --------------------------------------------------------------------------- #
# arm: mechanism (positive control, throwaway database)
# --------------------------------------------------------------------------- #

def run_mechanism() -> dict:
    """Prove (W) and (R) cause busy=1, and that clearing each restores busy=0.

    Deliberately on a scratch DB: establishing (W) means holding an open write
    transaction, which on the live store would block the daemon's writer for the
    duration of the busy handler. The locking rules being demonstrated are
    properties of SQLite's WAL implementation, not of corpus size, so scratch is
    the right place to demonstrate them -- and the live arms below then show
    whether real usage actually creates these conditions.

    busy_timeout is 500 ms here rather than the real 5000 ms purely so a
    positive control does not cost 20 s; the outcome does not depend on it (a
    blocked TRUNCATE stays blocked for as long as the blocker holds).
    """
    steps = []
    with tempfile.TemporaryDirectory(prefix=f"{SYNTH}-mech-") as tmp:
        db_path = str(Path(tmp) / "probe.sqlite3")

        def ckpt(label: str) -> dict:
            c = sqlite3.connect(db_path, timeout=0.5)
            try:
                row = c.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                r = {"step": label, "busy": row[0], "log_frames": row[1],
                     "checkpointed_frames": row[2]}
            except sqlite3.OperationalError as exc:
                r = {"step": label, "error": str(exc)}
            finally:
                c.close()
            steps.append(r)
            print(f"    {label:38s} -> {json.dumps({k: v for k, v in r.items() if k != 'step'})}",
                  flush=True)
            return r

        w = sqlite3.connect(db_path, timeout=0.5)
        w.execute("PRAGMA journal_mode=WAL")
        w.execute("CREATE TABLE t (x TEXT)")
        w.commit()

        # --- (R): a reader pinned to an older snapshot ---------------------- #
        r = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.5)
        r.execute("BEGIN")
        r.execute("SELECT count(*) FROM t").fetchone()   # materialise the snapshot
        for i in range(50):                               # frames the reader cannot see
            w.execute("INSERT INTO t VALUES (?)", (f"{SYNTH}-{i}",))
        w.commit()
        ckpt("(R) reader pinned to old snapshot")
        r.rollback()
        r.close()
        ckpt("(R) cleared: reader closed")

        # --- (W): another connection holds the write lock ------------------- #
        for i in range(50):
            w.execute("INSERT INTO t VALUES (?)", (f"{SYNTH}-b-{i}",))
        w.commit()
        w2 = sqlite3.connect(db_path, timeout=0.5)
        w2.execute("BEGIN IMMEDIATE")
        w2.execute("INSERT INTO t VALUES ('holding the write lock')")
        ckpt("(W) write lock held, uncommitted")
        w2.rollback()
        w2.close()
        ckpt("(W) cleared: write lock released")
        w.close()

    busy_seen = [s for s in steps if s.get("busy")]
    return {"arm": "mechanism", "steps": steps,
            "instrument_can_detect_busy": bool(busy_seen)}


# --------------------------------------------------------------------------- #
# arms on the live store
# --------------------------------------------------------------------------- #

def _server_params(home: str):
    from mcp.client.stdio import StdioServerParameters
    env = dict(os.environ)
    env["MCPBRAIN_HOME"] = home
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
    return StdioServerParameters(
        command=sys.executable, args=["-m", "mcpbrain.mcp_server"], env=env)


WRITE_LOADS = [
    # Two DIFFERENT writing tools, one per session, so both writable
    # draft_store handles are genuinely issuing INSERT/UPDATE at once.
    ("brain_meeting_pack_upsert", lambda i: {
        "event_id": f"{SYNTH}-meeting-{i % 3}",
        "event_title": f"{SYNTH} synthetic pack",
        "event_date": "2026-08-05",
        "pack_text": f"# {SYNTH}\n\n" + ("synthetic wal-contention probe row. " * 40),
        "attendees": [f"{SYNTH}-attendee"],
        "context_hash": f"{SYNTH}-{i}",
    }),
    ("brain_draft_save", lambda i: {
        "email_id": f"{SYNTH}-email",
        "thread_id": f"{SYNTH}-thread",
        "intent": f"{SYNTH} probe {i}",
        "final_draft": f"{SYNTH}: synthetic wal-contention probe draft {i}.",
    }),
]

READ_LOADS = [
    ("brain_graph", lambda i: {"entity": "Josh Kemp", "hops": 1}),
    ("brain_graph", lambda i: {"entity": "Josh Kemp", "hops": 2}),
]


async def _load_session(home: str, tool: str, args_for, stop: asyncio.Event,
                        log: list) -> None:
    """Hammer one tool through one real MCP session until `stop` is set.

    A real session over a real stdio subprocess, because the point is that the
    handle lives in a separate PROCESS holding the same sqlite file -- an
    in-process Store would not reproduce that.
    """
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    async with stdio_client(_server_params(home)) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=300.0) as session:
            await session.initialize()
            i = 0
            while not stop.is_set():
                try:
                    await session.call_tool(tool, args_for(i))
                except Exception as exc:  # noqa: BLE001
                    log.append(f"error: {type(exc).__name__}: {exc}")
                else:
                    log.append("ok")
                i += 1


async def _run_live_arm(home: str, store_path: str, arm: str, checkpoints: int,
                        settle_s: float, gap_s: float) -> dict:
    loads: list = []
    pin_reader = False
    if arm == "idle":
        pass
    elif arm == "mcp_writes":
        loads = list(WRITE_LOADS)
    elif arm == "mcp_reads":
        loads = list(READ_LOADS)
    elif arm == "mcp_mixed":
        # One writer supplying WAL frames + one multi-second brain_graph read
        # holding a snapshot: both causes present at once, which is the shape
        # two connected Desktop servers actually produce.
        loads = [WRITE_LOADS[0], READ_LOADS[1]]
    elif arm == "pinned_reader":
        # One writer supplying WAL frames, plus a read-only connection holding a
        # snapshot from BEFORE those frames -- (R), on the live store, safely.
        loads = [WRITE_LOADS[0]]
        pin_reader = True
    else:
        raise ValueError(arm)

    from mcpbrain.store import _open_db

    stop = asyncio.Event()
    logs: list[list] = [[] for _ in loads]
    tasks = [asyncio.create_task(_load_session(home, tool, fn, stop, logs[i]))
             for i, (tool, fn) in enumerate(loads)]

    result: dict = {"arm": arm, "load": [t for t, _ in loads],
                    "pinned_read_txn": pin_reader}
    pinned = None
    try:
        if loads:
            # Let both subprocesses spawn, initialize and get calls in flight --
            # a checkpoint fired before the load starts measures the idle case
            # under a different name.
            await asyncio.sleep(settle_s)

        if pin_reader:
            pinned = _open_db(store_path, read_only=True)
            pinned.execute("BEGIN")
            # Any SELECT inside the transaction materialises the snapshot;
            # count(*) on a tiny table is column-schema-agnostic and cheap.
            pinned.execute("SELECT count(*) FROM meta").fetchone()
            # Frames appended AFTER this point are invisible to `pinned`, which
            # is exactly the condition TRUNCATE refuses to reset the WAL under.
            await asyncio.sleep(settle_s)

        result["lsof_during_load"] = lsof_holders(store_path)
        result["sidecars_during_load"] = sidecars(store_path)

        attempts = []
        for k in range(checkpoints):
            # to_thread: the PRAGMA is blocking C and must not stall the event
            # loop driving the load -- otherwise the "concurrent" load would
            # pause for exactly the window being measured.
            attempts.append(await asyncio.to_thread(checkpoint_truncate, store_path))
            print(f"    checkpoint {k + 1}/{checkpoints}: {attempts[-1]}", flush=True)
            if k + 1 < checkpoints:
                await asyncio.sleep(gap_s)
        result["checkpoints"] = attempts
        result["calls_completed"] = [len(c) for c in logs]
        result["call_errors"] = [e for c in logs for e in c
                                 if e.startswith("error:")][:5]
    finally:
        if pinned is not None:
            pinned.rollback()
            pinned.close()
        stop.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    result["sidecars_after"] = sidecars(store_path)
    cps = result.get("checkpoints", [])
    result["any_busy"] = any(a.get("busy") for a in cps)
    result["max_log_frames"] = max((a.get("log_frames") or 0) for a in cps) if cps else None
    return result


# --------------------------------------------------------------------------- #

def main() -> int:
    from mcpbrain import __version__, config

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--arms", nargs="*", default=ARMS, choices=ARMS)
    ap.add_argument("--checkpoints", type=int, default=6,
                    help="checkpoint attempts per live arm (default 6)")
    ap.add_argument("--settle", type=float, default=3.0,
                    help="seconds to let the load get in flight (default 3)")
    ap.add_argument("--gap", type=float, default=0.3,
                    help="seconds between checkpoint attempts (default 0.3)")
    ap.add_argument("--home", default=None, help="MCPBRAIN_HOME (default: live app dir)")
    ap.add_argument("--out", default=None, help="optional JSON report path")
    ns = ap.parse_args()

    home = ns.home or str(config.app_dir())
    store_path = str(Path(home) / "brain.sqlite3")

    print(f"mcpbrain {__version__}  store={store_path}")
    print(f"  size={Path(store_path).stat().st_size / 1e9:.2f} GB")
    print(f"  before: holders={lsof_holders(store_path).get('holder_count')} "
          f"sidecars={json.dumps(sidecars(store_path))}")

    report = {"_meta": {"version": __version__, "store_path": store_path,
                        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")},
              "before": {"lsof": lsof_holders(store_path),
                         "sidecars": sidecars(store_path)},
              "arms": {}}

    for arm in ns.arms:
        print(f"\n== arm: {arm} ==", flush=True)
        if arm == "mechanism":
            report["arms"][arm] = run_mechanism()
            continue
        r = asyncio.run(_run_live_arm(home, store_path, arm, ns.checkpoints,
                                      ns.settle, ns.gap))
        report["arms"][arm] = r
        print(f"    holders during load: {r['lsof_during_load'].get('holder_count')}"
              f"  -wal: {json.dumps(r['sidecars_during_load']['-wal'])}")
        print(f"    calls completed: {r.get('calls_completed')}"
              f"  max_log_frames={r.get('max_log_frames')}"
              f"  any_busy={r['any_busy']}", flush=True)

    print("\n== verdict ==")
    mech = report["arms"].get("mechanism")
    if mech:
        print(f"  instrument can detect busy: {mech['instrument_can_detect_busy']}")
    for arm, r in report["arms"].items():
        if arm == "mechanism":
            continue
        cps = r.get("checkpoints", [])
        print(f"  {arm:14s} busy={[a.get('busy') for a in cps]}  "
              f"log_frames={[a.get('log_frames') for a in cps]}  "
              f"elapsed_ms={[a.get('elapsed_ms') for a in cps]}")

    if ns.out:
        Path(ns.out).write_text(json.dumps(report, indent=2))
        print(f"\nwrote {ns.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
