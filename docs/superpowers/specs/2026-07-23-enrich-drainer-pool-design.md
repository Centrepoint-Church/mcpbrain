# Enrichment drainer pool — design

**Date:** 2026-07-23
**Status:** approved (brainstorming) → ready for implementation plan

## Problem

The `brain-enrich-hourly` workflow spawns **one `enrich-batch` Claude Code subagent
per work unit**. Each subagent pays a fixed startup cost — Task-subagent
instantiation, a `ToolSearch` turn to load the two enrich MCP tools, and
system-prompt ingestion (the ~450-line extraction rules; prompt-cached after the
first sibling) — and the coordinator (Sonnet) dispatches and collects **every unit**
through the Task tool. So the fixed per-subagent overhead is paid `O(units)` times,
and the coordinator↔subagent chatter scales with the unit count.

Units are already packed to ~60KB (`unit_pull_cap`) to amortise per-*call* Haiku
overhead, so the remaining fat is the **number of subagent spawns** and the
**per-unit coordinator dispatch**, not unit size.

Goal: reduce both wall-clock and token cost of an enrichment run, equally.

## Approach: work-stealing drainer pool

Replace "one subagent per unit" with a small fixed pool of **looping drainers**. The
coordinator spawns `N` drainers in one message; each drainer loops — claim a unit,
extract, push, repeat — up to `K` units, then exits. The coordinator re-spawns a wave
while units remain. Spawns drop from `O(units)` to `O(units / K)`; the coordinator
leaves the per-unit loop entirely.

**Defaults:** `N = 10` drainers per wave, `K = 5` units per drainer. `K = 1` reproduces
today's one-per-unit behaviour, so `K` is a safe dial. Lease TTL stays 15 min.

### Why K is capped (the one trade-off)

A looping drainer accumulates each pulled unit's message bodies in its *own* context,
so per-turn cost and context-limit risk rise with `K`. `K = 5` amortises the
cold-start over several units while keeping a single drainer's context bounded
(≈5 × 60KB). The **coordinator's** context stays flat exactly as today — drainers hold
the bodies, the coordinator only sees counts.

## Components

### New MCP tools (additive)

Both live in `mcpbrain/mcp_server.py` alongside the existing enrich tools. The
existing `brain_enrich_units`, `brain_enrich_pull`, `brain_enrich_advance`, and
`brain_enrich_push` are **kept unchanged** — the general-purpose self-contained path
(`units` → `pull(with_rules=True)`) and `advance`/`push` still work as before.

1. **`brain_enrich_claim(with_rules: bool = False) -> dict`**
   - Atomically leases **one** ready (unleased or stale-leased) unit and returns its
     payload: `{unit_id, kind, block?, threads|items, context}` — the same shape as
     `brain_enrich_pull`, i.e. `units` + `pull` folded into a single MCP round-trip.
   - Returns `{empty: true}` when no claimable unit remains.
   - `with_rules` defaults `False` (the `enrich-batch` subagent carries the rules in
     its system prompt); a general-purpose caller may pass `True` to get the rules
     inlined, matching `pull`'s contract. Same `_PULL_SOFT_LIMIT` context-trim as
     `pull`.
   - **Atomic lease:** acquire via exclusive create (`os.open(claim_path,
     O_CREAT | O_EXCL)`) instead of the current non-atomic `Path.touch()`. If the
     claim file already exists, stat its mtime: if older than `_LEASE_TTL_S` (stale —
     crashed drainer), reclaim it (`os.utime` to now) and take the unit; otherwise
     skip to the next candidate. This closes the double-lease race that N concurrent
     drainers would otherwise hit (today's `units` shares that race; it only wastes
     work because drain-apply is idempotent, but the pool makes it likely).
   - Scans candidate unit files in sorted order (same as `units`), returning the first
     it successfully leases.

2. **`brain_enrich_pending() -> dict`**
   - Returns `{pending: N}` — the count of currently claimable units (files present,
     not under a live lease). **Does not claim.** Used only by the coordinator to
     decide whether to spawn another wave, preserving "done-ness is queue state, never
     reply text."

### Changed `enrich-batch` subagent (`plugin/agents/enrich-batch.md`)

Protocol prose changes from "process the one `unit_id` handed to you" to a bounded
drain loop. The **Extraction rules block is unchanged** (still byte-synced from
`mcpbrain/enrich_prompt.md` by `bin/sync_agents.py`; `test_enrich_agent_rules_in_sync`
still passes).

New protocol:
1. Load tools once: `ToolSearch("select:mcp__mcpbrain__brain_enrich_claim,mcp__mcpbrain__brain_enrich_push")`.
   (Implementation plan will check whether these can be pre-declared on the agent to
   drop this turn; if not, one `ToolSearch` per drainer — already `N`× not `units`×.)
2. Loop up to **K = 5** times:
   a. `brain_enrich_claim()`. If `{empty: true}` → stop (queue drained).
   b. Extract per the rules (thread unit → `extractions=[…]`; block unit → the block
      answer field), exactly as today.
   c. `brain_enrich_push(unit_id=…, …)`. Confirm `{written: true}`.
