# Console Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. **Every task ends with a review gate:** a fresh `feature-dev:code-reviewer` subagent reviews that task's diff against this plan and the spec; findings are fixed and re-reviewed before the next task starts.

**Goal:** Ship the redesigned local console (Overview, Settings, Graph) with correct numbers, a health verdict, and a working action behind every figure and setting the prototype shows.

**Architecture:** Python daemon modules gain four focused helpers: `console_stats.py` (counts, freshness, health), `change_undo.py` (undo registry), `auto_changes.py` (apply/ask/off modes and pending changes), and `system_ops.py` (restart, login item, update check, diagnostics, logs). `control_api.py` gains routes over them. The three pages are rewritten as plain HTML that share one stylesheet, one theme script and vendored fonts served from a new `/static/` route. The graph keeps its existing force-graph engine.

**Tech Stack:** Python 3 stdlib, SQLite, pytest, ruff; plain HTML/CSS/JS; vendored `d3` and `force-graph` (unchanged); Host Grotesk and Atkinson Hyperlegible Mono as vendored `.woff2` (SIL OFL). No new Python or JS dependencies.

**Spec:** `docs/superpowers/specs/2026-11-06-console-redesign-design.md` (sections 1 to 10). The visual reference is the owner's private prototype (pages `variation-1.html`, `settings.html`, `graph.html`, `console.css`, `skins.css`), kept outside this repo because it renders real data; ask the owner for it before Task 6.

## Staleness markers

- Last retirement check: 2026-11-09 against state/retired.md (52 entries scanned)
- Last cross-checked against state/decisions.md: 2026-11-09 (archived ops-brain log, no rows after 2026-06-09; live records-repo log, 18 lines)
- Deviation banner: none blocking. The 2026-05-29 "Dark-mode support" retirement applies to the retired Nexus workstation, not this console. The 2026-05-21 force-graph decision is consistent with keeping the graph engine. The 2026-05-16 voice-analyser revival is consistent with voice's row in Automatic changes.

## Global Constraints

- **Public repo.** No real person names, emails, organisation names or owner paths in code, tests, docs or commit messages. Use the fictional cast (`Dana Okafor`, `Acme`, `Sam Chen`, `Pat Nguyen`, `example.org`). `uv run pytest tests/test_no_tenant_literals.py` passes with `mcpbrain/tenant_people.json` present.
- **Scope rule.** Every control on a page works end to end. No control ships ahead of its endpoint.
- **Branch.** `console-redesign`. Commit per task after its review gate. No push or release without the owner.
- **Numbers.** Percentages are bounded 0 to 100, and `pct` never reads 100 while anything waits. Counts are never negative. Eligible chunks are those with `enrich_state` NULL, `''` or `'duplicate'`.
- **Stale threshold.** `STALE_HOURS = 6.0`, one constant for sources and enrichment.
- **Freshness keys.** Source freshness reads only the base cursors `gmail`, `drive`, `calendar` and `anarlog`.
- **Timestamps.** The API returns ISO 8601 UTC ending `+00:00`, or `null`. Readers accept both `YYYY-MM-DD HH:MM:SS` and ISO.
- **Health precedence.** paused, then stalled, then needs_you, then behind, then healthy.
- **Undo window.** 30 days. `prune_change_log` never deletes a row with non-empty `revert_ref` younger than that.
- **Modes.** Automatic-change modes are `apply`, `ask` or `off`; draft style tuning uses `apply`, `advise` or `off`. Defaults reproduce today's behaviour (Task 3 table).
- **Colours.** No yellow, amber, orange or brown anywhere, in either mode. Grounds are neutral greys (hue 250, chroma ≤ 0.007).
- **Default accent.** Raspberry: `--accent-h: 355`, `--accent-c: 0.19`. Teal (hue 205) means measured values. Status red uses hue 30.
- **Accent shades.** Each shade has a fixed lightness (light fill L55%, light text L52%, light soft L95%; dark fill L72%, dark text L78%, dark soft L29%). An accent within 40° of 205 moves measured values to 255. An accent within 35° of 30 moves status red to 5 or 55, whichever is farther.
- **Page floor.** Every colour and font goes through a token; nothing animates width, height, top or left; every control has `:focus-visible` and all states; nothing is tracked or uppercase in labels; there are no middle-dot meta strings or decorative dots; content shows with JS off; a `prefers-reduced-motion` fallback is present.
- **Fonts and network.** No network calls from the pages: no Google Fonts or CDN, fonts served from `/static/fonts/`.
- **Copy.** Australian English. No em dashes in UI copy.

## Review Focus

1. **Undo after the item changed again.** For example, a profile rewritten twice, then the first change undone. Undo must refuse with "This has changed since; undo the later change first", never overwrite newer data. Test: Task 2.
2. **Mixed timestamp formats, and a shared drive that stopped syncing.** Both parse. A dead `drive:<id>` cursor never marks Drive or the system stale, and it shows in the Drive row as "1 shared drive not syncing". Tests: Tasks 1 and 4.
3. **Settings saved from two tabs, or a partial nested block.** Saving one keep-out list must not wipe the other. An unknown key returns 400 and changes nothing. Test: Task 3.
4. **A re-included chunk.** It must stay eligible through the next prepare pass even though the salience or derived gate would mark it cold again. Test: Task 4.
5. **The daemon restarting or unreachable while a page is open.** Every page shows its offline state with the instruction to reopen from the menu bar. No page throws, and no control appears to succeed. Test: Task 6, the page script test with a failing fetch.

