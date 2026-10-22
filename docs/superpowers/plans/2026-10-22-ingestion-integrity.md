# Ingestion Integrity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the brain treating its own output (Claude sessions, briefs, drafts, summaries, digests, duplicates) as evidence; count strength by independent sources; then clean the existing store reversibly.

**Architecture:** One `source_class` classifier decides, for every chunk, whether it is primary evidence. Derived classes are cold (searchable, never enriched). Writers record per-source support rows instead of bumping counters. An attended `bin/clean_ingestion.py` applies the same rules to existing rows, reversibly, after a dry run and gold gate on a store copy.

**Tech Stack:** Python 3.12 stdlib, existing Store/migrations, bge-small embedder already in the daemon.

**Spec:** `docs/superpowers/specs/2026-10-22-ingestion-integrity-design.md`

## Global Constraints

- No new dependencies. Embedding similarity threshold for actions: cosine ≥ 0.88 (bge-small, normalised vectors).
- Classes and their evidence status exactly as the spec table: primary_mail / primary_doc / primary_event = evidence; session / brief / draft / consolidated / digest = not evidence; duplicate = carries the canonical's evidence, adds none.
- Brief rules are config, never code literals (`tests/test_no_tenant_literals.py` must pass): `ingest.generated_gmail_subject_prefixes` (list[str]), `ingest.generated_drive_folders` (list[str] of folder paths), generic markers: Gmail label NAME `mcpbrain/generated` (resolved to its id via `labels().list`, cached), Drive `appProperties.mcpbrain_generated == "1"`.
- Claude session id metadata key is `claude_session_id` (NOT `session_id`, which anarlog uses and `_chunk_key` keys on).
- Schema changes are additive only (ALTER ADD COLUMN with constant default / CREATE TABLE IF NOT EXISTS, following store.py's existing pattern; indexes on ALTERed columns created after the ALTER).
- Nothing is deleted by the clean-up; archives use `status='auto_archived'` + `resolved_by='<step>'`; flags/prior values make each step undoable.
- The deadline column is `actions.deadline`.
- Tests use real `Store(tmp_path/..., dim=4)`; never touch the live store or Google. Scope test runs to impacted files; the owner runs the full suite.
- Every commit ends with the session's Co-Authored-By / Claude-Session trailer lines.

## Review Focus

1. **A real email or Drive document misclassified as derived** (cold → its actions/links vanish). Expect: the brief rules match only configured subject prefixes WITH sender = recipient = owner, or the configured folders, or the explicit marker; a forwarded brief from someone else stays primary. Test in Task 2 (`test_brief_rule_requires_self_sent`).
2. **Sent mail vs drafts after send:** a draft that is later sent arrives as a new SENT message (primary) — its actions must come from the sent message. Test in Task 4 (`test_sent_version_is_primary_draft_is_not`).
3. **Recall regressions** from cold-marking: cold chunks stay in recall (`recall_excludes_cold` default OFF). Gold gate in Task 11 must hold.
4. **Undo really restores:** each clean-up step's `--undo` returns counts and rows to their prior values. Test in Task 10 (`test_each_step_undo_restores_rows`).
5. **Strength semantics change** surprises consumers (graph view, decay, org contribution thresholds): strength becomes "distinct primary sources", so values drop sharply. Test in Task 7 that decay never zeroes a relation that still has a primary source, and contribution thresholds use the new count.

---

### Task 1: Schema additions

**Files:** Modify `mcpbrain/store.py` (init migrations). Test `tests/test_store_schema_integrity.py` (new).

**Interfaces — Produces:**
- `chunks.dup_of TEXT DEFAULT NULL`; expression index `idx_chunks_noteid` on `_meta_extract("$.note_id")`.
- `email_entities.derived INTEGER NOT NULL DEFAULT 0`.
- `entity_relations.derived INTEGER NOT NULL DEFAULT 0`, `entity_relations.legacy_strength INTEGER DEFAULT NULL`.
- `entity_observations.derived INTEGER NOT NULL DEFAULT 0`.
- `actions.prior_thread_id TEXT DEFAULT NULL`, `actions.prior_owner TEXT DEFAULT NULL`.
- Tables `relation_sources(relation_id INTEGER NOT NULL REFERENCES entity_relations(id) ON DELETE CASCADE, source_key TEXT NOT NULL, source_class TEXT NOT NULL, first_seen TEXT NOT NULL, PRIMARY KEY(relation_id, source_key))` and `observation_sources(observation_id INTEGER NOT NULL REFERENCES entity_observations(id) ON DELETE CASCADE, source_key TEXT NOT NULL, source_class TEXT NOT NULL, first_seen TEXT NOT NULL, PRIMARY KEY(observation_id, source_key))`; table `cleanup_log(id INTEGER PRIMARY KEY, step TEXT, table_name TEXT, row_key TEXT, column_name TEXT, old_value TEXT, at TEXT)` for undo.

- [ ] Step 1: tests — fresh init has every column/table/index; re-init on an already-initialised store is a no-op and keeps `idx_chunks_noteid` usable (`EXPLAIN QUERY PLAN` uses it for a `$.note_id` filter).
- [ ] Step 2: run → FAIL. Step 3: implement. Step 4: run `uv run pytest tests/test_store_schema_integrity.py tests/test_store_schema.py tests/test_store_schema_p3.py -q` → PASS. Step 5: commit `feat(store): schema for source classes, per-source support, reversible clean-up`.

### Task 2: Source classifier

**Files:** Create `mcpbrain/source_class.py`; modify `mcpbrain/config.py` (getters). Test `tests/test_source_class.py`.

**Interfaces — Produces:**
- `class SourceClass(str, Enum): PRIMARY_MAIL, PRIMARY_DOC, PRIMARY_EVENT, SESSION, BRIEF, DRAFT, CONSOLIDATED, DIGEST, DUPLICATE` with values `"primary_mail"` … `"duplicate"`.
- `EVIDENCE = frozenset({PRIMARY_MAIL, PRIMARY_DOC, PRIMARY_EVENT})`.
- `@dataclass(frozen=True) class Rules: owner_emails: frozenset[str]; subject_prefixes: tuple[str, ...]; drive_folders: tuple[str, ...]; generated_label_ids: frozenset[str]` and `Rules.from_config(home, *, generated_label_ids=frozenset()) -> Rules` (owner emails from `config.owner_email` + `google_account` sidecar, lower-cased).
- `classify(doc_id: str, meta: dict, rules: Rules) -> SourceClass`. Order: `enriched-` prefix → DIGEST; `meta.dup_of`/chunk `dup_of` passed in meta → DUPLICATE; `doc_id` starts `note-consolidated-` or `meta.observation_type == "consolidated"` → CONSOLIDATED; note with `"session"` in comma-split `meta.tags` or title matching `^Session [0-9a-f]{8} \d{4}-\d{2}-\d{2}` or `meta.claude_session_id` → SESSION; Gmail (`source_type == "gmail"`): label ids contain `"DRAFT"` → DRAFT; label ids ∩ `generated_label_ids` or (subject startswith any prefix and sender address ∈ owner_emails and every `to` address ∈ owner_emails) → BRIEF; else PRIMARY_MAIL; Drive (`source_type == "gdrive"`): `meta.app_generated == "1"` or `folder_path` equals or is under any configured folder → BRIEF; else PRIMARY_DOC; calendar (`cal-` doc ids / `event_id`) → PRIMARY_EVENT; everything else (attachments, notes, anarlog) → PRIMARY_DOC.
- `config.generated_gmail_subject_prefixes(home) -> list[str]`, `config.generated_drive_folders(home) -> list[str]` (from `config.json` `ingest` block; default `[]`).
- `is_evidence(cls) -> bool`.

- [ ] Step 1: tests — one per class incl. `test_brief_rule_requires_self_sent` (same subject from another sender → PRIMARY_MAIL), `test_folder_rule_matches_subfolders_not_prefix_names` ("My Drive/Daily Briefs/x" matches, "My Drive/Daily Briefs Archive" does not), `test_session_by_tag_title_or_id`, `test_anarlog_session_id_is_not_claude_session`.
- [ ] Steps 2-5 as usual; run `uv run pytest tests/test_source_class.py tests/test_no_tenant_literals.py -q`; commit `feat: one source classifier for evidence vs derived content`.

### Task 3: Session capture — one note per session, no maintenance runs, cold

**Files:** Modify `mcpbrain/session_hooks.py`, `mcpbrain/control_api.py` (`/api/session/ingest`), `mcpbrain/drain.py` (`drain_captures` kind `ingest`). Tests `tests/test_session_hooks.py`, `tests/test_drain_captures.py`, `tests/test_control_api_post.py`.

**Interfaces — Consumes:** Task 2 `classify`. **Produces:**
- Envelope gains `claude_session_id` (full id).
- `drain_captures`: when the envelope has `claude_session_id`, `base_doc_id = "note-session-" + sha256(claude_session_id)[:24]` (stable), metadata includes `claude_session_id`; the capture REPLACES any prior chunks of that base id (delete pieces beyond the new count, upsert the rest), and every piece is written with `enrich_state='cold'`.
- `_read_transcript_turns` / `_capture_session` skip transcripts that are sidechain/subagent (`isSidechain` true on the turns, or entrypoint not interactive), or whose first user turn carries the scheduled-task marker (find the marker scheduled tasks put in the first prompt; read a real transcript under ~/.claude/projects read-only to confirm the field, and record it in the test).

- [ ] Tests: `test_recapture_replaces_not_appends` (two captures of one session → one base note, latest text), `test_session_notes_are_cold`, `test_sidechain_and_scheduled_transcripts_not_captured`, `/api/session/ingest` same rules. Run the three test files; commit `fix(sessions): one cold note per Claude session; skip maintenance transcripts`.

### Task 4: Derived classes are never enriched; drafts lose the owner boost

**Files:** Modify `mcpbrain/prepare.py` (`should_enrich` / `_apply_salience_gate` run the classifier first, independent of `salience_gate_enabled`), `mcpbrain/importance.py` (`_is_owner_authored` returns False for DRAFT labels), `mcpbrain/consolidation.py` (`_write_note` writes cold), `mcpbrain/sync/drive.py` (request `appProperties` in the file fields used by changes, `handle_drive_item`, shared-drive and backfill gets; stamp `app_generated` into metadata when `appProperties.mcpbrain_generated == "1"`), `mcpbrain/sync/gmail.py` (resolve the `mcpbrain/generated` label id once per cycle via `users().labels().list`, cache in meta; pass to `Rules`), `mcpbrain/store.py` (draft-version superseding: when a Gmail DRAFT message is ingested, older DRAFT messages of the same `thread_id` are set cold and `metadata.superseded=true`). Tests `tests/test_prepare.py`, `tests/test_salience_gate.py`, `tests/test_importance.py`, `tests/test_consolidation.py`, `tests/test_drive_sync.py`, `tests/test_gmail_sync.py`.

**Interfaces — Consumes:** Task 2. **Produces:** `prepare.derived_cold(store, chunks, rules) -> list[str]` (doc ids set cold because their class is not evidence and not DUPLICATE).

- [ ] Tests: `test_sent_version_is_primary_draft_is_not`, `test_brief_and_consolidated_never_reach_units`, `test_draft_no_owner_boost`, `test_drive_app_generated_marker_stamped`, `test_gmail_generated_label_resolved_and_cached`, `test_older_drafts_superseded_newest_stays_searchable`. Commit `feat: derived sources are searchable but never enriched`.

### Task 5: Notes are one thread; thread order by real time

**Files:** Modify `mcpbrain/thread_enrich.py` (`_chunk_key` note_id branch before the message-id fallback; `reassemble_thread` sort), `mcpbrain/store.py` (`_doc_ids_query`/`doc_ids_for_messages` note_id arm before the fallback, with its bind list), `mcpbrain/graph_write.py` (lead selection at ~:1120), `mcpbrain/prepare.py` (~:745 lead). Tests `tests/test_thread_enrich.py`, `tests/test_graph_write.py`, `tests/test_prepare.py`, `tests/test_drain.py`.

**Interfaces — Produces:** `thread_enrich.message_time(m: dict) -> datetime` (UTC; RFC-2822 via `email.utils.parsedate_to_datetime`, ISO fallback, unparseable → `datetime.min.replace(tzinfo=UTC)`), used by all three sort/lead sites.

- [ ] Tests: `test_note_chunks_group_into_one_thread`, `test_doc_ids_for_note_id_returns_all_pieces`, `test_thread_sorted_by_time_not_weekday_string`, `test_lead_is_earliest_message`. Commit `fix: notes extract as one thread; thread order and lead by real time`.

### Task 6: Action dedupe by meaning; owner canonical; re-extraction supersedes

**Files:** Modify `mcpbrain/graph_write.py` (`_find_near_duplicate_action` slow path → embedding similarity; `_write_actions` owner canonicalisation, drop "claude"/"assistant"/"unclear" owners to ''), `mcpbrain/drain.py` (forward `embedder` to `apply`), `mcpbrain/stale_reextract.py` + `mcpbrain/drain.py` (supersede). Tests `tests/test_graph_write.py`, `tests/test_drain.py`, `tests/test_stale_reextract.py`.

**Interfaces — Produces:**
- `graph_write.ACTION_SIM_THRESHOLD = 0.88`.
- `_find_near_duplicate_action(conn, text, owner, *, thread_id, embedder, window_days=30, today=None) -> int | None`: fast path unchanged (fingerprint/normalised equality); slow path compares the embedding of `text` against open actions with the same `thread_id` (any age) plus same-owner open actions in the window (cap 300 candidates), returns the best id with cosine ≥ threshold. `embedder=None` → lexical slow path as today (back-compat).
- Owner canonicalisation: any string matching an owner alias (case-insensitive) → `config.owner_name`; "claude", "assistant", "unclear" → `''`.
- Supersede: `stale_reextract.sweep` records the thread ids it re-queues (`stale_reextract.triggered_at` already exists); when `apply` processes a thread whose row is in `stale_reextract` with `triggered_at` newer than the action's `created_at`, open actions of that thread with `source='email'` that the new extraction neither resolved, updated nor re-matched are set `status='auto_archived', resolved_by='superseded_reextract'`.
- `mark_thread_unenriched` also matches `$.note_id` and `$.file_id` keyed actions (thread ids that are note/file ids).

- [ ] Tests: `test_paraphrase_dedupes_within_thread_at_088` (use a stub embedder that maps texts to fixed vectors), `test_unrelated_action_not_merged`, `test_owner_alias_canonical_and_claude_owner_blank`, `test_reextract_supersedes_unmatched_open_actions`, `test_drain_forwards_embedder`. Commit `feat(actions): dedupe by meaning, canonical owner, re-extraction supersedes`.

### Task 7: Strength = independent primary sources

**Files:** Modify `mcpbrain/graph_write.py` (`upsert_relation_in`, `_bump_observation`, `write_observation`, `_ensure_works_at`, `_bump_email_count`, `decay_relations`), `mcpbrain/store.py` (`upsert_topic_entity` no per-call bump; `upsert_entity` mentions; `link_email_entity` gains `derived` param), `mcpbrain/graph_view.py` (strength reads exclude `derived=1` and `origin='legacy'` ordering), `mcpbrain/org_contrib.py` (thresholds on the new strength). Tests `tests/test_graph_write.py`, `tests/test_graph_write_provenance.py`, `tests/test_graph_view.py` (or existing), `tests/test_org_contrib*.py`.

**Interfaces — Produces:**
- `graph_write.record_relation_source(conn, relation_id, source_key, source_class) -> None` (INSERT OR IGNORE) and `relation_strength(conn, relation_id) -> int` = count of rows with `source_class` in EVIDENCE (min 1 for user-asserted relations).
- `apply` computes `source_key` per extraction: thread_id for mail, file_id for Drive, base note id for notes, event id for calendar, `dup_of`'s key for duplicates; `source_class` via Task 2. Relations/observations/links from non-evidence classes are written with `derived=1` (or not at all for digests) and add no support row.
- `strength` column is SET from `relation_strength` after recording a source (never `+1`); observations likewise via `observation_sources` → `observed_count`.
- `email_count` = count of non-derived `email_entities` rows for the entity (incremented only when a NEW non-derived link row is inserted, as today, and topics stop bumping per call); `mentions` bumped only on a new source.
- `decay_relations` never zeroes a relation with ≥ 1 evidence source; relations with `origin='legacy'` are excluded from hub/strength ranking in graph_view.

- [ ] Tests: `test_reextraction_does_not_raise_strength`, `test_second_independent_source_raises_strength`, `test_derived_source_adds_no_support`, `test_topic_email_count_per_distinct_message`, `test_decay_keeps_evidenced_relation`, `test_graph_view_ignores_derived_and_legacy`. Commit `feat(graph): strength counts independent primary sources`.

### Task 8: Exact duplicates carry forward instead of re-extracting

**Files:** Modify `mcpbrain/store.py` (`upsert_chunk`: if `content_hash` of a new/changed chunk equals that of an existing ENRICHED chunk with a different doc_id, set `dup_of` to that doc_id and `enriched=1`, `enrich_state='duplicate'`), `mcpbrain/prepare.py` (`unenriched_chunks` already skips enriched). Tests `tests/test_store.py` or new `tests/test_duplicate_carry.py`.

**Interfaces — Produces:** `store.canonical_for(doc_id) -> str` (follows `dup_of` once); Task 7's `source_key` for a duplicate uses the canonical's key. Exclude chunks whose text is < 200 chars from duplicate carry (boilerplate lines).

- [ ] Tests: `test_identical_text_marks_dup_and_skips_extraction`, `test_short_chunks_not_carried`, `test_changed_canonical_does_not_orphan_dup` (if the canonical changes, the dup is re-queued: `dup_of` cleared, enriched=0). Commit `feat(ingest): identical content is extracted once`.

### Task 9: Removal paths invalidate actions and links

**Files:** Modify `mcpbrain/sync/drive.py` (remove events, My Drive and shared), `mcpbrain/sync/gmail.py` (`handle_gmail_item` 404 and the orphan sweeps), `mcpbrain/store.py` (`invalidate_sources_for_docs(doc_ids, *, reason)`). Tests `tests/test_drive_sync.py`, `tests/test_gmail_sync.py`.

**Interfaces — Produces:** `store.invalidate_sources_for_docs(doc_ids, *, source_ids, reason) -> dict` — archives open actions whose `source_doc_id` ∈ source_ids or `thread_id` ∈ source_ids (`resolved_by='source_gone'`), marks `email_entities` rows with `message_id` ∈ source_ids `derived=1`, and calls the existing relation invalidation.

- [ ] Tests: `test_drive_remove_archives_actions_and_marks_links`, `test_gmail_404_archives_actions`. Commit `fix(sync): removed sources take their actions and links with them`.

### Task 10: Attended clean-up script

**Files:** Create `bin/clean_ingestion.py`; test `tests/test_clean_ingestion.py`.

**Interfaces — Consumes:** Tasks 1-9 (`classify`, `Rules`, `relation_strength`, `cleanup_log`). **Produces:** CLI `python bin/clean_ingestion.py [--home H] [--store PATH] [--yes] [--steps a,b] [--undo STEP] [--skip-snapshot-for-tests]`.
- Refuses when the daemon runs (`restore.daemon_running(home)`), unless `--store` points at a copy outside the app dir.
- Before any write with `--yes`: `backup.snapshot` verified (as `bin/consolidate.py`), path printed.
- Steps in spec order: `source_session`, `rekey_notes`, `source_brief`, `source_draft`, `source_consolidated`, `source_digest`, `derived_links`, `dedup_embed`, `legacy_strength`, `sources_backfill`, `source_gone`, `owner_aliases`. Each step: dry-run prints counts; apply writes `cleanup_log` rows for every changed value (table, row key, column, old value) in one transaction per step; `--undo STEP` restores from `cleanup_log` and deletes those log rows.
- `dedup_embed` embeds open action texts with the installed embedder (batch), clusters single-link at ≥ 0.88 within logical source (thread id after `rekey_notes`), keeps deadline > owner > earliest.
- `sources_backfill` derives `relation_sources`/`observation_sources` from `source_doc_id`/`contrib_doc_id`/`email_entities` provenance (map chunk doc ids → logical source keys + classes), then recomputes `strength`, `observed_count`, `email_count`, `mentions` (old values logged).
- After a write run: `PRAGMA integrity_check` must be `ok`, else print the snapshot path and exit 2.

- [ ] Tests: build a small store with one row of each class + duplicates; `test_dry_run_writes_nothing`, `test_steps_apply_in_order_and_counts`, `test_each_step_undo_restores_rows`, `test_refuses_with_daemon_running`, `test_primary_rows_untouched`. Commit `feat(bin): attended, reversible ingestion clean-up`.

### Task 11: Docs and attended verification (controller)

- [ ] Update `CLAUDE.md` (section "Ingestion integrity (2026-10-22)": classes, rulings, numbers) and `docs/RELEASE-RUNBOOK.md` (how to run the clean-up).
- [ ] Controller: local reinstall; `VACUUM INTO` copy → `bin/clean_ingestion.py --store <copy>` dry run → `--yes` on the copy → gold gate on the copy (`MCPBRAIN_HOME=<copy dir> uv run python tests/eval/run_eval.py --gold --k 10`) vs the same gate on an untouched copy; report counts to the owner; the LIVE run waits for the owner's go.
