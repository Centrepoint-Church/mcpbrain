# ClickUp ⇄ mcpbrain actions sync — design

**Date:** 2026-06-08
**Status:** approved (brainstorm), pending spec review
**Owner:** Josh Kemp

## Goal

Give the new system (mcpbrain) the same useful ClickUp outcome the legacy
ops-brain has on Nexus, but in a far simpler form suited to a single user, a
single daemon, and localhost (no webhooks, no multi-org list mapping, no
separate web service).

The model is **not symmetric peer sync**. It is:

- **ClickUp is the editing surface and is authoritative for edits.** If Josh
  renames a task, changes its Org, due date, priority, or status in ClickUp,
  that flows back into the brain.
- **The brain creates tasks and closes them.** A new brain action becomes a
  ClickUp task; a brain action closed locally closes the task. The brain does
  not otherwise push edits up.

## Target list

- List: **"Josh Kemp To do"** — `901610549962` (space `90164846332`
  "Business Team", workspace `9003292002`).
- Link anchor is the **native ClickUp task id**, cached on the brain action as
  `clickup_task_id`. No custom field is used for linking (the earlier Brain ID
  field was dropped 2026-06-08 — it duplicated the native id and hit the plan's
  custom-field-usage quota; native-only is simpler and more robust).
- Fields used:
  - **Org** — dropdown custom field `9c73ab46-980b-4962-b09c-74bc532c3cb4`,
    options: `centrepoint` `8fd028a6-588f-4536-8bb5-9fcd93ddb17c`,
    `acc` `72a0118b-d4ca-4c91-be3b-851df9b4188c`,
    `courageous` `1a349d27-fd2d-4172-b7fc-31472fa64be8`,
    `curtin` `1bf34d11-d93b-4320-b864-5377dc57565d`,
    `personal` `263c064d-aeb8-4dca-aa1e-4c553a7e0629`.
- Status: ClickUp status `type` field is the signal — `open` ⇄ brain `open`,
  `closed`/`done` type ⇄ brain `done` (robust to label changes like
  "complete"/"done").

## The cutover problem (why this needs care)

- Nexus's **ops-brain** has been the live ClickUp syncer (its
  `clickup-outbound`/`clickup-poll` systemd timers run every 90s/20min). The
  94 existing tasks carry **Nexus** Brain IDs (e.g. 29497).
- The Mac mcpbrain is a *different, smaller* brain (≈111 actions, ids 1–111).
  Its action ids do not match the Brain IDs already on the list.
- Therefore two writers on one list would fight (clobbered Brain IDs,
  duplicated tasks).

### Cutover decision (chosen)

**Adopt the current ClickUp list as the baseline; the Mac owns sync from then
on; Nexus's ops-brain ClickUp sync is disabled.**

1. **Stop Nexus from writing.** Disable `ops-brain-clickup-outbound.timer` and
   `ops-brain-clickup-poll.timer` on Nexus (`systemctl --user disable --now`).
   This is the irreversible/operational step and is gated on explicit user
   confirmation at execution time.
2. **Baseline import (ClickUp → brain, one-off).** For every task on the list:
   - Match an existing Mac action by exact normalised text (lowercase, collapse
     whitespace, strip trailing punctuation). On match → **link** (cache
     `clickup_task_id` on the action; rewrite the task's **Brain ID** to the Mac
     action id).
   - No match → **create a Mac action** from the task (text, org, due, priority,
     status, source link parsed from description), then set its Brain ID.
   - Result: the list state becomes the brain's starting line; every task is
     anchored to a Mac action id.
3. **Cutover floor.** The Mac brain holds ~117 open email-extracted actions
   that conceptually duplicate the adopted ClickUp tasks (different wording, so
   no exact-text match). To honour "Nexus list = starting line, Mac adds from
   now", record `meta.clickup_sync_floor = max(action id)` at cutover. Outbound
   never pushes an action with `id <= floor`, so the pre-cutover backlog stays
   queryable in the brain but is not re-pushed as duplicates. Only actions the
   Mac creates *after* cutover sync out. Imported baseline actions get ids above
   the floor but are linked (have `clickup_task_id`) so they are never
   re-pushed either.
4. **From now on**, the ongoing sync pass (below) maintains it.

## Ongoing sync pass

New daemon periodic pass `maybe_clickup_sync`, gated on cadence
`clickup_interval_s` (≈300s), running inside the single daemon writer (no
locking concerns). Order each cycle:

