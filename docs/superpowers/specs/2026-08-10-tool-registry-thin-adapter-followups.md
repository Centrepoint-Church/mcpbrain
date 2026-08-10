# Tool registry + thin adapter — follow-ups backlog

Snapshot as of: Task 9 complete, Task 10 about to dispatch (2026-08-10). Everything here was
found and explicitly deferred while executing
`docs/superpowers/plans/2026-08-04-tool-registry-and-thin-adapter.md` — none of it blocked the
plan's own tasks, and none of it is fixed yet. This doc is the place that catches it so it
doesn't only live in `.superpowers/sdd/2026-08-04-tool-registry-and-thin-adapter/progress.md`,
which is workspace scratch and gets deleted when the plan finishes.

Not exhaustive going forward — Tasks 10-13 will likely surface more. Append rather than
replace.

## Needs its own spec + plan before being done

These are substantial enough that a fresh brainstorm/spec/plan pass is the right next step, not
a quick patch.

### 1. The backup fix (`backup.snapshot()`)

Named out-of-scope by Task 12 itself. WAL contention (Finding 3) has two causes: (W) concurrent
MCP writers, now closed by this plan's routing work, and (R) a single held read transaction —
`brain_graph`/`brain_actions` read transactions already outlive the 5000ms `busy_timeout` on the
live store. Phase 4 relocates (R) into the daemon; it does not fix it, and it does not fix
backups. The actual fix is specific: distinguish "frames remained in the WAL" from "the WAL
could not be truncated" (`checkpointed_frames == log_frames` in the observed live failures means
the data was already safe — only the reset failed), and/or checkpoint under a longer
`busy_timeout` while holding the bulk lock. 3 of 98 real backup failures (3%) are this cause;
57% are disk-space, 39% network/transport — size the fix's priority accordingly.

### 2. Fine-grained daemon routing for `brain_graph` and `brain_draft_context`

Task 10's resolution (2026-08-10): both tools stay executing locally in the MCP server — a
named, documented exception, not a bug — because their progress notifications
(`on_hop`/`on_stage`) call `ctx.session.send_progress_notification(...)`, which needs the live
MCP `ServerSession` that only exists inside the MCP server's own event loop for the duration of
one request. The daemon's `/api/tool` is a single blocking call with no channel back mid-call,
so routing either tool as one round-trip (like the other 9) silently drops progress —
concerning for `brain_draft_context` specifically, whose `critique` stage runs a ~30s blocking
subprocess with `on_stage` existing precisely to keep the client's idle timer alive during it.

The architecturally correct long-term fix, confirmed feasible by reading the actual code (not
just theorized):
- **`brain_graph`**: the BFS loop's carried state (`visited`, `edges`, `frontier` —
  `mcpbrain/tools.py:293-314`) is small, JSON-serializable string sets/dicts with nothing
  non-serializable. Each hop only needs the prior frontier plus the Store. This makes brain_graph
  a genuinely clean case for per-hop daemon round-trips: the MCP server keeps the loop and fires
  progress between hops exactly as today; each hop's Store work becomes its own `/api/tool`-style
  call.
- **`brain_draft_context`**: only 2 of its 4 stages (`email_lookup`, `samples`) touch the Store
  at all (`mcpbrain/draft.py:91,100` — two small `SELECT`s). `voice_rules` reads a local file
  (`context/voice.md`), no Store; `critique` shells out to a subprocess with the already-gathered
  dict, no Store either (`mcpbrain/draft_critic.py:214`, signature takes `draft_text`/`ctx`/`home`
  only). So the real fix here isn't "route the whole tool" — it's exposing just the 2 Store reads
  as fine-grained daemon calls, leaving `voice_rules`/`critique` exactly where they are today.
- No measured pure-loopback-HTTP overhead exists in this repo, but it's stdlib
  `ThreadingHTTPServer` on `127.0.0.1` with small JSON payloads — the extra round trips this
  implies are expected to be low-single-digit-ms, noise against `brain_graph`'s 6.3s/8.3s
  baseline or `brain_draft_context`'s 30s critique.
- Cost of doing this now instead of later: it's new API surface beyond the uniform
  "one `/api/tool` call per named tool" pattern Task 9 established for the other 10 tools (either
  new fine-grained daemon endpoints, or a narrow Store-proxy the MCP server can call through) —
  real design work, not a mechanical swap. That's why it's deferred rather than folded into
  Task 10.
