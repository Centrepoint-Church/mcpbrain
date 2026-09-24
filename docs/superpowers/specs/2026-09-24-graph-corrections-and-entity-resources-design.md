# Graph corrections and entity resources — design

Date: 2026-09-24. Status: approved in brainstorming, not yet planned.

Two features, prompted by reviewing `modelcontextprotocol/servers`:

1. **Graph corrections** (from the reference Memory server's model-editable
   graph): let the model correct the knowledge graph from conversation, in a
   way that survives re-enrichment and can be undone.
2. **Entity resources** (from the reference Everything server's resource
   templates + completion): make people/orgs/projects @-mentionable as MCP
   resources.

Swapping mcpbrain for any reference server was considered and rejected: the
Memory server is a single JSONL file re-read per call with substring search,
no embeddings, no ingestion, no time dimension.

---

## Part 1 — Graph corrections

### Why stickiness is the whole problem

A correction that enrichment silently undoes is worse than none, because the
user believes it landed. Three existing behaviours would undo a naive one:

- `graph_write.py` (~line 585) **revives** an invalidated relation when the same
  `(entity_a, relation, entity_b)` is observed again.
- Nothing records "these two entities are different", so `resolve` /
  merge-review can re-propose a wrong merge.
- `entities.org` is overwritten by `org_backfill.py:61` and
  `review_apply.py` (two appliers); name and email have setters any path can call.

Role is already solved: `write_role_observation(source="manual")` is rank 5,
the highest in `graph_write._SOURCE_RANK`.

### Scope (ops)

| op | Effect | Reuses |
|---|---|---|
| `reject_relation` | invalidate a relation, sticky | `entity_relations` bitemporal columns |
| `assert_relation` | add a relation the user states | `graph_write` relation writer |
| `merge` | fold loser into winner | `graph_view.merge_entities` (guarded) |
| `not_same` | durable block on merging a pair | new table |
| `set_field` | role / org / name / email | `write_role_observation`, `graph_view.update_entity` |
| `hide` | reversible suppression | `store.suppress_entity` |
| `undo` | reverse one applied correction | ledger snapshot |

### Trigger policy

- `basis: "user_stated"` — the user stated the correction in conversation.
  Applied immediately. Taken on trust; covered by ledger + undo.
- `basis: "inferred"` — the model inferred it (e.g. from a newer email). Must be
  **confirmed by the user through a channel the model cannot forge** (below).
  The model never applies an inferred correction on its own say-so.

### Data model (new, all additive migrations in `Store.init()`)

- `graph_corrections` — the ledger and the pending queue:
  `id INTEGER PK, op TEXT, basis TEXT, status TEXT` (`pending|applied|reverted|declined|failed`;
  `failed` = approved on the dashboard but no longer applicable),
  `payload TEXT` (JSON, the op's arguments), `snapshot TEXT` (JSON, what undo
  needs), `reason TEXT`, `confirmed_via TEXT` (`''|elicitation|dashboard`),
  `error TEXT` (why an approval failed),
  `dedup_key TEXT`, `created_at, applied_at, reverted_at TEXT`,
  `change_log_id INTEGER`. Index on `(status)` and `(dedup_key)`.
- `entity_relations.user_verdict TEXT` — `'rejected' | 'asserted' | NULL`.
- `entity_distinct_pairs(a TEXT, b TEXT, PRIMARY KEY(a, b))`, `a < b` enforced
  by the writer, both FK to `entities(id) ON DELETE CASCADE`.
- `entity_field_locks(entity_id TEXT, field TEXT, PRIMARY KEY(entity_id, field))`,
  FK cascade. `field ∈ {org, name, email}`. (Neither carries a correction id:
  it is not known until the ledger row is inserted in the same transaction, and
  the ledger payload already records which pair or lock a correction made.)

### Stickiness mechanisms

- **Rejected relations.** The revive branch in `graph_write` skips any row with
  `user_verdict='rejected'` (no revive, no confidence bump). The row keeps
  `invalidated_at` and gets `superseded_reason='user_rejected'`.
  `merge_entities` carries `user_verdict` with the repointed row; where the
  repoint collides with an existing winner triple, the stricter verdict wins
  (`rejected` over `NULL`) on the survivor row.
- **Asserted relations.** Written with `source_doc_id=''`, evidence = the
  user's reason, `valid_from` = today, `user_verdict='asserted'`. The existing
  singleton recency rule still lets a genuinely newer extracted fact supersede
  it; an older extracted fact cannot.
- **Distinct pairs.** Checked in `resolve._candidate_pairs`,
  `resolve._deterministic_merges`, `resolve._email_equality_merges`,
  `review_apply.apply_duplicate_verdicts` (counted as `guarded`) and
  `graph_view._orient` (refused with `error: "marked_distinct"`, so the graph UI
  honours it too). `merge_entities` repoints pairs when a third entity is merged
  into one side (dropping a pair that would become a self-pair).
