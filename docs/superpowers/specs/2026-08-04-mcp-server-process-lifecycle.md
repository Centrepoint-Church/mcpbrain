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

### Options (not chosen)

- **Server-side self-check.** Record the installed distribution's version/mtime at startup;
  on some cheap cadence (or per request) compare, and if it changed, exit cleanly so the
  client re-spawns. stdio clients generally restart a server that exits — needs verifying per
  client, and an exit mid-tool-call would be user-visible, so it should only fire when idle.
- **Updater-side signal.** Have `update.py` find and terminate live `mcpbrain mcp-server`
  processes after a successful wheel swap. Blunter, and racy against an in-flight tool call.
- **Surface it instead of fixing it.** Have `doctor` compare `mcp_heartbeat.json`'s process
  vintage against the installed version and warn. Cheapest, and honest, but leaves the user
  to act.

The self-check-when-idle option looks best; it is the only one that closes the loop without
the updater reaching into another client's process. Wants a decision, not just an
implementation.

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

## Suggested order

1. Decide Finding 1 — it is the one with fleet-wide delivery consequences.
2. Establish whether Finding 2 is intended client behaviour before changing anything.
3. Re-test Finding 3 under concurrent writes before attributing any backup failure to it.
