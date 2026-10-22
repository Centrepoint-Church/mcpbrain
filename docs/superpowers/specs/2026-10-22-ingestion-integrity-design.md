# Ingestion integrity: no self-generated evidence, no counted repeats

Date: 2026-10-22. Status: approved design (owner, in session), implementation follows.
Evidence: a read-only investigation of the live store on 2026-10-22 (12,518 open
actions, 235k chunks, 82,677 relations, 210,404 entity links, 34,346 observations).
The numbers below come from it.

## Why

The brain reads its own output back as evidence, and counts repeats as support.

- **Claude Code session transcripts make 62% of open actions (7,740).** Every
  session is captured as a note (`session_hooks._capture_session`) and enriched
  like mail, so in-session working steps become the owner's "open actions".
  Only 28 match anything in primary sources. Sessions are re-captured as
  overlapping windows (1,255 notes from 363 sessions; 72% repeated text);
  scheduled maintenance runs are captured too (170 notes).
- **Notes are extracted per chunk.** `thread_enrich._chunk_key` has no `note_id`
  branch, so each chunk of a note is its own thread (6,643 open actions keyed
  per chunk) — the same defect already fixed for Drive, calendar and anarlog.
- **Daily briefs come back twice a day.** Each brief is written by an external
  routine as a Gmail draft to self and as a Drive doc, both synced and enriched:
  197 open actions; 59% of Sep–Oct dated actions trace to self-generated sources.
- **Drafts count as commitments.** 451 draft versions (298 superseded) give 322
  open actions and get the owner-authored salience boost. The SOM Form thread's
  23 rows come from one draft, extracted three times, ≈7 real commitments.
- **Machine summaries are re-extracted.** Consolidated notes (430 open actions;
  11 of 12 always-injected core items) and digest residue (407 actions, 30,076
  links) are treated as evidence.
- **Action dedupe is lexical** (`graph_write._find_near_duplicate_action`:
  subset / Jaccard 0.85 / SequenceMatcher 0.82, latest 200 rows store-wide) and
  sees a unit-write-time snapshot of open actions. ≈3,400 open actions collapse
  at 0.88 cosine within one logical source (calibrated on the SOM Form thread:
  0.85 → 7 groups, all correct; 0.88 → 11).
- **Thread order is a date STRING sort** (`thread_enrich.py:424`,
  `graph_write.py:1118`): 38% of multi-message threads are shown out of order,
  31% get the wrong lead message (wrong provenance and deadline anchor).
- **Strength counts re-extractions, not sources.** Relation strength, observation
  counts, topic `email_count` and `mentions` are bumped on every re-observation.
  Legacy `involved_in` rows (April–June import, no provenance, never decay) hold
  96% of all relation strength. Person `email_count` sums to 150k against 57.7k
  real links.
- **40% of chunks ≥200 chars are exact duplicates** (91,541; 27,878 enriched
  again; 7,071 of 13,960 Drive files fully duplicated; one website-snapshot
  folder ≈29k chunks).
- **Orphans and owner split:** 883 open actions and ≈25.5k links point at sources
  that are gone; the owner's actions are split across two name strings.

Owner decisions (2026-10-22): session notes stay searchable but are never mined;
actions come from SENT mail only (no drafts); the source-class clean-up of
≈9,000 open actions is approved, reversible.

## Principles

1. **One classification of every chunk's source**, used by ingest, enrichment,
   counting, dedupe, recall boosts and clean-up alike.
2. **Derived sources are never evidence.** They stay searchable (cold) but
   create no actions, links, relations or observations, and add no strength.
3. **Strength = number of independent primary sources**, recorded per source,
   never incremented per extraction.
4. **Nothing is deleted by the clean-up.** Every change is a status, flag or
   saved prior value that a per-class undo restores.
5. **Search quality may not regress:** the gold gate (recall@10, MRR, on the
   production path) runs on a store copy before and after the clean-up.

## Design

### 1. Source classes (`mcpbrain/source_class.py`, new)

`classify(chunk_meta, doc_id) -> SourceClass` with classes:

| Class | Rule | Evidence? |
|---|---|---|
| `primary_mail` | Gmail message not labelled DRAFT, not a brief | yes |
| `primary_doc` | Drive / attachments / Apple notes / anarlog notes / owner `brain_note`/`brain_memory_write` captures | yes |
| `primary_event` | Calendar | yes |
| `session` | note tagged `session` (Claude Code transcript) | no |
| `brief` | brain-generated brief: Gmail or Drive item matching the configured brief rules or carrying the generated marker | no |
| `draft` | Gmail message labelled DRAFT | no |
| `consolidated` | `note-consolidated-*` | no |
| `digest` | `enriched-*` | no |
| `duplicate` | exact content duplicate of an already-enriched chunk (§6) | carries the original's evidence, adds none |

Brief rules are configuration, not code literals (tenant-neutral):
`ingest.generated_gmail_subject_prefixes` (list), `ingest.generated_drive_folders`
(list of folder paths), plus a generic marker any producer can stamp — Gmail
label `mcpbrain/generated` or Drive `appProperties.mcpbrain_generated = "1"`.
The owner's local config gets `["Brief — "]` and `["My Drive/Daily Briefs"]`.
Implementation notes (from the code map): Gmail labels arrive as label IDs, so
the marker label's id is resolved by name via `labels().list` and cached; Drive
`appProperties` must be added to the fetched file fields; the Claude session id
is stored as `claude_session_id` (`session_id` already belongs to anarlog); a
session note is also recognisable by its `session` tag and `Session xxxxxxxx
YYYY-MM-DD` title for rows captured before this change; the actions deadline
column is `deadline`.

