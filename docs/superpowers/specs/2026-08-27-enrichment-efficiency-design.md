# Enrichment pipeline efficiency — design

**Date:** 2026-08-27
**Status:** approved design, pre-implementation
**Scope:** eight fixes to the embed → enrich pathway, grouped into three workstreams plus scaffolding.

## Problem

The enrichment architecture is sound. Its pre-filtering is genuinely good: 65,753 of
174,826 chunks (37.6%) are cold-marked and never reach the model, while staying
embedded and keyword-searchable. The noise filter, the trivial-thread short-circuit,
and deterministic sender entities all do real work. mcpbrain is not over-extracting
*content*.

The cost is per-unit overhead, and it has compounded into a self-reinforcing loop.

Measured on the live store, 868 queued units (860 processable, 8 over cap):

| | bytes | ~tokens |
|---|---|---|
| work content across 860 units | 4,975,178 | 1.24M |
| the same `context.json` repeated 860× | 39,139,460 | 9.78M |
| **total claim payload** | **44,114,638** | **11.0M** |
| context share | | **88.7%** |

### The loop

`context.json` is 45,511 bytes, of which `known_people` is 39,017 (405 people). Only
40 come from `build_known_people`'s `core_cap`; the other 365 come from the
*uncapped batch overlay* over ~930 threads. That list then feeds the packing budget:

```python
budget = max(2000, pull_cap - _UNIT_RULES_RESERVE - ctx_len - 1500)
       = max(2000, 60000 - 11000 - 45508 - 1500) = max(2000, 1992) = 2000
```

The context ate the entire 60,000 budget, so `_pack_by_size` emits ~2,000-char units —
one thread each, 7× underfilled against the median 5,785 bytes of work — which
multiplies the number of times the 45KB context is sent. Context growth → budget
collapse → more units → more context copies.

### Compounding defects found alongside it

1. **`_UNIT_RULES_RESERVE = 11_000` is stale.** The real `_enrich_rules()` block is
   24,554 chars. With the context at 45,511, **all 868 units** exceed `unit_pull_cap`
   (60,000) on the `with_rules=True` path before a single byte of work is added
   (45,511 + 24,554 = 70,065). The cap is currently unmeetable by construction.

2. **The 50KB soft-limit trim inverts extraction quality.** 280 of 868 units (32%)
   cross `_PULL_SOFT_LIMIT`, and the fallback strips context to three keys, dropping
   `known_people` entirely. The largest, most substantive threads get the least
   context; trivial one-liners get all 405 people.

3. **`community_summaries` is dead payload.** `prepare._community_summaries_for_people`
   builds 6,255 bytes into every unit's context, but nothing consumes it — not
   `enrich_prompt.md`, not the `enrich-batch` agent, not `routines/enrich.md`. (The
   `community_summaries` *table* is used by `graph_view`/`lint_graph`/`community_synth`;
   the *context key* is not.) That is 5.38MB / ~1.34M tokens shipped with no
   instruction on how to use it.

4. **Units that can never complete.** 8 units exceed the cap; the largest is 5,075,515
   bytes — a saved shopping page (`Women's & Men's Clothing, Shop Online Fashion _
   SHEIN.html`) held as a single Drive document of 2,904 hot chunks / 4,946,743 chars.
   `_split_long_thread` cannot split a one-message thread, so it logs a warning and
   ships it whole. A Haiku drainer cannot hold ~1.27M tokens, so it never pushes;
   `_give_up_or_bump` fires only *on push*, so the attempt counter never increments and
   the unit never gives up. Its chunks stay `enriched=0`, and prepare re-reads and
   rewrites the same 5MB file every spool cycle. In the last 3MB of daemon log:
   **16,759** over-budget warnings and **11,528** "shipping unsplit".

5. **Captured notes are never chunked.** `drain_captures` calls
   `store.upsert_chunk(doc_id, text, …)` directly, bypassing `chunk_text`. Result:
   3,299 note chunks, 21.9MB, up to 133,791 chars each. Only the first ~2,000 chars of
   each are embedded (the BGE window), so the remainder is invisible to vector search.

6. **Rules are one-size-fits-all.** 12,142 of the 24,554 rule chars are block-kind
   protocols (merge-review, org-hygiene, ownerless, memory-distil, …). 850 of 868 units
   are `kind: "thread"` and need none of them.

### Not defects (checked, recorded so they are not re-derived)

- The reflow backlog is **0** — nothing is being needlessly re-extracted.
- Digest chunks (`enriched-<thread_id>`, 22,887 + 1,570 rows) are correctly excluded
  from both `unenriched_chunks` and `reflow_outdated_chunks`.
- 1,013 chunks carry `enriched_version = 2` while `ENRICH_LOGIC_VERSION = 1`. Harmless
  today, but they are immune to a future bump to 2. Recorded, not fixed here.