- Until this lands, the MCP server's "holds no Store handle when the flag is on" claim
  (Task 10 Step 2's test) has a carved-out exception for exactly these two tools — the test
  needs to special-case them, not just assert zero unconditionally.

### 3. MCP server process lifecycle (pre-existing, adjacent, not started)

Already specced at `docs/superpowers/specs/2026-08-04-mcp-server-process-lifecycle.md`, no
implementation plan yet (per `CLAUDE.md`'s own tracking). A running MCP server executes its
start-time code forever — nothing signals it on a wheel update — so any shipped fix (including
everything in this plan) reaches a given user only on their next Claude Desktop restart, not when
the daemon or wheel updates. This plan's thin-adapter work reduces what's frozen at start time
(no more Store construction, no more stale handler logic for the 9-11 routed tools) but does not
solve the signaling problem itself.

## Small, mechanical fixes — deferred to final-review triage, not yet actioned

Flagged during task reviews as Minor (non-blocking) or noted by the controller; none entered a
fix loop. Worth sweeping in the Phase 4 final review or a dedicated cleanup pass.

**From Phase 1:**
- `tool_registry.py`'s `_freeze` coerces `tuple` → `list` — would break the identical-
  re-registration equality check if two call sites for the same tool differed only in that
  regard.
- `_isolated_registry` is a testing convention, not enforced machinery.
- `dict.__setitem__` bypasses the registry's freeze — inherent to the subclass approach,
  undocumented.
- No byte-level pin (golden file) on the 26 tool description strings — the structural
  cross-checks only assert "not blank" / "not None." Gets more valuable now that a second
  process (the daemon, via Task 9/10) also depends on the registry being stable.

**From Phase 3 (Tasks 6-8), 7 items:**
- `bin/probe_wal_contention.py`'s "no write lock is ever held" safety claim is slightly
  imprecise — the checkpointer briefly contends for the writer lock; empirically benign (no
  `busy=1`/locked errors observed during the actual probe window), but the claim overstates it.
- The `mcp_writes` arm's result is a 1-of-6 sampled hit with no in-script field explaining that.
- The lifecycle spec's Finding-3 arm table omits `mcp_mixed`, which the script runs by default.
- `test_all_24_factories_are_accounted_for`'s third assertion is tautological
  (`len(STORE_TOUCHING)==12` checked against itself).
- `discover_args`'s comment overclaims an index is used, when `ORDER BY...LIMIT 1` actually
  short-circuits on an early row.
- The brief's own Step-3 pretty-printer would `KeyError` on this JSON's actual
  `_session`/`_probe_args`/`_meta` keys (sidestepped, not fixed).
- `assert "store." in branch` also matches `draft_store.` — semantically fine, just looser than
  it reads.
- ~~Pre-existing stale cross-reference in the lifecycle spec (the "daemon cadence stalled
  since 2026-07-23" note reading as a live alternative explanation for WAL contention)~~ —
  **fixed 2026-08-10** while appending Task 12's results to the same section: added a one-line
  disclaimer marking the note stale in place, rather than deleting the historical record.

**From Task 9, 3 items:**
- `daemon.py`'s `_routed_tool_handlers()` returns the mutable cached dict rather than a copy —
  fine today (nothing outside the test suite mutates it), but corruptible by any future caller
  once it holds all 12 tools.
- `mcp_server.py`'s `run_tool` logs a stderr warning on every call while the daemon is down —
  harmless, but noisy in the fleet's MCP log under a client that retries.
- `daemon.py` builds and tears down an `asyncio` event loop per routed async call
  (`asyncio.run(result)`) — correct and justified for now, but worth revisiting once more of the
  11 remaining tools (many async) route through it, if per-call loop setup shows up in Task 11's
  latency numbers.

## Watch items — not action items yet, just things to re-check if scope changes

- The control API's 1 MiB request-body cap **now genuinely applies to `brain_gardener_apply`**:
  Task 10 routed it, and its `content` argument carries a full replacement file body, so the whole
  file crosses `/api/tool` on every call. (Corrected 2026-08-10 in the Phase 4 final review — this
  bullet previously listed it among tools that "stay local", which was written before Task 10 and
  is no longer true.) Practical risk is low: model-generated `reference/`/`context/` file bodies
  are nowhere near 1 MiB. But it is not impossible, and the failure is a *misdiagnosis*, not a
  clean error — a 413 is in `_TRANSPORT_STATUSES`, so it surfaces as `DaemonUnavailable` ("the
  daemon is not reachable — check the daemon is running") when the daemon is in fact healthy and
  simply refused an oversized body. That deserves its own tiny follow-up: either raise/skip the
  cap for `/api/tool`, or map 413 to a message that names the body size. `brain_ingest`,
  `brain_memory_write` and `brain_enrich_push` remain local, so for them the cap is still moot.
- `run_tool`'s DaemonTimeout message still tells the user a stale records-repo `.git/index.lock`
  "blocks the git-backed writes indefinitely". That is wrong (`records_write._git` runs only
  `add`/`commit`, so the lock makes git fail fast with `CalledProcessError`, which
  `brain_gardener_apply` already reports as `git busy (retry next run)`; the real low-probability
  hangs are a gpg-signing pinentry prompt and a blocking repo hook). The surrounding comments and
  docstrings were corrected in the final review; the **user-visible string was deliberately left
  alone** because changing it is a behaviour change (and `test_the_stuck_and_absent_diagnoses_
  differ_at_the_mcp_boundary` asserts on it). Fix the wording next time that message is touched.
- `mcpbrain/drain.py` (~lines 56, 619) and `tests/test_bulk_lock_fairness.py` (~lines 568, 626)
  repeat the same "stale index.lock blocks forever" claim, entirely outside this plan's touched
  files. Worth a look on its own: if the corrected understanding above is right, the argument for
  running records-kind captures *outside* the bulk section rests on a premise that isn't true,
  which may be worth re-examining independent of anything in this plan.
- `ControlClient.TOOL_CALL_TIMEOUT_S=120.0` (not the tray's 5s default) already covers
  `brain_graph`/`brain_draft_context`'s measured slow paths for the 9 tools that did route in
  Task 10 — revisit if a future routed tool has a legitimately longer tail.
- **Task 11's latency gate was measured against an effectively idle daemon, not a loaded one** —
  the final whole-branch review flagged this: the 0.7.105 incident this whole plan traces back to
  was specifically about drain pinning the process and starving control-API threads under GIL/DB
  contention, and Task 11's window happened to be largely quiet (its own cadence work only
  surfaced at shutdown). Josh's explicit decision (2026-08-10, before release): do NOT re-measure
  under load — accept the existing mitigations (`TOOL_CALL_TIMEOUT_S=120` bounds a bad case to
  "slow" not "broken"; 0.7.105's expression indexes already removed the specific full-scan cause;
  `BULK_LOCK_YIELD_S` bounds starvation) and the `tool_exec_in_daemon` kill switch as the field
  response if `brain_context`/`brain_actions` recall latency regresses under real drain load
  post-release. If that happens, this is the first thing to check.

**Deferred Minors from the Phase 4 final whole-branch review (2026-08-10), triaged "not now":**
- Routed reads now execute on the daemon's *writable* handle, so a read-only tool no longer has
  SQLite-level `read_only=True` enforcement behind it — the guarantee is now "the handler only
  reads", by code inspection rather than by the connection mode.
- `/api/tool`'s `except ValueError` is slightly too broad: a genuine `ValueError` raised deep
  inside a handler is reported as a 400 "invalid arguments"-shaped refusal rather than a 500.
- `tests/test_tool_exec_routing.py::_daemon`'s injected-store branch does not call `store.init()`
  (only the default branch does) — latent, since no current caller passes a bare store.
- Routed async handlers run on asyncio's *default* executor via `to_thread` on the MCP side and a
  fresh `asyncio.run` per call on the daemon side; the default thread-pool ceiling is a shared
  resource nobody has sized for this traffic.

**Deferred Minors from the re-review of the final-review fix wave (2026-08-10), triaged "not now"
— residual scope limits of the new mapping-agreement test (`tests/test_tool_exec_routing.py`
section 8), not defects in it:**
- The one-sided-drop guard (`test_the_agreement_test_sees_a_one_sided_drop`) is weaker than its
  docstring implies — its "sabotage" is post-hoc surgery on an already-recorded dict, so the
  inequality it asserts is close to tautological. The section's real non-vacuity comes from
  `_FULL_ARGS`' payloads being genuinely value-rich (verified independently by the re-reviewer),
  not from this specific guard.
- The comparison only exercises the fully-populated argument point. A divergence in the two
  sites' `.get()` *default* for an argument that's ABSENT (e.g. one side defaulting a missing
  `status` to `"open"`, the other to `""`) is invisible here — the same blind spot that motivated
  the original finding, just not fully closed by the fix (the finding as scoped asked for a
  fully-populated set, so this is per-spec, not a missed requirement).
- Factory *construction* arguments (which `store` handle, which `home` string each site wires
  into a factory) aren't compared — `daemon.py` builds its `home` via `str(app_dir())` while the
  MCP side passes its own `home` parameter through. A second duplicated-wiring surface, no
  agreement test covers it, out of scope for the finding as written.
