# Memory "keep" re-review window — design

**Date:** 2026-07-25
**Status:** approved (brainstorming) → ready for implementation plan

## Problem

The findings closure-durability fix (`docs/superpowers/specs/2026-07-25-findings-closure-durability-design.md`)
added a `distilled_at` chunk-metadata stamp so `memory_distil` stops re-submitting an
already-classified note to Haiku on every distil run. All three verdicts (`keep`/`expire`/
`promote`) stamp it, and the stamp is permanent — `note_chunks(exclude_distilled=True)` excludes
any note that has one, forever.

That permanence is correct for `expire` (a genuine "this is stale" decision, and already
independently permanent via the separate `expired` flag) and for `promote` (the note became a
real memory file). It is wrong for `keep`: "keep" is the LLM's way of saying "not clearly
durable, not clearly stale" — a deferral, not a decision. Making it permanent means most memory
notes will get `keep`d exactly once, at their freshest and therefore least-informative moment,
and then never be looked at again. The live memory-note set (read by `memory_index.regenerate`
via `note_chunks(observation_type="memory")`, capped at `limit=500`) grows monotonically as a
result, and will eventually silently truncate the oldest notes out of `memory.md`.

Measured on the live store: 32 memory-type notes exist today, 0 already stamped (the fix
hasn't run a cycle yet) — not urgent, but worth deciding correctly before it compounds.

## Approach

Give `keep`-verdicted notes a re-review window; leave `expire`/`promote` exactly as they are.

This needs a way to tell, at exclusion-check time, which verdict produced a given
`distilled_at` stamp — today it's a bare timestamp with no record of why. Add a companion
metadata field, `distilled_verdict` (`"keep"` | `"expire"` | `"promote"`), stamped alongside
`distilled_at` in all three `drain_distil` branches. No schema migration: this is chunk
metadata (a JSON blob), not a SQL column.

**Window length: 30 days**, matching this codebase's existing staleness-threshold convention
(`profile_synth.py`'s `_STALE_DAYS = 30`; `store.open_waiting_actions(window_days=30)`) rather
than inventing a new number. Threaded as a parameter with that default, following
`open_waiting_actions`'s exact `window_days=30` style — not a `config.py` getter, matching how
`build_distil_requests`'s own `cap=30` is handled today (a plain default, not fleet-configurable).

## Components

### `mcpbrain/memory_distil.py` — `drain_distil`

All three branches gain `distilled_verdict=<the verdict>` alongside their existing
`distilled_at=now` in the same `patch_chunk_metadata` call:

- `keep`: `patch_chunk_metadata(doc_id, distilled_at=now, distilled_verdict="keep")`
- `expire`: `patch_chunk_metadata(doc_id, expired=True, distilled_at=now, distilled_verdict="expire")`
- `promote`: `patch_chunk_metadata(doc_id, distilled_at=now, distilled_verdict="promote")`

`expire`'s `distilled_verdict` is inert for exclusion purposes (the independent `expired=True`
check already excludes it, unconditionally, before the `exclude_distilled` check even runs) —
stamped anyway so `distilled_verdict` is a complete audit trail of what happened to every note
that has ever been through distillation, not just the ones this mechanism time-boxes.

### `mcpbrain/store.py` — `note_chunks`

`exclude_distilled`'s per-row check changes from "exclude if `distilled_at` is truthy" to:

```python
if exclude_distilled and meta.get("distilled_at"):
    stale = False
    if meta.get("distilled_verdict") == "keep":
        try:
            stamped = datetime.fromisoformat(meta["distilled_at"].replace("Z", "+00:00"))
            stale = datetime.now(timezone.utc) - stamped > timedelta(days=keep_review_days)
        except (ValueError, TypeError):
            stale = False  # malformed timestamp: fail safe, stay excluded
    if not stale:
        continue
```

A malformed or missing `distilled_at` on a `distilled_verdict="keep"` row fails safe — treated
as not-yet-stale (stays excluded) rather than crashing or spuriously resurfacing. `.replace("Z",
"+00:00")` before `fromisoformat` matches this codebase's established parsing convention
(`decay.py`, `fleet.py`, `importance.py`, `feedback.py`, `auto_enable.py`, `probes.py` all do
this) even though Python 3.12's `fromisoformat` would accept the bare `Z` natively.

New parameter: `keep_review_days: int = 30`. Only consulted when `exclude_distilled=True` and
the row's `distilled_verdict` is `"keep"` — inert otherwise.

### `mcpbrain/memory_distil.py` — `build_distil_requests`

Gains a matching `keep_review_days: int = 30` parameter, passed straight through:

```python
def build_distil_requests(store, *, cap: int = 30, keep_review_days: int = 30) -> list[dict]:
    ...
    chunks = store.note_chunks(observation_type="memory", exclude_distilled=True,
                               keep_review_days=keep_review_days, limit=cap)
```

`daemon.py:2092`'s call site, `build_distil_requests(self._store)`, needs no change — it picks
up the new default.

## Consequence, by design

A stale "keep" note resurfaces in the next `build_distil_requests` call, gets asked about
again, and whatever verdict comes back overwrites both `distilled_at` and `distilled_verdict`
via `patch_chunk_metadata`'s merge semantics (`meta.update(patch)` — a true merge, confirmed
against the current implementation, so this never clobbers unrelated metadata like `org` or
`title`). Either it's re-`keep`t (parked for another 30 days) or it graduates to a permanent
`expire`/`promote`. `memory_index.py`'s call
(`store.note_chunks(observation_type="memory")`, no `exclude_distilled`) is untouched — a
distilled note, keep or otherwise, still renders in the memory index.

## Out of scope

- Making `keep_review_days` fleet-configurable via `config.py` / `org-config.json`. No one has
  asked for this to vary per install, and `build_distil_requests`'s own `cap` isn't configurable
  today either — matching that precedent rather than adding a knob nobody requested.
- Any change to `expire`/`promote`'s permanence. Confirmed correct, out of scope by design.

## Testing

- A `keep`-verdicted note stamped more than `keep_review_days` ago resurfaces in
  `build_distil_requests`'s output.
- One stamped less than `keep_review_days` ago does not.
- `expire`- and `promote`-verdicted notes never resurface regardless of stamp age (one test
  each, with an artificially old stamp, proving the age check doesn't apply to them at all).
- A `distilled_verdict="keep"` row with a missing or malformed `distilled_at` does not resurface
  and does not raise.
- The pre-existing `test_drain_expires_and_promotes` (unmodified) still passes — its
  `note_chunks(observation_type="memory")` call has no `exclude_distilled`, so it is unaffected.

## Files touched

- `mcpbrain/memory_distil.py` — `drain_distil` (all three branches), `build_distil_requests`
- `mcpbrain/store.py` — `note_chunks`
- `tests/test_memory_distil.py`, `tests/test_store_schema_p3.py`