## Decisions taken

| Decision | Choice |
|---|---|
| Retroactive migration | Targeted one-shot attended sweeps, not fix-forward-only and not a blanket migration |
| Where scoped context is computed | **Approach A** — at write time, into the unit file |
| `known_people` inclusion rule | Lexical scan + alias-aware matching + the existing top-40 core |
| Note re-chunk sweep | In scope, behind the gold gate |
| Extraction-quality gate | Offline A/B over real queued units |

### Why approach A

`_unit_payload` is deliberately pure file I/O ("mirroring `brain_ingest`"). Filtering
at serve time would put a store read on the claim hot path for alias resolution, and
the alias/core lookups need the store, which `prepare` already has and the MCP server
does not. Freezing context at write time is consistent with units being immutable
work. Serve-time filtering's only real advantage is freshness, which is worth little
when units drain within hours.

Unit ids hash `kind:thread_ids`, not content, so a re-produced unit keeps its id and
`_atomic_write` refreshes its context in place. The existing "refreshing context under
in-flight units is harmless" invariant holds unchanged.

### Why the core stays at 40

Alias-aware matching was accepted on condition that alias coverage be checked first.
It fails that check today: **175 of 5,992 person entities (2.9%) carry any alias, and
zero of the 405 `known_people` carry a novel one.** Alias matching is still specified —
it is a few lines, it is the correct mechanism, and coverage grows on its own via
`merge_entities`' loser-alias carry (0.7.87) — but it must not be treated as earning a
smaller core. The top-40 core is what actually carries the nickname case
("Bob" for "Robert Smith") and is not reduced.

Stored aliases are pipe-delimited *inside* JSON list elements (`'Pete|Peter'`,
`'Taryn Hansen|Taryn'`), so matching must split on `|` as well as list boundaries.

---

# W1 — Payload economics (#1, #2, #5, #7, #8)

## Core change

`prepare` computes each unit's context at write time and writes it into the unit file.
`context.json` is deleted. `_unit_payload` reads the unit's own `context` key and
serves it verbatim, remaining pure file I/O.

The response keeps its `context` key, so **no change to `enrich_prompt.md`, the
`enrich-batch` agent, or `routines/enrich.md` is required.**

## Context selection

`prepare._scoped_known_people(store, unit_text)` — union of three sources, deduped by
entity id:

1. **Lexical** — every `known_people` entry whose canonical name, or a ≥4-char
   distinctive token of it, appears in the unit's serialized text. This is
   `drain._name_grounded`'s heuristic run in reverse; both call one shared helper so
   they cannot drift.
2. **Alias-aware** — the same match against stored aliases, parsed on both list
   elements and the `|` delimiter.
3. **Core** — the existing top-40-by-`email_count` list from
   `prompt.build_known_people`, unchanged.

