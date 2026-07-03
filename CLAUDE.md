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
| **mcpbrain-plugin** (`../mcpbrain-plugin`) | Public Claude Code plugin (agents/skills/hooks/commands/monitors + `.claude-plugin/`), mirrored from this repo's `plugin/` | org **plugin marketplace** (Claude Team/Enterprise settings) |

## CRITICAL: local work ≠ shipped to users

- `git commit` here → local until `git push`. `uv tool install --force .` and
  `launchctl kickstart` → **this machine only.** Neither changes what any other user installs.
- Do **not** push or release without an explicit instruction — shipping is an all-users action.

## Releasing to prod

**`docs/RELEASE-RUNBOOK.md` is the authoritative, step-by-step procedure** (the *do*);
`docs/DISTRIBUTION.md` is the *why*. Follow the runbook. The things that are easy to get
wrong and MUST be right:

- **Version lives in FOUR files, keep them equal:** `pyproject.toml`, `mcpbrain/__init__.py`,
  `plugin/.claude-plugin/plugin.json`, `plugin/.claude-plugin/marketplace.json`.
  The two plugin manifests are the marketplace's version and are **easy to forget** —
  bumping only `__init__.py`/`pyproject.toml` ships a wrong plugin version. (`uv.lock`'s
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
- **Current state (2026-07-03):** all four version files **and** the published wheel are at
  `0.7.88` — source, dist index, and plugin manifests are in step. **0.7.88 fixes the
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
