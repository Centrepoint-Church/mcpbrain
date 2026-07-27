# Daemon scheduling — design (2026-07-27)

Decouple periodic maintenance from bulk sync, bound every cycle phase, and make
a stalled daemon self-heal instead of failing silently.

Spec 1 of 3. Companions:
`2026-07-27-ingestion-defects-findings.md` (input to specs 2 and 3).

## Problem

`_run_periodic_passes()` is called sequentially after `run_one()` on the single
loop thread (`daemon.py:2319-2322`). `run_one()` has unbounded duration, so
until it returns, **no maintenance pass runs at all**.

Verified on the live store:

- `daemon_heartbeat.json` — written on the line immediately *after* the passes —
  was stale for 35.9 hours across three daemon restarts.
- Every one of the ~20 cadence passes last logged 2026-07-23 19:12. None since.
- Sync kept logging throughout, so the loop thread was alive; the passes
  specifically were starved.

Four distinct victims, all confirmed:

1. **Maintenance starved** — four days with no graph hygiene, dedup, scoring or
   decay.
2. **Enrichment producer starved** — `prepare.prepare_units` lives inside
   `run_cycle`, so no work units were created for ~36 hours. The queue held 5
   units from 25 Jul 23:41, drainers consumed them, and the dashboard then
   reported "queue clear" while **64,340 chunks sat unenriched and ungated**.
   The indicator says *nothing queued*, which reads as *nothing to do*. (Of
   those 64,340, some 58,098 are tabular and would be cold-marked the moment
   `prepare_units` next runs — the backlog figure is itself distorted by the
   stall, which is precisely the point: every downstream metric is unreliable
   while the producer is starved.)
3. **User-facing search failing** — `/api/recall` dying with `BrokenPipeError`:
   the daemon computes results after the client has already given up.
4. **Stalls invisible** — the heartbeat is written after the passes, so by
   construction it cannot detect a mid-cycle stall.

### Why the cycle overruns

Two independent mechanisms, both observed:

- **Compute saturation.** `index_pending` is unbounded: `store.unembedded_chunks()`
  has no LIMIT (`store.py:1236-1249`) and it embeds the entire pending set in
  32-row batches (`index.py:21-33`); `run_sync_cycle` calls it six times
  (`sync/__init__.py:60,63,67,102,147,187`). Measured: a 61,580-chunk backlog
  draining at 544/min ≈ 1.9 h, at 425% CPU with no `intra_op_num_threads`
  configured on a 10-core box. Enrichment `drain` is likewise unbounded.
- **Network hang.** A sampled main thread sat in
  `_ssl__SSLSocket_read → PySSL_select → poll` at 0.0% CPU with zero log output
  for 1 h 44 m. `auth.build_service` applies a single 600 s socket timeout
  (`auth.py:28-33`) — chosen for ~750 MB backup uploads — to *every* request.

Notably the embedding backlog later drained to 1 and the cycle *still* had not
completed after 19 h, so bounding embedding alone is not sufficient.

### Ruled out

`_backfill_active` gating is **not** the cause. `mcpbrain/enrich_backfill.py`
does not exist, so the import at `daemon.py:1080` raises before the thread
starts and all eleven `_backfill_active.is_set()` guards — including the
whole-call guard at `daemon.py:2212` — are dead code.

## Goals / non-goals

**Goals.** Maintenance runs on schedule regardless of sync state. Every cycle
phase returns promptly. A stalled daemon recovers itself. Recall stays
responsive during active ingest.

**Non-goals.** Ingestion correctness and repair (specs 2 and 3). No new
scheduling dependency — see *Rejected alternatives*.

## Architecture

Two independent timers in one process.

**Cycle loop (existing main thread)** keeps doing bulk work, but every phase is
deadline-bounded and resumable. A phase receives a wall-clock budget
(`CYCLE_BUDGET_S`, default **60 s** for the whole cycle, apportioned across
phases) and returns `more_work`. On expiry it yields; if work remains the loop
re-wakes promptly rather than sleeping the full interval, so throughput on a
large backfill is largely preserved while the loop always reaches the bottom.

**Maintenance scheduler thread** owns `_run_periodic_passes` and nothing else,
ticking every ~60 s. Each pass still self-gates through the existing `_is_due` /
injectable `_clock` (`daemon.py:1277-1290`), so cadence semantics and every
existing cadence test are preserved.