Ranked core → exact-name → token → alias, then trimmed to `CONTEXT_CAP = 8_000` bytes.
This trim *replaces* the current drop-`known_people`-entirely fallback (#7), so
degradation is graceful rather than inverted.

Measured distribution of the selection (lexical + top-40 core) over the 860 real units:

| p50 | p75 | p90 | p95 | p99 | max | mean |
|---|---|---|---|---|---|---|
| 5,618 | 6,535 | 7,643 | 8,312 | 10,467 | 14,679 | 5,952 |

`CONTEXT_CAP = 8_000` is p95 and trims 59 units (6.9%). Total scoped context across the
queue is 5,118,962 bytes vs 33,554,620 today — **15.3%**.

`community_summaries` is **deleted**, not scoped: it has no consumer.

## Rules by kind (#8)

`_unit_payload` serves only the sections a unit's kind needs:

- `kind: "thread"` → envelope + standing-context + entity-discipline + drive-mode +
  thread-mode (~12.4KB of 24.5KB).
- `kind: "block"` → envelope + standing-context + that block's own section.

Implemented by splitting `enrich_prompt.md` on its existing `##` headings behind a
section index. No prompt rewrite. `bin/sync_agents.py` keeps the drainer's full-set
copy byte-identical, because the drainer handles every kind and its system prompt is
cached across the pool.

## Derived reserve (#5)

`_UNIT_RULES_RESERVE` becomes `max(len(rules_for_kind))` computed from `_enrich_rules()`,
not a literal. A test asserts no kind's real rules exceed the reserve.

## Packing budget (#2)

```
budget = pull_cap − _UNIT_RULES_RESERVE − CONTEXT_CAP − margin
       = 60,000 − 12,500 − 8,000 − 1,500 = 38,000        (vs 2,000 today)
```

The reserve is the **max across kinds**, applied uniformly — one budget, not a
per-kind one. The thread kind is the max (~12.4KB); the largest block kind is
envelope + standing-context + org-hygiene (~6.8KB), so block units are sized
conservatively. A single budget keeps `_pack_by_size` kind-agnostic and cannot
under-reserve.

The circular dependency is gone: context no longer depends on the batch, and the budget
no longer depends on a value that grows with the corpus.

## Data flow

`build_pending` stops calling `_build_context`. `write_units` packs threads by work size
against the budget, then computes scoped context *per pack*, and writes
`{unit_id, kind, threads, context}`. The 405-entry match index is built **once per
`write_units` call**, not per unit.

## Failure modes

- Empty match set → core-only. Never empty.
- Store error during scoping → fall back to core-only and log; never fail the cycle
  (matches `_community_summaries_for_people`'s existing degrade posture).
- A unit written before this change has no `context` key → `_unit_payload` serves `{}`.
  Transitional guard only; the queue is rebuilt, so this is not a supported state.

## Tests

- Selection returns core ∪ lexical ∪ alias; pipe-delimited aliases parse.
- Trim is relevance-ranked and respects `CONTEXT_CAP`.
- Budget is deterministic and independent of corpus size.
- `_unit_payload` serves the unit's own context and never reads `context.json`.
- Rules-by-kind returns only the requested sections; every kind fits the reserve.
- **Regression:** no unit exceeds `pull_cap` on the `with_rules=True` path — the
  invariant all 868 units violate today.

## Projected effect

Context 39,017 → ~6,100 bytes/unit; 860 units → ~131; total payload
**11.0M → ~1.4M tokens (−87%)**.

---

# W2 — Oversize content (#3, #6)

## Splitting at chunk seams, not paragraphs

Paragraph-splitting cannot map a part back to the chunks it covered, and that mapping is
what makes marking correct. A message body *is* a join of chunks, so it splits back at
those seams.

- `reassemble_thread` currently joins chunk text and discards provenance (it keeps
  `parts[0]`'s metadata). It gains a per-message `chunk_doc_ids`, ordered parallel to
  the pieces `_join_with_gaps` already walks.
- `_split_long_thread` splits *within* an over-long message at those seams. Each part
  carries `part_doc_ids` — exactly the chunks whose text it contains.

Since `chunk_text` bounds chunks at ~1800 chars, any budget ≥1800 is reachable.
**Nothing is truncated; every character reaches the extractor.**

## Part-precise marking

This is what makes splitting safe. Today a Drive extraction's `message_id` is the
`file_id`, so `doc_ids_for_messages` resolves to *every chunk of the file*. Part 1 would
mark the whole document enriched and parts 2..N would be wasted.

In `drain`, at the point where `drop_cold` already narrows a Drive file-wide resolve:

```
doc_ids = extraction["part_doc_ids"]              if present
          else store.doc_ids_for_messages(msg_ids)
doc_ids = store.drop_cold(doc_ids)
```

`part_doc_ids` is system-owned and read from the unit file, exactly as `messages[]`
already is — never from the model's echo.

`_regroup_parts` unions `part_doc_ids` when merging parts within one file, and its
`of`-mismatch warning drops to `info` when every present part carries them: parts split
across units are **independently applicable by design**, not evidence of a dropped part.

Note the known trade-off: a part is extracted without sight of its siblings, so an
entity in part 1 is not visible in part 3. This is inherent to splitting, is mitigated
by `prior_thread_context`, and is strictly better than the status quo of not extracting
the document at all.

## Note chunking (#6)

Follows `consolidation.py`'s established precedent exactly:

- Single chunk keeps the bare `note-<hash>` — **2,109 of 3,299 notes (64%) need no
  migration at all.**
- Multi-chunk gets `note-<hash>-<i>`.
- `chash` stays the full-note hash on every piece, so re-capturing identical content is
  still a no-op.
- Each piece carries `note_id` (the base id) plus `chunk_index`/`chunk_total`, which
  `_join_with_gaps` already reads — so #6 composes with #3 for free.

### Two knock-ons that #6 must fix, or it breaks memory

- `store.note_chunks()` groups by `note_id` (falling back to `doc_id` for legacy rows),
  reassembles in `chunk_index` order, and returns one row per note with `doc_id` = base
  id. `memory_index`'s 120-char hook and `memory_distil`'s doc_id-keyed verdicts keep
  working unchanged.
- `memory_distil.drain_distil` calls `patch_chunk_metadata(doc_id, …)` on a base id that
  no longer exists as a row; the patch would silently no-op and notes would be
  re-distilled forever. It needs `store.patch_note_metadata(note_id, **fields)`, which
  patches every sibling. These fields are not read by `_fts_text`/`contextual_prefix`,
  so this does not touch the known `patch_chunk_metadata` FTS-drift bug — and must not
  make it worse.

## Backstop

`brain_enrich_claim` bumps the unit's attempt counter **on claim**, so anything
unprocessable gives up after `_EMPTY_ATTEMPT_CAP` instead of re-queuing forever. #3
should make this unreachable; it makes #3 being wrong non-fatal.

## Tests

- Splitting is lossless: concatenated parts equal the original text.
- Every part's `part_doc_ids` is exactly its chunks; the union over parts is the whole
  set.
- A part marks only its own chunks.
- `_regroup_parts` unions correctly and does not warn on independent parts.
- Notes round-trip: chunk → `note_chunks` → identical text.
- Distil verdicts stamp every sibling.
- Legacy single-chunk notes are unaffected.

---

# W3 — Salience ceiling (#4)

Add `text/html` to `prepare._COLD_DRIVE_MIMES`. Narrow and evidence-backed: Drive
`text/html` is **exactly two documents** on this corpus (3,000 chunks / 5,072,100 chars),
both saved web pages. Cold is reversible and keeps them embedded, in FTS, and in recall
(`recall_excludes_cold` is off).

**No size ceiling.** Size is the wrong signal — it would also cold-mark the legitimate
278KB theology PDF. #3 removes the need by making large legitimate docs splittable. #3
does have a cost side, which #4 bounds: docs >100KB previously shipped whole and failed,
consuming nothing; after #3 they are actually extracted.

| doc | after W2 + W3 |
|---|---|
| SHEIN, 4.95MB | cold-marked, 0 tokens |
| Bookabin, 123KB | cold-marked, 0 tokens |
| theology PDF, 278KB | ~8 parts, ~70k tokens one-off |
| Hardy Final Report, 128KB | ~4 parts, ~32k tokens one-off |

A 5.5MB unbounded liability becomes ~100k tokens of one-off work on two documents worth
extracting. (Four docs >100KB hold 5,475,585 of the 7,460,419 chars of hot Drive text —
73%.)

---

# W0 — Scaffolding

## A/B harness, split at the model boundary

There is **no Anthropic API key anywhere in mcpbrain**, and 0.7.106 deliberately removed
the only subprocess-`claude` path as a ~6s-per-call cost. The only model access is
Claude Code subagents. The harness is therefore two deterministic halves with a session
in the middle:

1. `prep` — take N real queued units and emit paired payloads: **A** = today's full
   405-person context, **B** = scoped context. Pure file I/O, no model.
2. *(a Claude Code session drains both sets through `enrich-batch` subagents — the
   mechanism that already exists)*
3. `score` — diff the two extraction sets on entity-set agreement, per-entity
   `org`/`role` agreement, relation-set agreement, and action count. Disagreements are
   the artifact.

**Gate:** B must not lose org/role assignments A got right. Disagreements are eyeballed,
not auto-passed — a count match would hide a systematic misattribution, which is exactly
why `enrich_eval.graph_metrics` is insufficient here.

## Gold gate

`tests/eval/run_eval.py --gold --k 10` before and after W2's note sweep and W3's cold
sweep. Floor: **recall@10 ≥ 0.780 / MRR ≥ 0.550** per the runbook. The note sweep should
*improve* it — 21.1MB currently has no vector past each note's first ~2,000 chars.

## Sweeps

Both attended, dry-run default, `--yes` gated, daemon stopped, backup verified first —
the `bin/consolidate.py` / `bin/optimise_store.py` posture. **Nothing in the daemon's
cadences calls either.**

- `bin/resalience.py` — re-run `should_enrich` over non-cold chunks and cold-mark those
  that now fail. Generalised rather than hardcoded to HTML, so the next gate change
  needs no new script. Reversible via `set_enrich_state`.
- Note re-chunk sweep — 1,192 notes / 21.1MB (96% of note text; 670 of them >24KB
  holding 16.5MB). 2,109 notes are untouched.

## Queue rebuild

Delete `enrich_queue/units/`, `claims/`, and `context.json` after W1 lands. Units are
content-addressed and regenerate from `enriched=0` chunks, so nothing is lost and no
migration code is needed.

## Sequencing

**W3 → W2 → W1.** W3 first: one line, and it removes the 4.95MB document being rewritten
every spool cycle right now. W2 next: W1's packing budget assumes nothing unsplittable
can enter the queue. W1 last, gated on the A/B.

---

# Out of scope

- Bumping `ENRICH_LOGIC_VERSION` (the 1,013 rows at version 2 are recorded, not fixed).
- `patch_chunk_metadata`'s FTS-mirror drift (7,343 chunks) — a known separate follow-up.
- `memory_distil`'s own payload cost (it ships full note bodies to the model).
- Splitting the `enrich-batch` agent into per-kind agents; its system prompt stays whole.
- Any change to `recall_excludes_cold`, `embed_skip_tabular`, or `schema_grounding`.
