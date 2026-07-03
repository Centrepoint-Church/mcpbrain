# Org baseline + personal overlay — design

**Date:** 2026-07-03
**Status:** Draft — pending Josh's review
**Problem:** Every new user's daemon independently re-extracts, re-embeds, and re-enriches
the same shared organisational content, and there is no shared organisational graph —
each person rebuilds "who is who / what is what" from scratch. At the same time, personal
brains contain content (PII, financial, pastoral) that must never leak to other users.

## Decisions taken in brainstorming

| Decision | Choice | Made by |
|---|---|---|
| Primary goal | Both cost *and* shared truth, built as two subsystems of one design | Josh |
| Org daemon role | **Curator, not extractor** — people-context lives in emails the org daemon can't see, so everyone contributes filtered claims; the org daemon adjudicates and keeps layer 1 clean | Josh |
| Layer split | Layer 1 (org): people, orgs, projects, filtered topics + relations between them. Layer 2 (personal): everything else | Josh |
| Embeddings | Shared per-file cache gated by Drive ACLs; devices self-embed new/changed files and publish back | Josh |
| Sharing rule for claims | **Allowlist + org-daemon adjudication** (type filter at the edge, AI judgment at the centre) | Josh (confirmed) |
| Layer-1 storage on consumers | Tagged rows (`origin` column) in the single local `brain.sqlite3`, not an attached second DB | Josh (confirmed) |
| Transport | Google-Drive-mediated throughout (fleet folder + in-drive cache folders); no server, no new auth | Josh (confirmed) |
| Relation allowlist | `works_at`, `member_of`, `mentioned_with` (co-occurrence, corroboration-guarded); `works_on`/`part_of` dropped — projects connect via co-occurrence | Josh |
| Contribution default | ON (typed/redacted/fail-closed by construction; opt-out still consumes) | Josh |
| Access revocation | Automatic purge, after the drive is absent for several consecutive sync cycles | Josh |
| Curator deployment | Josh's machine, own account, `role=org_curator` (design stays host-agnostic) | Josh |
| Build phasing | Phase 0 (fat foundation) → A ∥ B ∥ C in parallel worktrees → Phase D (convergence) | Josh |

## Core privacy invariant

> Content is shared **only** along paths where Google Drive ACLs already grant access,
> and graph claims are shared **only** if their *type* is on a conservative allowlist
> and they survive curator adjudication. Nothing sensitive-by-content ever leaves a
> machine because nothing content-shaped (chunk text, observations, profiles, doc ids)
> is ever contributed — only typed, redacted claims.

Josh's framing: "I ingest PII financial information — I'd never want that ingested by
others, *unless they have access to it, then it is their brain so fair*." The ACL-gated
ingest cache implements exactly the "fair" case; the claim allowlist implements the rest.

## Architecture overview

```
┌────────────── each user's daemon ──────────────┐
│ Gmail/Drive sync ─► extract ─► embed ─► enrich │
│        │ cache-first ▲              │          │
│        ▼             │              ▼          │
│  [ingest cache in    │        edge filter      │
│   each shared drive] │        (allowlist +     │
│   read+publish ──────┘         redaction)      │
│                                     │          │
│  local store: origin='local' rows   ▼          │
│               origin='org' rows ◄── contrib    │
│               (wholesale-replaced)   JSONL     │
└──────────────────────┬──────────────┬──────────┘
                       │ import       │ append
              ┌────────▼──────────────▼────────┐
              │  fleet Shared Drive folder     │
              │  contrib/<email>/*.jsonl       │
              │  org-graph/manifest.json       │
              │  org-graph/snapshot.jsonl.gz   │
              └────────▲──────────────┬────────┘
                       │ publish      │ consume
              ┌────────┴──────────────▼────────┐
              │  org daemon (curator role)     │
              │  stage ─► deterministic merge  │
              │  ─► AI adjudication (reuses    │
              │  brain-review machinery)       │
              │  ─► versioned snapshot +       │
              │  tombstones + decision log     │
              └────────────────────────────────┘
```

Three subsystems, each complete in itself (not a v1 to be redone):

