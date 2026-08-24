# mcpbrain — project context for Claude Code

## This is a distributed PLUGIN, not just a local app

mcpbrain ships to other users as a **Claude Code plugin + a pip-installable package**.
There are **three repos** (all under the `Centrepoint-Church` org), and a change only
reaches users when the relevant ones are **pushed/released** — committing here and
running `uv tool install` only affects *this* machine.

| Repo | What it is | How users get it |
|---|---|---|
| **mcpbrain** (this repo) | Python package (`mcpbrain/`), plugin source assets (`plugin/`), routines (`mcpbrain/routines/`), tests | source of truth; not installed directly |
| **mcpbrain-dist** (`../mcpbrain-dist`) | PEP 503 wheel index on GitHub Pages (`centrepoint-church.github.io/mcpbrain-dist/simple/`) | `uv tool install --index` pulls the wheel; installed daemons **auto-update daily** from here (`update.py`) |
| **mcpbrain-plugin** (`../mcpbrain-plugin`) | Public Claude Code plugin (agents/skills/hooks/commands + `.claude-plugin/` + `mcpb/`), mirrored from this repo's `plugin/` | org **plugin marketplace** (Claude Team/Enterprise settings) |

## CRITICAL: local work ≠ shipped to users

- `git commit` here → local until `git push`. `uv tool install --force .` and
  `launchctl kickstart` → **this machine only.** Neither changes what any other user installs.
- Do **not** push or release without an explicit instruction — shipping is an all-users action.

## Releasing to prod

**`docs/RELEASE-RUNBOOK.md` is the authoritative, step-by-step procedure** (the *do*);
`docs/DISTRIBUTION.md` is the *why*. Follow the runbook. The things that are easy to get
wrong and MUST be right:

