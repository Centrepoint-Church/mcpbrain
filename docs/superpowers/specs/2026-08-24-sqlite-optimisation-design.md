# SQLite optimisation — one rebuild, not seven migrations

**Status:** design. Scope approved 2026-08-24 (tiers 1–3 as a single pass, full
implementation, no deferred remainder).

**Independent of the hosting question.** Every change here applies to the local store as it
exists today. It is also a prerequisite for that decision rather than a competitor to it:
the store goes from **2.62 GB → ~1.6 GB** and recall gets faster, which materially changes
which hosting options are viable.

---

## Measured baseline (live store, 2026-08-24)

Runtime is Python 3.12.13 / **SQLite 3.50.4** (the uv-pinned interpreter — note system
Python 3.11 carries 3.38.4, which matters for the feature guards below).

```
file            2.62 GB   (639,769 pages @ 4096 B)
freelist          4 MB    0.2%  -- no space to reclaim; VACUUM is for page_size only
chunks          170,657 rows
chunks.text       0.76 GB   avg 4,435 B   max 293,203 B
metadata          86 MB     JSON text
fts_chunks_content 0.78 GB  <-- FTS5's own duplicate copy of the indexed text
fts_chunks_data   93 MB     <-- the actual inverted index
vectors          262 MB     170,657 x 384 x 4B
```

Pragma state, all defaults, never tuned:

| | current | |
|---|---|---|
| `cache_size` | **-2000** (2 MB) | on a 2.62 GB database |
| `mmap_size` | **0** | mmap disabled |
| `temp_store` | **0** | temp b-trees go to disk |
| `synchronous` | **2** (FULL) | fsync on every commit |
| `threads` | **0** | no parallel sort |
| `foreign_keys` | **0** | orphans accumulate silently |
| `sqlite_stat1` | **absent** | **ANALYZE has never run** |
| STRICT tables | 0 | |

`journal_mode=WAL`, `busy_timeout=5000` and `journal_size_limit=64 MiB` are already correct
and are not touched.

**On the missing statistics:** 0.7.105 hand-tuned query plans (`OR`→`UNION`,
`LIKE`→`json_extract`) against a planner that had no statistics at all. Those fixes stand on
their own measurements, but the absence of `sqlite_stat1` means some plan failures may have
been planner blindness rather than genuine unindexability. Re-measure the 0.7.105 benchmark
queries after ANALYZE; treat any further win as a bonus, not a reason to revert anything.

---

## Part A — runtime pragmas (no migration)

Applied in `_open_db` (`store.py:102`), which is the single chokepoint every connection
already passes through — both the daemon write path and the MCP read path.