- **A. Shared-drive sync + ingest cache** — fixes the cost/onboarding pain immediately.
- **B. Org graph** — contributions, curator, snapshot, consumer import.
- **C. Onboarding integration** — `mcpbrain setup` pulls snapshot + cache for instant brains.

They are built in phases (below) rather than one linear sequence: a foundation phase
freezes everything the three share, the three then proceed in parallel, and a
convergence phase wires the seams that belong to no single subsystem.

---

## Implementation phasing

The work is **not** a linear A→B→C. A, B, and C have almost no logical dependency on each
other in code — what they share is *files and interface contracts*. So the plan front-loads
a fat foundation phase that freezes that shared surface, runs A/B/C as parallel tracks
(separate worktrees/branches/sessions), and closes with a convergence phase for the
cross-cutting seams. **Parallelism is only as safe as Phase 0 is complete**: if a parallel
branch has to add a column, a config flag, or an interface type, it collides with the others.

### Phase 0 — foundations (on `main`, merged before A/B/C branch)

Everything A, B, and C touch in common lands here, so the parallel branches only add
*logic* on top of a frozen surface.

- **Full schema migration, all at once** (safe on already-populated stores): `origin TEXT
  DEFAULT 'local'` on `entities` + `entity_relations`; `drive_id` in chunk metadata;
  new tables `org_contrib_staging`, `org_contrib_outbox`, and the cross-layer re-point log.
  No later phase adds schema.
- **All config flags + accessors**: `org_contrib_enabled`, `org_import_enabled`, cache
  flags, `role`, plus the `fleet.py` / `org-config.json` pinning block (fingerprint inputs,
  relation allowlist, `fleet_secret`) — the plumbing A (pipeline fingerprint) and B (HMAC +
  allowlist) both consume.
- **Frozen interface contracts, as real code** (dataclasses / typed signatures, not prose):
  the cache-artifact JSON schema, the contribution-record shape, the snapshot / manifest /
  tombstone schema, and the daemon **cadence registration hook-points** (stubbed no-op
  registration slots that A and B each fill in, so they don't both edit the same
  `daemon.py` wiring block).
- **`role=org_curator` plumbing** — the role exists and is wired, even before B implements
  what it does.
- **Multi-user + curator test harness** — the simulation fixture (N users, one curator, one
  shared-drive fixture) that all three phases and Phase D test against.

**Exit gate:** migration runs clean on a real-corpus store copy; contracts compile; harness
spins up a simulated fleet. Only then do the branches fork.

### Phases A ∥ B ∥ C — parallel tracks

Forked from the merged Phase 0, in separate worktrees.

- **A (ingest cache)** — `sync/drive.py` shared-drive sync + `.mcpbrain-cache/` artifacts
  (subsystem A above). Touches files B never opens; the cleanest-isolated track.
- **B (org graph)** — contribution edge filter, curator pipeline, consumer import
  (subsystems B1–B5 above). Independent of A in code.
- **C (onboarding bootstrap)** — builds against Phase 0's frozen contracts with **fakes**
  standing in for A's cache and B's snapshot. C is the *convergence-bound* track: it can be
  written in parallel but its true end-to-end integration test only passes once A and B land.

Each track: green in isolation against the shared harness before it merges. The one shared
hotspot is `daemon.py` cadence wiring — neutralised by Phase 0's stubbed registration slots,
so A and B fill different slots rather than editing the same block.

### Phase D — convergence (after A, B, C merge)

The seams that belong to no single subsystem, and can only be verified on the merged system:

- **A↔B echo-dedup integration test** — cache-imported enrichment (A) must not inflate
  corroboration at the curator (B). This is a runtime interaction between two branches; the
  test is owned by neither and lives here.