### 2. Prevent at ingest and enrichment

- **Sessions.** Never enriched (cold from capture). One note per session id:
  capture upserts `session-<id>` (latest capture replaces earlier ones, chunked
  losslessly as today). Transcripts of scheduled tasks and subagents are not
  captured at all (`isSidechain`, non-interactive entrypoint, or the scheduled-
  task marker in the first user turn). `/api/session/ingest` follows the same rules.
- **Notes as one thread.** `_chunk_key` returns `note_id` for note chunks (before
  the message-id fallback); `store._doc_ids_query` gets the matching `note_id` arm.
- **Briefs, drafts, consolidated notes, digests:** cold — never enriched, no
  salience/owner boost (`importance._is_owner_authored` excludes DRAFT). Only
  the newest version of a draft thread stays in recall; older versions are cold
  and marked superseded.
- **Thread order.** Sort messages by `email.utils.parsedate_to_datetime` (UTC),
  ISO fallback, in `reassemble_thread` and `graph_write.apply`'s lead selection.
- **Action dedupe at write time.** Replace the lexical slow path with embedding
  similarity (bge-small, cosine ≥ 0.88) against open actions of the same logical
  source (thread / base note / file / event) and same-owner open actions in the
  30-day window; keep the exact/lexical fast path. Re-check against the LIVE open
  list at apply time. When `stale_reextract` re-runs a source, its new extraction
  supersedes that source's open extraction-sourced actions instead of adding.
- **Owner canonicalisation at write.** Owner strings resolve through the owner
  identity's aliases to one canonical owner; "Claude"/"assistant" owners are not
  written.
- **Core tier.** Consolidated notes are not eligible; core requires identity/
  procedural notes or a durability signal (observed across ≥ 3 distinct weeks),
  not tied salience.

### 3. Independent-source counting

New tables (additive): `relation_sources(relation_id, source_key, source_class,
first_seen)`, `observation_sources(observation_id, source_key, source_class,
first_seen)`; `source_key` = logical source (thread id, base note id, file id,
event id, or content hash for duplicates). Writers insert (ignore on conflict)
instead of bumping. `strength` = count of distinct primary-class source keys
(derived classes excluded); observation `observed_count` likewise. Entity
`email_count` and `mentions` are derived from `email_entities` rows of primary
sources, recomputed, not incremented. Decay/graph view/contribution read these.

### 4. Content duplicates

At ingest, a chunk whose normalised text hash equals an already-enriched chunk's
is marked enriched with `dup_of = <canonical doc_id>` and not re-extracted; its
evidence is the canonical's (same `source_key`), so it adds no strength.
Recall is unchanged (retrieval already collapses identical content).

### 5. Removal paths

Drive file removal and Gmail deletion invalidate the source's open actions
(`resolved_by='source_gone'`) and mark its entity links derived — as
`invalidate_local_relations_for_docs` already does for relations.

### 6. Attended clean-up (`bin/clean_ingestion.py`)

Dry-run by default; `--yes` to apply; refuses unless the daemon is booted out
(control-port probe, as `mcpbrain restore`); takes and verifies a snapshot first;
runs `PRAGMA integrity_check` after; prints per-step counts; `--undo <step>`
reverses one step. Steps, in this order (order matters: class archives before
similarity dedupe, or dedupe could keep a derived copy and archive the real one):

1. `source_session` — archive open actions from session notes; cold-mark session
   notes; collapse repeat captures to the latest per session id (others cold).
2. `rekey_notes` — set chunk-keyed note actions' `thread_id` to the base note id
   (old value saved).
3. `source_brief`, `source_draft`, `source_consolidated`, `source_digest` —
   archive their open actions; cold-mark their chunks.
4. `derived_links` — mark entity links / relations / observations whose source is
   a derived class `derived=1` (excluded everywhere; not deleted).
5. `dedup_embed` — cluster remaining open actions at cosine ≥ 0.88 within logical
   source; keep the one with a deadline/owner/earliest; archive the rest.
6. `legacy_strength` — legacy `involved_in` rows: save strength to
   `legacy_strength`, set `origin='legacy'`, exclude from strength ranking.
7. `sources_backfill` — populate `relation_sources` / `observation_sources` from
   existing provenance; recompute strength, observed_count, email_count, mentions.
8. `source_gone` — archive open actions whose source no longer exists; mark
   orphan links derived.
9. `owner_aliases` — canonicalise owner strings on open actions.

Archives use `status='auto_archived'` with the step's `resolved_by`; reversible.

### 7. Verification

- Dry run on a `VACUUM INTO` copy: counts per step; gold gate before/after on the
  copy (recall@10 must not drop; MRR no worse than one rank step).
- Live run attended (bootout → snapshot → run → integrity_check → bootstrap),
  then doctor, gold gate, and the SOM Form thread's open actions reviewed by the
  owner (expect ≈6–7).

## Not done

- Rewriting the external brief routine (outside this repo) — the generated marker
  is offered; config rules cover it today.
- Re-extracting history with the fixed thread order (only new extractions benefit).
