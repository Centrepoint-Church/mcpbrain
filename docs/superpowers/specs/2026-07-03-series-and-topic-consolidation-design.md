# Series & Topic Consolidation — Design

**Date:** 2026-07-03
**Status:** Design (brainstormed; ready for a writing-plans session)
**Author:** Josh + Claude

## Context

mcpbrain enriches email/Drive/calendar into an entity graph. Entity dedup is
deliberately restricted to name-identity types (`person`/`org`/`project`) via
`resolve._NAME_MERGEABLE_TYPES` — enforced in `_deterministic_merges` (#23),
`apply_duplicate_verdicts`, and `_candidate_pairs` (#4, shipped 0.7.86).
Structural/artifact types (`document`/`thread`/`meeting`/`event`/`topic`) are
excluded because merging them by title would wrongly collapse genuinely-distinct
items (generic-title collision) — corruption, worse than clutter.

That exclusion left two real modeling gaps:

- **Meetings fragment.** `meeting`/`event`/`topic` are LLM-extracted entity
  types (`contract.py:32`), minted from thread/doc text and keyed on
  `slugify(name)` — not on any stable calendar/recurrence id. "Centrepoint
  College" → 10 meeting entities over ~2 months; "Youth" → 7; "Staff Prayer" →
  5; 95 of 294 meeting entities sit in same-name groups. A weekly meeting → ~50
  nodes/year. Benign (lint excludes meetings from orphan detection) but
  fragments "what do I know about meeting X" across N nodes.
- **Topics fragment.** 412 topic entities sit in same-name groups. Unlike
  meetings, a topic ("budget", "prayer") is arguably a name-identity *concept* —
  the same topic across threads should be one node.

### The mechanical truth that shapes the whole design

Meeting IDs are `slugify(name)`; topic IDs are `slugify("topic-<tag>")`
(`store.upsert_entity` / `store.upsert_topic_entity`). **Exact-name repeats
already collapse to one node.** So the 294 meeting and 412 topic fragments are
**name / morphology / synonym variants** ("Centrepoint College" vs "…College
Board" vs "…Meeting 12 May"; "budget" vs "budgets" vs "annual budget") — *not*
exact duplicates.

Two consequences:

1. Post-hoc **name-merging is the banned corruption path** (fuzzy grouping of
   distinct-but-similar titles). Both fixes therefore move consolidation
   **upstream to write-time deterministic keying**, so fragments never form —
   and retire the pre-existing fragments **once, under supervision**.
2. **Calendar recurrence is not a usable key for the fragmenting entities.** All
   294 meeting entities are text-extracted and have **no calendar link**:
   `sync/calendar.py` mints `cal-<id>` chunks + person entities + `attended`
   relations only, never a meeting entity, and doesn't even capture
   `recurringEventId`. So "key on `recurringEventId`" can only *opportunistically
   enrich* a name-keyed series, not replace the key.

### Goals (confirmed)

Consolidation must serve, in priority order: **aggregation** ("what do I know
about X" in one node), **graph-explorer cleanliness** (`/graph`), and
**retrieval/recall** — i.e. the graph itself must change, not just a read-time
view.

### Constraints (from the project's arc)

- **Never corrupt the graph.** Consolidation must be reversible + logged,
  conservative-default (skip on uncertainty), and gold-gated (recall@10 ≥ 0.55 /
  MRR ≥ 0.35 unaffected). Unattended mutations especially (C1 role-inbox lesson).
- **Reuse existing machinery**: `entity_observations` (temporal instances), the
  enrich block-unit pipeline, `reflow`/`enriched` re-extraction, the merge
  appliers — over new systems.
- **Ship ON behind a kill-switch**, per project convention.

### Key machinery facts established during design

- `entity_observations(entity_id, attribute, value, source, valid_from,
  valid_to, confidence, …)` is a clean bi-temporal table — but
  `graph_write.write_observation` **supersedes** same-`(entity, attribute,
  source)` rows. Occurrences must **not** use it (it would retire prior
  occurrences); they need an **append-only** insert.
- `store.merge_entities` is **destructive** — it `DELETE`s the loser row
  (`store.py:1378`) and logs only the loser's *name* to `entity_merge_log`. A
  merge is therefore **not** cheaply reversible. Going-forward consolidation
  must avoid merges entirely (deterministic keying does); destructive folding is
  confined to the one-time migration, which is protected by a DB backup.
- Provenance for scoped re-extraction exists: `email_entities(entity_id,
  message_id)` + `store.doc_ids_for_messages()` map a meeting entity to its
  source chunks (`gmail-<msg_id>-body-<i>`); `entity_relations.source_doc_id`
  covers Drive-sourced meetings. No `ENRICH_LOGIC_VERSION` bump needed.

---

## Problem 1 — Meetings: name+org-keyed series with occurrence observations

### Model

One **series** entity per `(normalized series name, org)`. Each mention is a
temporal **occurrence** recorded in `entity_observations`. No name-merge; new
mentions converge on a deterministic id, so fragments never form.

### 1.1 Extraction contract change

- `contract.py` / `enrich_prompt.md` (kept byte-identical via
  `bin/sync_agents.py`): a `meeting` entity gains two **optional** fields:
  - `series_name` — the model normalizes the meeting to its series identity,
    stripping occurrence qualifiers ("12 May", "weekly", "#3", "Week 4",
    dated/occasion suffixes). Falls back to `name` when absent.
  - `occurrence_date` — ISO `YYYY-MM-DD` for this specific instance; falls back
    to the lead message date.
- `sanitize_extraction` passes both through untouched (they are entity-dict
  fields; the sanitizer only drops off-schema *types*/relations). Validation
  stays lenient — a meeting missing the new fields is still valid and degrades
  to today's behavior (name-keyed, single occurrence).

### 1.2 Deterministic series id (`graph_write.apply`)

- Meetings key on **`slugify(f"meeting-{org}-{series_name}")`** — **org-scoped**,
  which *structurally* prevents the "Staff Meeting across two orgs" collision (no
  heuristic, no review). This replaces the current bare `slugify(name)` for
  `type == "meeting"`. `event` follows the same scheme (or is folded into
  `meeting` — see Open Questions).
- Gated by `config.meeting_series_enabled` (default **ON**, kill-switch).

### 1.3 Occurrences as observations

- Each meeting mention appends **one** `entity_observations` row:
  `attribute="occurrence"`, `valid_from=occurrence_date`,
  `value=<occasion/short summary>`, `source=<lead message_id or doc_id>`,
  `confidence_source="llm_extraction"`.
- New store method **`append_occurrence(entity_id, valid_from, value, source)`**
  — a plain `INSERT`, **not** `write_observation` (whose supersession would
  retire prior occurrences). Idempotent on re-apply via a SELECT-guard (or a
  partial unique index) on `(entity_id, attribute='occurrence', valid_from,
  source)`; the daemon is the single writer, so SELECT-then-insert is race-free.
- This is the aggregation payload: a series node accumulates its full occurrence
  history in one place.

### 1.4 Calendar bridge (opportunistic, capture-now)

- `sync/calendar.normalise_calendar` starts recording **`recurringEventId`** in
  chunk metadata immediately (one line; cheap; no downside).
- When a series' `(normalized name, org)` + an `occurrence_date` **confidently**
  matches a known calendar occurrence (same normalized name, same org, date
  within the occurrence window), the series records the `recurringEventId` as an
  observation (`attribute="calendar_series"`). This is an **upgrade/annotation**,
  never a re-key — it can't mis-merge two series.
- **Out of scope:** actively fuzzy-matching every text meeting to calendar and
  re-keying (low yield — most text meetings are external/informal/past with no
  calendar event; high complexity).

### 1.5 Migration of the existing 294 (scoped, attended)

Scoped re-extraction — **not** a full-corpus `ENRICH_LOGIC_VERSION` bump:

1. **Collect source chunks that produced meeting entities.** Union of:
   - email path: `SELECT DISTINCT ee.message_id FROM email_entities ee JOIN
     entities e ON e.id = ee.entity_id WHERE e.type='meeting'` →
     `store.doc_ids_for_messages(message_ids)`;
   - relation-provenance path: `SELECT DISTINCT source_doc_id FROM
     entity_relations er JOIN entities e ON (e.id=er.entity_a OR e.id=er.entity_b)
     WHERE e.type='meeting' AND COALESCE(source_doc_id,'') != ''`.
   New store method **`meeting_source_doc_ids()`** returns the deduped set.
2. **Reset just those chunks:** `UPDATE chunks SET enriched=0, enriched_version=0
   WHERE doc_id IN (…)` (new **`reset_enriched(doc_ids)`**). The daemon's normal
   spool loop re-extracts them under the new contract → series entities form on
   the new `meeting-<org>-<series>` ids, with occurrences.
3. **Retire the pre-migration bare meeting entities.** After re-extraction
   settles, meeting entities whose id does not match the new scheme and are now
   superseded by a series get their `email_entities` links folded into the
   corresponding series, then deleted (`merge_entities`, destructive → hence the
   backup below).

### 1.6 lint / orphan handling

`lint_graph.check_orphan_entities` already excludes `type='meeting'`. Series
entities are richer (occurrences + links); no change required, but the exclusion
stays (a brand-new series awaiting its second occurrence is legitimately sparse).

---

## Problem 2 — Topics: deterministic normalization + curated synonym map

### Model

Topics stay entities (they earn their keep: the min-2-distinct-org gate makes a
topic a genuinely cross-cutting hub). Variants converge via **deterministic
write-time normalization** plus a **curated synonym map** — no LLM merge, so
"prayer" can never silently absorb "prayer meeting"; only an explicit curated
entry joins two topics.

### 2.1 Write-time normalization (`store.upsert_topic_entity`)

- Before `slugify`, normalize the tag with **conservative, reversible** rules
  only: lowercase (already), collapse whitespace (already), strip a small
  leading-qualifier stopword set ("the", "annual", "our"), and **singularize**
  simple plurals ("budgets"→"budget"). **No general stemmer** (over-merge risk).
- Result: "budget" / "budgets" / "the budget" / "annual budget" converge on
  `topic-budget`.
- Gated by `config.topic_consolidation_enabled` (default **ON**, kill-switch).

### 2.2 Curated synonym map (new `topics.py`, mirroring `orgs.OrgTaxonomy`)

- A Josh-owned `{variant → canonical}` table for true synonyms the normalizer
  can't catch ("finances"→"budget", "kids ministry"→"youth"?). Config-driven,
  like the org taxonomy; the curator (Josh's machine) owns it.
- Deterministic + reversible. Absence of an entry = topics stay distinct
  (conservative default).

### 2.3 Topics stay global (not org-scoped)

Unlike meetings, a topic is cross-cutting by definition — the existing
min-2-distinct-org gate is exactly what filters topics down to concepts worth a
shared hub. Org-scoping would defeat that. Topic id stays `topic-<canonical>`.

### 2.4 Rejected: add `topic` to `_NAME_MERGEABLE_TYPES`

Reintroduces the "prayer" vs "prayer meeting" collision the exclusion was built
to prevent; safety would rest entirely on LLM review + reversibility, and topics
are high-volume/low-signal (heavy review load). Deterministic consolidation is
strictly safer and cheaper.

### 2.5 Migration of the existing 412 (one-shot, attended)

A one-shot pass applies the 2.1 normalizer + 2.2 synonym map to existing topic
entities, folding each variant into its canonical `topic-<canonical>` id
(`merge_entities`, destructive → backup-protected). Because topic ids are
deterministic, this is a pure `{old_id → new_id}` mapping — no fuzzy matching.

---

## Cross-cutting: migration safety

The one-shot migrations (meetings §1.5 step 3, topics §2.5) are the **only**
destructive operations. They run **attended, by the curator (Josh's machine)**,
never as an unattended cadence (C1 lesson):

1. Take a **full DB backup** first.
2. Run the migration.
3. Run the **gold eval**. If recall@10 < 0.55 or MRR < 0.35, **restore the
   backup**. (This is the reversibility guarantee — we do **not** build an entity
   unmerge subsystem; a one-shot supervised op with a backup is sufficient and
   YAGNI-correct.)

Going-forward consolidation (deterministic keying, §1.2/§1.3, §2.1/§2.2) does
**no** merges, so it carries no reversibility burden and ships ON behind the
kill-switches from day one.

---

## Feature flags & config

- `meeting_series_enabled` (default **ON**) — meeting contract handling +
  deterministic series keying + occurrence writes + `recurringEventId` capture.
- `topic_consolidation_enabled` (default **ON**) — topic normalization +
  synonym-map lookup.
- Migrations are **manual curator commands**, gated by their own explicit
  invocation (no runtime flag).

---

## Validation

- **Unit tests:**
  - series keying is org-scoped (two same-name meetings in different orgs → two
    series; same org → one);
  - `append_occurrence` idempotency (re-apply a thread adds no duplicate
    occurrence rows);
  - topic normalization rules (plural/qualifier) and synonym-map lookup;
  - `recurringEventId` captured into calendar chunk metadata;
  - conservative defaults: a meeting with no `series_name` degrades safely; an
    unmapped topic stays distinct.
- **Gold harness** (recall@10 ≥ 0.55 / MRR ≥ 0.35) on a live-store copy, run
  before/after each migration.
- **Count checks post-migration:** meeting same-name-group fragments (the 95)
  collapse into series; topic same-name groups → ~0; occurrence counts on series
  reconcile with the retired mentions.

---

## Out of scope (YAGNI)

- Full text→calendar fuzzy bridge (only opportunistic `recurringEventId`
  annotation ships).
- Adding `topic` to the fuzzy LLM merge path.
- An entity unmerge / rollback subsystem (backup-restore covers the supervised
  one-shot migrations).
- Re-modeling calendar events as first-class meeting entities (they remain
  searchable chunks + `attended` relations).
- Full-corpus `ENRICH_LOGIC_VERSION` re-flow (migration is scoped to
  meeting-source chunks only).

---

## Open questions for the writing-plans session

1. **`event` type.** Fold `event` into the same `meeting-<org>-<series>` scheme,
   or leave `event` untouched for now? (Live counts were given for `meeting`;
   `event` volume unknown.)
2. **Occurrence `value` content.** What exactly does an occurrence row carry —
   just the date, or a short per-occasion summary/attendee snippet? Affects the
   contract and the aggregation payload richness.
3. **Singularization library.** Hand-rolled small ruleset vs a dependency
   (`inflect`)? Prefer dependency-free + conservative unless coverage is poor.
4. **Retire-vs-keep for unmatched meetings.** After scoped re-extraction, a
   handful of text meetings may not re-form as a series (source chunk cold-marked
   / no `series_name`). Delete, or leave as single-occurrence series?