- **C end-to-end** — real cache + real snapshot: new-user bootstrap produces a working brain
  with zero extraction calls on cached content (replacing C's fakes).
- **Security / egress gate** — adversarial pass over the merged A+B surface: prove nothing
  content-shaped escapes (no chunk text, `profile`, raw `doc_id`, or non-allowlisted type in
  any contribution; cache artifacts only ever inside ACL-scoped drives). This is the single
  most important gate and it spans both A and B.
- **Rollout enablement ordering** — the flag choreography for a distributed fleet, which no
  per-branch default can express: curator seeded and publishing a first snapshot *before*
  `org_import_enabled` goes fleet-wide; `fleet_secret` distributed *before* `org_contrib_enabled`
  turns on. Documented as an explicit enablement runbook.
- **Observability slice** — origin colouring in the `/graph` explorer, cache hit-rate, and the
  curator's pending/adjudicated queue surfaced in fleet `status.html`.

---

## Subsystem A — shared-drive sync + ingest cache

### A1. Shared-drive sync (gap fix)

`sync/drive.py` today never sets `includeItemsFromAllDrives` / `corpora="drive"` /
`driveId`, so true Shared Drive files are not synced by the Changes API path (the flags
exist only in `backup.py`/`fleet.py`). Add:

- `drives.list` enumeration at sync time; per-drive cursor rows in `sync_cursors`
  (`drive:<driveId>` cursor keys) using `changes.list(driveId=…, includeItemsFromAllDrives=True, corpora="drive")`.
- `backfill_drive` gains the same per-drive path.
- Chunk metadata gains `drive_id` (nullable — null means My Drive/shared-with-me).

### A2. Ingest cache

**Location:** a `.mcpbrain-cache/` folder at the root of **each shared drive**. Storing
artifacts inside the drive they describe means Google's ACLs are the access control —
a user who can't see the drive can't see the cache. No mcpbrain-side ACL logic exists
or is needed.

**Artifact:** one file per (source file × content version × pipeline):
`<fileId>.<contentHash12>.<pf8>.mbc.gz` — where `pf8` is the first 8 hex chars of a
**pipeline fingerprint** `sha256(embed_model | dim | chunker_version)`. Fingerprinting
the filename means a mixed-version fleet (mid-rollout) never churns artifacts by
overwriting each other — old- and new-pipeline artifacts coexist until the old ones are
GC'd. The enrichment logic version deliberately lives *inside* the artifact, not in the
fingerprint: an `ENRICH_LOGIC_VERSION` bump updates the `enrich` block in place and must
not force the fleet to re-embed unchanged files. Gzipped JSON:

```json
{
  "schema": 1,
  "file_id": "…", "content_hash": "…",
  "extraction_method": "…", "chunker_version": "…",
  "embed_model": "…", "dim": 768,
  "chunks": [{"idx": 0, "text": "…", "embedding_b64": "…f32le…", "metadata": {…}}],
  "enrich": {"logic_version": N, "…optional pre-drained extraction payload…"},
  "published_by": "<email>", "published_at": "…"
}
```

Chunk **text** is included — it is derived from a file the reader can access by ACL, so
shipping it is exactly as private as the file itself. This is what makes the cache save
extraction *and* embedding *and* (via the optional `enrich` block) Haiku enrichment cost.

**Read path (cache-first ingest):** on a new/changed shared-drive file, look up the
artifact by `fileId + contentHash`; if present and `embed_model+dim+chunker_version`
match local config → import chunks + vectors directly (`upsert_chunk` + vec insert),
mark enriched from the `enrich` block when logic versions match. Any mismatch or
corruption → fall back to local extract/embed, then publish.

**Write path:** after locally extracting+embedding a shared-drive file, publish the
artifact. Content-hash keying makes races idempotent: two users racing produce
byte-equivalent artifacts; last-write-wins is harmless. No locking needed. To shrink
herd races (e.g. the morning after a logic bump, when every daemon sees a fleet-wide
miss), the cache is **re-checked immediately before processing each file** — the first
publisher wins and everyone else imports; residual duplication is bounded and harmless.

**Artifact lifecycle (GC):**
- *File changed* → the publisher of the new artifact best-effort deletes artifacts for
  the same `fileId` with older content hashes or stale pipeline fingerprints.
- *File deleted* (changes.list removal event) → the observing daemon deletes the
  artifact and its own local chunks/vectors for that `doc_id` (as removal handling
  does today).
- *Enrich logic bump* → whoever re-enriches first updates the artifact's `enrich`
  block in place; embeddings untouched.
- *Sweep* → any daemon syncing a drive opportunistically GCs artifacts whose `fileId`
  no longer exists in that drive; no dedicated GC owner needed.

**Scopes:** no new OAuth scopes. Reads are covered by the existing read-only Drive scope;
writes use `drive.file`, which is per-OAuth-client — all installs share the bundled
client ID, so every daemon can read/update artifacts any other daemon created (subject to
Drive ACLs), the same mechanism `backup.py` already relies on.

**Non-goals:** the cache is not used for My Drive or shared-with-me files (no shared
folder to put artifacts in that others can see; single-consumer anyway).

### A3. Access revocation

The fairness rule ("if they have access, it's their brain") runs both ways: when access
goes away, so should the content — **automatically, no human in the loop**. To keep a
transient Drive glitch from reading as revocation, purge triggers only after the drive
is absent from `drives.list` (or its cursor 404s) across **several consecutive sync
cycles**. Then:

- **Purge** that drive's chunks + vectors + FTS rows locally (`drive_id` metadata makes
  this a targeted delete).
