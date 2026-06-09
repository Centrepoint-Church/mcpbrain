# Stale-action auto-close (re-extraction trigger) + ClickUp reopen sync — design

**Date:** 2026-06-09
**Status:** approved (brainstorm), pending spec review
**Owner:** Josh Kemp

## Goal

Make the brain close actions that are no longer needed **without adding a
parallel, keyword-driven closer**. The existing LLM extractor is already the
smart decision-maker: when a thread is re-extracted it is handed the thread's
`open_actions` and instructed to *"prefer resolving or updating over creating"*,
returning `resolved_action_ids` which close the matched actions (and ClickUp
follows on the next sync). This design closes the two real gaps in that path
without ever letting a raw keyword match close anything:

1. **Gap A — resolution the thread re-extraction never re-sees.** A chunk is
   re-extracted only while `enriched=0`. Once a thread is `enriched=1` it is
   never re-run. So if the resolving message was enriched *before* the action
   existed or was linked (backfill / out-of-order sync), nothing re-triggers the
   close. The action stays open forever.

2. **Reopen gap — ClickUp reopen doesn't propagate for non-ClickUp closes.**
   Inbound sync reopens a brain action only when `resolved_by == "clickup"`. Any
   action closed by the LLM (or a capture block) that the user reopens in
   ClickUp silently fails to reopen in the brain — and the outbound-close step
   then re-closes the task on the next cycle. This change produces *more* LLM
   closes, so this gap would bite more often; it is folded into scope.

## Explicitly out of scope (and why)

- **No keyword-driven close.** `action_is_stale()` (retrieval.py) is a cheap
  keyword heuristic (`done`, `sorted`, `handled`, …) with a short exclusion
  list. It has false positives ("well done team", "done deal"). When it fires
  but an action is still open, that is usually because the LLM *already saw the
  message and correctly declined to close*. Closing on the keyword would
  override the smarter judgement. The keyword flag is used **only as a cheap
  trigger to give the LLM another at-bat**, never as a close decision.

- **Gap B — off-thread / obsolete resolution** (a note, calendar event, or
  in-person "handled that" resolving an email-sourced action; time-based
  obsolescence). Considered and deferred: it needs a per-candidate LLM
  adjudication sweep with its own token cost and a genuinely new close path.
  Not built now.

## Part 1 — Stale flag as a re-extraction trigger (Gap A)

### Principle

The only reason a thread-resolved action stays open is that its thread is
`enriched=1` and never re-run. So: when an open action looks stale but its
thread has no pending enrichment work, reset that thread to `enriched=0`. The
**normal enrichment cycle** then re-extracts it with `open_actions` in context,
and the **existing** `resolved_action_ids` path closes it. The LLM stays the
sole decision-maker; we only schedule the at-bat. No new close logic.

### Candidate selection (no LLM)

An open action qualifies for a re-extraction trigger when **all** hold:

- `action_is_stale(store, action)` is true (existing keyword heuristic; actions
  with no `thread_id` already return false and are skipped);
- the action's thread has **zero unenriched chunks** — otherwise the normal
  enrichment path already covers it this cycle, so we must not double-trigger;
- the thread has not already been triggered **at its current content state**
  (loop guard, below).

### Trigger

- New store method `mark_thread_unenriched(thread_id) -> int` sets `enriched=0`
  on **only** that thread's chunks (returns the count flipped). It does not
  touch `embedded` and does not touch any other thread.
- Record a per-thread marker = a **signature** of the thread's current chunk
  set, so the same unchanged thread is never re-triggered twice.

### Loop guard (the critical detail)

If the LLM *declines* to close (the keyword was a false positive), the action
stays stale indefinitely. Without a guard the sweep would reset the same thread
every run, paying the full re-extraction token cost forever.

- The signature is `sha256` over the thread's `(doc_id, content_hash)` pairs in
  `doc_id` order. It changes if and only if the thread's content changes.
- A thread is re-triggered **at most once per content-state**. When a genuinely
  new message later arrives, the content (and signature) changes — and the
  normal `upsert_chunk` path already reset those chunks to `enriched=0`, so we
  never fight the normal path. In practice the manual trigger fires only for the
  backfill / late-link case it is meant for.
- Storage: a dedicated tiny table
  `stale_reextract(thread_id TEXT PRIMARY KEY, signature TEXT NOT NULL,
  triggered_at TEXT NOT NULL)`. On trigger, upsert `(thread_id, signature,
  now)`. Skip a candidate when an existing row's `signature` equals the current
  signature.

