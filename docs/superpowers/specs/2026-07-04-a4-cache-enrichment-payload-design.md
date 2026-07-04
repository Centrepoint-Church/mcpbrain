# A#4 — cache the enrichment payload (save Haiku cost on shared-drive docs)

**Date:** 2026-07-04
**Status:** Draft — design captured; awaiting go to plan + implement.
**Origin:** deferred item from the org-baseline hardening pass. The ingest cache
(subsystem A) already saves *extraction* + *embedding* cost for shared-drive files,
but NOT *enrichment* — every importer re-runs Haiku locally on cached docs. This is
the third of the cache's stated value props, unimplemented.

## Current behaviour (verified)

- A published `CacheArtifact` carries chunks (text + embedding) and an `enrich` dict,
  but the `enrich` dict is **never populated with a real extraction payload** — so on
  import `_import_artifact` computes `logic_v = 0`, which is below the floor, so the
  chunk is **not** marked enriched and the importer **re-enriches it locally** (Haiku).
- Net: correct graph, but every importer pays enrichment cost for every shared-drive
  doc it imports from cache. That's the gap A#4 closes.
- Shared-drive content **is** enriched (prepare.py routes `gdrive`/`file_id`/`mime_type`
  docs through the same spool→drain→`graph_write.apply` path as email threads), so
  there is a real per-doc payload to cache.

## Why this is safe now (echo path)

Applying cached enrichment on import writes `origin='local'` entities/relations on the
importer, which then flow into `collect_from_drain` → contributions. N importers of the
same cached doc all use the same `doc_id` → the same `source_ref = HMAC(fleet_secret,
doc_id)` → the curator counts them as **one** source (corroboration is echo-safe;
verified by Phase D Task 1). So activating A#4 cannot inflate org-graph corroboration —
the protection that made this "deferred until Phase D" is now in place.

## Design

### 1. Capture the per-doc extraction payload (at drain)

At `drain.py:451`, immediately after `apply(store, extraction, doc_ids=doc_ids, …)`
succeeds, persist the **pre-drained `extraction`** (the model's structured output for
that unit) keyed to its `doc_ids`, with the current `ENRICH_LOGIC_VERSION`.

**Storage decision:** a new table (additive, no migration risk):
```sql
CREATE TABLE IF NOT EXISTS enrich_payloads(
    doc_id        TEXT PRIMARY KEY,     -- one row per chunk doc_id the unit covered
    payload       TEXT NOT NULL,        -- json.dumps(extraction)
    logic_version INTEGER DEFAULT 0,
    at            TEXT DEFAULT CURRENT_TIMESTAMP)
```
Keyed per `doc_id` so cache publish (per shared-drive file → its chunk doc_ids) can
retrieve exactly the payload for that file. A unit covering N chunks writes N rows
(same payload) — cheap, and keeps retrieval a simple `doc_id` lookup. Only persisted
for shared-drive-sourced docs (email payloads never enter a shared cache — the cache is
shared-drive-only by design), gated so we don't bloat the table for email.

### 2. Publish the payload in the artifact (at publish)

`ingest_cache.publish`/`collect_chunks`: when building the artifact for a file, look up
the file's chunks' `enrich_payloads`; if present at the fleet's floor version, set
`artifact.enrich = {"logic_version": N, "extraction": <payload>}`. If the doc isn't
enriched yet this cycle, publish with `enrich={}` as today (the importer re-enriches;
a later cycle re-publishes the artifact in place with the payload — spec A2 lifecycle).

### 3. Apply on import (at import)

`_import_artifact`: when `art.enrich` carries an `extraction` payload AND
`logic_version >= max(pin.enrich_logic_floor, ENRICH_LOGIC_VERSION)`:
- apply it via `graph_write.apply(store, payload, doc_ids=[imported doc_ids], home=…,
  owner=…, embedder=…)` — writing `origin='local'` graph rows exactly as local
  enrichment would, and
- `mark_enriched(doc_ids, version)` so the importer's spool never re-enriches them.
Any mismatch/missing payload → today's fallback (import chunks, leave unenriched, local
re-enrich). **Idempotent:** re-import of the same artifact is safe because `apply` is
idempotent on `(source_doc_id)` and `mark_enriched` is a set operation.

**Integration surface:** `_import_artifact`/`try_import`/`bootstrap_drive` currently
take `(store, fleet_storage, drive_id, file_id, content_hash, pin)`. Applying enrichment
needs `home` + an `owner`/`identity` + an `embedder`. These are available at the two
call sites (`run_sync_cycle` has `home`+`embedder`; onboarding has `home`). Thread an
optional `apply_ctx` (home/owner/embedder) through; when absent (e.g. a pure chunk
round-trip test), skip apply and just mark-enriched-if-version-matches as today. This
keeps the cache-only round-trip path unchanged.

### 4. Lifecycle (logic-version bump)

On an `ENRICH_LOGIC_VERSION` bump, `reflow_outdated_chunks` re-enriches locally; the
first daemon to re-enrich a shared-drive file **re-publishes** its artifact with the new
payload/version in place (embeddings untouched — the pipeline fingerprint is unchanged).
Importers on the old version keep their (still-valid) local enrichment until they import
the newer payload.

## Risks / mitigations

- **Trusting a peer's extraction:** an importer applies enrichment a *different* install
  produced. Mitigation: it's ACL-gated (same drive), the payload is validated through the
  same `validate_extraction`/`sanitize_batch`/grounding filter `drain` already applies
  before `apply` — never applied raw. Apply the payload through drain's existing
  validation path, not straight into `graph_write.apply`.
- **Echo inflation:** handled (corroboration echo-safe, verified Phase D Task 1).
- **Payload staleness vs logic version:** gated on `logic_version >= floor`; stale
  payloads are ignored (fall back to local re-enrich).
- **Table growth:** `enrich_payloads` is shared-drive-docs-only and one row per chunk;
  bounded by the shared-drive corpus. GC when a chunk is deleted (drive revocation
  already deletes chunks — extend the purge to drop payload rows).

## Testing (for the plan)

- Capture: after a drive-doc drain, `enrich_payloads` has the doc's payload at the
  current logic version; email drains do NOT write payload rows.
- Publish: a published artifact for an enriched drive file carries
  `enrich={"logic_version", "extraction"}`; an unenriched file carries `enrich={}`.
- Import-apply: importing an artifact with a floor-satisfying payload applies the graph
  rows (origin='local', via the validated drain path) AND marks the chunks enriched
  (no local re-enrich); a below-floor/missing payload falls back to re-enrich.
- Idempotence: re-importing the same artifact doesn't double-write.
- Echo: N installs importing the same cached doc → curator sees one source_ref (already
  covered by Phase D Task 1; add a targeted A#4 variant that goes through the real apply).
- Revocation GC: purging a drive drops its `enrich_payloads` rows.
- Round-trip (existing): a cache-only import with no apply_ctx is unchanged.

## Out of scope

- Email-thread enrichment payloads in any shared cache (cache is shared-drive-only).
- Changing the extraction/validation logic itself (reuse drain's existing path).