- **Field locks.** `store.update_entity_org`, `rename_entity`,
  `set_entity_email` gain `user: bool = False`; when a lock exists for that
  field and `user` is False they return False without writing. Fail-safe by
  default: every existing caller is covered without being edited. The
  corrections path and the graph UI's `update_entity` (a human edit) pass
  `user=True`. The graph UI edit does not create locks; only a correction does.

### Unmerge snapshot

Before a `merge`, inside the same transaction, the ledger `snapshot` records:
the loser's full `entities` row; its `entity_suppressions` row if any; the ids
of every `entity_relations`, `entity_observations` and `email_entities` row that
will be repointed; full copies of every row that will be deleted (duplicate
triples, self-loops, colliding email links); the winner's pre-merge
`name/type/org/mentions/aliases/email_addr/notes`; and any `entity_distinct_pairs`
rows repointed. Undo, in one `BEGIN IMMEDIATE`:

1. Refuses if the winner no longer exists, the loser id now exists, or any
   snapshotted repointed row no longer points at the winner (e.g. the winner was
   itself merged since). The refusal names the conflict. A partial restore is
   worse than none.
2. Re-inserts the loser, moves exactly the snapshotted ids back, re-inserts the
   deleted copies, restores the winner's scalar fields, deletes the
   `entity_merge_log` row, marks the ledger row `reverted`.

Undo of the other ops: `reject_relation` restores `invalidated_at`,
`superseded_reason`, `user_verdict` from snapshot; `assert_relation` deletes the
row if the correction created it, else restores its prior verdict;
`not_same` deletes the pair; `set_field` restores the prior value and removes
the lock (role: invalidates the manual observation, which re-exposes the prior
ranked one); `hide` unsuppresses.

### Tool interface

`brain_graph_correct` (one tool; declared in `tools.py` via `@tool`, schema
in `tool_registry`):

- input: `{op, basis, reason, ...}` with a `oneOf` per op. Entities are
  referenced by **id** (from `brain_context` / `brain_graph`), never by name.
  `undo` takes `correction_id`; `basis` is ignored for undo.
- output: `{correction_id, status, summary, undo}` where `undo` is the literal
  call that reverses it; `error` on refusal.
- annotations: `destructiveHint: true, idempotentHint: false`.
- description tells the model: only `user_stated` for things the user said;
  `inferred` otherwise; never claim a pending correction was applied.

### Confirmation gate for `inferred`

The handler runs in the daemon (`/api/tool`), but only the MCP server process
holds the client session, so `mcp_server.on_call_tool` intercepts
`brain_graph_correct` when `basis == "inferred"`:

1. The tool's input schema sets `additionalProperties: false`, so the model
   cannot supply a confirmation at all. The MCP server passes the user's answer
   to the daemon OUT OF BAND, as a `confirmation` object beside the arguments
   in the `/api/tool` body (`{"via": "elicitation"}` or `{"declined": true}`).
2. If the client advertised form-mode elicitation (Claude Code 2.1.76+): send
   an elicitation with the plain-English correction and the evidence.
   - accept → forward with `confirmed_via="elicitation"` → applied.
   - decline → forward as a `declined` ledger row (so it is not re-proposed).
   - cancel → nothing written; tool returns "not applied".
3. Otherwise (Claude Desktop — no elicitation): forward unconfirmed → daemon
   stores `pending`; tool returns "staged; ask the user to approve it on the
   dashboard". Pending rows surface in `brain_proactive` and as a dashboard
   row with Apply / Decline, served by new control-API routes
   (`POST /api/corrections/<id>/apply|decline`) which set
   `confirmed_via="dashboard"`.

There is deliberately **no in-chat approve op**: "the user said yes" relayed by
the model is the exact unenforceable claim the gate exists to prevent.
The `confirmation` object is only honoured over the bearer-authenticated control
channel.

Guards: inferred proposals dedup on `dedup_key` (op + sorted ids + field/value)
against pending, declined and applied rows (no-op with a message); at most 25
pending rows (further inferred proposals refused with a message); `set_field`
role rejects `graph_write._JUNK_ROLE_VALUES`; merges go through
`graph_view._orient` (self, role inbox, distinct pair) and are limited to
`resolve._NAME_MERGEABLE_TYPES`.

Every applied correction also writes a `change_log` row
(`change_type="graph_corrected"`, `source="brain_graph_correct"`,
`revert_ref="correction:<id>"`).

---

## Part 2 — Entity resources

### Scope

Entities only (person, org, project). Documents are excluded: no one can
@-mention a doc id, and `brain_read` already serves the model.

### Client support (measured 2026-09-24, Claude Code CLI 2.1.281)