A useful simplification: most phases are already naturally resumable, because
they are driven by a DB predicate (`embedded=0`, `enriched=0`) rather than an
in-memory position, and Gmail/Drive already carry delta tokens. This needs
*bounding*, not new cursor machinery.

### Why a second thread is safe

`SingleWriterLock` (`daemon.py:227-305`) is an OS **file** lock on
`app_dir()/daemon.lock` — process-scoped, not thread-scoped. It excludes a
second daemon *process* and does nothing intra-process. There is no intra-process
store-write lock at all; serialisation is SQLite's (WAL, `busy_timeout=5000`,
`store.py:87-90,142`), and concurrent writers already exist and are supported:
HTTP threads write via `daemon.search → decay.update_on_recall`
(`daemon.py:901-906`) and `record_recall_feedback_batch`
(`control_api.py:353-360`), and the MCP process holds a writable handle
(`store.py:118-119`).

### Contention policy

Of the ~20 passes, only four touch `chunks` and can produce a LOGICAL race with
ingest — read-modify-write of the same rows the cycle is mutating:
`stale_reextract` (`daemon.py:1526`), `salience_score` (`:1576`), `decay_pass`
(`:1609`), `consolidation` (`:1640`). Those acquire a coarse advisory
`_bulk_lock` that the cycle holds around `run_one()`. This is cheaper and less
deadlock-prone than routing all writes through a single queue, and matches what
the store already tolerates.

`_bulk_lock` is scoped to that logical hazard and **nothing else**. It is *not*
a general writer mutex, and the other sixteen passes do **not** "write disjoint
tables and run freely": SQLite's write lock is **database-wide**, not per-table,
so every non-gated pass still contends with the cycle thread for the single
write lock. What handles that is Task 1's machinery — `BEGIN IMMEDIATE` plus
`busy_timeout=5000` and a jittered retry — which makes contention a bounded wait
rather than a lost update, for *all* writers, gated or not. It is a mitigation,
not a guarantee: a pass that holds one very long transaction (e.g.
`communities` → `store.replace_communities`, which deletes and reinserts the
whole `entity_communities` partition in a single transaction) can still exceed
the retry budget and surface `OperationalError: database is locked`. That raise
is caught per-pass by the dispatch loop and the pass retries on its next
cadence, so it degrades to a skipped tick rather than a crash.

`_bulk_lock` is also held around `maybe_backup()` in `run()`. `backup.snapshot()`
runs `PRAGMA wal_checkpoint(TRUNCATE)` and aborts if the checkpoint reports
busy; its "daemon is the sole writer" premise stops being true the moment
maintenance gets its own thread, and a racing writer either silently stops
backups advancing or tears the snapshot mid-`copy2`.

**The maintenance thread's acquisition of `_bulk_lock` must be bounded**
(`BULK_LOCK_ACQUIRE_S`), and its own cadence checked *before* the lock is
attempted. The cycle thread holds the lock for the whole of `run_one()`, so a
wedged cycle holds it indefinitely; an unbounded `with self._bulk_lock:` in the
dispatch loop parks the maintenance thread — and with it the progress heartbeat
and the watchdog check that follow `_run_periodic_passes()` in
`_maintenance_loop` — making the self-healing watchdog unreachable during
exactly the stall it exists to detect, and starving every pass ordered after the
first gated one. On timeout the pass is skipped for that tick and retried later.

Inter-pass ordering constraints to preserve: `lint → review`
(`review.py:82`), `communities → blocks` (`community_synth.py:54`),
`salience_score → decay_pass` (`decay.py:153,175`). Note the dispatch comment at
`daemon.py:2204-2205` claiming "communities first so lint reads fresh
entity_communities" is inaccurate — `lint_graph.py` never reads
`entity_communities`; the consumer is the `blocks` pass. Correct the comment.

## Components

**New**

- `Budget` — value type (`deadline`, `max_items`) with `.expired()`. Threaded
  into long phases, which check it between units and return `more_work`.
- `MaintenanceScheduler` — thread; ticks ~60 s, calls `_run_periodic_passes`,
  respects `_pause`, keeps the existing per-pass exception isolation.
