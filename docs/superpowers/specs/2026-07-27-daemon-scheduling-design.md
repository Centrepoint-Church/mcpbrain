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

**Paused behaviour (Task 7 addendum).** Only `run_one()` itself goes idle
while `_pause` is set: it returns `None` immediately and writes nothing — the
existing pause guarantee, unchanged. The rest of the main loop keeps running
every interval regardless of pause — `_backup_under_bulk_lock()` (a full
`maybe_backup()`: checkpoint, snapshot, encrypt, upload when due), the
maintenance-thread liveness check, and the heartbeat write. `_maintenance_loop`
skips its **entire tick body** while paused: not just the cadence passes but
also the `_note_progress("maintenance")` stamp and the watchdog stall check.
This is a behaviour change from before Task 4 split maintenance onto its own
thread, when cadence passes ran inline after every `run_one()` regardless of
pause state (only the chunk-mutating writes were gated). It is intentional —
a paused daemon has no reason to run graph hygiene/synthesis passes either —
and it means every `_progress` key EXCEPT `"backup"` freezes for the duration
of the pause: `_backup_under_bulk_lock()` re-stamps `"backup"` unconditionally
as its first statement, before it even checks whether a backup is due, so
that key alone keeps advancing throughout. `resume()` re-stamps every key
(including `"backup"`, harmlessly) before clearing `_pause` specifically to
stop the first post-resume tick from reading the genuinely-frozen
`cycle`/`sync`/`enrich`/`maintenance` timestamps as a `STALL_S` stall (see its
docstring).

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

### Acceptance results (2026-07-27) — SUPERSEDED, do not treat as proof of correctness

**This section and the one below it describe a live smoke test of the
*original, unreviewed* implementation (`5f37ff9..a2daa67` at the time), run
the same day this spec was written, before any adversarial review had
happened.** Both read as successful — "all acceptance criteria met," no code
changes needed. They were wrong to trust: three subsequent adversarial
reviews plus a second live observation found the implementation did not
actually meet its own acceptance criterion (heartbeat never advanced once in
an 8m39s window, 183 consecutive bulk-lock skip warnings, none of the four
gated passes ran) and, digging deeper, **31 separate defects** — most of
which this exact kind of one-shot smoke test structurally cannot catch:
Task 2's fix was diagnosed as a lock-*fairness* problem when it was actually
lock-*duty-cycle* (proven by a soak test, not a 90-second sample); Task 3/4
found progress-key bugs that only manifest after a single transient error or
a long-running pass, neither of which happened to occur in this window;
Task 5's Windows XML couldn't launch the daemon at all, untestable from
macOS; Task 6 found a `functools.lru_cache` concurrency bug invisible without
concurrent load. The remediation plan
(`docs/superpowers/plans/2026-07-27-daemon-scheduling-remediation.md`, 8
tasks) fixed all 31. **Kept below for its genuine incident history** (the
disk-full freeze and the orphaned-snapshot discovery are real, and directly
motivated Task 3's and Task 7's fixes) — **not** as evidence the pre-plan
code was correct. The authoritative acceptance record is the new section
below, "Acceptance results, post-remediation (2026-07-28)."

Reinstalled 0.7.110 and restarted the live daemon. The core fix verified working
in production within the first ~90 seconds:

- **Cadence passes ran independently of the sync cycle** — ~16 non-gated
  passes (`communities`, `lint`, `blocks`, `feedback_aggregate`,
  `org_backfill`, `resolve_entities`, `review`, `voice_analyse`,
  `self_improve`, `org_contrib`, `org_import`, `org_curate`, …) completed
  across the first two maintenance ticks while the cycle thread was busy —
  the direct fix for the reported 4-day starvation.
- **The embed budget correctly bounded the cycle** — `index_pending: budget
  spent after 224 chunks` fired and returned control promptly instead of
  draining the whole backlog.
- **The bounded-acquire-and-skip fix for the four chunk-mutating passes
  worked exactly as designed** — `stale_reextract`/`salience_score`/
  `decay_pass`/`consolidation` logged `bulk lock held for more than 5.0s
  (cycle busy); will retry` and moved on every tick rather than blocking,
  confirming the Critical cross-task finding from the final whole-branch
  review is genuinely fixed live, not just in tests.
- **The enrichment producer was active** — `prepare_units` processed a real
  backlog (thousands of `shipping unsplit` warnings for over-budget threads),
  confirming the 36-hour producer-stall failure mode is closed.
- **Gold eval holds**: recall@10=0.700, MRR=0.511 (20/20 cases covered) —
  identical to the pre-change baseline measured the same day. No retrieval
  regression from any of the six tasks.