---

### Task 1: Correct numbers, freshness and health

**Files:**
- Create: `mcpbrain/console_stats.py`, `tests/test_console_stats.py`
- Modify: `mcpbrain/dashboard.py` (`stats()`; delete `_graph_counts`; keep `queue_state`), `tests/test_dashboard_stats.py`, `mcpbrain/wizard/dashboard.html` (repoint only; the redesign is Task 6), `tests/test_dashboard_routes.py`

**Interfaces:**
- Produces:
  - `index_counts(db) -> {"total", "eligible", "cold", "duplicate", "enriched_eligible", "waiting", "pct"}` (ints)
  - `pct_of(done: int, eligible: int) -> int`
  - `graph_counts(db) -> {"by_type": {person, org, project, topic, meeting, event, document, thread, unknown: int}, "relations_live", "observations_live", "unlinked"}`
  - `parse_utc(str | None) -> datetime | None`
  - `SOURCE_KEYS = ("gmail", "drive", "calendar", "anarlog")`
  - `source_times(db) -> dict[str, str | None]`
  - `shared_drive_health(db, now) -> {"total": int, "stale": int}`
  - `enrich_activity(db, now) -> {"last_push_at", "pushes_24h"}`
  - `STALE_HOURS`
  - `health(*, now, paused, stalled, watchdog_limit_reached, connections, open_findings, pending_reviews, sources, waiting, last_push_at) -> {"state", "reasons"}`
  - `stats(store, home, status, *, now=None)`, with top-level keys exactly `as_of, index, enrich, graph, sources, health, needs_you, connections, system`.

- [ ] **Step 1: Write the failing tests** in `tests/test_console_stats.py`. Exact values:
  - 6 cold-and-enriched, 4 cold, 3 eligible-enriched and 1 eligible-waiting chunks give `{"total":14, "eligible":4, "cold":10, "duplicate":0, "enriched_eligible":3, "waiting":1, "pct":75}`.
  - 999 of 1000 gives `pct` 99.
  - All enriched gives 100.
  - No eligible chunks gives 0.
  - Every `(state, enriched)` combination keeps `waiting >= 0` and `pct` within 0 to 100.
  - A missing table gives zeros.
  - `graph_counts`:
    - all nine type keys are always present;
    - a NULL or unrecognised type goes to `unknown`;
    - invalidated relations and observations are excluded;
    - "acme-c only in an invalidated relation" and "acme-d in none" give `unlinked == 2`;
    - a schema without `invalidated_at` counts every row live.
  - `parse_utc`:
    - `"2026-11-06 01:40:03"` gives `"2026-11-06T01:40:03+00:00"`;
    - an ISO string with offset round-trips;
    - garbage gives `None`.
  - `source_times` ignores `drive:<id>` and `*_backfill_*`.
  - `shared_drive_health` counts a `drive:0AFAKE` cursor 4 months old as stale.
  - `enrich_activity` with pushes at 01:40 today, 23:00 yesterday and 3 days ago gives `pushes_24h == 2`.
  - Parametrised `health` table:
    - paused beats everything;
    - `stalled="cycle"` gives stalled;
    - the watchdog limit gives stalled;
    - Gmail 7 h old gives stalled with reason exactly `"Gmail has not synced for 7 hours"`;
    - a source never synced (`None`) is not stale;
    - `open_findings=2` gives needs_you;
    - `pending_reviews=1` gives needs_you;
    - a connection in `needs_action` gives needs_you;
    - `waiting=3578` with the last push 14 h ago gives behind;
    - `waiting=0` with no push gives healthy;
    - needs_you beats behind.
- [ ] **Step 2: Run** `uv run pytest tests/test_console_stats.py -q` and expect an ImportError.
- [ ] **Step 3: Implement `console_stats.py`.**
  - All of `index_counts` comes from one `SELECT … SUM(CASE …)`.
  - `pct_of` floors, caps at 99 while `done < eligible`, and returns 0 when `eligible == 0`.
  - Guard missing tables and columns with `sqlite_master` and `PRAGMA table_info`.
  - `enrich_activity` parses times in Python, because the column mixes formats.
  - Reason strings come from spec section 2a and the health table above. Source labels: Gmail, Drive, Calendar, Meetings.
- [ ] **Step 4: Rewrite `dashboard.stats()`.**
  - Open one `_open_ro` connection for all counts.
  - `enrich` is `{"queue_state": queue_state(queued=spool.pending, unenriched_eligible=waiting), "units_pending", "last_push_at", "pushes_24h"}`.
  - `sources` is a list in `SOURCE_KEYS` order of `{name, label, last_synced_at, history: {done, reached}}`. Drive also carries `shared: {total, stale}`.
  - `needs_you` is `{"connections": [state=="needs_action"], "findings": int, "reviews": int}`. `reviews` is `len(store.pending_corrections())` for now; Task 3 adds pending changes.
  - `system` is `{version, paused, watchdog_exits, reflow}`, where `reflow` is non-null only while it is halted, blocked or has work.
  - `enrich_enabled` and `links_per_entity` are removed.
  - In `tests/test_dashboard_stats.py`, rewrite the four tests that pin the old shape:
    - `test_stats_regression_153_percent` gives pct 96 from a 56/44/96/4 fixture;
    - top-level keys exact;
    - Meetings is the fourth source;
    - a starved queue is reported;
    - a `pending_corrections` failure degrades to 0.
