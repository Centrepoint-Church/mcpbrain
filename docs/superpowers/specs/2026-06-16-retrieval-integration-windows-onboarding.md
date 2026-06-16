# Retrieval Scores + Calendar→Person Context + Windows Parity + Onboarding

> Spec date: 2026-06-16. Baseline: mcpbrain 0.0.6. Roadmap items #4 (retrieval), #7 (integration depth), #8 (Windows), #9 (onboarding). Grouped as the "quality/expansion" tail — each is small after brainstorming narrowed it.

---

## Part 1 — Retrieval: surfaced scores + tuned fusion (#4)

### What the code does today

`retrieval.py:hybrid_search` does RRF fusion of vector KNN + FTS BM25, drops expired notes, and returns chunk dicts — **with no score field**. The caller (`brain_search`) cannot tell a strong hit from a weak one, and there is no way to measure retrieval quality.

### Constraint

The daemon cannot call an LLM, and `brain_search` is a latency-bound live call. A cross-encoder rerank would mean a local model dependency. Decision (brainstorm): **do scores + tuned fusion only**; defer any cross-encoder until an eval set proves it's worth the weight.

### Design

1. **Expose a score.** `hybrid_search` returns each result with a `score` field — the RRF fusion score, normalised to 0–1 (divide by the top score in the result set). `brain_search` passes it through to the caller so relevance is visible.
2. **Build an eval set.** `tests/eval/retrieval_eval.jsonl` — ~30 `{query, expected_doc_ids}` pairs drawn from a representative fixture store (or anonymised real queries). A small harness (`tests/eval/run_eval.py`) reports recall@k and MRR.
3. **Tune fusion against it.** With the eval harness in place, sweep the RRF `k` and any vec/BM25 weighting and pick the setting that maximises recall@k / MRR on the eval set. Record the chosen values + the before/after numbers in the spec's implementation plan.

### Components
- **Modified `mcpbrain/retrieval.py`:** add normalised `score` to each returned dict; parameterise the fusion weighting so it's tunable.
- **Modified `mcpbrain/mcp_server.py`:** `brain_search` surfaces `score`.
- **New `tests/eval/`:** `retrieval_eval.jsonl` + `run_eval.py` + a `test_eval_baseline.py` that asserts recall@k stays above a floor (regression guard).