- **Invalidate** layer-2 graph rows sourced *solely* from those docs — bitemporal
  `invalidated_at` via `source_doc_id`, the existing supersession machinery; rows also
  corroborated by still-accessible sources survive.
- Layer-1 (org) rows are untouched — they are safe-by-construction (typed, redacted,
  curator-adjudicated) and not derived from the user's access.

### A4. Fleet pipeline pinning

`org-config.json` (the existing fleet overlay, allowlist extended) pins fleet-wide:
`embed_model` + `dim`, `chunker_version`, an `ENRICH_LOGIC_VERSION` floor, the
layer-1 relation allowlist, and the `fleet_secret` (HMAC key). Pinning is what makes
cache artifacts and contributions comparable across installs; daily auto-update keeps
skew short-lived, and the pipeline fingerprint (A2) makes any residual skew harmless
rather than churny. A daemon whose local config predates the pin falls back to local
processing without overwriting newer artifacts.

---

## Subsystem B — org graph (layer 1)

### B1. What layer 1 contains

- **Entities:** `person`, `org`, `project` (+ `topic` only when curator-promoted; topics
  are not contributed automatically — too noisy and occasionally sensitive).
  Fields shared: `name`, `type`, `org`, `email_addr`, `aliases`. **Never** `profile`,
  `mentions` counts, or anything synthesized from personal context.
- **Relations:** allowlisted types only. Initial allowlist: `works_at`, `member_of`,
  and `mentioned_with` (co-occurrence). `mentioned_with` is communication metadata, so
  it carries a **per-type corroboration guard**: it becomes canonical only with ≥2
  independent `source_ref`s — a one-off co-occurrence in a single mailbox never
  surfaces org-wide. `works_on`/`part_of` are deliberately **not** contributed
  (project involvement can reveal unannounced work); projects connect to people
  through corroborated co-occurrence instead. Explicitly excluded: sentiment/role
  observations, anything financial/pastoral/health. The allowlist is **distributed via
  the existing fleet `org-config.json` overlay** (extend `fleet._ALLOWLIST` with an
  `org_graph` key), so it can be tuned org-wide without a release.
- **Org taxonomy:** the canonical org list (already a config concept) becomes org-managed.
- Bitemporal fields (`valid_from`/`valid_to`) carry over; layer 1 is bitemporal like
  layer 2.

Everything else — observations, actions, email/thread context, communities, profiles,
sentiment, and every non-allowlisted relation — is layer 2 and never leaves the machine.

### B2. Contribution pipeline (edge)

New module `mcpbrain/org_contrib.py`:

- Hook: after each enrichment drain commits, compute the delta of allowlisted
  entities/relations written in that drain.
- **Redaction:** a contribution record is
  `{claim, confidence, valid_from, contributor_email, source_kind: email|drive|calendar, source_ref: HMAC-SHA256(fleet_secret, doc_id)}`.
  The HMAC lets the curator dedupe and count *independent corroboration* across users
  without learning which email/doc a claim came from. The `fleet_secret` is distributed
  in `org-config.json`.