3. Exit after K units or the first empty claim. No required reply format (unchanged —
   completion is queue state).

### Changed coordinator routine (`mcpbrain/routines/enrich.md`)

1. `brain_enrich_pending()`. If `pending == 0` → report `DONE: queue empty`.
2. Spawn **N = 10** `enrich-batch` drainers in one message (Task tool,
   `subagent_type: enrich-batch`, `model: haiku` explicit). Each is told: *"Drain up to
   5 enrichment units: loop claim → extract → push until you get an empty claim or have
   done 5. Act autonomously; do not ask questions."*
3. When the wave returns, call `brain_enrich_advance` (daemon drains the inbox, applies,
   deletes units).
4. Go to 1. Stop at `pending == 0`, or report `PARTIAL: units still pending — re-run to
   continue` if a wave makes **no progress** (pending unchanged → only live-leased or
   stuck units remain; the next hourly run / a re-run sweeps them once their lease
   expires — same recovery model as today).

## Data flow

```
coordinator                          drainer × N (Haiku, looping ≤K)      daemon
-----------                          ------------------------------      ------
pending() ─── {pending:N} ──────────
spawn N drainers ───────────────────▶ loop:
                                        claim() ── lease+payload ──▶ (reads units dir)
                                        extract (Haiku)
                                        push() ── inbox/<uid>.json ─▶ (writes inbox)
                                      (≤K times, then exit)
◀── wave returns ────────────────────
advance() ──────────────────────────────────────────────────────▶ drain+apply+delete
loop to pending()
```

## Error handling / safety

- **Crashed drainer:** its in-flight unit's lease is stale after 15 min → `claim`
  reclaims it on a later wave or the next hourly run. Unpushed → chunks stay
  `enriched=0` and re-queue. Identical to today's per-unit recovery.
- **Concurrent claims:** `O_CREAT|O_EXCL` create is atomic at the filesystem level
  **across processes** (its defining guarantee on the local `~/Library` store), so two
  drainers — even in different MCP-server processes — can never both acquire the same
  fresh lease. The stale-lease reclaim (`utime` after an mtime check) is the one
  non-atomic window: two drainers could both judge the same stale lease reclaimable and
  both take it. That degrades to today's idempotent double-apply (drain dedups), not
  corruption — and only for a unit whose previous owner already crashed. Acceptable;
  noted so it isn't mistaken for a new race.
- **Derailed extraction (prose, no push):** loses only the current unit; the drainer
  continues to its next claim. Better than today, where a derail also wastes the whole
  spawn.
- **Push schema guard** unchanged — a non-list `extractions` or an empty push is
  rejected at the tool boundary (existing behaviour).
- **Overlap with the hourly cycle / a manual backfill:** two coordinators both spawning
  drainers is safe — claims serialise access; each unit goes to one drainer.

## Testing (TDD the tools; prompts are prose)

- `brain_enrich_claim`: returns one unit's payload and leases it; a second `claim`
  returns a *different* unit; `{empty: true}` once all are leased; `with_rules=True`
  inlines the rules and `with_rules=False` omits them; `_PULL_SOFT_LIMIT` trim applies.
- Atomic lease: two back-to-back claims never return the same `unit_id`; a claim whose
  lease file is older than `_LEASE_TTL_S` is reclaimed (stale-lease path); a fresh lease
  is skipped.
- `brain_enrich_pending`: counts claimable units without leasing (a `pending()` call
  does not change what a subsequent `claim` can take); reflects leases (a leased unit is
  not counted until its lease goes stale).
- Existing `pull`/`units`/`push`/`advance` tests stay green (unchanged tools).
- `test_enrich_agent_rules_in_sync` stays green (rules block untouched).

## Out of scope

- Removing `brain_enrich_units`/`brain_enrich_pull` (kept for back-compat and the
  self-contained general-purpose path).
- Daemon-side (`claude -p` / API) extraction — same cold-start problem, loses the
  subagent fan-out; not pursued.
- Making N configurable via `config.json` — N/K are routine-prompt literals (matching
  today's "~12 fan-out"); `K` gets an env override (`MCPBRAIN_ENRICH_UNITS_PER_DRAINER`)
  mirroring `MCPBRAIN_ENRICH_UNITS_BATCH`.

## Files touched

- `mcpbrain/mcp_server.py` — add `make_brain_enrich_claim`, `make_brain_enrich_pending`,
  register both in the tool list + `_call` dispatch; factor the lease/scan helper shared
  with `brain_enrich_units`.
- `plugin/agents/enrich-batch.md` — protocol prose → drain loop (rules block unchanged).
- `mcpbrain/routines/enrich.md` — coordinator loop → pending/spawn-pool/advance.
- Tests: new `tests/test_enrich_claim.py` (or extend an existing enrich tool test).
- Version bump (five files + `uv.lock`) + CLAUDE.md state at release time.