- **Version lives in FOUR files, keep them equal:** `pyproject.toml`, `mcpbrain/__init__.py`,
  `plugin/.claude-plugin/plugin.json`, `plugin/.claude-plugin/marketplace.json`. The plugin
  manifests are the marketplace's version — **easy to forget**; bumping only
  `__init__.py`/`pyproject.toml` ships a wrong plugin version. (`uv.lock`'s
  mcpbrain entry is also kept in step but isn't a marketplace source-of-truth.)
- **There is no `.mcpb` Desktop Extension — it was REMOVED 2026-08-24, do not reintroduce it.**
  It registered a server under the same name (`mcpbrain`) as the connector `mcpbrain setup`
  writes into `claude_desktop_config.json`, **won** that collision, then failed to launch:
  mcpb's `server.type: "uv"` drops the `--from mcpbrain mcpbrain` argv, so Desktop ran a bare
  `uv mcp-server` → `unrecognized subcommand`, taking the brain tools down on a machine where
  they had been working. Its manifest was unresolvable anyway (no `--index`, and mcpbrain is
  not on PyPI). `mcpbrain setup` is the ONLY supported connector registration; the plugin
  deliberately bundles no server. Guarded by `tests/test_plugin_manifest.py`
  (`test_no_mcpb_extension_dir`, `test_mcp_json_bundles_no_server`).
- Release = push `mcpbrain` source → `python bin/release.py --dist ../mcpbrain-dist` then
  commit+push `mcpbrain-dist` (mind the stale-wheel gotcha in the runbook) → sync `plugin/`
  into `mcpbrain-plugin` via `git archive HEAD:plugin` + push. All three repos end at the
  same version.
- If extraction rules changed, run `python bin/sync_agents.py` first (keeps
  `plugin/agents/enrich-batch.md` byte-identical to `mcpbrain/enrich_prompt.md`).

## Shipping caveats

- Some feature flags (`schema_grounding`, `write_time_dedup`) still default **OFF** in
  `config.py` — releasing the wheel does NOT activate them; they need config + real-data validation.
- The **Q1 salience gate (`salience_gate`) is the exception: validated on the live store
  (~40% of the corpus gated as tabular/low-signal with no recall impact) and flipped default
  **ON** in 0.7.65** (commit `cfe0338`). It ships active for all users. Source-aware
  `should_enrich()` in `prepare.py` cold-marks promotional email + tabular/short Drive docs
  before extraction; cold-marking is reversible (chunks stay embedded/searchable). The aggressive
  `salience_require_drive_mention` sub-flag remains opt-in OFF.
- **Cold-marking is an ENRICHMENT-cost optimization, NOT a retrieval filter (0.7.72).** A
  one-shot backfill grew the cold set to ~40% of the corpus and HALVED gold recall@10
  (0.750→0.350) because `daemon.search` was excluding cold chunks from recall. Fixed in 0.7.72:
  cold-exclusion is decoupled from `tiered_memory` into `recall_excludes_cold` (**default OFF**),
  so cold chunks stay in recall (recall restored to 0.750, MRR 0.556) while still being skipped
  for graph-extraction. `tiered_memory` now controls only the core-tier prepend.
- **SQLite optimisation (2026-08-24 plan, Tasks 1-9): store rebuild is BUILT and
  GATED, not yet run against the live store.** `bin/optimise_store.py`'s
  `rebuild()`/`main()` performs a full out-of-place rewrite of `brain.sqlite3`
  (page_size 4096→8192, contentless FTS5, STRICT tables + FK
  constraints, a trigram index scaffold) behind an attended CLI (`--yes` gate,
  verified encrypted snapshot before anything is written, `--swap` that
  retains the old file, and a sidecar-aware `--rollback --yes`) — see
  `docs/RELEASE-RUNBOOK.md` § 7 for the full procedure. **This is attended and
  NEVER automatic**, same posture as `bin/consolidate.py`: nothing in the
  daemon's cadences calls it. Gated on a real copy of the live store (2.62 GB,
  170,705 chunks) rebuilt to 1.495 GB (57.1% of original), `has_stat1`
  false→true, `integrity_check`/`foreign_key_check` clean, row-count
  reconciliation clean. The four 0.7.105 benchmark latencies, warmed:
  `doc_ids_for_messages` ~690ms→~50-100ms, `thread_chunks` ~234ms→~1ms,
  `chunks_for_file` ~228ms→~0.8ms, `inbound_chunks_since` ~376ms→~135ms — the
  live store currently has **no** `sqlite_stat1` at all (`has_stat1: false`),
  so these expression-index queries are on a slow, stats-free plan today;
  the rebuild's mandatory `ANALYZE` is what fixes that, independent of the
  schema changes themselves. **Gold gate: recall@10 0.750 / MRR 0.514 is the
  plan's non-negotiable floor; both the pre-rebuild copy (0.800/0.591) and the
  rebuilt copy (0.850/0.599) clear it, and the small pre→post movement is
  EXPLAINED, not a construction defect — but the mechanism is DATA drift from
  a metadata-only writer, not a code-versioning gap.** `contextual_prefix()`'s
  `folder_path` clause has existed since the initial commit (317ea4d,
  2026-06-02); `FTS_CONTEXT_VERSION` was introduced seven weeks later
  (731a620, 2026-07-22, Phase C), so the clause cannot be what escaped that
  version. `folder_path` metadata itself only started being STAMPED onto
  Drive chunks on 2026-07-28 (`b7bf024`, "C5"). The real gap:
  `Store.patch_chunk_metadata` (`mcpbrain/store.py`) writes
  `UPDATE chunks SET metadata=?` and returns — it never touches the
  `fts_chunks` mirror and never resets `fts_context_version`.
  `mcpbrain/sync/drive.py`'s re-sync path calls exactly this
  (`store.patch_chunk_metadata(c.doc_id, **c.metadata)`) whenever a Drive
  file's content is unchanged but its metadata should refresh — so a chunk
  that already carried `fts_context_version=1` (from before `folder_path`
  existed) silently acquires `folder_path` in `chunks.metadata` on its next
  Drive sync, while its `fts_chunks` row keeps the old, folder-less text
  forever: the version was never invalidated, so
  `reindex_fts_batch`'s `fts_context_version < FTS_CONTEXT_VERSION` check can
  never re-catch it. 7,343/170,705 chunks (4.3%) carry this drift today. The
  rebuild's `_rederive_fts` unconditionally regenerates FTS text for every row
  from current metadata, incidentally correcting all 7,343 in one pass.
  Confirmed directly on the ONE gold case that flipped
  (`gdrive_3_capes_budget_jan_to_sept`): its cross-acceptable P&L chunk
  (`gdrive-1IfsM_VJGu81hOY_8DeEvJ6_-g0ib8FZ0-0`) is one of the 7,343 — its
  stored FTS text lacked "in My Drive/…/2024 - Capes Church/Board
  Meetings/2026 Capes Community Board Meetings/2026.07.02 Capes Board Pack",
  a folder path that itself contains "Capes"/"Community"/"Board" — exactly
  the query's own terms. Re-deriving it directly boosted this document's
  keyword-search rank into the gold set's top 10 (a specific, direct token
  match, not a diffuse corpus-wide BM25 stat shift). Vector search and the
  `recall_max_distance` injection gate were spot-checked identical pre/post
  on real queries (unaffected — vectors are copied byte-exact). **Follow-up,
  not fixed here (out of this docs-only task's scope):**
  `patch_chunk_metadata` (and any other metadata-only writer) needs to
  refresh the `fts_chunks` mirror, or at least reset `fts_context_version` to
  0, whenever it touches a metadata field `_fts_text`/`contextual_prefix`
  reads — otherwise the exact same drift resumes on the very next Drive sync
  after any future rebuild. Bumping `FTS_CONTEXT_VERSION` once would only
  clear today's backlog, not stop it recurring.
  **Metadata is JSON TEXT, NOT JSONB — Task 6 delivered the
  `store._meta_extract()` single-source-of-truth pattern, not a storage-format
  change, and the earlier "JSONB metadata" framing overstated it.** What is real
  and valuable: every expression INDEX in `init()` and every one of the ~24 query
  read sites that pull a value out of `chunks.metadata` now build their SQL
  through one function, so an index's expression and its query's expression
  cannot independently drift — which is exactly the 0.7.105 full-`SCAN chunks`
  failure mode. That function emits **`json_extract` unconditionally**, never
  `jsonb_extract` and never version-dependent: `CREATE INDEX IF NOT EXISTS` keys
  on the index NAME, so an existing store's five expression indexes
  (`idx_chunks_msgid`/`fileid`/`threadid`/`eventid`/`inbound_date`) are NOT
  rebuilt when the fragment changes, and SQLite does not match a `jsonb_extract`
  query against a `json_extract` index. The branch briefly did emit
  `jsonb_extract` behind a 3.45 guard; measured on the real live store that
  killed all five indexes on daemon restart (message_id 0.0ms→233.8ms, thread_id
  0.0ms→215.9ms — full scans), i.e. it would have reinstated the 0.7.105 outage
  fleet-wide, silently and with correct-but-slow results, the moment the wheel
  shipped. Fixed before merge, and pinned by
  `tests/test_metadata_jsonb.py::test_reinit_on_existing_store_*` (the missing
  test class: re-`init()` an ALREADY-init'd store and assert the plan is still
  `SEARCH … USING INDEX`; every prior index test built a FRESH store, where DDL
  and query text necessarily agree). Cost of staying on `json_extract`: 15.6ms
  vs 14.9ms over a 50k-row scan — noise. True JSONB *storage* was investigated
  and is **structurally foreclosed** by Task 7: `metadata` is declared `TEXT`
  inside a **STRICT** `chunks` table, so a `jsonb()` blob write is rejected
  outright, and `bin/optimise_store.py`'s `_copy_all` copies `metadata`
  verbatim. Enabling it would need that column loosened to `ANY`/`BLOB` plus a
  dedicated rebuild — a separate follow-up, not this plan. `jsonb_supported()`
  remains defined as an unused version-guard utility. **Not
  yet run against any user's actual live store** — that is a separate,
  explicit, attended operation per machine, to be scheduled deliberately.
- **Current state (2026-08-04):** the **five** version files (+ `uv.lock`) are at `0.7.113`,
  **released** — source `51e665f`, dist `546ef40`, plugin `2feedd8`; the index serves only
  `mcpbrain-0.7.113-py3-none-any.whl`. **0.7.113 is the `mcp` 2.x migration + backup hardening,
  and it was URGENT:** the published 0.7.112 wheel was built with an unbounded `"mcp>=1.2"`, and
  `update.py` scopes its index override to `mcpbrain=` only — so every machine's daily
  auto-update resolved `mcp` fresh from PyPI with `--upgrade` and could pull **2.0.0**, which
  deleted the 1.x low-level decorator API (`@server.list_tools()` etc.) with no shim and crashed
  the MCP server on every connect (`AttributeError: 'Server' object has no attribute
  'list_resources'`). That happened live on the author box on 2026-08-04.
  **What shipped:** the port to 2.x's `on_*` constructor-kwarg handlers returning full
  `types.*Result` models, pinned `mcp>=2.0,<3` — **plus a re-implementation of the input
  validation 2.x silently drops** (1.x's `call_tool(validate_input=True)` was the default and the
  code relied on it implicitly; 2.x's low-level server validates *nothing*, so `tool_schemas()`
  is now the single source both the advertised list and `_validate_tool_arguments` read, and it
  must never inject defaults or mutate the args dict — `brain_enrich_push` distinguishes an absent
  `extractions` from an empty one). Validation failures return `isError`, **not** a raised
  exception: a bare `ValueError` gets `code=0` plus a ~20-line traceback into the fleet's MCP log.
  2.x is **dual-era**, so this stays wire-compatible with clients negotiating `2025-11-25` (which
  is what Claude Desktop still sends — verified). Surface upgrades: safety annotations on all 26
  tools, `outputSchema` on 13, MCP **prompts** (4 routines + `draft-reply`, whose body is canonical
  at `mcpbrain/prompts/draft-reply.md` and generated into the plugin SKILL.md by
  `bin/sync_agents.py`), `resources/list_changed`, and progress notifications for `brain_graph`
  /`brain_draft_context`. Backup side: resumable upload (the old multipart path flattened a 4.24 GB
  body into one `BytesIO` — **97 failures vs 52 successes** since 2026-06-25), streaming
  encryption, `shutil.disk_usage` instead of POSIX-only `os.statvfs` (**every Windows backup
  previously raised, was swallowed, and silently never ran**), abort-safe close, archive-id frames
  as **v3 with a legacy v2 read path** (the v2 archives already on Drive stay restorable — a
  same-magic header change would have made them *silently misparse*), and `num_retries=0` because
  googleapiclient cannot re-seek a retried chunk. Verified live end-to-end from the **published**
  wheel: real Desktop handshake incl. `prompts/list`, and a full backup cycle on the real 11.92 GB
  store (4.25 GB v3 artifact, byte-exact upload, peak RSS 226 MB, peak temp exactly one store
  copy). Evidence: `docs/superpowers/specs/2026-08-04-release-verification-record.md`.
  **Open:** the **Windows HARDWARE QA GATE remains OPEN** — do not onboard Windows users. Two
  follow-ups are specced, not done: `docs/superpowers/specs/2026-08-04-mcp-server-process-lifecycle.md`
  (a live MCP server runs its **start-time code forever** — nothing signals it on update — so a
  shipped fix reaches each user on their *next client restart*, not when the wheel lands; this also
  staggered the 2026-08-04 outage) and consolidating the now-**four** parallel per-tool mappings
  (`tool_schemas`/`_TOOL_DESCRIPTIONS`/`tool_annotations`/`tool_output_schemas`) into one
  `TOOL_SPECS` record — deliberately deferred so it wouldn't invalidate this release's live
  verification. Earlier: **0.7.110 was a session-context +
  actions-hygiene release.** Three threads, all driven by observed live-store behaviour:
  **(1) SessionStart policy block** — the MCP server's connect-time `instructions` already ask
  Claude to use the brain tools/@-resources, but that text gets lost in a long system prompt and
  in practice the tools were used only when explicitly asked for. `session_hooks._TOOL_REMINDER`
  now restates it at high salience (resources-vs-tools distinction for
  voice/identity/preferences, the ToolSearch select string for the deferred recall tools, and the
  unprompted write triggers + a "never hand-edit daemon-managed records files" guardrail).
  `session_start()` is split so this timeless framing prints on **every** firing **including
  `compact`** (compaction is exactly when early-context instructions are at risk), while the
  hot.md continuity/open-actions **state** stays compact-silent as before.
  **(2) Open-actions selection fix** — `_open_actions` appended every bucket in order then
  truncated to 8 lines, so a long `overdue` list made `due_today`/`upcoming` **structurally
  unreachable**; on the live store that was the normal case (20 overdue, all 2018-2023, vs 3
  genuinely due today) so the whole budget was dead rows and today's work was never injected.
  Same bug class as the 0.7.103 expansion fix (truncating an ordered set after the fact).
  `_select_actions` now reserves per-bucket quota (3/3/2, unused redistributed), de-dupes on
  normalised text, and drops rows past the archive cutoff. **The display cutoff must not be
  stricter than the sweep** — the dashboard hands the hook the 20 **oldest** overdue rows
  (`dashboard.py` sorts ascending then caps), so a tighter filter drops every row it is given and
  starves the bucket; a guard test pins `_STALE_AFTER_DAYS` to
  `archive_stale_actions(overdue_cutoff_days=…)`.
  **(3) Actions kept clean, automatically** — `archive_stale_actions` had **no automatic caller
  at all** (only a manual `bin/consolidate.py` run) and deliberately spared *every* dated action
  ("an overdue task is high-signal"). True for a recent slip, not a 2018 deadline. It gains a
  second rule for **dated but long-dead** rows (default **730** days — far more generous than the
  120-day undated rule, so the high-signal recent-overdue case is still spared and the original
  test contract holds), marked `resolved_by='ttl_overdue'`; new **`archive_duplicate_actions`**
  collapses re-extracted copies keeping `MIN(id)`, marked `'dedup'` (blank fingerprints are
  **never** grouped — many unrelated rows share an empty string). Both are wired into a new daily
  **`action_hygiene`** cadence. Both are status-only and reversible; nothing is deleted and the
  distinct markers make each sweep separately auditable/undoable. Applied to the live store:
  open actions **3,529 → 3,069** (38 long-dead + 422 duplicates), zero long-dead and zero
  duplicate groups left. Also **`prompt_recall` timeouts raised** (`_TIMEOUT_S` 1.2→3.0s,
  `/api/core` 0.5→2.0s): a cold daemon call (first hit after ~45s idle, i.e. the normal gap
  between reading a reply and typing) measured 1.3-2.6s live, so per-prompt recall was **silently
  timing out** on a realistic share of turns. **`recall_max_distance` was investigated and
  deliberately NOT changed** — raising 0.80→0.85 recovers **zero** on-topic queries (both misses
  measured 0.864/0.878, above 0.85) while taking off-topic noise 1/8 → 5/8; note the gold harness
  calls `hybrid_search` directly so it never exercises this gate (same blind spot as the removed
  sufficiency gate). ~~**Known/open:** daemon cadence passes appear **stalled** since
  2026-07-23~~ — **RESOLVED, and this note was stale: do not re-derive it.** Verified on the live
  install 2026-08-04: `_backfill_active` is now checked **per-pass** (ten individual guards) rather
  than as one wholesale early-return in `_run_periodic_passes`, which now iterates
  `_CADENCE_PASSES` with each `_run_X` individually wrapped so one raise cannot block the rest.
  The contention that motivated finding #3 was also addressed: `BULK_LOCK_ACQUIRE_S` bounds the
  bulk-lock acquire so a stuck pass can't park the watchdog that exists to detect it, and
  `BULK_LOCK_YIELD_S` fixes the CPython lock-unfairness that previously starved the gated passes
  ("183 consecutive skip warnings, live"). **Evidence the cadences run:** `action_hygiene` — the
  0.7.110 addition this note claimed was blocked — logged on 2026-07-29, twice on 2026-08-03, and
  2026-08-04 11:06; `decay_pass`/`tier_pass` at 2026-08-04 20:25. Occasional skip lines are
  by-design (a not-due or lock-contended pass stays due and retries), not a stall. The **Windows
  HARDWARE QA GATE from 0.7.97 remains OPEN.** Earlier: **0.7.109** shipped the findings pipeline + closure durability +
  memory keep window; **0.7.108** zero-touch onboarding + Windows install fixes. **0.7.107 replaces enrichment's
  one-subagent-per-unit fan-out with a work-stealing drainer pool** — cutting subagent cold-start
  and coordinator↔subagent comms overhead. Two new MCP tools: **`brain_enrich_claim`** atomically
  leases ONE unit and returns its payload in a single call (folds `units`+`pull`; `O_CREAT|O_EXCL`
  lease so concurrent drainers never double-claim a fresh unit, with stale-lease reclaim after the
  15-min TTL), and **`brain_enrich_pending`** returns a non-claiming `{pending: N}` for the
  coordinator's loop. The `enrich-batch` subagent now **loops `claim → extract → push` up to 5
  units** then exits (bounding a drainer's context growth); the `enrich.md` coordinator routine
  spawns a **pool of 10 Haiku drainers per wave** (`pending` → spawn → `advance` → repeat until
  `pending: 0`, `PARTIAL` on a no-progress wave) instead of dispatching one subagent per unit. Net:
  subagent spawns drop O(units)→~O(units/5) (fewer cold-starts + system-prompt/ToolSearch payments =
  token AND wall-clock), coordinator leaves the per-unit loop. Existing `brain_enrich_units`/`pull`/
  `push`/`advance` are unchanged (retained for the self-contained general-purpose path); a Task-1
  refactor factors shared lease/`_unit_payload` helpers (also closing `units`' latent double-lease
  race). Built subagent-driven + adversarially reviewed (opus final review: READY TO MERGE, no
  Critical/Important); full suite 2424 pass. A planned `MCPBRAIN_ENRICH_UNITS_PER_DRAINER` env
  override was **dropped as inert** (an LLM can't read env from prompt text — K stays the "5" literal
  in the prompt). Spec/plan: `docs/superpowers/specs/2026-07-23-enrich-drainer-pool-design.md`,
  `docs/superpowers/plans/2026-07-23-enrich-drainer-pool.md`. The **Windows HARDWARE QA GATE from
  0.7.97 remains OPEN.** Earlier: **0.7.106 removed the LLM sufficiency gate.** The gate (`sufficiency.py`, `daemon.search`) spawned the `claude` CLI as a subprocess on
  **every** recall (both `brain_search` and UserPromptSubmit auto-injection) to classify each hit
  relevant/irrelevant — **~6s of pure CLI cold-start per recall**. Removed because it (a) duplicated
  the downstream LLM (recalled chunks are injected into a prompt the model reads anyway; a
  permissive "err toward true" pre-filter re-does 6s slower what the consumer does for free), (b)
  was **never in the gold-eval harness** (the recall@10 0.750 / MRR 0.514 numbers were measured
  without it; it defaulted ON in a 0.7.65 batch, not on its own evidence), and (c) is redundant with
  the cheap absolute `recall_max_distance` gate (kept — drops genuinely off-topic queries at zero
  LLM cost). Deleted `sufficiency.py`, `config.sufficiency_gate_enabled`, the `daemon.search` call,
  and `test_sufficiency.py`; regression guard in `test_recall_gate`. Recall latency drops ~6s. The
  **Windows HARDWARE QA GATE from 0.7.97 remains OPEN.** Earlier: **0.7.105 was a chunk-metadata
  indexing perf fix.** Recall (`brain_search`/`brain_actions`/`brain_context`) was timing out because the
  daemon's drain cycle pinned the single process for minutes, starving the control-API threads
  (GIL + DB contention). Root cause: several `store` queries filter chunks on
  `json_extract(metadata, …)` with **no matching index → full `SCAN chunks`** over the ~108k-chunk
  live store. Fixed with four expression indexes created in `init()` (one-time ~7.5s at daemon
  startup) — on `metadata.$.message_id`, `$.file_id`, `$.thread_id`, and the
  `COALESCE(date,date_iso)` expr — plus two query rewrites where an index alone wasn't enough:
  **`doc_ids_for_messages`** `OR`→`UNION` (SQLite won't union expression-indexes across an `OR`, so
  the `OR` form still planned as `SCAN`; each `UNION` arm is single-path and index-backed), and
  **`chunks_for_file`** `doc_id LIKE 'gdrive-<id>-%' ESCAPE`→`metadata.$.file_id` match (`ESCAPE`
  disables the LIKE-to-index optimisation; proven equivalent over 200 live file_ids incl. base64url
  LIKE-metachar ids). Per-call latency on the live store, verified through the real methods:
  `doc_ids_for_messages` 2979ms→0.2ms, `thread_chunks` (on the fleet-wide `retrieval_expand` recall
  path) 4287ms→10ms, `chunks_for_file` 432ms→2.8ms, `inbound_chunks_since` (waiting-on sweep, every
  cycle) 2882ms→42ms; identical results. TDD'd; 230 impacted tests + ruff clean. Left unindexed
  deliberately: `email_mentions` (opt-in salience sub-flag OFF; its `text LIKE` isn't indexable),
  `delete_calendar_chunks_after`/`doc_ids_for_drive` (infrequent maintenance/GC). The **Windows
  HARDWARE QA GATE from 0.7.97 remains OPEN.** Earlier: **0.7.103 was an adversarial-review
  hardening pass over the recall-quality + fleet work** (4 parallel reviewers → 7 findings + a
  simplicity sweep, each fix TDD'd + reviewed; full suite 2424, gold held 0.750/0.514). Fixes:
  **(expansion)** unified to one char budget owned by `expand_hits` that also bounds the first
  parent, and moved ALL truncation/count-capping before `_head_tail` so the injection consumer no
  longer front-truncates an ordered set and drops the 2nd-best parent; span-stitch gap marker.
  **(enrichment)** `drain` now drops `cold` chunks from the Drive file-wide `doc_ids` resolve
  before apply/mark, so a Drive extraction only marks the hot chunks it actually covered (was
  over-marking cold siblings → broke cold-reversibility on 127 live files); shared `_chunk_key`.
  **(contextual BM25)** the `fts_context_version` marker is stamped at write time and encodes
  whether the prefix was actually applied (raw writes stay v0), so a contextual_retrieval OFF→ON
  toggle self-corrects and fresh rows aren't reprocessed; `_fts_text` reads the flag from the
  passed `home`; `embed_doc` flag-gated. **(fleet)** `fleet_flag` coerces values (a string
  `"false"` no longer force-enables) with a local-kill-switch precedence; `read_org_config`
  returns `None` on a transient fetch failure so `merge_org_config` KEEPS the staged overlay
  instead of wiping `org_pin`/`fleet_secret` on a Drive blip, and skips no-op writes; the startup
  merge is gated on a Google token existing (no per-boot Drive I/O / warning on non-authed
  installs). Plan: `docs/superpowers/plans/2026-07-23-review-fixes.md`. The **Windows HARDWARE QA
  GATE from 0.7.97 remains OPEN.**
  **0.7.102 fixes the caller-half of the
  0.7.90 org-config fallback bug:** `daemon._maybe_merge_org_config` guarded on
  `config['fleet']['folder_id']` and early-returned for the common case (not set at setup), so it
  never called `merge_org_config` — defeating that function's own baked-in `FLEET_FOLDER_ID`
  fallback. Net effect: the fleet `org_pin` **and** the 0.7.101 fleet feature flags
  (`retrieval_expand`) **reached nobody** on the common-case install. Now the daemon resolves the
  folder the same way `merge_org_config` does (explicit → `org_defaults.FLEET_FOLDER_ID`) and only
  skips when neither resolves. **This is the release that actually makes the fleet-wide
  `retrieval_expand` enable take effect** — installs pick up the fix via daily wheel auto-update,
  then stage `org_config.flags` and activate injection expansion on next daemon start. (Verified
  end-to-end on the author box: merge staged `flags.retrieval_expand=true`, `org_pin` preserved,
  `retrieval_expand_enabled()` → True.) The **Windows HARDWARE QA GATE from 0.7.97 remains OPEN.**
  **0.7.101 makes `retrieval_expand`
  fleet-flippable** and enables it fleet-wide. New generic mechanism: `org-config.json`'s
  allowlist gains `"flags"`, and `config.fleet_flag(home, name, default)` resolves any feature
  flag by precedence **org overlay (`org_config.flags[name]`, org wins) → top-level config →
  default**; `retrieval_expand_enabled` delegates to it. So enabling org-wide is
  `org-config.json = {"flags": {"retrieval_expand": true}}` (staged into `config['org_config']
  ['flags']` on each install's next daemon start). Any future feature flag is fleet-flippable the
  same way. A **tabular/cold expansion-skip was built and reverted**: the rep-chunk signal was
  unreliable (cold false-positived a prose email → lost hits; untagged tabular missed) and it
  suppressed roster/calendar tables that are often the actual answer; expansion is bounded (4k
  cap) and `brain_search` never expands, so tabular expansion is low-harm — no skip shipped.
  Spec/plan: `docs/superpowers/specs/2026-07-22-expansion-tabular-skip-fleet-flag.md`,
  `docs/superpowers/plans/2026-07-22-expansion-tabular-skip-fleet-flag.md`. The **Windows
  HARDWARE QA GATE from 0.7.97 remains OPEN.**
  **0.7.100 (released) is the recall-quality
  retrieval work.** Two levers shipped, one behind a flag; two others were built, gated on the
  live store, and **reverted** (the gate did its job). What ships:
  **(C) contextual BM25** — `embed.contextual_prefix` is now folded into the **FTS/keyword arm**
  too (not just embeddings), completing the existing default-ON `contextual_retrieval` feature;
  a bounded `store.reindex_fts_batch` backfill (wired into `index_pending`, FTS-only, no re-embed)
  brings existing rows up. Validated: gold holds recall@10 0.750 / MRR 0.514.
  **(A) injection-only small-to-big expansion** (`retrieval_expand`, **default OFF**) — enriches
  ONLY the UserPromptSubmit auto-RAG path (`prompt_recall`), NEVER `brain_search`'s flat candidate
  list. Consumer-split: `daemon.search(…, expand=False)` calls `retrieval_expand.maybe_expand`
  (no-op unless `expand=True` AND the flag is on); `/api/recall` reads `expand` from the body;
  `brain_search` never sets it; `prompt_recall` sets it and uses larger formatting caps
  (1500/4000 vs the flat 200/1200) so stitched context isn't re-truncated. Deterministic gates:
  brain_search gold UNCHANGED (0.750/0.514), injection context 632→2469 avg chars within the
  4000-char cap. **`retrieval_expand` ships OFF** — flip it per-machine to eyeball injected
  context on/off before any fleet-wide enable.
  **REVERTED after gating (do NOT resurrect without new evidence):** a **cross-encoder reranker**
  (fastembed MS-MARCO MiniLM) genuinely ranked this personal email/Drive corpus WORSE than the
  existing three-axis RRF (MRR 0.514→0.354 with a loaded model) — dropped; and a **first
  expansion attempt wired into `daemon.search` for all consumers** cratered `brain_search`
  recall@10 (0.750→0.300) by capping the candidate list — that mis-wiring is what (A)'s
  consumer-split fixes. Specs/plans: `docs/superpowers/specs/2026-07-22-recall-quality-expansion-design.md`,
  `…/2026-07-22-expansion-injection-followup.md`, `docs/superpowers/plans/2026-07-22-expansion-injection-only.md`.
  The **Windows HARDWARE QA GATE from 0.7.97 remains OPEN** — do NOT onboard Windows users until it passes.
  **0.7.99 (released) is the shared-drive
  ingest-cache CENTRALIZATION**: `.mcpbrain-cache/` no longer lands in every team drive's root —
  each source drive's cache is now stored centrally at `<fleet folder>/ingest-cache/<source_drive_id>/.mcpbrain-cache/`
  (inside the MCPBrain Backups shared drive). Achieved WITHOUT touching `ingest_cache.py`: a new
  `DriveFleetStorage(base_path=…)` prefix + `fleet_storage.cache_storage_factory` (rooted at the
  fleet folder via `centralized_cache_storage`) that both call sites (`sync/__init__.py`,
  `onboarding.py`) route through; per-drive scoping (GC/revocation/bootstrap) preserved because each
  drive keeps its own `base_path` subfolder. Flag `ingest_cache_central` (default **ON**,
  org-config-flippable) with automatic in-drive fallback if no fleet folder resolves. One-shot
  cleanup `bin/relocate_ingest_cache.py` (dry-run default, `--delete-legacy`) removes the legacy
  in-drive folders — **run once, only AFTER the fleet has auto-updated to ≥0.7.99** (runbook §1e).
  Spec/plan: `docs/superpowers/specs/2026-07-22-centralize-ingest-cache-design.md` /
  `docs/superpowers/plans/2026-07-22-centralize-ingest-cache.md`. Follow-up (out of scope, flagged):
  the escrow keys live in the all-members-readable fleet folder — worth locking down. The **Windows
  HARDWARE QA GATE from 0.7.97 remains OPEN** (see below) — do NOT onboard Windows users until it passes.
  **0.7.98 (released) bundled two post-0.7.97 fixes from concurrent sessions:**
  **(1) Drive-doc enrichment matching (fix)** — Drive documents were effectively **never enriched
  into the graph** via the thread-enrich drain path: `_group_key` (batching), `reassemble_thread`,
  and `store.doc_ids_for_messages` disagreed on a Drive chunk's identity, so the extraction's
  `message_id` (= `file_id`) matched no chunk and drain skipped **every** Drive apply (95% of 11,782
  "matched no chunk" warnings on the live store; ~85k Drive chunks stuck `enriched=0`, re-queuing and
  burning Haiku). Fixed by aligning all three on **`file_id` as the Drive doc's identity** (whole doc
  = one thread): `_group_key` groups Drive chunks by `file_id`, and `doc_ids_for_messages` resolves
  an id matching a chunk's `metadata.file_id` to every chunk of that file (email/`doc_id` resolution
  unchanged). Verified read-only on the live store (the 2,303-chunk PDF now resolves file_id→all its
  chunks). **Fix-forward — no chunk had hit the give-up cap, so no remediation.** Spec:
  `docs/superpowers/specs/2026-07-21-drive-enrichment-match-design.md`.
  **(2) the two 0.7.97-review deferred Windows Minors, now FIXED** — uninstall removes the
  Startup-folder `.lnk` (shared `_startup_shortcut_path`); `doctor._true_os_arch` detects Rosetta 2
  (`sysctl.proc_translated`) so an x86_64 interpreter on Apple Silicon reads OS arch as arm64 →
  `arch_line = "emulated — expected"`.
  **0.7.97 was the Windows install rework
  (use-the-platform)** — it corrects a misdiagnosis at the root of the 0.7.95/0.7.96 Windows work.
  A real Windows-on-ARM install proved: (a) **native ARM64 is not viable** — `sqlite-vec`,
  `cryptography`, `pymupdf`, `leidenalg` ship **no `win_arm64` wheels** (so the 0.7.95 arch-native
  `install.ps1` failed outright); (b) **uv already installs x86_64 CPython by default on ARM64**
  and Windows runs it transparently under Prism emulation; (c) the original "onnxruntime crashes
  under emulation" was a **missing x64 `MSVCP140_1.dll`** (likely from installing the ARM64 redist
  first → version-skip), NOT an emulation incompatibility. So the installer now **uses the
  platform**: slim `install.ps1` = ensure uv → ensure the **x64** VC++ redist (x64 ONLY, never
  arm64) → `uv tool install --python <x64-pin> "mcpbrain[daemon]"` → `mcpbrain setup` (no
  arch-detection, no Python provisioning). Plus the downstream fixes a real ARM64 install exposed:
  the run-at-logon shim reverted to the **absolute `mcpbrain.exe`** (the 0.7.96 signed-`pythonw`
  shim resolved a bare `pythonw` not on PATH → daemon never started at login); `cli.py` forces
  **UTF-8 stdio** (doctor's `✅/⚠️` glyphs crashed cp1252 Windows consoles); a durable
  **`mcpbrain/vcruntime.py`** safety net (`app_dir()/vcruntime` on the DLL search path via
  `add_search_dir`, populated from an MS-signed x64 copy by a `doctor` repair — survives reinstalls)
  as a fallback if the redist ever leaves onnxruntime unable to load; `doctor.arch_line` now reads
  x64-on-ARM64 as **"emulated — expected"**; the tray gains the Startup-shortcut fallback; the
  `mcpbrain.maintenance` import is optional (no wheel-install warning). Gates green at release
  (full suite passed, ruff clean). **HARDWARE QA GATE STILL OPEN (runbook §5):** 0.7.97 is published
  (safe — existing installs auto-update only the daemon wheel; `install.ps1` is opt-in,
  used only when someone runs a Windows install), but the reworked installer is **not yet validated
  on a real ARM64/x64 Windows box — do NOT onboard Windows users until that QA passes.** (The two
  deferred review Minors that were noted here are now **fixed in 0.7.98**, above.) Earlier:
  **0.7.95/0.7.96 were the (now-superseded) arch-native
  Windows preflight-installer releases** — 0.7.96 also removed the plugin's top-level `bin/` (shims
  + `monitors/`) that **fails claude.ai marketplace validation** (a `test_no_toplevel_bin_dir` guard
  prevents regressions; that removal STANDS). The lazy embedder, wizard-owned model download,
  `[daemon]` optional dep + `update.py` reinstalling `mcpbrain[daemon]` from
  that line **remain** (the `.mcpb` bridge does NOT — removed 2026-08-24, see above); only the native-ARM64 install strategy was replaced. Earlier:
  **0.7.90 was the org-baseline ACTIVATION release**: it fixes `fleet.merge_org_config` to fall back to
  `org_defaults.FLEET_FOLDER_ID` when `fleet.folder_id` is unset (the common case) — the
  prerequisite for the fleet-wide `org_pin` to reach installs at all (it previously early-returned
  and reached nobody) — plus graph-explorer polish (particle/curvature/hover + search-driven ego
  jump). With 0.7.90 deployed, the `org_pin` (fleet_secret + embed_model=bge-small, dim=384,
  chunker_version=v1, enrich_logic_floor=1, default relation allowlist) is distributed via
  `org-config.json` in the fleet folder, activating the org-baseline on each install's next daemon
  start. The real-Drive read+write paths were validated live (16 shared drives enumerated;
  export() confirmed; cache publish/import/apply + curator↔member snapshot + contribution round-trip
  against real Drive, cleaned up). Earlier: **0.7.89 ships the complete
  org-baseline feature** (org shared graph + personal overlay): shared-drive ingest cache
  (subsystem A), curated org graph — contribution edge / curator / consumer import (B),
  onboarding baseline-bootstrap (C), a full hardening pass, real LLM fuzzy-merge adjudication via
  the async enrich-spool, Phase-D convergence tests + `/graph` origin colouring + cache/curator
  `/api/status` metrics + `docs/ORG-BASELINE-ROLLOUT.md`, and A#4 (cache the enrichment payload so
  importers skip Haiku re-enrichment on shared-drive docs). Also pytest-xdist parallel-by-default.
  **Fleet-wide enablement gates on distributing `fleet_secret` via `org-config.json`** — nothing
  content-shaped, no cache, and no contributions move until the pin is present (see
  `docs/ORG-BASELINE-ROLLOUT.md`). Earlier: **0.7.88 fixes the
  `bin/consolidate.py` migration itself**, surfaced by its first attended run on the live
  store: `meeting_source_doc_ids()` and `meeting_series_for_old()` both assumed provenance via
  `email_entities`/`entity_relations.source_doc_id`, which calendar-sourced meetings never
  populate (the calendar-chunk enrichment path writes `attended`/`instance_of`/`involved_in`
  relations with the bare Calendar event id in `evidence` but never threads `source_doc_id` or
  `email_entities` through) — this made `meetings-reset` find 0 of 294 legacy meetings' chunks
  to re-extract, and `meetings-retire` retire 0 of them even after re-extraction produced
  genuine series. Both functions now also match via a shared Calendar event id (base-event-id
  comparison, so a recurring series' per-instance date suffix doesn't block the match), same
  ambiguous-returns-None non-destructive policy. **The live-store run itself: attended, topics
  phase clean (1,508 merged → 1,484 canonical, gold gate held), meetings phase partially run**
  — 28 of 294 legacy meetings retired into 6 genuine re-extracted series so far; the remaining
  266 are non-destructively left (most still draining through re-extraction, a smaller subset
  permanently unrecoverable because their source email chunks were already pruned by the
  routine retention job) — **re-running `bin/consolidate.py meetings-retire` later is safe and
  expected** (idempotent: already-retired ids are skipped, left ones get a fresh chance) to
  sweep further as re-extraction catches up. Gold gate held at recall@10 0.750 / MRR 0.564
  across every checkpoint of the run.
- **0.7.87 ships series/topic
  consolidation** (write-time deterministic keying: meetings→org-scoped `meeting-<org>-<series>`
  entities with append-only `entity_observations` occurrences, driven by LLM `series_name`/
  `occurrence_date`; topics→`normalize_topic` = inflect-singularize + curated synonym map;
  calendar `recurringEventId` capture + opportunistic `calendar_series` annotation; attended,
  backup-gated migration `bin/consolidate.py` — built and shipped, first live run described
  above). Both kill-switches (`meeting_series_enabled`/`topic_consolidation_enabled`) default ON.
  0.7.87 also fixes the gold-eval gate (the `--gold` harness + migration runbook now measure the
  PRODUCTION three-axis path — recall@10 0.750 / MRR 0.564 — not the relevance-only baseline that
  misleadingly reads MRR 0.281), and folds in concurrent-session work (graph stored-XSS escaping +
  search LIKE-escape, radial-layout default, `merge_entities` observation/email repointing + loser-
  alias carry). 0.7.85 added graph
  readability (clustered map + semantic zoom); **0.7.86 fixes issue #4** — `_candidate_pairs`
  now restricts merge-review candidate generation to name-identity types (person/org/project),
  the same allowlist as `_deterministic_merges` (#23) and `apply_duplicate_verdicts`, cutting
  the live pair count 365,895→25,711 and keeping structural entities out of the merge queue.
  Issues #23 and #24 closed (fixed in 0.7.74 / removed in Session 3). 0.7.78–0.7.82 shipped
  Session-4 (brain-review: AI-adjudicated graph hygiene on a daily cadence — reversible/
  capped appliers) + the interactive knowledge graph (Sigma/force-graph explorer at `/graph`);
  0.7.83 added the live force-graph renderer; **0.7.84 hardens the review appliers** —
  they target the finding's own stored `ref_id`/type (via `store.get_finding`) so a
  malformed unattended verdict can't redirect a mutation, skip self-pair merges, and the
  daily `resolve_entities`/`review` cadences are correctly documented as ON-by-default.
  Earlier: 0.7.77 — source, dist index, and plugin manifests are in step. 0.7.76 shipped Session-3
  efficiency (deterministic sender person-entities so Haiku extracts only body-mentioned
  people, trivial-thread short-circuit, `spool_thread_cap` default 500→2000, `parallel_backfill`
  removed, `resolve_entities` wired into a daily cadence). **0.7.77 fixes a CRITICAL bug that
  0.7.76 introduced:** the daily `resolve_entities` cadence + deterministic sender-email stamping
  would have irreversibly merged distinct people who share a role/shared inbox (`office@`/`info@`);
  now `is_role_address()` blocks role-address groups in `_email_equality_merges` and refuses to
  key any person on a role address. Also broadened trivial-thread cues (was dropping short
  commitments). Below the state line: 0.7.72 shipped the Session-1
  enrichment-efficiency work (provenance stamping, message-metadata off the model, bigger
  batches, strict push schema) + the cold-recall decouple. 0.7.73 adds Session-2 graph-depth
  (header `email_addr`, deterministic org default + `works_at`/`mentioned_with` relations,
  reconciled entity/relation vocabulary, temporal `entity_observations`, and write-time
  email/token dedup — `write_time_dedup` now default ON). 0.7.74 fixes the
  `_deterministic_merges` structural-collapse bug (issue #23). 0.7.75 keeps the enrich
  **coordinator on Sonnet** (Claude Code scheduled tasks only offer Auto permission mode on
  Sonnet — a Haiku coordinator stalls unattended; executor subagents stay Haiku) and raises
  throughput caps (units/wave 30, producer window 600, subagent fan-out ~12, hourly wave cap 15).
- **`_deterministic_merges` structural collapse — FIXED in 0.7.74 (issue #23).** It used to group
  by `(type, canonical_key)` across ALL types, so structural nodes (document/thread/topic/…)
  sharing generic titles ("Untitled document") collapsed — ~3,980 merged in one shot on a
  real-corpus copy. Now restricted to name-identity types (`person`/`org`/`project`) via an
  allowlist (fail-safe). Note: `resolve_entities` still has **no live caller** — wiring one in
  remains a separate, deliberate step.