1. **Inbound (ClickUp authoritative).** List tasks (incl. closed). For each task
   linked by Brain ID / `clickup_task_id`, mirror changed fields into the brain:
   - name → `text`
   - Org option → `org`
   - status type → `status` (`done`/`open`)
   - due_date → `deadline`
   - priority → `priority`
   - assignee (Josh) → owner (single-user: low value; sets owner if mapped)
   A task with a Brain ID that no longer resolves to an action is logged and
   skipped (never auto-deletes a brain action).
2. **Outbound (create + close only).**
   - Open brain action with no `clickup_task_id` and no exact-text match on the
     list → create a task (name=text, due, priority, Org, Brain ID, assignee
     Josh, **description** composed once — see below); cache the returned id.
   - Brain action closed locally while its task is open → set the task status to
     a closed-type status.

### Field mapping (matches ops-brain semantics)

| Brain action | ClickUp | Direction |
|---|---|---|
| `text` | task name | out (create) ; in (authoritative) |
| `deadline` (YYYY-MM-DD, Perth midnight) | `due_date` (epoch ms) | both |
| `priority` (new column: urgent/high/normal/low) | priority (1/2/3/4) | both |
| `org` | Org dropdown option | both |
| `status` (open/done) | status `type` | both |
| `context_tag` + `source_doc_id` | **description** (`text` + `Context:` + `Source:` gmail link) | out, **create-only** (so ClickUp notes survive) |
| `id` | **Brain ID** number field | out (anchor) |
| owner (Josh) | assignee `72748441` | out (create) |

Priority vocabulary: store ClickUp's own names (`urgent`/`high`/`normal`/`low`)
in `actions.priority` rather than inventing a P1–P4 scale, since the Mac brain
has no priority-derivation logic.

## Schema change

Add two columns to `actions` (idempotent migration in `Store.init`):

- `clickup_task_id TEXT DEFAULT ''` — cached link for fast outbound lookup.
- `priority TEXT DEFAULT ''` — ClickUp priority name, populated inbound.

## Modules

- `mcpbrain/clickup.py` (extend): `create_task`, `list_tasks(include_closed)`
  returning name/status-type/org/priority/due/brain_id/assignees,
  `update_task(...)` for status + custom-field writes, `set_brain_id`.
- `mcpbrain/clickup_sync.py` (new): pure-ish `sync(store, home, client)` doing
  inbound-then-outbound; plus `import_baseline(store, home, client)` for the
  one-off cutover import. Field-mapping helpers kept pure and unit-tested.
- `mcpbrain/daemon.py`: add `clickup_interval_s` to `_CADENCE_KEYS`, a
  `maybe_clickup_sync` pass in `_run_periodic_passes`.
- `mcpbrain/config.py`: already has `clickup_api_key`/`clickup_list_id`; add
  field-id config or hardcode the discovered ids in a small constant table.
- Config: `~/.mcpbrain/config.json` gets `clickup_api_key` (the `pk_727…` key
  from ops-brain `.env`), `clickup_list_id` `901610549962`, and
  `cadences.clickup_interval_s` (left UNSET until the baseline import is done).

## Safety / rollout

- Build + unit-test everything with no live calls (test doubles for the client).
- Run `import_baseline` as a **dry-run first** (log what it would link/create),
  review, then commit for real.
- Only after a clean import, set `clickup_interval_s` to enable the ongoing pass.
- Nexus timer disable is confirmed with the user at execution time.
- Every ClickUp call degrades gracefully (network errors logged, never crash the
  daemon loop), mirroring ops-brain's write-through resilience.

## Testing

- Pure mappers: deadline⇄ms, priority⇄name, org option⇄name, status type→done,
  description composition, normalised-text match.
- `sync()` with a fake client: inbound diff applies; outbound creates only
  unlinked/unmatched; closed action closes task; no ping-pong on a steady state.
- `import_baseline` with a fake list: exact-text matches link + rewrite Brain ID;
  non-matches create actions; idempotent on re-run.
- Migration: columns added once, safe on existing DB.

## Out of scope (v1)

- Tasks created natively in ClickUp after cutover become brain actions via the
  inbound pass only if they carry a Brain ID; un-anchored native tasks still
  show on the dashboard via the existing read path but are not imported as
  actions automatically (avoids unbounded growth). Revisit if needed.
- Multi-list / org→list routing (single list only).
- Realtime webhooks (poll cadence only).