- `_bulk_lock`, `_stash_lock`, `_embedder_lock` — see *Shared-state hazards*.
- `_note_progress(phase)` — per-phase progress timestamp, replacing the
  end-of-cycle-only heartbeat.

**Changed**

- `index_pending(limit=N)` → `store.unembedded_chunks(limit=N)`, returns
  `more_work`. The single largest unbounded phase.
- `run_sync_cycle` and enrichment `drain` accept the budget and check it between
  sources/pages.
- `store`: `BEGIN IMMEDIATE` on write transactions, plus `journal_size_limit`.
- `auth`: a routine read timeout (~60 s) split from the 600 s backup timeout.
- `embed`: cap `intra_op_num_threads` (leave ~2 cores) so the control plane is
  schedulable.
- Dashboard: distinguish *nothing queued* from *nothing to enrich*, so victim 2
  is visible rather than reading as success.

### Shared-state hazards (must close in the same change)

Currently safe only by accident of single-threading:

- `_pending_blocks` / `_pending_audit` / `_pending_synthesis` — written by passes
  (`daemon.py:1863,1973,2029,2135,2180`), read and key-deleted by `run_one`
  (`:1238-1272`). Guard with `_stash_lock`.
- The lazily-built embedder — `_LocalEmbedder._model` (`embed.py:117-128`) is
  used by `index_pending` on the loop thread and by `consolidation` (`:1648`) /
  `self_improve` (`:1710`) on the scheduler thread; the lazy build
  (`daemon.py:432-447`) is itself unsynchronised. Guard with `_embedder_lock`.
- `_last_*` cadence anchors — become race-free by construction once only the
  scheduler thread runs passes.

## Data flow

Main loop tick: build a budget → run the cycle within it, holding `_bulk_lock`
only around chunk-mutating phases → `_note_progress` → sleep the full interval
only if no work remains, else re-wake promptly.

Scheduler tick (~60 s): for each due pass, acquire `_bulk_lock` if it is one of
the four contending passes, run it, record progress.

## Error handling and self-healing

**Watchdog.** A monitor in the scheduler thread compares now against the
per-phase progress timestamps. If no phase advances within `STALL_S`
(default 30 min) it logs an ERROR naming the stalled phase and its last-progress
time, then triggers recovery.

**Recovery is platform-aware**, because a uniform exit is wrong:

- **macOS** — clean exit. `~/Library/LaunchAgents/com.mcpbrain.plist` has
  `KeepAlive: True` (verified) and launchd restarts with its 10 s default
  throttle.
- **Windows, Task Scheduler available** — register the on-logon task from XML
  (`schtasks /create /XML`) rather than the current flag-based CLI call
  (`agents.py:168`), carrying
  `<RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure>`.
  The CLI's `/RI` cannot express restart-on-failure for an on-logon task; XML
  can. A clean exit is then genuinely supervised, as on macOS.
- **Windows, Task Scheduler policy-blocked** — the Startup-folder `.lnk`
  fallback (`agents.py:151-157`) cannot be supervised by anything, so here the
  daemon spawns a detached replacement and then exits. Detached means the
  Windows creation flags `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` —
  `close_fds` alone leaves the successor sharing the dying parent's console and
  process group. The incoming process retries the writer lock briefly
  (`HANDOVER_LOCK_WAIT_S`) to cover the handover: `SingleWriterLock.acquire`
  is non-blocking by default, so without an explicit wait the successor would
  lose the race against its own slow-exiting parent and leave nothing running.

This widens what the open 0.7.97 Windows QA gate must cover, since task
registration changes. That is the accepted cost of making Windows genuinely
supervised rather than working around its absence.

**Restart-loop bounding.** Firing only after 30 min of zero progress self-limits
to ~2 restarts/hour. On top of that the watchdog records consecutive
watchdog-triggered exits; after 3 within 6 hours it stops self-exiting and
degrades to log-and-surface, so a persistently broken install becomes visibly
stuck rather than restarting forever. State is exposed on `/api/status` as
`watchdog_exits` / `watchdog_limit_reached` (both derived from the same
`watchdog_exits.json` history the limiter itself reads, so they cannot
disagree), and `mcpbrain doctor` renders it as a Watchdog line — a reached limit
is an actionable ❌, since "visibly stuck" is only visible if something says so.