### Out of scope
- Cross-encoder / LLM-judge rerank (deferred; the eval set decides if it's ever needed).

---

## Part 2 — Calendar attendees → person-entity context (#7)

### What the code does today

`sync/drive.py` already exports Google Docs/Slides/Sheets to text and indexes content. `sync/calendar.py:normalise_calendar` already writes attendees into the indexed **chunk text**. But the **graph** (person entities + relations) is built only from email-thread enrichment (`prepare.py` groups un-enriched *email threads*; calendar events never enter the enrich queue). So attending a meeting does **not** update a person's entity in the graph today — even though `graph_write.py` already defines an `"attended"` relation type that nothing feeds.

### Goal (from brainstorm)

"Person entity context should be updated when they attend meetings." Attendee name + email is **structured data** on the calendar event, so the daemon can write it directly into the graph — **no LLM required**, fully inside the no-LLM-daemon constraint.

### Design

At calendar sync time, for each event with attendees:

1. For each attendee (excluding the owner/self via `graph_write` owner identity):
   - Upsert a person entity (`graph_write.upsert_entity`) keyed by email, with name + email — same dedup/alias logic email enrichment uses.
   - Filter junk/role names via the existing `graph_write.is_junk_entity` (skips "attendee", "participant", room resources, etc.).
2. Write an `"attended"` relation between the owner and each attendee (or event↔person — chosen at implementation to match the existing relation schema), with `valid_from` = event date, so "who I met with and when" accrues over time and feeds communities + degree.
3. Idempotent: re-syncing the same event re-applies the same upserts/relations without duplication (relies on existing upsert idempotency).

This runs in `sync_calendar` (and `backfill_calendar_window`) right after chunk upsert — pure structured-data writes, no enrichment, no LLM.

### Components
- **Modified `mcpbrain/sync/calendar.py`:** after upserting chunks, call new `_apply_attendees_to_graph(store, event, owner)` that does the entity + relation upserts.
- Reuses `graph_write.upsert_entity`, `upsert_relation`, `is_junk_entity`, owner identity — no new graph primitives.

### Testing
`tests/test_calendar_graph.py`:
- event with 2 external attendees → 2 person entities + 2 `attended` relations
- owner/self attendee → excluded
- junk/role attendee ("Conference Room A") → excluded
- re-sync same event → no duplicate entities/relations (idempotent)

### Out of scope
- Zoom / meeting-notes ingestion (brainstorm: not wanted now).
- Enriching calendar event *bodies* via the LLM pipeline.

---

## Part 3 — Windows parity validation (#8)

### Nature

Not a design — a **validation runbook**. `agents.py` already has a `win32` schtasks path for the daemon/tray; the desktop scheduled-task mechanism + wizard are unvalidated on Windows.

### Deliverable

A Windows clean-machine validation checklist mirroring the macOS hard gate (C3 in the 0.0.6 plan), recorded in `docs/RELEASE-RUNBOOK.md`:

1. Install plugin → `/mcpbrain-install` on a clean Windows machine.
2. uv + wheel install; PATH correct.
3. `mcpbrain setup` registers the daemon + tray via **schtasks** (verify both tasks exist in Task Scheduler).
4. Wizard loads; non-author Google sign-in works.
5. The four Cowork Desktop Scheduled Tasks can be created (working folder = `mcpbrain home`).
6. `/reload-plugins` connects MCP; `brain_search` returns.
7. Hourly enrich task drains `enrich_inbox`.
8. `mcpbrain restore` round-trips a snapshot.
9. `mcpbrain doctor` runs and its auto-fixes work on Windows (restart/re-register via schtasks).

Fix any gaps found (likely candidates: PATH/uv-shim differences, `mcpbrain home` resolution, schtasks arg quoting).

### Code deliverable
- **`tests/test_agents_cadence_xplat.py`** (extend): assert the win32 schtasks arg lists for the daemon, tray, prune, and health cadences are well-formed (the cross-platform paths are exercised in CI even though the live validation is manual).

### Dependency
- Requires a Windows machine + a non-author Centrepoint Google account. Manual gate; cannot be fully automated.

---

## Part 4 — Onboarding: the Cowork "My Brain" project (#9)

### Verdict (decided 2026-06-16 — no investigation needed)

**Cowork project creation stays manual.** The project + its instructions cannot be created programmatically (same class as scheduled tasks — config lives in the desktop app DB, plugins can't register it). This is settled; the implementation plan does **not** spike an auto-create path.

**But the project's working folder is auto-created.** The one friction point we *can* remove: the user shouldn't have to figure out *which* folder to point the Cowork project at. The install flow ensures that folder exists and hands the user its exact path.

### Deliverable

1. **Auto-create the project folder.** During `mcpbrain setup` / the install skill, ensure the Cowork project's working folder exists (this is `mcpbrain home` — already created at setup; this step just guarantees it and resolves its absolute path). No new location is invented.
2. **Improve the how-to.** The install skill's "create the My Brain project" step is rewritten to be one clean, unambiguous copy-paste: the exact project name, the exact instructions block, and the **exact pre-resolved working-folder path** (from `mcpbrain home`) — so the user pastes a known-good path rather than browsing for it.
3. **Document the manual reality.** State plainly in the skill that project creation is a manual Cowork step by design, so it isn't re-investigated later.

### Components
- **Modified `plugin/skills/install/SKILL.md`:** the project-creation step shows the resolved `mcpbrain home` path inline and presents name + instructions as a single copy-paste.
- **Setup** already creates `mcpbrain home`; add an explicit existence guarantee + path echo if not already surfaced.

### Flag
No Cowork API dependency remains — this part is **documentation + a path echo**, not a capability spike.

---

## Sequencing within this spec

1. **Retrieval scores + eval** (self-contained, immediately measurable).
2. **Calendar → person context** (self-contained, pure-data graph writes).
3. **Onboarding** (cheap; install-skill copy + working-folder path echo — no spike).
4. **Windows validation** (needs hardware; do last, after the above land so the runbook tests the full current build).