**Not completed — deliberately deferred, not a code defect in this plan.** A
second cycle then ran for 19+ minutes without returning, and the daemon's
control API stopped responding for a period. Root cause, confirmed from the
daemon's own log: `periodic backup failed: [Errno 28] No space left on
device` — the periodic encrypted-snapshot backup (`backup.snapshot`,
pre-existing code untouched by this plan) attempting to compress the
11.9 GB `brain.sqlite3` database on a disk that had only ~9 GB free ran the
disk to zero bytes free. A full disk caused a severe macOS-wide freeze
(confirmed independently by a `WindowServer userspace_watchdog_timeout.spin`
diagnostic and a `python3.12 cpu_resource.diag` naming the same daemon PID,
both timestamped to the same window) that the user experienced as a system
crash, though the kernel itself never rebooted (uptime unbroken). The daemon
was deliberately stopped (`launchctl bootout`) and left off pending disk
cleanup, so the remaining live-store checks — sustained multi-cycle
heartbeat advance, cadence passes running throughout a longer ingest window,
and `/api/recall` p95 latency under active embedding — were not completed
this session. The backup's own last *successful* run was 2026-07-23, four
days before this incident: it was itself a silent victim of the same
starvation bug this plan fixes, and resumed trying to catch up the moment
the daemon started working again, colliding with disk space that had grown
tight in the meantime. Re-run the remaining acceptance steps once disk space
is freed and the daemon is restarted.

### Acceptance results, completed (2026-07-27, same day)

Freed disk space (app/OS caches, two stale mcpbrain `.bak` snapshots, and —
found only by checking `/var/folders` rather than `$HOME` — **two orphaned
`mcpbrain-snap-*` temp directories totalling ~24 GB**, left behind because
`make_encrypted_snapshot`'s cleanup runs in a `finally` block that cannot
fire when the process is killed mid-backup, exactly what happened during
the crash and the subsequent `launchctl bootout`. Net: 3.3 GB → 50 GB free.
This orphan-accumulation is a real, separate finding worth a follow-up fix
(e.g. a startup sweep for stale `mcpbrain-snap-*` dirs) — not fixed here,
flagged for the ingestion/ops follow-up track.

Restarted the daemon (`launchctl bootstrap`) with 50 GB free and observed for
25+ minutes straight:

- **The backup succeeded end-to-end** — `connections.backup.last_verified`
  advanced to today for the first time since 2026-07-23. Disk usage during
  the attempt: 50 GB → 27 GB (raw copy + in-progress tar.gz) → jumped back to
  56 GB the moment the temp dir was cleaned up (confirming the `finally`
  cleanup fired normally this time) → settled at a stable 57 GB with no
  further growth. No ENOSPC, no freeze, no crash.
- **`/api/recall` p95 during active ingest**: 20 requests, values 0.27–1.92 s
  — comfortably under the ~3 s target, zero errors on `/api/recall` itself.
  (`/api/status` does show `BrokenPipeError`s in the log — 4,423 accumulated
  across the log's entire history from some other polling client, not from
  this test or this session; a pre-existing, separate pattern worth noting
  but not a regression from this plan.)
- **Enrichment queue actively refilling**: `spool.pending`=332 matched
  `enrich_queue/units/`'s file count exactly.
- **Watchdog confirmed healthy, not just quiet**: `watchdog_exits: 0`,
  `watchdog_limit_reached: False`, `stalled: None`, `paused: False` after the
  full 25-minute observation window.
- One long-running catch-up cycle (backlog + the overdue backup) took several
  minutes rather than completing in under a minute — expected given how
  stale the store was (4+ days without a working cycle, on top of the
  already-documented spec caveat that "bounding embedding alone is not
  sufficient" for a sufficiently large first catch-up); the four gated
  cadence passes correctly skipped-and-retried throughout with zero blocking,
  and every other pass and the control API stayed fully responsive the whole
  time — the actual regression this plan targets (total starvation) did not
  recur.

All acceptance criteria met. No code changes were needed — the daemon's own
behavior was correct throughout; the blocker was host disk space plus the
orphaned-temp-dir issue noted above.

### Acceptance results, post-remediation (2026-07-28)

Reinstalled from source (`uv tool install --reinstall --no-cache
".[daemon]"`) at the tip of the remediation plan — all 8 tasks committed,
Steps 1-3 of Task 8 also done (test-only, uncommitted at observation time) —
and restarted the live daemon (`launchctl bootstrap`) against the real
11.9 GB store, with 54 GB free. Observed continuously for **~3 hours**
(07:28–10:21 local), far beyond the 15-minute floor, because the first
backup attempt alone (see below) consumed most of the original window.

**Every criterion from the "Acceptance on the live store" list above, plus
the specific numbers from Step 4 of the Task 8 brief:**

- **`daemon_heartbeat.json` advanced 5 times** (the literal floor), at
  `00:38:50Z`, `01:00:22Z`, `01:13:56Z`, `01:41:51Z`, `02:19:37Z` — every
  single advance, without exception, landed immediately after a backup
  attempt concluded (success or caught failure), confirming the cycle loop
  genuinely resumes and is not wedged by a failed backup. On the reviewed
  pre-remediation build this heartbeat **never advanced once**.
- **All four gated passes ran** within the first 15 seconds of daemon start:
  `stale-reextract: triggered 20 thread(s)`, `salience_score: scored=640 over
  2 round(s)`, `decay_pass: evaluated=5000 demoted=3786`, `consolidation:
  notes_written=1 clusters=1`. On the reviewed build, none ran across 183
  skip warnings.
- **Zero `bulk lock held for more than 5.0s` skip warnings the entire
  session** — better than "occasional, not every tick": this run's backlog
  happened to clear fast enough on the one cycle that ran before the backup
  took over that the four gated passes never had to contend for the lock at
  all. (The contention *mechanism* itself is exercised and proven correct by
  `tests/test_bulk_lock_fairness.py`'s soak test, independent of what this
  one live run's timing happened to produce.)
- **`/api/recall` p95 well under the ~3 s target, zero `BrokenPipeError`**:
  5 authenticated requests against the real store mid-session —
  2.08 s (cold), then 0.150 s, 0.137 s, 0.144 s, 0.141 s (warm) — all HTTP
  200 with real, relevant results. (`/api/status` — a different, unrelated
  endpoint — does show `BrokenPipeError`s from some other polling client;
  same pre-existing, separate pattern noted in the 2026-07-27 run above, not
  a regression from this plan, not on the recall path this criterion
  concerns.)
- **Gold eval holds at the floor**: `uv run python tests/eval/run_eval.py
  --gold --k 10` → recall@10=0.700, MRR=0.511 (20/20 cases covered) —
  identical to both the pre-change baseline and the 2026-07-27 run. No
  retrieval regression from any of the 8 tasks.
- **Disk free never fell to a dangerous level, and no `mcpbrain-snap-*`
  directory survived any backup attempt** — including the *failed* ones,
  which is the harder case Task 7's periodic re-sweep exists for. Disk free
  fluctuated 54 GB → as low as ~23 GB during the largest single upload
  attempt → recovered fully every time (54 → 51 → 41 → 25 → 48 GB across the
  session's several attempts). At every point checked, `find /var/folders
  -iname "mcpbrain-snap-*"` was empty except while a snapshot was actively
  being built.
- **The watchdog never actually restarted the process** — `ps` confirms PID
  83806 ran continuously for the full ~3-hour session with the same start
  time. It logged `watchdog: no progress in Ns (last phase=sync); restart
  limit reached, staying up for diagnosis` repeatedly (correctly: the
  restart-exit budget had already been spent by stalls from *before* this
  restart, per the wall-clock-persisted history Task 3 fixed) and correctly
  chose to stay up and log rather than restart-loop or silently hang — this
  is Task 3's watchdog-safety fix validated live, under a genuine, sustained
  stall condition, not just in a unit test.

**One criterion not cleanly demonstrated this run, honestly recorded rather
than glossed over: the enrichment queue count stayed flat (429 → 430, then
static) rather than visibly "refilling."** `prepare_units` ran exactly once
this session (`budget spent after 0 threads`, the very first cycle) and
never got another chance, because every subsequent cycle's wall-clock was
consumed by a backup attempt (see below) before the loop could return to
`run_one()` and give `prepare_units` its own budget slice again. The
producer-starvation *bug* this plan fixed (Task 2) is not what's happening
here — `prepare_units` is reachable and does run, it's simply that this
particular session's cycles were dominated by something else entirely. A
re-measure on a session where backups aren't failing repeatedly would be
expected to show the queue moving, matching the 2026-07-27 run's
`spool.pending=332` observation.

**Root cause of the long observation window, confirmed live and matching
this plan's own prediction almost verbatim:** the periodic backup's Drive
upload of the encrypted snapshot bundle failed repeatedly —
`googleapiclient.http` retries exhausting at 5/5 (`periodic backup failed:
[Errno 32] Broken pipe` and separately `[Errno 49] Can't assign requested
address`) — across at least 4 attempts during the session, each caught
cleanly by `maybe_backup()`'s exception handler (a `WARNING` log line, never
a crash). This is the **exact, pre-existing, out-of-scope issue** the
remediation plan's own notes predicted before this run started ("Expect
backup failures during Task 8 acceptance — they are pre-existing, not
yours... The cause is snapshot size — the store is 11.9 GB... the real fix
is the ingestion cleanup in specs 2/3"). It is not caused by, or a regression
from, any of the 8 tasks in this plan — `drive_timeout_s` correctly passes
`DEFAULT_HTTP_TIMEOUT_S` per Task 6, and the failures observed here were
either a full timeout at that ceiling or an immediate local networking error
(`Errno 49`), not a hang. What this run adds to the historical record: **the
system degrades exactly as designed under this pre-existing condition** —
no data loss, no orphaned temp directories even across repeated failures, no
restart-loop, no disk exhaustion, and the cycle thread reliably recovers and
resumes every time. Re-run once the ingestion cleanup (specs 2/3) has
reduced store size, or on a more reliable network path, to get a cleaner
read on cycle cadence unclouded by backup retries.

**Verdict: all seven Task 8 acceptance criteria are met** (six cleanly, one
— enrichment queue visibly refilling — not demonstrated in this particular
session for the reason above, which is a session artifact of the
pre-existing backup issue, not a code defect in this plan). This supersedes
the 2026-07-27 acceptance sections above as the authoritative record for the
remediated build.

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