- **Bitemporal updates are contributed, not just assertions:** when the local graph
  supersedes or invalidates a previously-allowlisted claim (someone leaves an org →
  `valid_to` set), the supersession is contributed too. Without this, layer 1
  fossilizes — it would only ever learn that things started, never that they ended.
- **No echo inflation:** cache-imported enrichment (A2) makes every importer of a
  shared doc emit the same claims — but `source_ref = HMAC(doc_id)` is identical
  across users, so the curator dedupes them to one source. Corroboration counts only
  genuinely distinct sources; importing org data can never inflate confidence in
  org data.
- **Fail closed:** unknown types, missing fields, entities keyed on role addresses
  (`is_role_address`, the 0.7.77 guard), or entities appearing only in cold/low-salience
  chunks → not contributed.
- Transport: deltas accumulate in a local outbox table per drain; a daily cadence
  uploads them as one append-only JSONL batch to
  `fleet/contrib/<email>/<utc-timestamp>.jsonl` in the fleet folder, keeping Drive
  traffic trivial.
- Config: `org_contrib_enabled` (default ON once shipped — contribution is typed and
  redacted by construction; users who opt out simply consume without contributing).

### B3. Curator (org daemon)

A standard mcpbrain install with `config.role = "org_curator"`. It does **not** need
access to every shared drive or anyone's email — it curates claims, it doesn't extract.
The design is host-agnostic; at Centrepoint it runs on Josh's machine under his account
(zero new infra; curation freshness is bounded by that machine being on, which the
daily-cadence contract tolerates — moving it to a dedicated identity/host later is a
config change, not a design change). Pipeline (daily cadence, reusing the brain-review pattern —
AI-adjudicated, reversible, capped appliers, per the 0.7.84 hardening):

1. **Ingest** new contribution files → staging tables (`org_contrib_staging`).
2. **Deterministic merge** using the existing `resolve.py` machinery: canonical-key
   merge for person/org/project, email-equality merge with the role-address guard.
   Claims corroborated by ≥2 distinct contributors *or* ≥2 distinct `source_ref`s get
   elevated confidence; singletons are not blocked (adjudication decides) — except
   types with a per-type corroboration guard (`mentioned_with`), which stay pending
   until independently corroborated.
3. **AI adjudication** for everything deterministic merging can't settle: fuzzy
   name-pair candidates, contradictions (two contributors assert conflicting
   `works_at` with overlapping validity), suspicious merges. Verdicts target the
   finding's own stored `ref_id`/type (the 0.7.84 invariant) and are logged.
   Adjudication sees **structural evidence only** — names, emails, aliases, types,
   confidence, corroboration counts — never content (contributions carry none, by
   design). When that isn't enough to decide, the verdict is **pending**: the claim
   stays out of the snapshot and waits for more corroboration, rather than forcing
   a call on thin evidence.
4. **Publish** a versioned snapshot to `fleet/org-graph/`:
   - `manifest.json` — `{version, created_at, entity_count, relation_count, tombstone_count, snapshot_sha256}`
   - `snapshot.jsonl.gz` — entities, relations, org taxonomy
   - `tombstones.jsonl` — ids the curator deleted/merged-away, so consumer re-imports
     never resurrect them
   - decision log kept curator-side (`entity_merge_log`/`change_log` reuse) for audit
     and rollback; rolling back = re-publishing a previous version's snapshot.

### B4. Consumer import

- Schema: `entities` and `entity_relations` gain `origin TEXT DEFAULT 'local'`
  (`'local' | 'org'`).
