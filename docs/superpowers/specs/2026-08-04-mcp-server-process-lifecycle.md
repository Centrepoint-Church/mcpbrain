# MCP server process lifecycle — stale code after auto-update, and duplicate servers

**Status:** investigation complete, no fix implemented. Raised while verifying the
mcp 2.x migration (`docs/superpowers/plans/2026-08-04-mcp-2-migration.md`); deliberately
kept out of that branch as a separate concern.

**Why this matters more than it looks:** mcpbrain ships to a fleet that **auto-updates
daily**. Finding 1 below means a shipped fix does not necessarily reach a user when their
wheel updates — it reaches them when they next restart their MCP client, which may be
never. That directly undercuts the delivery assumption behind every release.

---

## Evidence (author box, 2026-08-04)

Three `mcpbrain mcp-server` processes alive simultaneously:

| pid | started | parent | code vintage |
|---|---|---|---|
| 41296 | 10:25:11 | `claude` (Claude **Code**, ppid 41285) | **stale** — predates the 16:18 install |
| 65948 | 16:18:45 | Claude Desktop → `Helpers/disclaimer` (ppid 65945) | current |
| 65956 | 16:18:46 | Claude Desktop → `Helpers/disclaimer` (ppid 65951) | current |

Installed `mcp_server.py` mtime: **16:18:06**. So 41296 was started ~6h before the wheel
it is executing was replaced, and 65948/65956 were spawned 39–40s after it.

Corrected along the way: 41296 is **not** an orphan of Claude Desktop, as first assumed —
its parent is a Claude Code process. Two separate MCP clients each hold their own server.

---

## Finding 1 — a live MCP server keeps running the code it started with, and nothing tells it otherwise

`mcpbrain/update.py` contains **no** reference to `mcp-server`, `pkill`, `terminate`, or
`SIGTERM` (verified by grep). `mcp_server.py` writes `mcp_heartbeat.json` at startup
(`write_heartbeat`, `mcp_server.py:12-23`) but has **no** mechanism to notice that the wheel
underneath it changed.

Python loads modules once at import, so a long-lived MCP server executes the code present at
its start for its entire life. After the daily auto-update replaces the wheel:

- the **daemon** is restarted by the updater and picks up new code;
- every **MCP server** keeps running old code until its client re-spawns it.

Consequences, both real:

1. **A fix does not ship when the wheel ships.** It ships when the user restarts Claude
   Desktop / Claude Code. A user who leaves the app open for a week runs week-old MCP code
   while `mcpbrain doctor` reports the new version — because `doctor` reads the installed
   distribution, not the live process.
2. **Conversely, this partially masked the 2026-08-04 outage.** A user whose Desktop stayed
   open kept a working `mcp` 1.x server running after the bad 2.0.0 wheel landed; the crash
   only appeared on their next restart. That explains staggered rather than simultaneous
   fleet impact, and it means "nobody reported it" was not evidence of not being affected.