| pragma | value | rationale |
|---|---|---|
| `cache_size` | `-16384` (16 MB) default, `-65536` (64 MB) for `bulk=True` | **CORRECTED 2026-08-25 (PR #25 finding 5):** per-connection, and the daemon opens roughly one connection per call across ~21 threads — the 64 MB value applied unconditionally could add ~1 GB of RSS between them (this project gates releases on peak RSS). Reserved for genuinely long-lived, throughput-sensitive connections (the rebuild tool, `reindex_fts_batch`, backup's `VACUUM INTO`) via a new `bulk` parameter; the common per-call case gets 16 MB — bigger than SQLite's own 2 MB default, not the full tuned value |
| `mmap_size` | `67108864` (64 MB) default, `268435456` (256 MB) for `bulk=True` | same per-connection/`bulk` scoping as `cache_size` above |
| `temp_store` | `2` (memory) | FTS5 / ORDER BY / vector temp b-trees stop hitting disk |
| `synchronous` | `1` (NORMAL) | safe against corruption under WAL; the store is **derived** and re-ingestable, so FULL buys nothing and costs every write |
| `threads` | `4` | parallel sort on large ORDER BY |
| `analysis_limit` | `400` | bounds `PRAGMA optimize` so it cannot stall a close |

Plus **`PRAGMA optimize` on connection close**, the documented recommendation. `Store`
already owns connection lifetime via `_connect`, so this goes in that teardown — not
sprinkled at call sites.

**Read-only connections must not attempt `optimize`** (it writes). Guard on the existing
`read_only` flag.

**CORRECTED 2026-08-25** (PR #25 finding 1): `read_only` alone is not the
right gate — it also fires on every `write=False` USE of a writable `Store`
(e.g. `daemon.search`'s read path), acquiring the write lock after a
read-only transaction is already done and contending with a concurrent
drain at exactly the recall path's latency budget. The correct gate is
`write and not read_only` — only an actual write transaction should pay
for a stats refresh.

---

## Part B — one rebuild for every schema change

Six of the changes below individually require rewriting the whole 2.6 GB file. Doing them as
six sequential migrations means six full rewrites. **They are done once, in a single
out-of-place rebuild** — `bin/optimise_store.py`:

1. Take a backup first, via the existing `backup.make_encrypted_snapshot`. Non-negotiable.
2. Acquire the daemon bulk lock (or require the daemon stopped) so nothing writes mid-copy.
3. Create `brain.sqlite3.new` with `page_size=8192` set **before** any table exists.
4. Create the full target schema: STRICT tables, JSONB metadata, contentless FTS5, partial
   indexes, trigram index, FK constraints.
5. Copy data through, filtering orphans (see B6), converting `metadata` TEXT → JSONB, and
   re-deriving FTS via the existing `_fts_text` so the contextual prefix is preserved.
6. `ANALYZE`.
7. Verify (see Gates). Only then swap; keep the old file until the next successful run.

### B1. Contentless FTS5 — 780 MB

```sql
CREATE VIRTUAL TABLE fts_chunks USING fts5(text, content='', contentless_delete=1);
```

Safe because **nothing uses `snippet()` or `highlight()`** — the only FTS5 function in the
codebase is `bm25(fts_chunks)` (`store.py:2033`), which contentless supports. External
content (`content='chunks'`) is *not* an option: `_fts_text` indexes contextual prefix +
body while `chunks.text` stays raw, so the content table could not reproduce the indexed
string.

Same tokens indexed, same BM25 ranking, so **retrieval is unchanged by construction**.

**Version guard, mandatory.** `contentless_delete` needs SQLite ≥3.43. The runtime has
3.50.4, but the SQLite version comes from whichever Python the wheel installs under, and the
Windows path pins its own x64 interpreter. `init()` checks `sqlite3.sqlite_version` and
falls back to the current content-storing form below 3.43 — silently degrading storage, never
correctness. A test pins both branches.

`reindex_fts_batch` already re-derives FTS from `chunks`, so the rebuild path exists and is
reused rather than reinvented.

### B2. JSONB metadata

`metadata` is 86 MB of JSON text and **all four** expression indexes from 0.7.105 are on
`json_extract(metadata, …)` — the hottest path in the store. Store JSONB (SQLite ≥3.45) and
switch reads to `jsonb_extract`, removing parse cost per query. Same version-guard pattern as
B1.

The expression indexes are recreated over `jsonb_extract` with identical semantics; the
0.7.105 rewrites (`doc_ids_for_messages` UNION, `chunks_for_file` file_id match) are
preserved exactly.

### B3. `page_size` 4096 → 8192

Average `chunks.text` is 4,435 B and each vector is 1,536 B, so most rows overflow a 4 KB
page today. Set at creation time in the rebuild — free, since the rebuild happens anyway.

### B4. Partial indexes

Hot queries filter `embedded=1`, `enriched=0`, and the cold flag. Partial indexes
(`… WHERE embedded=1`) are smaller and stay resident in the now-64 MB cache. Audit the
existing index set in the same pass and drop any index no query uses — the rebuild is the
cheapest possible moment to do it.

### B5. Trigram index for `email_mentions`

`CLAUDE.md` records that `email_mentions`' `text LIKE` "isn't indexable". True for
`unicode61`; a **trigram** FTS index makes LIKE indexable. This is a direct fix for a
documented limitation and removes one of the cost reasons
`salience_require_drive_mention` is opt-in-OFF.

**REVERTED 2026-08-25 (PR #25 finding 6):** this was built without a task to
wire a reader (`email_mentions` never changed to use it) and without pricing
the populate cost, which measured +1.089 GB on the live store — erasing
most of this whole plan's storage saving for an index nothing queries. It
shipped deferred (an opt-in `--populate-trigram` flag), then was removed
entirely rather than left as unused infrastructure. `email_mentions`'
`text LIKE` remains genuinely unindexable, unchanged from before this plan.
A future reader would likely want different tokenizer settings anyway.

### B6. Foreign keys ON, after an orphan sweep

`foreign_keys` is **OFF** today, so orphans accumulate unnoticed. Both the 0.7.74 structural
collapse and the 0.7.86 candidate-pair explosion produced bad graph state that FK constraints
with `ON DELETE CASCADE` would have contained.

Sequenced, because enabling it on a store with existing orphans starts failing writes:

1. **Count and report** orphans per relation first — `entity_relations`, `email_entities`,
   `entity_observations`, `entity_communities` against `entities`; chunk-referencing rows
   against `chunks`. Report before deleting anything.
2. The rebuild copies only referentially-valid rows; dropped rows are logged with counts by
   table so the sweep is auditable.
3. Target schema declares the constraints; `_open_db` sets `foreign_keys=ON`.

If the orphan count is large or surprising, **stop and report rather than delete** — an
unexpected orphan population is a bug signal, not cleanup work.

### B7. STRICT tables

Type enforcement at write time (SQLite ≥3.37). Free during the rebuild. Requires auditing
each column's real types first — a `STRICT` table rejects the loose types SQLite has been
accepting, so this is the change most likely to surface latent bugs. That is the point, but
it means the type audit is part of the work, not an afterthought.

---

## Deliberately rejected

| | why |
|---|---|
| `detail=none` on FTS5 | smaller index but breaks phrase queries; measure before considering |
| Porter stemming | changes ranking — a gold-harness experiment, not a free win |
| Vector quantisation (fp16/int8) | real 50–75% saving but changes recall; gate on gold separately |
| `auto_vacuum` | freelist is **0.2%** — nothing to reclaim |
| `BEGIN CONCURRENT` | not in mainline SQLite |

---

## Gates

Run after Part A, and again after Part B. Both must pass.

1. **Row counts preserved** for every table except deliberate orphan removals, which must
   match the reported sweep counts exactly.
2. **Gold harness: recall@10 ≥ 0.750, MRR ≥ 0.514.** B1 and B2 should be ranking-neutral by
   construction, so any movement here means something is wrong — investigate, do not accept.
3. **Latency, before/after, on the real store** for the 0.7.105 benchmark methods:
   `doc_ids_for_messages`, `thread_chunks`, `chunks_for_file`, `inbound_chunks_since`.
   Measured through the real store methods, not hand-written SQL.
4. **Size** reported before/after; expect ≈2.62 GB → ≈1.6 GB.
5. `PRAGMA integrity_check` and `PRAGMA foreign_key_check` clean on the rebuilt file.
6. Impacted tests plus `ruff`. The full suite is Josh's to run.

## Rollback

The encrypted snapshot from step 1 plus the retained old file. `backup.restore` already
round-trips to a SQLite file, so recovery is an existing, live-verified path rather than new
code.

## Order of work

Part A first and separately — it is config-only, immediately reversible, and its measurements
establish the baseline the rebuild is judged against. Part B second, as one pass.
