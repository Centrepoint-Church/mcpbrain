# Expansion: tabular/cold skip + fleet-flippable flag

**Date:** 2026-07-22
**Status:** approved, implementing
**Builds on:** `2026-07-22-expansion-injection-only.md` (injection-only expansion, `retrieval_expand` default OFF)

## Motivation

Injection-only expansion (0.7.100) validated on real data: a clear win for **prose**
docs/threads (a 200-char snippet → the full relevant email), but on **tabular** Drive
docs (rosters, calendars) it stitches a wall of CSV commas — bounded by the caps, but
low-value. And it can't be enabled fleet-wide: `org-config.json`'s allowlist is
`{cadences, org_pin}` and `retrieval_expand_enabled` reads only top-level config, so no
`org-config` key reaches it.

Two changes, one release, then flip on fleet-wide.

> **UPDATE (post-validation): Change 1 was DROPPED, not shipped.** The rep-chunk
> skip proved unreliable and net-negative in validation: `cold` wrongly skipped a
> prose email whose top chunk was a cold fragment (losing the good hits); untagged
> tabular files were missed; and the skip suppressed tabular content (rosters,
> calendars) that is often the actual answer for tabular queries. Expansion is
> bounded (4k cap) and `brain_search` never expands, so tabular expansion is
> low-harm — a reliable skip wasn't worth the machinery. **Shipped = Change 2
> (fleet-flippable flag) + expansion with no skip.** The below is retained for
> the record.

## Change 1 — skip expansion for tabular/cold parents (DROPPED — see note above)

Signal (from the live store): gdrive chunks carry `content_subtype` (`table` 15,855 /
`prose` 7,000 / none), and the salience gate cold-marks tabular/low-signal docs
(`memory_tier='cold'`). Both align on the roster/calendar files.

In `retrieval_expand.expand_hits`, before expanding a parent, check its rep chunk: if
`metadata.content_subtype == "table"` OR the chunk is cold (`get_chunk(...)['memory_tier']
== "cold"`), **skip stitching and keep the flat representative snippet** (the same
fallback the code already uses when a parent expands to nothing). Prose/unknown parents
expand unchanged. Helper: `_is_low_signal(store, doc_id) -> bool`.

Result: prose queries keep their rich expansion; tabular queries fall back to the flat
snippet (no CSV walls).

## Change 2 — generic fleet-flippable feature flags

Rather than special-casing this flag, add a general mechanism:
- `fleet._ALLOWLIST` gains `"flags"`, so `org-config.json = {"flags": {"retrieval_expand":
  true}}` is staged into `config["org_config"]["flags"]` (the wholesale-replaced overlay).
- `config.fleet_flag(home, name, default)` — precedence: `org_config.flags[name]` (org wins,
  so a fleet enable reaches everyone) → top-level `config[name]` (local override) → `default`.
- `config.retrieval_expand_enabled` delegates to `fleet_flag(home, "retrieval_expand", False)`.

Any future feature flag becomes fleet-flippable the same way.

## Release + fleet-enable

Bump version, ship all three repos, then publish `{"flags": {"retrieval_expand": true}}`
into the fleet `org-config.json` (Drive). Installs pick it up on next daemon start.

## Validation
- Gold gate unchanged (0.750/0.514) — brain_search untouched.
- Injection comparison: tabular queries (roster/youth-calendar) fall back to flat; prose
  queries (Aaron Close, College sem-2, board actions, SOM) still expand.
- `fleet_flag` unit tests: org overlay wins; top-level fallback; default.

## Out of scope
- The reranker (dropped, on-corpus net-negative). The full RAGAS answer-quality harness.