The watchdog also needs a starting point: `_progress` is seeded with a `cycle`
timestamp when `run()` enters its loop. `_stalled_phase()` returns `None` on an
empty `_progress`, and only the maintenance thread writes to it if the cycle
wedges before its first `run_one()` returns — so without the seed the very
failure shape from the live incident (stale for 35.9 h across three restarts)
would be invisible. Symmetrically, `resume()` re-stamps every tracked phase
before clearing the pause: `_maintenance_loop` skips its whole body while
paused, so a pause longer than `STALL_S` would otherwise be read as a stall on
the first tick after resume and trigger a false-positive restart.

**Ordinary errors** keep today's semantics: each pass individually wrapped
(`daemon.py:2218-2221`), cycle exceptions caught and retried next interval
(`:2307-2315`). Two additions — budget expiry is a normal return
(`more_work=True`), never an exception; and `SQLITE_BUSY` gets a bounded retry
with jitter around the `BEGIN IMMEDIATE` write path, which becomes reachable
with two writer threads.

`BEGIN IMMEDIATE` matters independently of threading: Python's `sqlite3` uses
DEFERRED, so a transaction that reads then writes must upgrade, and the upgrade
fails *immediately* with `SQLITE_BUSY` regardless of `busy_timeout`. There are
currently zero uses of `BEGIN IMMEDIATE` in `store.py`.

**Degradation.** If the scheduler thread dies the daemon logs it and continues
syncing rather than exiting; the watchdog covers its heartbeat too.

## Testing

**Unit, against the injected clock.** Existing cadence tests already drive
`_is_due` through a fake `_clock`, so the scheduler is testable the same way:
assert passes fire on cadence *with the cycle loop deliberately blocked* — the
direct regression test for this bug, which nothing currently covers.

**Bounding.** `index_pending(limit=N)` reports `more_work` correctly at the
boundary; a budget expiring mid-phase yields rather than raising; the next call
resumes with no item skipped or double-processed. Property-style: N slices over
a K-item backlog process exactly K items.

**Concurrency — real threads, not mocks**, since the whole bug class lives in
interleaving. Two threads writing through `BEGIN IMMEDIATE` neither lose writes
nor deadlock; `_bulk_lock` serialises only the four contending passes;
`_stash_lock` prevents the `_pending_*` read-delete race.

**Watchdog.** Stale progress triggers recovery; the consecutive-exit limiter
stops after 3 in 6 hours; platform dispatch asserted per branch with the
platform faked.

**Existing tests to update.** `tests/test_cadence_dispatch.py:63` asserts
`_backfill_active` suppresses all graph passes — that guard is dead code and the
assertion encodes behaviour we are removing.

**Acceptance on the live store**, since unit tests cannot prove this:

- a cycle completes in under a minute with a large backlog present;
- cadence passes run on schedule while ingest is active;
- `/api/recall` p95 stays responsive during active embedding;
- the enrichment queue refills continuously — the 36-hour producer stall becomes
  impossible;
- gold eval holds (recall@10 / MRR) — no retrieval regression.

## Rejected alternatives

**APScheduler.** `max_instances` defaults to 1 and silently skips overruns; the
instance counter has been reported not to decrement, permanently stopping a job
([#644](https://github.com/agronholm/apscheduler/issues/644)) — precisely the
failure being fixed here, reproduced in a dependency. `misfire_grace_time`
defaults to 1 s, hostile to a laptop that sleeps. The default executor is a
10-thread pool, i.e. 10 concurrent SQLite writers. The existing `_is_due` +
injectable clock is well-tested and every cadence test depends on it.

**Single writer thread with a queue.** The standard answer for high write
concurrency, but it serialises reader latency behind queued writes and is a
larger change than the contention profile justifies — only four passes contend.

**Splitting the embedder into a child process** (the Recoll/Tauri-sidecar
pattern). Measurements on the convoy effect suggest this may eventually be
needed for recall latency under load, but ORT thread capping should be measured
first. The bounded-slice work here is its prerequisite, so it is not wasted
either way.

## Out of scope

Ingestion correctness and repair — see the findings register. Sequenced after
this spec because the daemon fix stops active data loss, and because a repair
backfill run against the current extractor would re-import the same defects.