A throwaway probe MCP server (static resource + template + completion +
prompt handlers, each logging on invocation) was run headlessly via
`claude -p --mcp-config ./mcp.json --strict-mcp-config "<prompt>"`, once per
row below, and `probe.log` was read back to see which methods the client
actually called. `claude mcp add`/Desktop's config were left untouched;
Desktop was not exercised (owner's live setup, out of scope for this probe).

| Feature | Claude Code CLI (headless `-p`, 2026-09-24) | Claude Desktop |
|---|---|---|
| Static resource listed (`resources/list`) | yes — asking "list the MCP resources available" triggered `resources/list` and returned the one static entry | not tested (left for owner) |
| Static/templated resource read via `@server:uri` (`resources/read`) | yes — `@probe:probe://entity/marcus-reyes` and `@probe:probe://entity/dana-okafor` both resolved and the model quoted the returned markdown | not tested (left for owner) |
| Templates listed (`resources/templates/list`) | no — never appeared in the log across 4 separate `-p` invocations, including ones that read a templated (non-listed) URI | not tested (left for owner) |
| Templated URI readable | yes — `probe://entity/marcus-reyes` (never returned by `resources/list`, only matching the `probe://entity/{id}` template) was read successfully via the `@`-mention with no `resources/templates/list` call in between | not tested (left for owner) |
| Prompt fetched (`prompts/get`) | yes — the slash form `/mcp__probe__probe <args>` called `prompts/get`; positional-arg quoting behaviour was also observed (unquoted multi-word args split on spaces) | not tested (left for owner) |
| Completion called, template variable (`completion/complete`) | no — not observed in any headless call | not tested (interactive-only; left for owner) |
| Completion called, prompt argument (`completion/complete`) | no — not observed in any headless call | not tested (interactive-only; left for owner) |
| @-picker showing the resource while typing, `/probe` argument completion | not tested (interactive, left for owner) | not tested (left for owner) |

Tasks 10-11 still ship the template and completion handlers (spec-correct,
cheap), but they are inert in Claude Code today (Claude Desktop unconfirmed,
left for the owner); the user-visible win is the static top-N. A templated URI
is directly readable by `@`-mention without the
client ever calling `resources/templates/list`, so a user (or the model)
constructing the URI by hand still works even though template discovery is
unverified end-to-end.

### Static top-N

`resources/list` appends ~100 entities (person/org/project, not suppressed),
ranked by `degree`. URI `mcpbrain://entity/<id>`, `name` = display name,
`title` = "Name, type, org", `mimeType` `text/markdown`. The daemon computes the
set on first request each UTC day and caches it in memory (no new cadence pass:
the query is one indexed `ORDER BY degree LIMIT 100`), serving it via the
control API; the MCP server caches it for 10 minutes. Because the set only moves daily, the
existing `watch_resources` fingerprint fires `list_changed` at most about daily.
Daemon unreachable → the entity entries are omitted (the `file://` resources
still list), never an error on `resources/list`.

### Template and completion

- `on_list_resource_templates` → `mcpbrain://entity/{id}`.
- `on_completion`: for the entity template's `id`, `graph_view.search_entities(q, 10)`
  ids; for the `draft-reply` prompt's `email_id`, recent reply-needed threads.
  Empty list on any failure.

### Read path

`on_read_resource` dispatches on scheme. `file://` keeps the existing allowlist
guard untouched; `mcpbrain://entity/<id>` asks the daemon for the
`brain_context(mode="profile")` data and renders it as markdown (header, role,
org, relations grouped by type with the same hub/size bounds as `brain_graph`
and a truncation note, recent observations). A merged-away id resolves through
`entity_merge_log` to its survivor (with a "merged into" line). Unknown or
suppressed → error. Neither scheme can reach the other.

---

## Error handling

All failures return `isError` results through the existing path, never
uncaught exceptions. Daemon unreachable → the existing `_RoutedCallFailed`
message; an accepted elicitation that could not be forwarded is reported as
not applied. One `BEGIN IMMEDIATE` per correction: ledger row and graph change
commit together or not at all.

## Testing

TDD per task, scoped test runs. Stickiness tests use a **real Store**, not
fakes: this repo has shipped inert logic twice where fakes passed.

- rejected relation re-extracted via `graph_write` stays rejected;
- distinct pair never proposed/merged by all five merge paths;
- locked org survives `org_backfill` and both review appliers;
- merge → unmerge round-trips entity row + relation/observation/email-link sets
  exactly; a subsequent merge of the winner makes undo refuse;
- elicitation gate: fake session with/without capability × accept/decline/
  cancel; model-supplied `confirmed_via` stripped;
- dashboard apply/decline routes;
- resources: top-N excludes suppressed, fingerprint stable across a
  degree-only change, template + completion handlers, merged-away id resolves,
  scheme isolation;
- `tests/test_mcp_sdk_contract.py` pins the new handlers.

## Live verification (before claiming done)

Each op once against the real store through a real MCP stdio session, then
undone; `PRAGMA integrity_check` + `foreign_key_check` clean; gold gate
unchanged; the two-client check. Corrections are applied BY the daemon, so it
stays running for this (it is the single writer; nothing else writes
alongside it) — take a verified snapshot first. The schema migration lands on
the daemon's own `init()` after a reinstall, using the three install-trap
fixes in CLAUDE.md (`bootout` → `--reinstall --force ".[daemon]"` → clear
`__pycache__` → `bootstrap`, then confirm the running version). Source only;
no version bump or release.
