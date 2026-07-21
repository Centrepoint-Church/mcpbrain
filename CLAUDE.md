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

- **Version lives in FIVE files, keep them equal:** `pyproject.toml`, `mcpbrain/__init__.py`,
  `plugin/.claude-plugin/plugin.json`, `plugin/.claude-plugin/marketplace.json`,
  `plugin/mcpb/manifest.json`. The plugin manifests are the marketplace's version and the
  `.mcpb` manifest is the Desktop Extension's version — all **easy to forget**; bumping only
  `__init__.py`/`pyproject.toml` ships a wrong plugin/extension version. (`uv.lock`'s
  mcpbrain entry is also kept in step but isn't a marketplace source-of-truth.)
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
- **Current state (2026-07-21):** the **five** version files (+ `uv.lock`) are bumped to `0.7.98`
  in **source only (local `main`, not pushed)**; the **published wheel, dist index, and plugin
  marketplace remain at `0.7.97`** — **0.7.98 is NOT released.** 0.7.98 bundles two post-0.7.97
  fixes from concurrent sessions:
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
  `arch_line = "emulated — expected"`. **Still to do before release:** full suite + gold-eval gate
  (per runbook), then the release steps. The **Windows HARDWARE QA GATE from 0.7.97 remains OPEN**
  (see below) — do NOT onboard Windows users until it passes.
  **0.7.97 (last released) is the Windows install rework
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
  (safe — existing installs auto-update only the daemon wheel; `install.ps1`/`.mcpb` are opt-in,
  used only when someone runs a Windows install), but the reworked installer is **not yet validated
  on a real ARM64/x64 Windows box — do NOT onboard Windows users until that QA passes.** (The two
  deferred review Minors that were noted here are now **fixed in 0.7.98**, above.) Earlier:
  **0.7.95/0.7.96 were the (now-superseded) arch-native
  Windows preflight-installer releases** — 0.7.96 also removed the plugin's top-level `bin/` (shims
  + `monitors/`) that **fails claude.ai marketplace validation** (a `test_no_toplevel_bin_dir` guard
  prevents regressions; that removal STANDS). The lazy embedder, wizard-owned model download,
  `[daemon]` optional dep + `update.py` reinstalling `mcpbrain[daemon]`, and the `.mcpb` bridge from
  that line all **remain**; only the native-ARM64 install strategy was replaced. Earlier:
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