- [ ] **Step 5: Repoint `dashboard.html`.**
  - The meter uses `idx.pct`, with the sub-line "<waiting> waiting, <units_pending> queued for Claude".
  - The Unlinked tile replaces Links / entity.
  - Sources show their sync time; the Spool row is removed.
  - The Daemon card shows Health and Last push.
  - In `tests/test_dashboard_routes.py`, the html no longer contains `enriched_pct`, `links_per_entity`, `enrich_enabled` or `Spool depth`, and does contain `idx.pct`, `data.health` and `last_synced_at`.
- [ ] **Step 6: Run** `uv run pytest tests/test_console_stats.py tests/test_dashboard_stats.py tests/test_dashboard_routes.py tests/test_dashboard_queue.py -q`. Expect a pass.
- [ ] **Step 7: Live read-only check.** Run `index_counts`, `graph_counts`, `source_times` and `shared_drive_health` on `~/Library/Application Support/mcpbrain/brain.sqlite3`, opened with `mode=ro`. Expect `pct` between 0 and 100 (the spec measured 96), every base source fresh within hours, and `shared.stale >= 1`.
- [ ] **Step 8: Review gate, then commit** `feat(console): correct digest numbers, freshness and health`.

### Task 2: Every automatic change recorded and undoable

**Files:**
- Create: `mcpbrain/change_undo.py`, `tests/test_change_undo.py`
- Modify: `mcpbrain/store.py` (`record_change` callers, `prune_change_log`), `mcpbrain/profile_synth.py`, `mcpbrain/community_synth.py`, `mcpbrain/consolidation.py`, `mcpbrain/voice_apply.py`, `mcpbrain/tools.py` (`make_brain_gardener_apply`), `mcpbrain/records_write.py`, `mcpbrain/threshold_bandit.py`, `mcpbrain/review_apply.py`, `mcpbrain/org_curate.py`, `mcpbrain/drain.py` (`capture_action_update`, `apply_duplicate_verdicts`), `mcpbrain/control_api.py`, existing writer tests

**Interfaces:**
- Produces:
  - `change_undo.revert_json(before: dict, after: dict) -> str`
  - `change_undo.UNDO: dict[str, Callable[[store, home, change_row: dict], None]]`
  - `change_undo.undo(store, home, change_id: int) -> {"ok": bool, "reason": str}`
  - `change_undo.feed(store, *, hours: int = 24, now=None) -> list[dict]`, items `{id, change_type, summary, ref_id, created_at, undoable, undone, group}`
  - `change_undo.added_since(store, *, hours: int) -> list[{entity_id, name, type}]`
  - Routes:
    - `GET /api/changes?hours=24` returns `{"items": feed}`.
    - `POST /api/changes/<id>/undo` returns 200 `{"ok":true}`, 409 `{"ok":false,"reason"}`, or 404.
    - `GET /api/changes/added?hours=24`.
    - `POST /api/circles/<id>/title {"title": str}` renames a circle and records an undoable `community_titled` change with `source="dashboard"`.
- Change types made undoable, with what `before` holds:

| change_type | writer | before / undo |
|---|---|---|
| `profile_updated` | profile_synth.py:131 | `{profile, profile_updated_at}`; restore |
| `community_titled` | community_synth.py:91 | `{title, summary}`; restore |
| `consolidation_pass` | consolidation.py:427 | `{notes: [doc_id], tiers: {doc_id: tier}, graduated: [path]}`; delete the note chunks, restore tiers, remove graduated memory files via `records_write` with a commit |
| `voice_apply` | voice_apply.py:67 | `{content}` of `context/voice.md`; write back and commit `undo: voice` |
| `gardener_write` (new) | `brain_gardener_apply` | `{path, content}`; write back and commit `undo: gardener` |
| `bandit_applied` (new) | threshold_bandit.py:233 | `{recall_max_distance}`; `write_config` back |
| `tidy_suppress`, `tidy_org`, `tidy_owner`, `tidy_merge`, `tidy_org_rewrite` (new) | review appliers registered at drain.py:155-180 | unsuppress / prior org / prior owner / `_unmerge_tx` via `entity_merge_log` id / prior field |
| `capture_action_update` | drain.py:990 | `{status}`; restore |
| `action_snoozed`, `role:`/`org:` (profile_audit), `correction:<id>`, `entity_suppressed` | existing | parse the legacy `revert_ref` forms; `correction:` calls `graph_corrections.undo` |

`ingest_skip` and `capture_ingest` are not undoable: they change nothing that can be put back. The feed marks them `undoable: false`.