The version the server reports is its own start-time version, which is now correct
(`serverInfo.version` = mcpbrain's version, fixed in the migration) — so a **client** can
see the drift even though nothing acts on it.

### Chosen design (approved 2026-08-04) — fix the cause, don't manage the symptom

**Superseded:** an earlier revision of this spec chose *experiment, then self-exit-when-idle*.
That is now the fallback, not the plan. It manages a symptom; the cause is that the updater owns
the **daemon's** lifecycle (via launchd) but has no relationship with MCP server processes, which
clients own.

**The cause, precisely.** Staleness only matters because tool logic *executes inside the MCP
server process*. `brain_search` already delegates to the daemon via `ControlClient`, but
`brain_graph`, `brain_context`, the captures and the enrichment tools all run in-process against
the server's own `Store` handles. Make the MCP server a **thin protocol adapter** and stale code
stops mattering: tool fixes then land with the daemon restart the updater already controls.

**The seam is Store access, not "everything".** Routing *all* tools through the daemon would
regress real behaviour — six capture tools (`brain_note`, `brain_decision`, `brain_ingest`,
`brain_action_create`, `brain_action_update`, `brain_memory_write`) work **with the daemon down
today**, because `capture.write_capture` only writes a JSON envelope into `capture_inbox/`. Those
are exactly the tools you most want working when things are broken. So:

- **Stays in the MCP server:** stdio/protocol, `initialize`, `tools/list`, prompts, resources,
  progress — plus the six filesystem-only capture tools.
- **Moves behind the control API:** every tool holding a `Store` handle — `brain_read`,
  `brain_context`, `brain_actions`, `brain_graph`, `brain_proactive`, `brain_finding_resolve`,
  `brain_draft_context`, `brain_draft_save`, `brain_meeting_pack_get`/`upsert`.
  (`brain_search` is already there.)

What that buys, by construction rather than by measurement:
- The MCP server ends up holding **no `Store` handle at all** — removing Finding 3's entire class
  and the writable-`draft_store` concern outright.
- A stale server then affects only **protocol handling and spool writes**, the parts that change
  rarely.
- Captures keep working daemon-down.

**Prerequisite: the colocated tool registry** (`2026-08-04-tool-specs-consolidation-design.md`).
It is the seam — once each tool's metadata is declared beside its handler in one module, the daemon
can execute from the registry while the MCP server reads it for `tools/list`, or fetches the list
from the daemon and holds no tool knowledge at all. Land that first.

**Latency is the gate, and it can veto this.** The daemon is a single process under a GIL, and
routing store reads through it adds load to the component whose contention already caused two
incidents (0.7.105 recall timeouts from the drain pinning the process; 0.7.110 raising
`prompt_recall`'s timeout to 3.0s after measuring 1.3-2.6s cold). Measure per-tool latency before
and after; if it regresses recall, the move does not ship for the affected tools.

**Not a prerequisite after all — verified 2026-08-04.** An earlier revision proposed fixing daemon
concurrency first (CLAUDE.md's finding #3 and a claimed cadence stall since 2026-07-23). **Both
were already fixed**, in commits `693a8cd` / `e55880b` on 2026-07-28: `_backfill_active` is now
checked per-pass (ten guards) rather than as one wholesale early-return, `BULK_LOCK_ACQUIRE_S`
bounds the acquire so a stuck pass cannot park the watchdog, and `BULK_LOCK_YIELD_S` fixes the
lock unfairness behind "183 consecutive skip warnings, live". Evidence the cadences run:
`action_hygiene` logged 2026-07-29, twice 2026-08-03, and 2026-08-04 11:06;
`decay_pass`/`tier_pass` at 2026-08-04 20:25. What survives is the latency *measurement* above,
as a gate inside this work — not a separate project.

**Still worth doing independently: the `doctor` drift warning.** Even a thin shim contains
version-sensitive code, and the warning is useful while the migration is partial. Use **per-process
version records** (`mcp_heartbeat/<pid>.json` carrying version + start time, pruned by pid
liveness) rather than the single shared `mcp_heartbeat.json`, which is last-writer-wins and cannot
answer "is *any* live server stale?" — see Finding 2. This reports the actual version rather than
inferring it from a process-start-time vs file-mtime proxy.

**Fallback, only if the thin adapter is vetoed by the latency gate:** the original
experiment-then-self-exit design below.

**1. Experiment (gates the fix).** Kill a live `mcpbrain mcp-server` and observe whether
Claude Desktop and Claude Code respawn it, on the same connection. Record the answer for both
clients; they are separate implementations and may differ.

**2. `doctor` surfaces the drift — ships regardless of the experiment.**

Originally this was framed as "record the version in `mcp_heartbeat.json` and have `doctor`
compare". **That is the wrong mechanism**, for a reason Finding 2 makes concrete: the heartbeat
is a *single* file and there are *multiple* live servers, so last-writer-wins and the file
describes whichever server started most recently — not whether *any* live server is stale.

Instead `doctor` enumerates live `mcpbrain mcp-server` processes and compares each one's
**process start time** against the installed package's mtime. Any server started before the
currently-installed `mcp_server.py` is running superseded code. This needs **no server-side
change at all**, is correct for N servers, and is exactly the check performed by hand on
2026-08-04. It reports the actionable line, e.g. *"1 MCP server is running older code —
restart Claude to pick up 0.7.113."*

Known limitation, accepted: start-time-vs-mtime is a proxy. Touching the installed file
without reinstalling would false-positive. That is a strictly better failure than the current
silence, and no cheap way exists to read the version a foreign process actually imported.

**3. The fix branches on the experiment.**
- *Clients respawn* → the server self-checks on a cheap cadence and exits cleanly when it is
  both **drifted and idle**. Idleness is load-bearing: exiting mid-tool-call is user-visible.
- *Clients do not respawn* → the `doctor` warning **is** the fix, and it should also surface
  where the user will see it without running `doctor` (the tray). Do **not** build self-exit.

Deliberately rejected either way: having `update.py` reach into another client's process to
kill it. It is racy against in-flight calls and the updater has no way to know whether a tool
call is running.

---

## Finding 2 — Claude Desktop runs two mcpbrain servers at once

65948 and 65956 are both current, both parented by Claude Desktop through separate
`Helpers/disclaimer` wrappers, started one second apart. `claude_desktop_config.json`
declares `mcpbrain` **once**.

Each server process constructs **two** `Store` handles — a read-only primary and a
**writable** `draft_store` (`mcp_server.py:2057`, needed because draft/meeting-pack/finding
tools write). So two Desktop servers plus one Claude Code server is up to three writable
SQLite handles on an 11.9 GB store, in addition to the daemon's.

Unknowns worth establishing before treating this as a defect:

- Is one-per-window/workspace intended Claude Desktop behaviour?
- Are both actually initialised, or is one a probe that never connects? (Both wrote
  heartbeats and both answered `initialize` in the logs, so both appear live.)
- Cost: each is a full Python process with the mcpbrain import graph — measure RSS.

## Finding 3 — the writable-handle / WAL-checkpoint hypothesis is NOT supported by current evidence

Backups have been failing with `wal_checkpoint(TRUNCATE) busy=1`
(`backup_state.json`: `consecutive_failures: 1`, newest good archive 12:27). The obvious
hypothesis was that the MCP servers' writable handles block a TRUNCATE checkpoint, which
requires no other connections.

**Measured, and it does not hold up:** `lsof` on `brain.sqlite3` shows exactly **one**
holder — the daemon (65882) — and there is **no `-wal` or `-shm` file** present, i.e. the DB
is in a checkpointed state. The `Store` handles in the MCP servers are constructed but
connect lazily, so an idle server holds nothing.

So the 13:31 failure was transient — plausibly a concurrent writer at that instant (the
daemon's own drain, or an MCP write landing mid-checkpoint) — not a standing block. Worth
re-testing under load: call a writing MCP tool (`brain_note`) in two servers concurrently and
attempt a checkpoint. **Do not** assume this cause without that evidence.

Related and separately recorded: `CLAUDE.md` notes daemon cadence passes have appeared
stalled since 2026-07-23 (`_run_periodic_passes` early-returns wholesale when
`_backfill_active` is set), which would also suppress the backup cadence.

---

## Also noticed while measuring

- `com.mcpbrain.err` in the app dir is **208 MB** and appears unbounded — no rotation. Not
  urgent, but it is the daemon's stderr sink growing without limit on every install.
- Pre-existing lint debt: **85 ruff errors** across ~8 test files that neither current
  workstream touched (unused imports/variables, multiple-imports-on-one-line). The migration
  branch's own 9 changed files are clean.

## Scope and order (approved 2026-08-04)

Two projects, in dependency order. Each gets its own plan.

**Project 1 — colocated tool registry.** `2026-08-04-tool-specs-consolidation-design.md`. Lands
the metadata win on its own merits and creates the seam Project 2 needs. No behaviour change.

**Project 2 — thin adapter (this spec).** In order:

1. **`doctor` drift warning** with per-process version records. Independent of everything else,
   useful while the migration is partial, and the honest answer for whatever version-sensitive code
   remains in the shim.
2. **Latency baseline** — measure per-tool latency *before* moving anything, so the gate in step 4
   has something to compare against. Without this the gate is unfalsifiable.
3. **Move the Store-touching tools behind the control API**, one group at a time, so a regression
   is attributable. The MCP server must end holding **no `Store` handle**; that is the observable
   success criterion, not a vibe.
4. **Latency gate.** Compare against step 2. A tool whose latency regresses materially does not
   move; recall paths (`brain_context`, `brain_actions`, `brain_graph`) are the ones to watch,
   given the 0.7.105 and 0.7.110 incidents.
5. **Finding 2, folded in as a measurement** — confirm both Desktop servers complete `initialize`
   (the logs suggest they do) and measure each one's RSS. This gets *cheaper* to care about after
   step 3, since a shim without a `Store` is a much smaller process. A documented "this is client
   behaviour, nothing to change" is a legitimate outcome; the point is to stop guessing.
6. **Finding 3, folded in as a measurement** — with two servers connected, invoke a **writing** tool
   in each concurrently, then attempt `wal_checkpoint(TRUNCATE)` and see whether it returns busy.
   Run it **before** step 3 (so it still reproduces the current arrangement) and **after** (to
   confirm the class is gone once the shim holds no writable handle). Note the refutation already
   established under *idle* conditions in § Finding 3 — this tests the loaded case that was never
   tested.

**Success criterion:** each of the three findings ends fixed or **explicitly closed with
evidence**. "Still unexplained" is a failure for Finding 3 specifically, because live backups were
failing and the cause is currently unattributed.

**Release shaping — decide before implementing.** Project 2 is a larger behavioural change than
anything in 0.7.113, on a package that auto-updates unattended, with the Windows hardware QA gate
still open. Options worth weighing: put the daemon-routing behind a feature flag defaulting OFF (the
established pattern here — cf. `retrieval_expand`, `salience_gate`), or hold it until the Windows
gate closes. A flag also makes the latency gate reversible in the field rather than only in review.

**Success criterion for the plan as a whole:** every one of the three findings ends either
fixed or **explicitly closed with evidence**. "Still unexplained" is a failure for Finding 3
specifically, because live backups were failing and the cause is currently unattributed.