### Cadence and cost honesty

- New daily cadence `maybe_stale_reextract`, config key
  `stale_reextract_interval_s` (default `86400`), following the existing
  `_CADENCE_KEYS` / `maybe_*` gate pattern in `daemon.py`
  (`if now - last >= interval`), with the same `_last_*` in-memory anchor (runs
  once on each process start, then on cadence).
- The sweep itself does **no LLM work**. It does, however, cause the flagged
  threads to be re-extracted once by the normal enrichment cycle — a real,
  **bounded, one-time** token cost per thread (contrast Gap B, which would cost
  per-candidate per-sweep).
- A per-run cap `STALE_REEXTRACT_MAX = 20` threads bounds the cost. If the
  candidate set exceeds the cap, the sweep triggers the first 20 and **logs the
  number deferred** (no silent truncation); the rest are picked up next run.

### Flow

```
maybe_stale_reextract (daily)
  └─ for each open action, in id order, until cap:
        action_is_stale? ───no──> skip
              │yes
        thread has unenriched chunks? ──yes──> skip (normal path covers it)
              │no
        signature == recorded? ──yes──> skip (already had its at-bat)
              │no
        mark_thread_unenriched(thread_id); record signature
  (deferred count logged if capped)

next normal enrichment cycle
  └─ re-extracts the thread WITH open_actions
        └─ LLM returns resolved_action_ids ──> set_action_status(done, resolved_by=msg_id)
              └─ next clickup_sync outbound close ──> ClickUp task closed
```

## Part 2 — ClickUp reopen by tracking last-synced state

### Problem

Current inbound reconciliation (clickup_sync.py:61-69):

```python
a_done = (action.get("status") or "").lower() in ("done", "closed")
resolved_by = (action.get("resolved_by") or "").lower()
if task["closed"] and not a_done:
    store.set_action_status(aid, "done", "clickup")          # close
elif (not task["closed"]) and a_done and resolved_by == "clickup":
    store.set_action_status(aid, "open", "")                 # reopen
```

`resolved_by == "clickup"` is the wrong signal. It conflates *who closed it*
with *did the user reopen it*. An LLM-closed action (`resolved_by` = an email
message id) reopened in ClickUp never reopens in the brain, and the outbound
step re-closes its task next cycle. The guard exists for a real reason, though:
to avoid reverting a **brain-local close mid-propagation** — the moment an
action goes `done` its ClickUp task is still open until step 3 closes it, and we
must not "reopen" it in that window.

### Fix — persist last-observed ClickUp closed-state

The real question is a **state transition**: *was this task closed the last time
we looked, and is it open now?* That is a genuine user reopen. Persist the
last-observed ClickUp closed-state per action and key the reopen off it.

- **New column** `actions.clickup_closed` (nullable INTEGER; `NULL` = never
  observed, treated as "not previously closed" — the conservative default, so no
  backfill is required). `1` = last seen closed, `0` = last seen open.

- **Reopen rule becomes:**

  ```python
  prev_closed = bool(action.get("clickup_closed"))   # NULL -> False
  if task["closed"] and not a_done:
      store.set_action_status(aid, "done", "clickup")          # close (unchanged)
      diff["status"] = "done"
  elif (not task["closed"]) and a_done and prev_closed:
      store.set_action_status(aid, "open", "")                 # reopen (any close origin)
      diff["status"] = "open"
  ```

  - Task we synced as closed (`prev_closed=True`) is now open → the user
    reopened it → reopen the brain action regardless of who closed it. **The
    fix.**
  - Action done + task open + `prev_closed` False/NULL → brain-close
    mid-propagation → leave it. The race the old guard protected stays
    protected.
  - This **subsumes** the old `resolved_by=="clickup"` case (a ClickUp-closed
    task has `clickup_closed=1`) and extends it to every close origin — a strict
    improvement, not a parallel path.

### Maintaining `clickup_closed` (so the signal never goes stale)

Update it everywhere the true ClickUp state is known:

- **inbound apply** (`_apply_inbound`) → set to `task["closed"]` each cycle;
- **outbound close, on success only** → set `1` (so a reopen happening before
  the next inbound poll is still detected; a *failed* `close_task` leaves it `0`,
  so the next cycle retries the close instead of falsely reopening);