- [ ] **Step 1: Write the failing tests** in `tests/test_change_undo.py`, one per row above. Each runs the real writer against a tmp store (fixtures use `Acme` names), asserts the `change_log` row has JSON `revert_ref` with `before`/`after`, calls `undo`, and asserts the prior value is back. Also:
  - `test_undo_refuses_when_changed_since`: profile written twice, undo of the first gives `{"ok":False,"reason":"This has changed since; undo the later change first"}`, and the data is unchanged.
  - `test_undo_twice_is_refused`: the second call gives the reason "Already undone".
  - `test_prune_keeps_undoable_rows_for_30_days`: 600 plain rows plus 3 undoable rows (one 31 days old); after `prune_change_log(500)` the two young undoable rows survive and the old one is gone.
  - `test_feed_groups_by_type_and_minute`: 6 `profile_updated` rows in one minute share one `group`.
  - `test_added_since_reads_relation_and_observation_sources`.
  - Route tests: undo returns 200, 409 and 404; the feed returns `items`.
- [ ] **Step 2: Run** `uv run pytest tests/test_change_undo.py -q` and expect failures.
- [ ] **Step 3: Implement.**
  - Each writer reads the prior value on the same connection, immediately before its write, and passes `revert_ref=revert_json(before, after)`. `record_change` keeps its signature.
  - `undone` is recorded by writing a `change_undone` row with `ref_id=str(change_id)`. `undo` checks for one first.
  - `prune_change_log` deletes only rows outside both the newest 500 and `(revert_ref != '' AND created_at >= now - 30 days)`.
  - Deleting consolidation note chunks uses the existing chunk-removal path that `ingestion_cleanup` uses (find it, reuse it).
  - Records-repo writes go through `records_write` so each undo is its own commit.
- [ ] **Step 4: Run** `uv run pytest tests/test_change_undo.py tests/test_profile_synth.py tests/test_community_synth.py tests/test_consolidation.py tests/test_voice_analyser.py tests/test_threshold_bandit.py tests/test_review_apply.py tests/test_drain_captures.py tests/test_change_log.py tests/test_phase2_gardener.py -q`. Expect a pass.
- [ ] **Step 5: Review gate, then commit** `feat(console): record and undo every automatic change`.

### Task 3: Apply, ask first or off; pending changes; settings endpoint; findings

**Files:**
- Create: `mcpbrain/auto_changes.py`, `tests/test_auto_changes.py`, `tests/test_settings_api.py`
- Modify:
  - `mcpbrain/config.py`
  - `mcpbrain/store.py` (new table `pending_changes`)
  - the Task 2 writers (branch on mode)
  - `mcpbrain/routines/reference-gardener.md` (line 8 wrongly says propose-only is the default; it reads the mode)
  - `mcpbrain/control_api.py`
  - `mcpbrain/dashboard.py` (`needs_you.reviews` counts pending changes too)

**Interfaces:**
- Produces:
  - `config.auto_change_mode(home, kind) -> str` for `kind in KINDS = ("profiles", "circles", "reference", "voice", "consolidation", "tidy", "draft_style")`.
  - Default modes:

    | Kind | Default | Source of the default |
    |---|---|---|
    | profiles | apply | no switch today |
    | circles | apply | no switch today |
    | reference | apply | from `gardener_auto_apply` (true gives apply, false gives ask) |
    | voice | apply | from `voice_auto_apply` |
    | consolidation | apply | from `consolidation` (false gives off) |
    | tidy | apply | no switch today |
    | draft_style | advise | from `bandit_auto_apply` (false gives advise) |

    A new `auto_changes: {kind: mode}` config block overrides the old keys.
  - `auto_changes.propose(store, kind, target, summary, payload) -> int`: inserts `pending_changes(id, kind, target, summary, payload, status, created_at, decided_at, change_log_id)` and records a `pending_change` finding (`ref_id=str(id)`).
  - `auto_changes.APPLY: dict[str, Callable[[store, home, payload], int]]`, one per kind. Each returns its `change_log` id, and is the same function the writer calls in apply mode.
  - `auto_changes.decide(store, home, pending_id, *, approve: bool) -> {"ok", "reason"}`.
  - `settings_api.get_settings(home) -> dict` and `settings_api.apply_settings(home, body: dict) -> dict`.
  - Routes:
    - `GET /api/pending` returns `{"items": [{id, kind, target, summary, created_at}]}`.
    - `POST /api/pending/<id>/apply|decline`.
    - `GET /api/settings` and `POST /api/settings`.
    - `GET /api/findings` returns `{"items": [{id, finding_type, summary, detail, severity, detected_at, count}]}`, grouping identical `finding_type` + `summary`, and excluding `pending_change` and `graph_correction` findings (those are listed by `/api/pending` and `/api/corrections/pending`, so Needs you shows each item once).
    - `POST /api/findings/<id>/fixed` and `POST /api/findings/<id>/dismiss`.
  - Allowlisted settings keys:
    - `auto_changes.<kind>`
    - `review_max_apply_per_run` (int, 1 to 500)
    - `sources.gmail|drive|calendar` (bool)
    - `anarlog.enabled` (bool)
    - `ingest.generated_gmail_subject_prefixes|generated_drive_folders|exclude_senders|exclude_labels` (lists of str)
    - `owner_aliases` (list of str)
    - `start_at_login` (bool)

    `apply_settings` deep-merges one level, validates types, and returns 400 `{"error": "unknown key: x"}` for anything else, leaving config unchanged.