- Daily cadence: fetch `manifest.json`; if version is newer, import transactionally:
  upsert all snapshot rows with `origin='org'`, remove `origin='org'` rows absent from
  the snapshot (wholesale-replace per origin — the proven `fleet.merge_org_config`
  semantics, applied to data), apply tombstones. `origin='local'` rows are never touched.
  **Removal never orphans local data:** if an org entity being removed has local
  relations/observations attached, it is demoted to `origin='local'` instead of deleted
  (unless tombstoned as a mis-merge, in which case local references are re-pointed to
  the tombstone's `merged_into` target).
- **Same-slug collision** (org and local both know `joel-chelliah`): one entity row.
  Org snapshot supplies name/type/org/email_addr; local aliases, mentions, profile and
  all local relations/observations are preserved on top. The row is marked
  `origin='org'` with local enrichment intact — org is the skeleton, personal is the
  flesh.
- **Cross-layer edges are free by construction:** a layer-2 relation (e.g.
  `document → mentions → joel-chelliah`, or a private observation on an org person)
  is an ordinary local row pointing at whatever entity row owns that slug, org-origin
  or not. There is no bridge table and no layer awareness in recall/graph traversal —
  mixed-origin neighborhoods are just the graph.
- **Slug-drift reconciliation:** deterministic keying can split one person across two
  nodes when the local variant differs (`joel-c` locally vs `joel-chelliah` in the
  snapshot), stranding local observations on the orphan. At import time, incoming org
  entities are reconciled against existing local entities using the existing
  `resolve.py` machinery: email-equality merges deterministically (role-address guard
  applies), and org-supplied alias lists match name variants via canonical-key/token
  similarity. The local node merges *into* the org node (local flesh re-attached);
  anything still ambiguous goes to the normal local fuzzy-review queue instead of
  auto-merging. Merges are logged in `entity_merge_log` as usual.
- **Conflict rule:** where org and local relations contradict, both coexist
  bitemporally (the store already models supersession); display/recall prefers the
  latest `valid_from` regardless of origin, so *the user's own fresher knowledge wins
  locally*. Local contradictions of org claims are themselves contributed (they're
  allowlisted types), giving the curator the signal to fix layer 1.
- Recall/search: unchanged — org rows are ordinary rows in the same store; `brain_search`,
  graph tools, and the `/graph` explorer see the union for free.
- Config: `org_import_enabled` (default ON once shipped), `fleet.folder_id` reused.

### B4a. Merge rules across layers

Local installs run their own merge machinery (write-time dedup cascade, daily
`resolve_entities` cadence, brain-review appliers). Without constraints, local merges
of org rows fight the wholesale-replace import. The rules:

1. **Local machinery never merges org↔org.** Deduplicating the org layer is the
   curator's job. When local resolution flags two org-origin entities as a duplicate
   pair, the pair is *contributed upstream* as a merge-candidate signal instead of
   applied locally (applying it would be resurrected by the next import anyway).
2. **Any local↔org merge leaves the org node surviving.** Local flesh re-attaches to
   the org node (the slug-drift direction). Merging an org node *into* a local node is
   forbidden — the import would resurrect the org node with the local data stranded on
   its twin.
3. **Local writes never overwrite org skeleton fields.** Writes landing on an
   org-origin entity (same slug, write-time dedup, enrichment upserts) may add —
   aliases, mentions, profile, local relations/observations — but `name`, `type`,
   `org`, `email_addr` stay org-authoritative between imports. Prevents silent local
   drift that the next import would churn back.
4. **All cross-layer re-points are logged** in `entity_merge_log` (which local
   references were moved to which org node, and why). This is what makes a curator
   *split* recoverable: when a later snapshot splits a previously-merged entity, the
   consumer consults its re-point log to restore local flesh to the resurrected node
   instead of leaving it all on the merged target.

Summary: org→org merges happen only at the curator; local→org merges are the only
cross-layer merges consumers perform; org→local merges never happen.

### B5. Source-of-truth rules (the "data going bad" answer)

1. Layer 1 is writable **only** by the curator; consumers treat it as replaceable cache.
2. Every canonical fact traces to ≥1 immutable contribution record (contributor +
   hashed source ref + confidence + timestamp).
3. Every curator mutation is adjudicated, logged, capped, and reversible; snapshots are
   versioned so rollback is a re-publish.
4. Tombstones prevent deleted/mis-merged entities from resurrecting via stale imports.
5. Personal layer always wins locally on freshness; disagreement flows back as
   contributions, not as direct writes.

---

## Subsystem C — onboarding integration

`mcpbrain setup` (and `doctor`) gain a baseline-bootstrap step, run before first sync:

1. Detect fleet folder (existing `org_defaults.py` IDs) → download and import the
   current org-graph snapshot → instant layer-1 graph.
2. Enumerate accessible shared drives → bulk-download `.mcpbrain-cache/` artifacts →
   import chunks/vectors/enrichment for every cache hit.
3. Normal sync then runs; only cache-misses (new files, changed files, My Drive, Gmail)
   cost extraction/embedding/enrichment.

Expected effect: a new user goes from "hours of backfill + thousands of Haiku calls on
shared content" to "minutes of downloads + enrichment spend only on their personal mail."

---

## Error handling

- Cache artifact corrupt / hash or model mismatch → silent fallback to local pipeline,
  artifact republished; log at info.
- Fleet folder unreachable / no org-config → daemon runs fully local (existing
  `fleet.py` degradation behaviour).
- Snapshot import is a single transaction; any failure leaves the previous org layer
  intact. `snapshot_sha256` verified before import.
- Contribution filter **fails closed** — on any uncertainty, nothing leaves the machine.
- Curator crash mid-publish: manifest is written last, so consumers never see a partial
  snapshot.

## Testing

- **Edge filter:** property-style unit tests — no non-allowlisted type, no `profile`,
  no raw `doc_id`, no chunk text can appear in a serialized contribution, for arbitrary
  drain deltas. Role-address and cold-source withholding.
- **Import semantics:** wholesale-replace per origin never touches local rows;
  tombstone suppression; same-slug merge preserves local aliases/profile; transactional
  rollback on injected failure.
- **Slug-drift reconciliation:** local `joel-c` with private observations + snapshot
  `joel-chelliah` sharing an email or alias → single node with observations intact;
  ambiguous name-only pairs land in the fuzzy queue, never auto-merge; role-address
  pairs never merge.
- **Cross-layer merge rules:** local `resolve_entities`/appliers presented an org↔org
  duplicate pair → no local merge, a merge-candidate contribution is emitted; a
  local↔org merge always survives as the org node; enrichment upsert on an org-origin
  slug cannot change skeleton fields; curator split after merge → local flesh restored
  from the re-point log.
- **Curator:** two synthetic contributors with overlapping + conflicting claims →
  deterministic merge + adjudication → snapshot; role-address groups never merge
  (0.7.77 regression tests extended); corroboration counting via HMAC refs.
- **Cache round-trip:** publish from store A, import into fresh store B → identical
  chunks, vectors (bitwise), enrichment state.
- **Version skew:** two stores on different embed models never overwrite each other's
  artifacts (fingerprint separation); enrich-logic bump updates `enrich` in place
  without re-embedding; pre-pin daemon falls back locally without clobbering.
- **Lifecycle:** file change GCs superseded artifacts; file deletion removes artifact
  + local chunks; drive-access revocation purges chunks and bitemporally invalidates
  solely-sourced layer-2 rows while multi-source rows and org rows survive.
- **Supersession + echo:** a contributed `valid_to` update ends the canonical claim;
  N users importing the same cached enrichment yield exactly one source_ref at the
  curator (no corroboration inflation).
- **End-to-end (Phase D):** three simulated users, one curator, one shared drive fixture —
  new-user bootstrap produces a working brain with zero extraction calls on cached
  content. This replaces C's fakes with real A cache + real B snapshot.
- **Egress gate (Phase D):** adversarial pass over the merged A+B surface — for arbitrary
  drain deltas and cache publishes, nothing content-shaped (chunk text, `profile`, raw
  `doc_id`, non-allowlisted type) escapes; artifacts only ever land inside ACL-scoped drives.

Tests are owned by the phase that owns the behaviour: A/B/C unit + isolation tests run
green per-branch against the shared harness; the A↔B echo, C end-to-end, and egress tests
are Phase D because they can only be verified on the merged system.

## Explicitly out of scope

- Real-time propagation (daily cadence is the freshness contract).
- Sharing anything content-shaped outside Drive-ACL paths (no chunk text in
  contributions, ever).
- A server or new auth model — everything rides existing per-user OAuth + Drive.
- Automatic `topic` contribution (curator can promote topics manually/by review).
- Unguarded `mentioned_with`: co-occurrence enters layer 1 only via the ≥2-source
  corroboration guard; project-involvement relations (`works_on`/`part_of`) don't
  enter at all.
