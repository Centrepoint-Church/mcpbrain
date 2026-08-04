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

### Chosen design (approved 2026-08-04)

**The pivotal unknown is whether a client respawns an exited stdio MCP server.** Both
"real fix" options depend on it, and the outage logs suggest it may not happen: after
`Server transport closed unexpectedly` / `Server disconnected`, nothing restarted until a new
Desktop launch ~50 minutes later. **If clients do not respawn, self-exit and updater-kill both
make things strictly worse** — they downgrade a user from "stale but working" to "no mcpbrain
until they restart". So that question is settled by experiment *before* any fix is built.

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

All three findings are in scope for one plan — they are all "how the MCP server process
behaves", and Findings 2 and 3 are cheap measurements whose value is mostly in closing off
wrong explanations.

1. **Finding 1, experiment first** (§ Chosen design step 1) — gates step 3.
2. **Finding 1, `doctor` drift check** (step 2) — independent of the experiment, ships either way.
3. **Finding 1, the branched fix** (step 3) — shape determined by step 1's measured result.
4. **Finding 2** — bounded measurement: confirm both Desktop servers actually complete
   `initialize` (the logs suggest they do), measure each one's RSS, and establish whether the
   count tracks windows/workspaces. A documented *"this is client behaviour, nothing to
   change"* is a legitimate and expected outcome; the point is to stop guessing.
5. **Finding 3** — real experiment: with two servers connected, invoke a **writing** tool
   (e.g. `brain_note`) in each concurrently, then attempt `wal_checkpoint(TRUNCATE)` and see
   whether it returns busy. This either identifies the cause of the observed backup failures or
   refutes the hypothesis with evidence. Note the refutation already established under idle
   conditions (§ Finding 3) — this tests the *loaded* case that was never tested.

**Success criterion for the plan as a whole:** every one of the three findings ends either
fixed or **explicitly closed with evidence**. "Still unexplained" is a failure for Finding 3
specifically, because live backups were failing and the cause is currently unattributed.
