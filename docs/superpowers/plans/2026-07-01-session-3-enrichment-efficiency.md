# Session 3 — Enrichment Efficiency: Haiku Does Only the Semantic Delta

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Push the last mechanical work off the Haiku extractor — sender/header people and trivial-thread summaries — so Haiku spends tokens only on the irreducibly-semantic delta, cutting cost and latency with no graph-quality or recall regression; then settle the producer/consumer balance and the dead-harness tech debt.

**Architecture:** The daemon already assembles context, stamps provenance, sets `email_addr`, derives `org`, and writes structural `works_at`/`mentioned_with` — but only for people the *model* surfaced (`graph_write.py:1216` skips senders the model didn't list). So Haiku still spends tokens re-listing message senders that are structured header data. Session 3 has the daemon **create** sender person-entities deterministically (junk-guarded) so Haiku extracts only body-mentioned people, and short-circuits trivial threads with a deterministic extractive summary instead of a model call. Every graph-affecting change is A/B-gated on the mcpbrain gold set.

**Tech Stack:** Python 3.12, SQLite (`brain.sqlite3`), pytest, ruff, Claude Code subagents (Sonnet coordinator / Haiku executors), MCP tools (`brain_enrich_units`/`_pull`/`_push`/`_advance`).

---

## STATUS — 2026-07-02

- **All phases SHIPPED as 0.7.76**, then a critical fix in **0.7.77** (source/dist/plugin + local daemon all at 0.7.77). Suite 1816 passed.
- **Phase 1** (deterministic sender entities + prompt scope), **Phase 2** (trivial-thread short-circuit), **Phase 3.1** (`spool_thread_cap` 500→2000), **Phase 3.2** (`parallel_backfill` removed, #24), **Phase 3.3** (`resolve_entities` daily cadence, default ON, kill-switch = interval 0) — all landed.
- **Post-ship review (multi-agent) found a CRITICAL bug 0.7.76 introduced (fixed in 0.7.77):** the daily `resolve_entities` cadence ran `_email_equality_merges`, which grouped ALL persons by `email_addr` with no shared-inbox guard — combined with Session-3's deterministic sender-email stamping, distinct people on a role/shared inbox (`office@`/`info@`) would have been irreversibly merged, daily and unattended. Fix: `resolve.is_role_address()` guards `_email_equality_merges` and all three person email-stamping sites in `graph_write.apply()`. **No live corruption occurred** — a live check showed 129 persons carry a role email but 0 role address had >1 person (no collision yet), so the fix was preventive. Also broadened trivial-thread cues (0.7.76 could drop short commitments like "I'll send it Monday").
- **Phase 4 (at-scale aggregate validation): OPEN — accruing.** Now unblocked (daemon on 0.7.77 + higher throughput); measure `enrich-eval --compare enrich-baseline-session3.json` after a few hundred threads process. Baseline snapshot committed in Phase 0.
- **Open (not code):** marketplace deployment (manual org-admin step); optional cosmetic cleanup of the 129 role-email-stamped persons (no merge exposure, low priority).

## Global Constraints

- **Version lives in FOUR files, kept equal:** `pyproject.toml`, `mcpbrain/__init__.py`, `plugin/.claude-plugin/plugin.json`, `plugin/.claude-plugin/marketplace.json` (+ `uv.lock` mcpbrain entry). Current released version is **0.7.75**. Release is a separate, explicitly-instructed step (`docs/RELEASE-RUNBOOK.md`) — do not release within a task.
- **Improvements ship ON.** Every behavior added here defaults **ON** via `config.read_config(home).get("<flag>", True)`; a flag is an emergency kill-switch only (default `True`). Matches the `salience_gate`/`recall_excludes_cold` precedent.
- **Gates are pass-before-merge, on real data.** Each graph-shape task A/Bs on the mcpbrain gold set (`tests/eval/test_eval_baseline.py`, `mcpbrain enrich-eval`) and on a ~300-thread scratch or live sample. A gate that regresses recall@10 (floor 0.55) / MRR (floor 0.35) / relation precision is **fixed before merge**, never shipped off. Production recall path is include-cold + three-axis (recall@10=0.750, MRR=0.556).
- **Single-writer invariant:** only the daemon writes the store during a cycle. All graph mutations stay in the daemon/`drain`/`apply` path.
- **TDD throughout:** failing test → minimal impl → green → commit. Run `uv run pytest <file> -q` and `uv run ruff check mcpbrain/` before each commit.
- **Test isolation:** never touch the real app-dir store/lock — use `tmp_path` + injected `Store(str(tmp_path/'b.sqlite3'), dim=4)`; a thread extraction posted to drain must seed a chunk for its thread (drain skips + caps extractions with no resolvable chunk — the I-1 guard).
- **If `mcpbrain/enrich_prompt.md` is edited, run `python bin/sync_agents.py`** and keep `test_enrich_agent_rules_in_sync` green (mirrors the rules block into `plugin/agents/enrich-batch.md`).
- **Coordinator = Sonnet (Auto-mode), executors = Haiku** (0.7.75) — do not change.
- **Do NOT wire `resolve_entities` into a live caller without confirming the `_deterministic_merges` fix is present** (allowlist `person`/`org`/`project`, shipped 0.7.75 / issue #23). It is safe now, but Task 3.3 must re-verify before enabling.

---

## Session plan (single session, phase-gated)

- **Phase 0** — Efficiency baseline: extend `enrich_eval` with the metric that proves the win (Task 0.1). Snapshot before touching anything.
- **Phase 1** — Deterministic sender entities (the primary win): Tasks 1.1 (create), 1.2 (prompt scope), 1.3 A/B gate.
- **Phase 2** — Trivial-thread short-circuit (secondary token/latency lever): Tasks 2.1, 2.2 A/B gate.
- **Phase 3** — Balance + tech debt: Task 3.1 (`spool_thread_cap` default), Task 3.2 (`parallel_backfill` port-or-remove, issue #24), Task 3.3 (decide/validate wiring `resolve_entities` live — eval-gated).
- **Phase 4** — At-scale aggregate validation (the item-3 measurement Session 2 couldn't finish), now feasible on the higher-throughput 0.7.75 live path.

**Checkpoint:** full suite green; `enrich-eval --compare` shows fewer model-returned entities per thread with no drop in sender/entity coverage, recall@10/MRR at or above floors; Phase-3 decisions recorded; all new behavior default ON.

---

## File Structure

| File | Responsibility | Tasks |
|---|---|---|
| `mcpbrain/enrich_eval.py` | Add `model_returned_entities` / sender-coverage metric. | 0.1 |
| `mcpbrain/graph_write.py` | `apply()`: create deterministic sender entities before the structural pass; trivial-thread deterministic summary path. | 1.1, 2.1 |
| `mcpbrain/enrich_prompt.md` (+ `bin/sync_agents.py`) | Scope the model's `entities` to body-mentioned people; note senders are system-created. | 1.2 |
| `mcpbrain/prepare.py` | Trivial-thread detection + `content_type` hint carry (thin-thread short-circuit marker). | 2.1 |
| `mcpbrain/config.py` | New kill-switch flags (`enrich_sender_entities`, `enrich_trivial_thread_summary`); `spool_thread_cap` default. | 1.1, 2.1, 3.1 |
| `mcpbrain/maintenance/parallel_backfill.py` (+ `bin/fast_backfill`) | Port to the units flow, or remove with its entry point. | 3.2 |
| `mcpbrain/resolve.py` + a live caller | Optional: wire `resolve_entities` into a periodic pass (eval-gated). | 3.3 |

---

## Phase 0 — Efficiency baseline

### Task 0.1: Add a "model-returned entities" metric to enrich_eval

**Files:**
- Modify: `mcpbrain/enrich_eval.py` — extend `graph_metrics`
- Test: `tests/test_enrich_eval.py`

**Interfaces:**
- Consumes: existing `graph_metrics(store) -> dict` (Session-1).
- Produces: `graph_metrics` gains `person_email_pct` (exists) plus `senders_as_entities_pct` = of `person` entities that have an `email_addr`, the fraction also linked in `email_entities` with role `authored`/sender — a proxy for "senders are entities." The win we measure at merge: sender-coverage holds ≥ baseline while the model returns fewer entities. (Model-token counts aren't in the DB; measure the proxy — sender coverage — plus a live A/B on returned-entity counts in Task 1.3.)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_enrich_eval.py (add)
def test_sender_coverage_metric(tmp_path):
    from mcpbrain.store import Store
    from mcpbrain.enrich_eval import graph_metrics
    s = Store(str(tmp_path / "b.sqlite3"), dim=4); s.init()
    with s._connect() as db:
        db.execute("INSERT INTO entities(id,name,type,email_addr) VALUES('p1','A','person','a@x.org')")
        db.execute("INSERT INTO entities(id,name,type,email_addr) VALUES('p2','B','person','')")
        db.execute("INSERT INTO email_entities(message_id,entity_id,role) VALUES('m1','p1','authored')")
    m = graph_metrics(s)
    assert m["senders_as_entities_pct"] == 50.0   # 1 of 2 persons is an authored sender
```

- [ ] **Step 2: Run to verify it fails** — `uv run pytest tests/test_enrich_eval.py::test_sender_coverage_metric -q` → FAIL (KeyError).
- [ ] **Step 3: Implement** — in `graph_metrics`, add:

```python
        senders = scalar("SELECT COUNT(DISTINCT entity_id) FROM email_entities WHERE role='authored'")
        persons_cov = scalar("SELECT COUNT(*) FROM entities WHERE type='person'")
    # ... in the return dict:
        "senders_as_entities_pct": pct(senders, persons_cov),
```

- [ ] **Step 4: Run** → PASS. `uv run ruff check mcpbrain/` clean.
- [ ] **Step 5: Snapshot + commit**

```bash
uv run mcpbrain enrich-eval --baseline docs/superpowers/plans/enrich-baseline-session3.json
git add mcpbrain/enrich_eval.py tests/test_enrich_eval.py docs/superpowers/plans/enrich-baseline-session3.json
git commit -m "feat(eval): sender-coverage metric + Session-3 baseline snapshot"
```

---

## Phase 1 — Deterministic sender entities (primary)

> The win: `graph_write.py:1216` currently skips senders the model didn't surface. Create them deterministically so Haiku extracts only body-mentioned people. Ships ON behind `enrich_sender_entities` (default True).

### Task 1.1: Create sender person-entities in apply() before the structural pass

**Files:**
- Modify: `mcpbrain/graph_write.py` — `apply()`, immediately before block `2.5` (`:1185`)
- Modify: `mcpbrain/config.py` — add `enrich_sender_entities(home)` kill-switch
- Test: `tests/test_graph_write_provenance.py`

**Interfaces:**
- Consumes: `messages` (list of `{message_id, sender, date}`), `name_to_id` (dict name→entity_id built in the entity loop), and existing helpers `_extract_email_addr`, `_extract_name`, `strip_affiliation`, `is_junk_entity`, `upsert_entity`, `_is_owner`, `org_from_email`, `write_role_observation`, `lead_date_iso`, `taxonomy`.
- Produces: for each message sender, if its name is not already in `name_to_id` and passes `is_junk_entity(name, "person")` and isn't the owner, `upsert_entity(...)` a `person` with `email_addr` from the header, and add it to `name_to_id` (so the existing structural-relations pass at `:1196` then links it). No behavior when the flag is off.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_graph_write_provenance.py (add)
def test_sender_entity_created_without_model(tmp_path, monkeypatch):
    monkeypatch.setattr("mcpbrain.config.enrich_sender_entities", lambda home: True)
    s = _store(tmp_path)
    extraction = {
        "thread_id": "t9", "org": "unknown", "content_type": "update", "summary": "s",
        "messages": [{"message_id": "m1", "sender": "Dana Lee <dana@centrepoint.church>", "date": "2026-02-01"}],
        "entities": [],   # model surfaced NO entities
        "relations": [], "actions": [], "topics": ["x"],
    }
    graph_write.apply(s, extraction, doc_ids=["doc-1"])
    with s._connect() as db:
        row = db.execute("SELECT type, email_addr FROM entities WHERE name='Dana Lee'").fetchone()
    assert row is not None, "sender must be created as an entity even when the model omits it"
    assert row[0] == "person" and row[1] == "dana@centrepoint.church"
```

- [ ] **Step 2: Run to verify it fails** — `uv run pytest tests/test_graph_write_provenance.py::test_sender_entity_created_without_model -q` → FAIL (no entity; today apply relies on the model's `entities`).

- [ ] **Step 3: Implement** — add `config.enrich_sender_entities`:

```python
# mcpbrain/config.py
def enrich_sender_entities(home) -> bool:
    """Whether the daemon creates person entities for message senders from headers
    (so the LLM extracts only body-mentioned people). Default True; kill-switch only.
    Junk-guarded (is_junk_entity) and owner-excluded; senders on noise threads never
    reach apply() because the salience/noise filter drops them upstream."""
    return bool(read_config(home).get("enrich_sender_entities", True))
```

In `graph_write.apply()`, insert BEFORE the `2.5` structural-relations block (`:1185`). It creates a `person` for every message sender the model didn't already surface, so the structural pass below links them (`works_at`/`mentioned_with`):

```python
    _sender_home = str(home) if home is not None else str(config.app_dir())
    if config.enrich_sender_entities(_sender_home):
        for msg in messages:
            hdr = msg.get("sender", "") or ""
            if not hdr:
                continue
            s_name = strip_affiliation(_extract_name(hdr)).strip()
            s_email = _extract_email_addr(hdr)
            if not s_name or _is_owner(s_name, owner) or is_junk_entity(s_name, "person"):
                continue
            if s_name.strip().lower() in {n.strip().lower() for n in name_to_id}:
                continue  # model already surfaced this person
            sid = upsert_entity(store, name=s_name, entity_type="person", org="",
                                email_addr=s_email, taxonomy=taxonomy, valid_from=lead_date_iso)
            name_to_id[s_name] = sid
            if store.link_email_entity(lead_msg_id, sid, role="authored"):
                _bump_email_count(store, sid)
```

- [ ] **Step 4: Run to verify it passes** — `uv run pytest tests/test_graph_write_provenance.py -q` → PASS.
- [ ] **Step 5: Run graph-write + drain suites** — `uv run pytest tests/ -q -k "graph_write or drain or apply or resolve"` → PASS (write_time_dedup must fold a model-surfaced + sender-created duplicate into one).
- [ ] **Step 6: Commit**

```bash
git add mcpbrain/graph_write.py mcpbrain/config.py tests/test_graph_write_provenance.py
git commit -m "feat(graph): create sender person-entities deterministically (model extracts only body people)"
```

### Task 1.2: Scope the extractor prompt to body-mentioned people

**Files:**
- Modify: `mcpbrain/enrich_prompt.md` — the `entities` guidance
- Run: `python bin/sync_agents.py`
- Test: `tests/test_enrich_prompt_doc.py`

**Interfaces:**
- Produces: prompt text telling the model the system already creates an entity for every message sender, so `entities` should list only people/orgs/projects **named in the body** that are not message senders — mirroring the existing relation-scoping at lines 146-158.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_enrich_prompt_doc.py (add)
def test_prompt_scopes_entities_to_body():
    from pathlib import Path
    text = Path("mcpbrain/enrich_prompt.md").read_text().lower()
    assert "already creates an entity for every message sender" in text
    assert "body" in text  # entities are the body-mentioned delta
```

- [ ] **Step 2: Run to verify it fails** → FAIL.
- [ ] **Step 3: Implement** — in the `entities` section of `enrich_prompt.md`, add a sentence:

```markdown
  The system already creates an entity for every message sender from the header
  (name + email), so list in `entities` only people/orgs/projects named in the BODY
  that are not message senders — the sender-people are handled for you. Re-listing a
  sender is harmless (it dedups) but wastes effort.
```

Then `python bin/sync_agents.py`.

- [ ] **Step 4: Run** — `uv run pytest tests/test_enrich_prompt_doc.py -q` (incl. `test_enrich_agent_rules_in_sync`) → PASS.
- [ ] **Step 5: Commit**

```bash
git add mcpbrain/enrich_prompt.md plugin/agents/enrich-batch.md tests/test_enrich_prompt_doc.py
git commit -m "prompt: scope entities to body-mentioned people (senders are system-created)"
```

### Task 1.3: PHASE-1 GATE — A/B on a ~300-thread sample (ships ON)

- [ ] **Step 1:** Copy the live store to a scratch home; enrich ~300 real threads through the live units flow on 0.7.75+ (Desktop `/mcpbrain-backfill`), OR on a scratch copy via a units-flow driver. Measure with `enrich-eval --compare docs/superpowers/plans/enrich-baseline-session3.json`.
- [ ] **Step 2:** REQUIRED deltas: `senders_as_entities_pct` NOT lower (sender coverage holds), `person_email_pct` up or equal, and the **model returns fewer entities per thread** (spot-check 20 pushed extractions: sender-people should be largely absent from the model's `entities`). Gold `recall@10 ≥ 0.55`, `MRR ≥ 0.35`.
- [ ] **Step 3:** If sender coverage drops (junk guard too aggressive) or recall regresses, fix and re-run — ships ON, never off. Record numbers in the plan STATUS.

---

## Phase 2 — Trivial-thread short-circuit

> Abstractive `summary`/`contextual_summary` run on every thread, including one-line "thanks/ok" threads. Detect trivial threads deterministically and skip the model call entirely, writing an extractive summary + sender entities. Ships ON behind `enrich_trivial_thread_summary`.

### Task 2.1: Deterministic trivial-thread detection + extractive summary

**Files:**
- Modify: `mcpbrain/prepare.py` — mark thin threads so they are NOT packed into model units
- Modify: `mcpbrain/graph_write.py` — a deterministic extractive-summary writer for marked threads (or `drain` applies a synthesized extraction)
- Modify: `mcpbrain/config.py` — `enrich_trivial_thread_summary(home)` kill-switch
- Test: `tests/test_prepare.py`, `tests/test_graph_write_provenance.py`

**Interfaces:**
- Consumes: a thread block's `messages` (bodies).
- Produces: `prepare.is_trivial_thread(messages) -> bool` (True when total body chars < `_TRIVIAL_CHARS` (300) AND no message contains an action cue — reuse the extractor's action heuristics if cheap, else a simple `?`/"can you"/"please" scan). Marked-trivial threads get a deterministic extraction (extractive summary = lead subject + first sentence; sender entities via Task 1.1; empty relations/actions/topics) applied WITHOUT a model call.

- [ ] **Step 1:** Failing test: `is_trivial_thread([{"text":"Thanks, sounds good."}])` is True; a thread with a body over 300 chars OR containing "can you" is False.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement `is_trivial_thread` in `prepare.py` + the config flag; wire prepare to route trivial threads to a deterministic-extraction path (not a model unit).
- [ ] **Step 4:** Run `uv run pytest tests/test_prepare.py -q` → PASS.
- [ ] **Step 5:** Commit `feat(enrich): trivial-thread short-circuit — deterministic extractive summary, no model call`.

### Task 2.2: PHASE-2 GATE — recall not harmed by short-circuiting

- [ ] On the ~300-thread sample, confirm trivially-summarized threads' chunks still return in recall (they stay embedded), and gold `recall@10`/`MRR` hold. Record the fraction of threads short-circuited (the token/latency saving). Ships ON; if recall drops, tighten `_TRIVIAL_CHARS` and re-run.

---

## Phase 3 — Producer/consumer balance + tech debt

### Task 3.1: Ship the `spool_thread_cap` default bump

**Files:**
- Modify: `mcpbrain/config.py` — `spool_thread_cap` default 500 → 2000
- Test: `tests/test_config.py`

> Rationale: Session-2/0.7.75 raised the *consumer* (fan-out 12, 30 units/wave, window 600) but left the *producer* at 500 threads/cycle, so aggressive backfill outran production and tripped the "queue empty 3×" stop. 2000 was validated live on 2026-07-01 (queue refilled 111→156+ and sustained). Ship it so every install's producer matches the consumer.

- [ ] **Step 1:** Failing test: `config.spool_thread_cap(tmp) == 2000` by default; override honored.
- [ ] **Step 2:** Run → FAIL (default is 500).
- [ ] **Step 3:** Change the default to 2000 in `config.spool_thread_cap`.
- [ ] **Step 4:** Run `uv run pytest tests/test_config.py -q` → PASS.
- [ ] **Step 5:** Commit `perf(daemon): raise spool_thread_cap default 500->2000 to match the 0.7.75 consumer`.

### Task 3.2: Resolve the `parallel_backfill` dead harness (issue #24)

**Files:**
- Modify or delete: `mcpbrain/maintenance/parallel_backfill.py`, `bin/fast_backfill`
- Test: `tests/` (a smoke import test if kept)

> `parallel_backfill` is stale (wrong `extractor_io` import, stale prompt path) AND architecturally incompatible with the units flow (no unit files → drain skips every extraction). Decide with the maintainer: **(a) remove** it + `bin/fast_backfill` (the Desktop `/mcpbrain-backfill` units-flow skill is the supported headless path), or **(b) port** it to pull units → extract → push → drain. Default recommendation: **remove** (YAGNI — the units-flow backfill exists and works).

- [ ] **Step 1:** Confirm no production code imports `parallel_backfill` except `bin/fast_backfill` (grep).
- [ ] **Step 2:** Remove both files (or port; if porting, add a units-flow smoke test).
- [ ] **Step 3:** Run full suite → PASS (nothing depended on it).
- [ ] **Step 4:** Commit `chore(maintenance): remove dead parallel_backfill harness (issue #24); units-flow backfill is the supported path` — close #24.

### Task 3.3: Decide + validate wiring `resolve_entities` into a live caller (eval-gated)

**Files:**
- Modify: `mcpbrain/daemon.py` (a periodic cadence) or the resolution tier
- Test: `tests/test_resolve.py`, gold set

> `resolve_entities` (deterministic `_deterministic_merges` + `_email_equality_merges`) has **no live caller**. `_deterministic_merges` is now safe (allowlist fix, 0.7.75). Wiring it into a periodic pass would keep the entity graph deduped automatically. **Deliverable is a measured decision:** run `resolve_entities()` on a real-corpus copy, confirm merges are only genuine person/org/project dedups (as Session-2 validation showed 94, zero structural), spot-check 20 merges for correctness, then either wire it into a daily cadence (ships ON) or record why not.

- [ ] **Step 1:** On a real-corpus copy, run `resolve_entities()`; assert 0 structural-type merges and spot-check the person/org/project merges.
- [ ] **Step 2:** If clean, add a daily `resolve_entities` cadence in the daemon (behind a default-ON flag) with a test; else record the decision.
- [ ] **Step 3:** Gold `recall@10`/`MRR` hold after a merge pass. Commit with the decision recorded.

---

## Phase 4 — At-scale aggregate validation

> The item-3 measurement Session 2 couldn't complete (the CLI harness was incompatible). Now feasible: 0.7.75 daemon + higher throughput + higher `spool_thread_cap` drains hundreds of threads through the real units flow.

- [ ] **Step 1:** With the Session-3 changes merged and the daemon updated, let the live backfill/hourly task process several hundred threads on the real store.
- [ ] **Step 2:** `enrich-eval --compare docs/superpowers/plans/enrich-baseline-session3.json` — confirm the aggregate deltas move as designed: `person_email_pct` up, `relations_semantic_pct` up, `senders_as_entities_pct` holds, non-`role` observation attributes grow, provenance ~100% on new relations, and gold `recall@10`/`MRR` at/above floors.
- [ ] **Step 3:** Record the at-scale numbers in the plan STATUS — this closes the Session-2 open item.

---

## Sequencing & dependencies

```
Phase 0 (baseline+metric) ─> Phase 1 (sender entities) ─> Phase 1.3 gate
                                        │
                                        └─> Phase 2 (trivial threads) ─> 2.2 gate
Phase 3.1 (spool default)  [independent]
Phase 3.2 (parallel_backfill remove)  [independent]
Phase 3.3 (resolve_entities wiring)  [needs the 0.7.75 _deterministic_merges fix — present]
Phase 4 (at-scale validation)  [after Phases 1–2 merged + daemon updated]
```

- Phase 1 is the headline efficiency win; Phase 2 stacks on it (both cut Haiku work).
- Phase 3 tasks are independent and can land in parallel.
- Phase 4 is the closing measurement — needs Phases 1–2 shipped and the daemon on the new version.
- **Everything ships ON.** Kill-switches (`enrich_sender_entities`, `enrich_trivial_thread_summary`, `resolve_entities` cadence) default `True`; a gate that fails is fixed, never merged off.

## Do NOT build (trap list)

- Deterministic creation of entities for **body-mentioned** people — that's the model's semantic job; only *senders* (structured header data) become deterministic.
- Re-coupling cold-exclusion to recall (`recall_excludes_cold` stays OFF).
- Changing the coordinator off Sonnet (Auto-mode requirement).
- Wiring `resolve_entities` live without re-confirming the `_deterministic_merges` allowlist fix.

## Self-review checklist (run before handoff)

- [ ] **Spec coverage:** sender-entities→P1, prompt-scope→1.2, trivial-threads→P2, spool default→3.1, parallel_backfill→3.2 (#24), resolve wiring→3.3, at-scale validation→P4, efficiency metric→0.1. All mapped.
- [ ] **No placeholders:** P0/P1/P3.1 carry real test+impl code; the eval-gated tasks (1.3, 2.2, 3.3, P4) carry concrete measure→decide criteria.
- [ ] **Type consistency:** `enrich_sender_entities`, `enrich_trivial_thread_summary`, `is_trivial_thread`, `senders_as_entities_pct`, `upsert_entity(..., email_addr=, taxonomy=, valid_from=)` names match across tasks and the real `graph_write`/`config` signatures.