- Finding semantics:
  - **Mark fixed** resolves the finding with verdict NULL, so it reopens if it recurs (today's dismiss behaviour for `agent_stderr`).
  - **Dismiss** resolves it with verdict `dismissed_by_human`, so it never reopens.

- [ ] **Step 1: Write the failing tests.** In `test_auto_changes.py`:
  - defaults equal the table, including legacy-key mapping;
  - each writer in `ask` mode leaves data untouched, adds one `pending_changes` row and one `pending_change` finding;
  - `decide(approve=True)` applies through `APPLY`, records an undoable change and closes the finding;
  - decline closes the finding and leaves the data unchanged;
  - `off` does nothing;
  - `draft_style=advise` writes nothing;
  - `gardener` in ask mode returns "queued for review" from `brain_gardener_apply`;
  - tidy in ask mode turns 3 verdicts into 3 pending rows, capped by `review_max_apply_per_run`.

  In `test_settings_api.py`:
  - posting `{"ingest": {"exclude_senders": ["news@example.org"]}}` keeps `generated_drive_folders`;
  - an unknown key gives 400 and config is byte-identical;
  - a wrong type gives 400;
  - `GET` returns every allowlisted key with its effective value.

  Findings route tests: grouping, fixed versus dismiss reopen behaviour (record the same `agent_stderr` again: fixed reopens, dismissed stays closed).
- [ ] **Step 2: Run** the new tests and expect failures.
- [ ] **Step 3: Implement** the table (created in the store schema block alongside `graph_corrections`), modes, `propose`/`APPLY`/`decide`, the writers' branch (`mode = config.auto_change_mode(home, kind)`; `off` returns early; `ask` calls `propose`; `apply` calls `APPLY[kind]`), settings and the routes. `stats().needs_you.reviews` becomes pending corrections plus pending changes.
- [ ] **Step 4: Run** `uv run pytest tests/test_auto_changes.py tests/test_settings_api.py tests/test_change_undo.py tests/test_control_api*.py tests/test_gardener_routine.py -q`. Expect a pass.
- [ ] **Step 5: Review gate, then commit** `feat(console): apply, ask-first or off for every automatic change`.

### Task 4: Source controls

**Files:**
- Create: `mcpbrain/source_controls.py`, `tests/test_source_controls.py`
- Modify:
  - `mcpbrain/config.py` (`source_enabled(home, name) -> bool`, default True; anarlog keeps `anarlog.enabled`)
  - `mcpbrain/sync/__init__.py` (`run_sync_cycle` top: a disabled source has its service set to None and its queued rows return `DEFER`)
  - `mcpbrain/daemon.py` (`sync_now(source: str | None = None)` sets a one-shot `_sync_only`, read in `run_one`)
  - `mcpbrain/sync/gmail.py` (`handle_gmail_item`: sender and label exclusion skips with `record_skip(store, "gmail_excluded", …)`; flush the existing `skips` dict)
  - `mcpbrain/prepare.py` (`should_enrich` and `_is_derived` honour `meta["force_enrich"]`)
  - `mcpbrain/store.py`: `reinclude_chunks(doc_ids) -> int` (sets `enrich_state=''`, `enriched=0`, `enriched_version=0`, `enrich_attempts=0` and `metadata.force_enrich=true`); a `priority INTEGER DEFAULT 0` column on `chunks`, with `unenriched_chunks` ordered `priority DESC, rowid DESC`; `prioritise_chunks(doc_ids) -> int`
  - `mcpbrain/control_api.py`; remove `/api/enrich-backfill/start|cancel` and `Daemon.start_enrich_backfill`/`cancel` (they import a deleted module)

**Interfaces:**
- Produces routes:
  - `POST /api/sync-now` with body `{"source": "gmail"|"drive"|"calendar"|"anarlog"}` or `{}`.
  - `GET /api/chunks/cold?page=&page_size=50` and `GET /api/chunks/waiting?page=&page_size=50`, each returning `{"items": [{doc_id, title, source, date, reason}], "total", "page"}`. `reason` names the rule: promotions, bulk, table, own automated mail, Drive folder, superseded draft or consolidated note.
  - `GET /api/chunks/reflow-gave-up`: the chunks of owners whose reflow outcome is `gave_up`, same item shape.
  - `POST /api/chunks/reinclude {"doc_ids": [...]}` and `POST /api/chunks/prioritise {"doc_ids": [...]}`.
  - `source_controls.cold_reason(meta, enrich_state) -> str`.

- [ ] **Step 1: Write the failing tests.**
  - With `sources.gmail=false`, a sync cycle calls no Gmail discovery and a queued Gmail row is deferred, not failed.
  - `sync_now("drive")` runs only Drive in the next cycle.
  - A message from an excluded sender, or carrying an excluded label, is not indexed and leaves one `ingest_skip` row whose detail names the rule.
  - A re-included promotions chunk is still eligible after `prepare_units` runs (the Review Focus 4 case).
  - Prioritised chunks come first from `unenriched_chunks`.
  - Cold and waiting lists page correctly and give the reason.
  - `/api/enrich-backfill/start` now returns 404.
- [ ] **Step 2: Run** the tests and expect failures.
- [ ] **Step 3: Implement** as listed. The `priority` column is added by `ALTER TABLE` guarded on `PRAGMA table_info`, the way other columns are migrated in `store.py`.
- [ ] **Step 4: Run** `uv run pytest tests/test_source_controls.py tests/test_sync_cycle.py tests/test_gmail_sync.py tests/test_prepare.py tests/test_salience_gate.py tests/test_thread_enrich.py tests/test_source_class.py tests/test_control_api*.py -q`. Expect a pass.
- [ ] **Step 5: Review gate, then commit** `feat(console): per-source control, exclusions, re-include and priority`.

### Task 5: System controls

**Files:**
- Create: `mcpbrain/system_ops.py`, `tests/test_system_ops.py`
- Modify: `mcpbrain/agents.py` (`_launchd_plist` takes `run_at_load: bool`; `_install_launchd` reads `config.start_at_login(home)`, default True; Windows uses `schtasks /change /enable|/disable`), `mcpbrain/tray.py` (menu item "Start mcpbrain" when the daemon is unreachable, calling `agents.restart_agent`), `mcpbrain/control_api.py`, `tests/test_tray.py`, `tests/test_agents*.py`

**Interfaces:**
- Produces:
  - `system_ops.request_restart(daemon) -> None`: replies first, then a thread calls `daemon.stop()` and `agents.restart_agent(platform)`. It never uses `_restart_process`, which spends the watchdog budget.
  - `system_ops.set_start_at_login(home, on: bool) -> {"ok", "applies": "next_login"}`.
    - On: the plist gets `RunAtLoad true`, `KeepAlive true`.
    - Off: `RunAtLoad false`, `KeepAlive {SuccessfulExit:false}`.
    - It rewrites the plist without reloading, so the running daemon is untouched.
  - `system_ops.check_update() -> {"installed", "latest", "newer": bool}`, using `update._installed_version` and `update._latest_version` with the existing 30 s timeout. `system_ops.apply_update(daemon) -> None` sets `daemon._pending_update`.
  - `system_ops.Diagnostics` holds one background run. `start(home, daemon_status)` calls `doctor.run_doctor(home, repairs={}, daemon_status=…)`. `result() -> {"running", "ran_at", "code", "lines": [{"status": "ok"|"warn"|"bad"|"info", "text"}]}`: a line starting `✅` is ok, `⚠️` warn, `❌` bad, anything else info.
  - `system_ops.tail_log(home, name: "daemon"|"tray", max_bytes=65536) -> str`.
  - `system_ops.support_report(home, daemon_status) -> str`: version, health, the last diagnostics, and the last 200 log lines. It redacts the owner's email addresses, names and aliases (from config), plus home-directory paths.
  - Routes:
    - `POST /api/restart` returns 202 `{"restarting": true}`.
    - `GET /api/system` returns `{start_at_login, model: model_status(), version, watchdog_exits}`.
    - `POST /api/system/login {"on": bool}`.
    - `GET /api/update/check` and `POST /api/update/apply`.
    - `POST /api/diagnostics/run`, which returns 202, and `GET /api/diagnostics`.
    - `GET /api/logs/<name>`, served as `text/plain`.
    - `GET /api/support/report`, served as `text/plain`.

- [ ] **Step 1: Write the failing tests.**
  - The restart route returns 202 before `restart_agent` is called (a fake records call order).
  - Login off writes a plist with `<key>RunAtLoad</key>\n    <false/>` and the crash-only `KeepAlive`, and does not call launchctl.
  - Update check: when `latest > installed`, `newer` is True; network failure returns `latest: null, newer: false`.
  - Diagnostics runs with `repairs == {}` and the fake `daemon_status`, and parses ✅/⚠️ into statuses.
  - The support report contains no string from a fixture `owner_email`, `owner_full_name` or `owner_aliases`, and no `/Users/`.
  - The tray shows "Start mcpbrain" when unavailable and not when available.
- [ ] **Step 2: Run** the tests and expect failures.
- [ ] **Step 3: Implement** as listed.
- [ ] **Step 4: Run** `uv run pytest tests/test_system_ops.py tests/test_tray.py tests/test_agents*.py tests/test_doctor*.py tests/test_update*.py tests/test_control_api*.py -q`. Expect a pass.
- [ ] **Step 5: Review gate, then commit** `feat(console): restart, login item, updates, diagnostics and logs`.

### Task 6: Shared shell, theme and the Overview

**Files:**
- Create:
  - `mcpbrain/wizard/console.css`: tokens and components from the prototype's `console.css` and `skins.css` ("room" skin only, renamed to the neutral palette), keeping only what the three pages use.
  - `mcpbrain/wizard/console.js`: the theme head snippet (mode and accent applied before paint) plus `api(path, opts)` with the offline handling.
  - `mcpbrain/wizard/fonts/` holding `HostGrotesk-{400,500}.woff2`, `AtkinsonHyperlegibleMono-{400,500}.woff2` and `OFL.txt`.
  - `tests/test_console_assets.py`.
- Modify:
  - `mcpbrain/wizard/dashboard.html` (full rewrite to layout A)
  - `mcpbrain/control_api.py`: `GET /static/<name>` and `/static/fonts/<name>`, served before auth and matching `^[A-Za-z0-9._-]+\.(css|js|woff2|txt)$`, with a resolve-and-parent check and a MIME map, like `_serve_vendor`
  - `pyproject.toml` package-data: `"mcpbrain.wizard" = ["*.html", "*.css", "*.js"]` and `"wizard/fonts/*"`
  - tests pinning the old page: `tests/test_dashboard_circles.py`, `tests/test_reflow_visibility.py`, `tests/test_graph_page.py` (only its dashboard "Explore graph" assertion), `tests/test_package_data.py`

**Interfaces:**
- Consumes Tasks 1 to 5:
  - `/api/dashboard/stats`, `/api/changes`, `/api/changes/<id>/undo`, `/api/changes/added`
  - `/api/pending`, `/api/findings`, `/api/corrections/pending`
  - `/api/sync-now`, `/api/pause|resume`, `/api/backup/run`
  - `/api/chunks/cold|waiting|reinclude|prioritise`
- Produces:
  - `console.js` exports on `window.mcpConsole`: `api(path, {method, body}) -> Promise<json>`, which marks the page offline on failure; `applyTheme()`; `relTime(iso)`.
  - Theme storage keys `mcpbrain-mode` and `mcpbrain-accent` (`{hex, h, c, coolH, badH}`).
- Overview sections, in this order:
  - the top bar with the health chip and the mode toggle;
  - the reading panel (96% plus facts and two cells), with the Needs-you panel beside it;
  - "What changed" in Today and Yesterday columns, grouped;
  - Knowledge graph and Sources (with System folded in).
- Every action from spec section 7 is wired. Each Undo calls `/api/changes/<id>/undo` and shows the reason on 409. The page draws loading (skeleton), empty, error and full states for each region.

- [ ] **Step 1: Write the failing tests** in `test_console_assets.py`:
  - `/static/console.css`, `/static/console.js` and `/static/fonts/HostGrotesk-400.woff2` serve 200 with the right MIME types;
  - `/static/../store.py` and `/static/x.py` give 404;
  - the built wheel's file list includes `wizard/console.css` and `wizard/fonts/OFL.txt` (`uv build` into a temp directory, then inspect the wheel);
  - `dashboard.html` contains no `fonts.googleapis`, no `http` URLs, and no `#[0-9a-f]{3,6}` or `oklch(` outside `console.css`;
  - `node --check` passes on every inline script;
  - the page source contains each endpoint path listed under Consumes.

  Update the pinned tests to the new ids. `s-communities` becomes `g-circles`. The reflow hooks become `system.reflow` rendering in the System block. "Explore graph" becomes "Open the graph".
- [ ] **Step 2: Run** the tests and expect failures.
- [ ] **Step 3: Implement** the CSS, JS, fonts (download the four `.woff2` files once from the Google Fonts static CSS, record their URLs and the OFL in `fonts/OFL.txt`), the static route, packaging and the page.
- [ ] **Step 4: Run the Hallmark linter.** Run `node ~/.claude/skills/hallmark/scripts/sloplint.mjs mcpbrain/wizard/dashboard.html --genre modern-minimal --scope app` if the skill is installed. Expect 0 FAIL, and fix any that appear. If it isn't installed, note that in the commit.
- [ ] **Step 5: Run** `uv run pytest tests/test_console_assets.py tests/test_dashboard*.py tests/test_reflow_visibility.py tests/test_package_data.py tests/test_graph_page.py -q`. Expect a pass.
- [ ] **Step 6: Live check** with the `run` skill. Start the daemon from this branch, open `/dashboard`, and confirm:
  - light and dark both render;
  - an Undo on a real `profile_updated` row restores it;
  - stopping the daemon shows the offline state.
- [ ] **Step 7: Review gate, then commit** `feat(console): overview redesign on a shared shell and theme`.

### Task 7: Settings and Setup

**Files:**
- Create: `mcpbrain/wizard/settings.html`, `tests/test_settings_page.py`
- Modify:
  - `mcpbrain/wizard/index.html`: setup only, three linear steps, then a done screen that links to `/dashboard`; the configured-state `#home-status` block is removed
  - `mcpbrain/control_api.py`: `GET /settings`, with token injection like `_serve_dashboard`; `GET /` returns 302 to `/dashboard` when `config.is_configured(home)`
  - `tests/test_wizard_assets.py` and `tests/test_wizard_serve.py`, rewritten to the setup-only markers
  - `tests/test_tray.py` (Settings is reachable from the top bar; the tray keeps "Open Dashboard")

**Interfaces:**
- Consumes:
  - `/api/config` (profile, org rows), `/api/settings` (everything else)
  - `/api/pending` (count only)
  - `/api/system`, `/api/system/login`, `/api/restart`, `/api/update/check|apply`, `/api/diagnostics/run`, `/api/diagnostics`, `/api/logs/<name>`, `/api/support/report`
  - `/api/model/status|ensure`, `/api/sync-now` with `source`, `/api/backup/run`
  - `/api/graph/suppressed`, `/api/graph/entity/<id>/restore` (Task 8)
  - `/api/auth/start`, `/api/connect-desktop`
- Sections, in order, from spec section 9 as amended by section 10:
  - **Profile**, including Names you go by. Derived names come from `owner_full_name` / `owner_name`.
  - **Appearance.** The mode radio and the main colour (six presets plus "Any colour") write the theme storage keys, with the colour rules from Global Constraints.
  - **Sources.** One switch per source. Drive shows "N shared drives" and "M not syncing" from Task 1's `shared`. Two kinds of keep-out list are labelled:
    - "Searchable, kept out of the graph" for subject starts and folders;
    - "Not indexed" for senders and labels.
  - **Automatic changes.** Seven rows, including Graph tidy-up with its per-pass cap. These save on change.
  - **Connections.**
  - **Hidden entities**, with Restore.
  - **System.**
    - The status line, Check for update / Update now, and Restart, whose confirm text names the reconnect.
    - Start when I log in, with "Takes effect next time you log in".
    - Search model, Diagnostics, Open logs, and Copy a report.
    - No catch-up row.
  - **Advanced.** Fleet IDs read-only until Edit.
- Exactly one filled primary button: Save profile.

- [ ] **Step 1: Write the failing tests.**
  - `/settings` serves with the token.
  - `/` redirects to `/dashboard` when configured and serves setup when not.
  - `settings.html` contains every section id; references each consumed endpoint; has no `enrich-backfill`; has exactly one `btn--primary`; passes `node --check`; and has no colour literals outside the tokens.
  - The setup page keeps the strings the onboarding tests require ("Connect Google", "About you", "Connect Claude Desktop", `/api/backup/auto`, `/api/records/scaffold`). Assertions about the configured-home markers are removed, and each removed assertion is listed in the commit message.
- [ ] **Step 2: Run** the tests and expect failures.
- [ ] **Step 3: Implement** the pages and routes.
- [ ] **Step 4: Run** the Hallmark linter on `settings.html` and `index.html` (as in Task 6), then `uv run pytest tests/test_settings_page.py tests/test_wizard*.py tests/test_tray.py tests/test_onboarding*.py -q`. Expect a pass.
- [ ] **Step 5: Live check.** Toggle Graph tidy-up to "Ask me first". Within the next daily review (or a forced `_run_review` in a test harness against a copy of the store), confirm that items appear in Needs you instead of being applied. Then switch it back.
- [ ] **Step 6: Review gate, then commit** `feat(console): settings page and setup flow`.

### Task 8: Graph page, then whole-branch verification

**Files:**
- Modify:
  - `mcpbrain/wizard/graph.html`: shell, theme and the new filter column. The force-graph setup, layouts, hover rings, right-click focus, search-and-jump and the edit/merge flows stay.
  - `mcpbrain/graph_view.py`:
    - `graph_canvas(…, unlinked: bool = False, since_hours: int = 0)`;
    - `graph_types(store) -> list[{type, count}]`;
    - `list_suppressed(store) -> list[{id, name, type, suppressed_at, reason}]`;
    - `ego` takes `relation: str = ""`.
  - `mcpbrain/control_api.py`:
    - `GET /api/graph/types` and `GET /api/graph/suppressed`;
    - `POST /api/graph/entity/<id>/restore` calls `store.unsuppress_entity` and records an undoable `entity_unsuppressed` change;
    - `POST /api/graph/entity/<id>/facts {attribute, value}` writes a manual observation the way `graph_corrections._apply_set_field` does (`source='manual'`), rejects values over 80 characters with 400, and records an undoable change.
  - Tests: `tests/test_graph_page.py` (markers kept or updated) and `tests/test_graph_routes.py`.

**Interfaces:**
- URL state: `?type=` (repeatable), `?entity=<id>`, `?show=unlinked`, `?view=circles`, `?since=24h`, `?rel=<relation>`.
- Kinds: the filter column lists every type from `/api/graph/types`. `document` and `thread` start off, and so does any type outside `contract.ENTITY_TYPES` (shown as "Unclassified").
- Colours: on a mode or accent change, the page calls `readPalette()` again and refreshes force-graph's colour accessors.

- [ ] **Step 1: Write the failing tests.**
  - `unlinked=1` returns only zero-degree entities.
  - `since_hours=24` returns only entities with a relation or observation source first seen in 24 h.
  - `types` counts match `console_stats.graph_counts`.
  - The suppressed list and restore round-trip, and the restore is undoable.
  - A fact over 80 characters gives 400; a valid fact appears in `entity_detail`.
  - The page markers from `tests/test_graph_page.py` (`new ForceGraph`, `renderTypeChips` or its replacement, `showMergePreview`, every `/api/graph/*` path) still hold, and `node --check` passes.
- [ ] **Step 2: Run** the tests and expect failures.
- [ ] **Step 3: Implement** as listed.
- [ ] **Step 4: Whole-branch verification.**
  - `uv run pytest -q` all pass.
  - `uv run ruff check .` is clean.
  - `uv run pytest tests/test_no_tenant_literals.py -q` passes with `tenant_people.json` present.
  - The Hallmark linter reports 0 FAIL on all three pages.
- [ ] **Step 5: Live check** with the `run` skill against the real store:
  - the Overview number is at most 100 and matches `index_counts`;
  - one Undo works;
  - one Ask-me-first item appears in Needs you;
  - Settings saves a keep-out sender and the next Gmail sync skips it;
  - Diagnostics runs;
  - the Graph opens filtered from an Overview link.

  Record the results in the PR description draft.
- [ ] **Step 6: Final review gate.** Run one `feature-dev:code-reviewer` over the whole branch diff against the spec, fix any findings, then commit `feat(console): graph page and console verification`. Report to the owner. No push or release until they say so (`docs/RELEASE-RUNBOOK.md`).