- **create / link a new task** (outbound create, `set_action_clickup_id`) → `0`;
- **baseline import** → `t["closed"]` for both linked and newly created actions.

### Why state, not timestamps

Comparing `task.date_updated` vs `action.resolved_at` would avoid a new column
but `date_updated` bumps on *any* edit (rename, comment), so it would spuriously
reopen. The closed→open transition is exactly what persisted state captures
cleanly, with no clock-skew dependence.

## Data model changes

- `actions.clickup_closed` — nullable INTEGER, added in the `store.py` migration
  block alongside `clickup_task_id` / `priority` (same idempotent
  `ALTER TABLE … ADD COLUMN` guarded by a column-exists check).
- `stale_reextract(thread_id TEXT PRIMARY KEY, signature TEXT NOT NULL,
  triggered_at TEXT NOT NULL)` — new table created in the same migration path.

## New / changed code

- `mcpbrain/store.py`
  - `mark_thread_unenriched(thread_id) -> int` — set `enriched=0` on a thread's
    chunks; returns count.
  - `thread_has_unenriched(thread_id) -> bool` — true if any chunk in the thread
    is `enriched=0` (candidate-selection guard against double-triggering a
    thread the normal path already owns).
  - `thread_signature(thread_id) -> str` — sha256 over ordered
    `(doc_id, content_hash)` pairs (empty thread → stable empty-signature
    constant).
  - `get_stale_reextract(thread_id)` / `set_stale_reextract(thread_id, sig, ts)`.
  - `set_action_clickup_closed(action_id, closed: bool)` (or extend
    `update_action_fields` to accept `clickup_closed`).
  - Migration: add `clickup_closed` column + `stale_reextract` table.
- `mcpbrain/stale_reextract.py` (new, small) — `sweep(store, *, now, cap=20)`:
  candidate selection + trigger + loop guard + capped/deferred logging. Pure
  logic over the store (unit-testable with a fake store; no daemon import).
- `mcpbrain/daemon.py` — add `stale_reextract_interval_s` to `_CADENCE_KEYS`;
  add `maybe_stale_reextract` gate calling `stale_reextract.sweep`.
- `mcpbrain/clickup_sync.py` — `_apply_inbound` reopen rule uses `prev_closed`;
  maintain `clickup_closed` in inbound, outbound close (on success), outbound
  create, and `import_baseline`.

## Testing

**Part 1 (stale → re-extract):**
- candidate selection picks an action that is stale **and** whose thread has no
  unenriched chunks **and** has no matching signature row;
- a stale action whose thread still has unenriched chunks is **skipped** (normal
  path owns it);
- `mark_thread_unenriched` flips `enriched=0` for the target thread's chunks
  only, leaving other threads untouched;
- loop guard: a second sweep at the **same** content-state triggers nothing;
  after the thread's content changes (signature differs) it re-arms;
- per-run cap: with > cap candidates, exactly `cap` are triggered and the
  deferred count is logged;
- integration: a stale action whose thread carries a resolution, with a **stub
  extractor** returning `resolved_action_ids`, ends up `done` after sweep + one
  enrichment cycle.

**Part 2 (reopen):**
- task closed→open with `clickup_closed=1` reopens the action (regardless of
  `resolved_by`);
- action done + task open + `clickup_closed` `0`/`NULL` does **not** reopen
  (mid-propagation race protected);
- a failed outbound `close_task` leaves `clickup_closed=0`, so the next cycle
  retries the close instead of reopening;
- full round-trip: brain close → outbound close sets `clickup_closed=1` →
  ClickUp reopen → inbound reopens the brain action → outbound does not re-close
  (action now open);
- `clickup_closed` is set on create (`0`), inbound (`task.closed`), and baseline
  import (`t.closed`).

## Risks / notes

- **Token cost is bounded but non-zero.** The sweep re-extracts flagged threads
  once each (capped at 20/run, daily). This is the minimal cost to close a
  genuinely-resolved-but-stuck action and is far below a per-candidate LLM
  adjudication. The cap + signature guard prevent runaway re-enrichment.
- **No new false-positive close surface.** Every close still goes through the
  LLM's `resolved_action_ids`; a false-positive keyword merely buys one wasted
  (guarded, one-time) re-extraction, never a wrong close.
- **Reopen is now origin-agnostic**, which is the desired behaviour, and it
  removes a latent desync (LLM-closed action reopened in ClickUp → silently
  re-closed). The mid-propagation race remains covered by `prev_closed`.
