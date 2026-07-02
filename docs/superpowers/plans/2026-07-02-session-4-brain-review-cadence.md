# Session 4 — brain-review: AI-Adjudicated Graph Hygiene

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Clear the graph-hygiene finding queue (fuzzy duplicates, orphans, missing-org, ownerless actions, org drift) automatically and safely, by handing a Haiku adjudicator a self-contained *evidence packet* per finding and applying its verdicts through guarded, reversible ops on a scheduled cadence.

**Architecture:** This is a second *mode* of the existing enrich pipeline, not a new system. `lint_graph` already writes findings to `proactive_findings`; we add a **review producer** that turns each open finding into a **review block-unit** carrying its evidence packet, the existing `enrich-batch` Haiku subagent adjudicates it (block units already flow through `brain_enrich_pull`/`_push`), and new entries in drain's `BLOCK_DRAINERS` registry apply each verdict through **reversible, logged** store ops (`merge_entities`, `suppress_entity`, `update_entity_org`, `assign_action_owner`). A `brain-review` daemon cadence drives it. Conservative default (no-op on uncertainty), per-run cap, and the existing role-address / structural-type guards make unattended graph mutation safe — the lesson from the 0.7.76→0.7.77 C1 fix.

**Tech Stack:** Python 3.12, SQLite (`brain.sqlite3`), pytest, ruff, Claude Code subagents (Sonnet coordinator / Haiku executor), MCP tools (`brain_enrich_units`/`_pull`/`_push`/`_advance`), existing `drain.BLOCK_DRAINERS` + `prepare` work-unit machinery.

## Global Constraints

- **Version lives in FOUR files** (`pyproject.toml`, `mcpbrain/__init__.py`, `plugin/.claude-plugin/{plugin,marketplace}.json`) + `uv.lock`. Current released: **0.7.77**. Release is a separate, explicitly-instructed step (`docs/RELEASE-RUNBOOK.md`).
- **Every graph mutation is REVERSIBLE and LOGGED.** Merges go through `store.merge_entities` (writes `entity_merge_log`); suppressions through a `suppressed_entities` row (reversible); org/owner writes are field updates. No destructive op without a recorded trail. This is the non-negotiable rail — the review loop mutates the graph *unattended*.
- **Conservative default: on any uncertainty the verdict is NO-OP** (`skip`). A wrong merge/suppress is worse than an un-cleared finding. The adjudicator is instructed to prefer `skip`.
- **Reuse existing guards:** `resolve.is_role_address` (never merge on a shared inbox), `resolve._NAME_MERGEABLE_TYPES` (only person/org/project name-merge), owner-exclusion. No new merge may bypass these.
- **Per-run cap** on applied mutations (`config.review_max_apply_per_run`, default 50) so a bad batch can't cascade; log what was capped.
- **Improvements ship ON** behind a cadence kill-switch: `review_interval_s` default 86400 (daily); `0` disables — the same numeric-interval idiom as every other daemon cadence.
- **Coordinator = Sonnet (Auto-mode), executor = Haiku** (0.7.75) — unchanged. Review blocks are adjudicated by the same `enrich-batch` Haiku subagent.
- **TDD throughout;** `uv run pytest <file> -q` + `uv run ruff check mcpbrain/` before each commit. Test isolation: `tmp_path` + injected `Store`.
- **If `mcpbrain/enrich_prompt.md` gains review-block rules, run `python bin/sync_agents.py`** and keep `test_enrich_agent_rules_in_sync` green.
- **Gold gate:** after each finding-type lands, confirm gold `recall@10 ≥ 0.55` / `MRR ≥ 0.35` is unaffected (adjudication shouldn't move recall, but merges/suppressions touch the graph — verify).

---

## File Structure

| File | Responsibility | Phase |
|---|---|---|
| `mcpbrain/review.py` *(new)* | Evidence-packet builder (`build_review_packet`) + the review producer (`build_review_units`) — turns open findings into block-units with evidence. | 0, 1–3 |
| `mcpbrain/review_apply.py` *(new)* | The guarded, reversible verdict appliers registered into `drain.BLOCK_DRAINERS` (one per review kind). | 1–3 |
| `mcpbrain/store.py` | Add `suppress_entity` (+ reversal) and `assign_action_owner`; read helpers for evidence (entity mentions/spans). | 1, 2, 3 |
| `mcpbrain/enrich_prompt.md` (+ `bin/sync_agents.py`) | Review-block adjudication rules + verdict schema (per finding kind). | 1–3 |
| `mcpbrain/contract.py` | Validate review verdict payloads (per kind), conservative-default enforcement. | 1–3 |
| `mcpbrain/daemon.py` | `brain-review` `CadencePass` (produce review units on a schedule) + per-run cap. | 4 |
| `mcpbrain/config.py` | `review_interval_s`, `review_max_apply_per_run` accessors. | 4 |
| `mcpbrain/review_eval.py` *(new)* | Metric: findings-adjudicated, verdict mix, apply/skip rates; the safety gate. | 0 |

---

## Session plan (phase-gated)

- **Phase 0** — Evidence-packet builder + review metric (foundation; no graph writes).
- **Phase 1** — End-to-end loop on `possible_duplicate` (reuse `merge_review`): review-batch rules + verdict contract + guarded merge apply + a gold gate. Proves the safest finding-type first.
- **Phase 2** — `orphan_entity` (suppress) + `missing_org` (assign org) review kinds.
- **Phase 3** — `ownerless_action` (assign owner) + org-hygiene (`ambiguous_org`/`duplicate_org`/`org_unrecognised`).
- **Phase 4** — `brain-review` daemon cadence + per-run cap + whole-loop safety gate.

**Checkpoint:** full suite green; a dry-run over the live finding queue shows sensible verdicts with the conservative default honored; every applied mutation is in a reversal log; the open-findings count drops with **zero** wrong merges/suppressions spot-checked; recall/MRR unaffected.

---

## Phase 0 — Evidence-packet builder + review metric

### Task 0.1: `build_review_packet` — assemble a finding's evidence

**Files:**
- Create: `mcpbrain/review.py`
- Test: `tests/test_review.py`

**Interfaces:**
- Produces: `build_review_packet(store, finding: dict) -> dict`. `finding` is a `proactive_findings` row (`finding_type`, `ref_id`, `org`, `summary`, `detail`). Returns a self-contained packet:
  `{"finding_type", "ref_id", "summary", "entity": {id,name,type,org,email_addr,aliases,mentions}, "source_spans": [text,…], "relations": [{relation,other_name}], "observations": [{attribute,value}], "taxonomy": [org names]}`.
  For `possible_duplicate`, additionally `"candidate"` = the paired entity's same sub-dict (both sides).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_review.py
from mcpbrain.store import Store
from mcpbrain.review import build_review_packet


def _seed(tmp_path):
    s = Store(str(tmp_path / "b.sqlite3"), dim=4); s.init()
    with s._connect() as db:
        db.execute("INSERT INTO entities(id,name,type,org,email_addr,mentions) VALUES('e1','Sam Lee','person','Acme','sam@acme.com',3)")
        db.execute("INSERT INTO chunks(doc_id,text,content_hash,metadata,embedded) "
                   "VALUES('d1','Sam Lee leads the Acme rollout.','h1','{}',1)")
        db.execute("INSERT INTO email_entities(message_id,entity_id,role) VALUES('d1','e1','authored')")
    return s


def test_packet_has_entity_and_source_text(tmp_path):
    s = _seed(tmp_path)
    finding = {"finding_type": "lint:orphan_entity", "ref_id": "e1", "summary": "orphan", "detail": ""}
    pk = build_review_packet(s, finding)
    assert pk["entity"]["name"] == "Sam Lee"
    assert pk["entity"]["email_addr"] == "sam@acme.com"
    assert any("Acme rollout" in span for span in pk["source_spans"]), "must carry the source text the entity came from"
```

- [ ] **Step 2: Run to verify it fails** — `uv run pytest tests/test_review.py::test_packet_has_entity_and_source_text -q` → FAIL (module missing).
- [ ] **Step 3: Implement** `build_review_packet` in `mcpbrain/review.py`:

```python
"""Assemble a self-contained evidence packet per proactive finding so a Haiku
adjudicator can decide without fetching. See docs/superpowers/plans/
2026-07-02-session-4-brain-review-cadence.md."""
from mcpbrain import orgs


def _entity_sub(store, eid):
    with store._connect() as db:
        r = db.execute("SELECT id,name,type,org,email_addr,aliases,mentions FROM entities WHERE id=?", (eid,)).fetchone()
        if not r:
            return None
        rels = db.execute("SELECT relation, entity_b FROM entity_relations WHERE entity_a=? LIMIT 20", (eid,)).fetchall()
        obs = db.execute("SELECT attribute, value FROM entity_observations WHERE entity_id=? LIMIT 20", (eid,)).fetchall()
        mids = [m[0] for m in db.execute("SELECT message_id FROM email_entities WHERE entity_id=? LIMIT 5", (eid,)).fetchall()]
        spans = []
        for mid in mids:
            row = db.execute("SELECT text FROM chunks WHERE doc_id=? LIMIT 1", (mid,)).fetchone()
            if row and row[0]:
                spans.append(row[0][:400])
    return {"id": r[0], "name": r[1], "type": r[2], "org": r[3], "email_addr": r[4] or "",
            "aliases": r[5] or "", "mentions": r[6] or 0,
            "relations": [{"relation": x[0], "other": x[1]} for x in rels],
            "observations": [{"attribute": x[0], "value": x[1]} for x in obs],
            "source_spans": spans}


def build_review_packet(store, finding: dict) -> dict:
    ftype = finding.get("finding_type", "")
    ref = finding.get("ref_id", "")
    ent = _entity_sub(store, ref) or {}
    pk = {"finding_type": ftype, "ref_id": ref,
          "summary": finding.get("summary", ""), "detail": finding.get("detail", ""),
          "entity": {k: ent.get(k) for k in ("id", "name", "type", "org", "email_addr", "aliases", "mentions")},
          "source_spans": ent.get("source_spans", []),
          "relations": ent.get("relations", []), "observations": ent.get("observations", []),
          "taxonomy": list(orgs.taxonomy_from_config().names)}
    return pk
```

- [ ] **Step 4: Run to verify it passes** → PASS. Ruff clean.
- [ ] **Step 5: Commit** `feat(review): evidence-packet builder for finding adjudication`.

### Task 0.2: review metric + producer skeleton

**Files:**
- Modify: `mcpbrain/review.py` — add `build_review_units(store, kinds, cap) -> list[dict]`
- Create: `mcpbrain/review_eval.py`
- Test: `tests/test_review.py`

**Interfaces:**
- Produces: `build_review_units(store, *, kinds: list[str], cap: int) -> list[dict]` — pulls up to `cap` open findings of the given `kinds` via `store.open_findings`, wraps each as `{"finding_id", "packet": build_review_packet(...)}`. `review_eval.review_metrics(store) -> {"open_findings", "by_type", "resolved_last_run"}`.

- [ ] **Step 1–4:** failing test that `build_review_units` returns ≤ cap packets for seeded open findings of a kind; implement using `store.open_findings(finding_type)`; `review_metrics` reads `proactive_findings`. Run green.
- [ ] **Step 5: Snapshot + commit**

```bash
uv run mcpbrain enrich-eval --baseline docs/superpowers/plans/review-baseline-session4.json   # reuse existing eval CLI for graph counts
git add mcpbrain/review.py mcpbrain/review_eval.py tests/test_review.py
git commit -m "feat(review): review-unit producer + review metric"
```

---

## Phase 1 — End-to-end loop on `possible_duplicate`

> Start with the finding-type that already has apply infra (`drain._apply_merge_answers`) and the strictest safety need. Prove: packet → Haiku verdict → guarded merge → finding auto-resolves.

### Task 1.1: Verdict contract + conservative-default validation

**Files:**
- Modify: `mcpbrain/contract.py` — add `validate_review_verdict(kind, verdict) -> list[str]`
- Test: `tests/test_contract.py`

**Interfaces:**
- Produces: `validate_review_verdict(kind: str, v: dict) -> list[str]`. For `possible_duplicate`: `v = {"pair_id", "verdict": "merge"|"distinct"|"skip", "winner_id"?}` — `merge` requires `winner_id` ∈ the pair; any unknown verdict → treated as `skip` (empty problems, but caller coerces to skip). Returns problems list (empty = valid).

- [ ] **Step 1:** failing test: a `merge` verdict without `winner_id` is invalid; `skip`/`distinct` valid; an unknown verdict string is coerced to skip (validator returns `["coerced-to-skip"]` sentinel the caller honors).
- [ ] **Step 2–4:** implement; run green.
- [ ] **Step 5:** commit `feat(contract): review verdict validation with skip-on-uncertainty default`.

### Task 1.2: Guarded merge applier (`review_apply.apply_duplicate_verdict`)

**Files:**
- Create: `mcpbrain/review_apply.py`
- Modify: `mcpbrain/drain.py` — register `BLOCK_DRAINERS["review_duplicate"]`
- Test: `tests/test_review_apply.py`

**Interfaces:**
- Consumes: `store.merge_entities(loser_id, winner_id, *, method=)`, `resolve.is_role_address`, `resolve._NAME_MERGEABLE_TYPES`, `store.resolve_findings_not_in`.
- Produces: `apply_duplicate_verdicts(store, verdicts, *, cap) -> dict{"merged","skipped","capped"}`. For each `merge` verdict: re-verify BOTH entities are `_NAME_MERGEABLE_TYPES` and neither is email-keyed on a role address; merge loser→winner via `merge_entities` (logged); stop at `cap`. `distinct`/`skip` → resolve the finding without merging (record it adjudicated). Registered as the `review_duplicate` block drainer.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_review_apply.py
from mcpbrain.store import Store
from mcpbrain.review_apply import apply_duplicate_verdicts


def test_merge_verdict_merges_but_respects_guards(tmp_path):
    s = Store(str(tmp_path / "b.sqlite3"), dim=4); s.init()
    with s._connect() as db:
        db.execute("INSERT INTO entities(id,name,type,mentions) VALUES('a','Sam Lee','person',5)")
        db.execute("INSERT INTO entities(id,name,type,mentions) VALUES('b','Samuel Lee','person',1)")
        # a doc-typed pair must NEVER merge even if the model says merge
        db.execute("INSERT INTO entities(id,name,type) VALUES('d1','Untitled','document')")
        db.execute("INSERT INTO entities(id,name,type) VALUES('d2','Untitled','document')")
    verdicts = [
        {"pair_id": "a|b", "verdict": "merge", "winner_id": "a"},
        {"pair_id": "d1|d2", "verdict": "merge", "winner_id": "d1"},
    ]
    out = apply_duplicate_verdicts(s, verdicts, cap=50)
    ids = {e["id"] for e in s.list_entities()}
    assert "b" not in ids and "a" in ids, "person duplicate should merge"
    assert {"d1", "d2"} <= ids, "document pair must NOT merge (structural-type guard)"
    assert out["merged"] == 1
```

- [ ] **Step 2: Run to verify it fails** → FAIL (module missing).
- [ ] **Step 3: Implement** `apply_duplicate_verdicts` in `review_apply.py` with the guards (reject non-`_NAME_MERGEABLE_TYPES`, role-address, cap), then register in `drain.py`:

```python
from mcpbrain import review_apply
BLOCK_DRAINERS["review_duplicate"] = lambda store, inbox: review_apply.apply_duplicate_verdicts(
    store, inbox.get("review_duplicate") or [], cap=config.review_max_apply_per_run(str(home_dir)))
```

- [ ] **Step 4: Run** `uv run pytest tests/test_review_apply.py tests/ -q -k "review or drain"` → PASS.
- [ ] **Step 5: Commit** `feat(review): guarded reversible merge applier for duplicate verdicts`.

### Task 1.3: Adjudication rules in the prompt (duplicate kind)

**Files:**
- Modify: `mcpbrain/enrich_prompt.md` — a `review_duplicate` block section; run `python bin/sync_agents.py`
- Test: `tests/test_enrich_prompt_doc.py`

**Interfaces:**
- Produces: prompt rules telling the adjudicator: given the two entities' packets (names, source spans, relations, titles), decide `merge` (same real entity — pick the higher-mention `winner_id`), `distinct`, or `skip` when unsure. Emphasize: **prefer `skip`; never merge on a shared org/role signal alone; a shared personal email or matching role+org across the same-named person is strong evidence.**

- [ ] **Step 1–4:** doc test asserting the `review_duplicate` rules + verdict schema are present and in sync; run `bin/sync_agents.py`; green.
- [ ] **Step 5:** commit `prompt(review): duplicate-adjudication rules + verdict schema`.

### Task 1.4: PHASE-1 GATE — dry-run on the live duplicate findings

- [ ] Build review-units for `lint:possible_duplicate` (~50), run them through a Haiku adjudicator on a scratch copy, apply verdicts with `cap`. Required: **0 wrong merges** in a 20-item spot-check; every merge in `entity_merge_log`; the adjudicated findings resolve on re-lint; gold recall/MRR unchanged. If any wrong merge, tighten the rules/guards and re-run — ships ON only when the spot-check is clean.

---

## Phase 2 — orphan_entity + missing_org

### Task 2.1: `suppress_entity` (reversible) + orphan applier

**Files:**
- Modify: `mcpbrain/store.py` — `suppress_entity(entity_id, reason)` writing a `suppressed_entities` row + soft-hiding the entity (reversible via `unsuppress_entity`)
- Modify: `mcpbrain/review_apply.py` — `apply_orphan_verdicts`; register `BLOCK_DRAINERS["review_orphan"]`
- Test: `tests/test_review_apply.py`, `tests/test_store.py`

**Interfaces:**
- Produces: `store.suppress_entity(entity_id: str, reason: str) -> bool` (reversible — row in `suppressed_entities`, entity not hard-deleted). `apply_orphan_verdicts(store, verdicts, *, cap)`: verdict ∈ `suppress`|`keep`|`skip`; `suppress` only for entities the adjudicator judged extraction-noise; `keep`/`skip` resolve the finding without change.

- [ ] **Step 1:** failing test: `suppress` verdict on a junk orphan calls `suppress_entity` (row appears in `suppressed_entities`), the entity is recoverable; `keep` leaves it. Structural non-person types are not auto-suppressed without an explicit verdict.
- [ ] **Step 2–4:** implement; run green.
- [ ] **Step 5:** commit `feat(review): reversible entity suppression + orphan applier`.

### Task 2.2: missing_org applier + rules

**Files:**
- Modify: `mcpbrain/review_apply.py` — `apply_missing_org_verdicts` (uses `store.update_entity_org`); register `BLOCK_DRAINERS["review_missing_org"]`
- Modify: `mcpbrain/enrich_prompt.md` (+ sync) — orphan + missing_org rules/verdict schema
- Test: `tests/test_review_apply.py`, `tests/test_enrich_prompt_doc.py`

**Interfaces:**
- Produces: `apply_missing_org_verdicts(store, verdicts, *, cap)`: verdict `{ref_id, verdict: "assign"|"external"|"skip", org?}`; `assign` requires `org` ∈ taxonomy → `store.update_entity_org(ref_id, org)`; `external`/`skip` resolve without change. Rules: infer org from the packet's source spans + taxonomy; **`skip` when the text doesn't clearly indicate one**.

- [ ] **Step 1–4:** failing test (assign sets org only when in taxonomy; unknown org → skip); implement; sync prompt; green.
- [ ] **Step 5:** commit `feat(review): missing-org applier + orphan/missing-org rules`.

### Task 2.3: PHASE-2 GATE — dry-run orphans + missing_org

- [ ] Dry-run on the live findings; spot-check 20: no real entity wrongly suppressed, no wrong org assigned; findings resolve; recall/MRR unchanged. Ships ON on a clean check.

---

## Phase 3 — ownerless_action + org-hygiene

### Task 3.1: `assign_action_owner` + ownerless applier

**Files:**
- Modify: `mcpbrain/store.py` — `assign_action_owner(action_id, owner, owner_entity_id="")`
- Modify: `mcpbrain/review_apply.py` — `apply_ownerless_verdicts`; register `BLOCK_DRAINERS["review_ownerless"]`
- Test: `tests/test_review_apply.py`

**Interfaces:**
- Consumes: the finding's `ref_id` = action id; packet extended (Task 0.1 branch) to carry the action text + thread participants + sender.
- Produces: `assign_action_owner(action_id, owner, owner_entity_id)`; `apply_ownerless_verdicts`: verdict `{ref_id, verdict: "owner"|"waiting_on"|"unowned"|"skip", owner?}`. Rules: "I'll…" → sender owns; "can you…" → recipient owns; else `unowned`/`skip`.

- [ ] **Step 1–4:** failing test (owner verdict sets the action owner; unowned leaves it); implement; run green.
- [ ] **Step 5:** commit `feat(review): action-owner assignment + ownerless applier`.

### Task 3.2: org-hygiene applier (ambiguous/duplicate/unrecognised)

**Files:**
- Modify: `mcpbrain/review_apply.py` — `apply_org_verdicts`; register `BLOCK_DRAINERS["review_org"]`
- Modify: `mcpbrain/enrich_prompt.md` (+ sync) — ownerless + org rules
- Test: `tests/test_review_apply.py`

**Interfaces:**
- Produces: `apply_org_verdicts(store, verdicts, *, cap)`: `canonicalize` (fold an org variant into a canonical one — reuse `merge_entities` for org-typed entities, respecting `_NAME_MERGEABLE_TYPES` which includes `org`), or `add_to_config` (record a structured suggestion — do NOT auto-edit config.json; surface it), or `skip`. `org_unrecognised` → `add_to_config` suggestion only.

- [ ] **Step 1–4:** failing test (org canonicalize merges org entities; add_to_config records a suggestion, no config write); implement; sync prompt; green.
- [ ] **Step 5:** commit `feat(review): org-hygiene applier (canonicalize / suggest-config)`.

### Task 3.3: PHASE-3 GATE — dry-run ownerless + org

- [ ] Dry-run; spot-check; ships ON on a clean check. `add_to_config` suggestions surface for the user, never auto-applied.

---

## Phase 4 — brain-review daemon cadence

### Task 4.1: config accessors

**Files:**
- Modify: `mcpbrain/config.py` — `review_interval_s` (default 86400) + `review_max_apply_per_run` (default 50)
- Test: `tests/test_config.py`

- [ ] **Step 1–4:** failing test for defaults + overrides; implement; green.
- [ ] **Step 5:** commit `feat(config): review cadence interval + per-run apply cap`.

### Task 4.2: `brain-review` CadencePass — produce review units on schedule

**Files:**
- Modify: `mcpbrain/daemon.py` — a `CadencePass("review", "_review_interval_s", "_last_review", "_run_review")`, due-gated, `needs_configured=True`; `_run_review` builds review-units via `review.build_review_units` and writes them as block units into the enrich queue (so the existing subagents pick them up), capped.
- Modify: `mcpbrain/daemon.py` `_CADENCE_DEFAULTS` (add `review_interval_s`)
- Test: `tests/test_daemon_p4.py`

**Interfaces:**
- Consumes: `review.build_review_units`, the cadence machinery (mirror `_run_resolve_entities` / `_run_lint`).
- Produces: on the daily cadence (kill-switch `review_interval_s: 0`), review block-units are produced; the enrich subagents adjudicate; drain's `review_*` drainers apply verdicts (capped, reversible). Failure swallowed (won't crash the daemon).

- [ ] **Step 1:** failing test: with `review_interval_s` set and open findings present, one daemon cycle produces review block-units; with it `0`, none. (Mirror `tests/test_daemon_p3.py` for the resolve cadence.)
- [ ] **Step 2–4:** implement the CadencePass + `_run_review`; run green.
- [ ] **Step 5:** commit `feat(daemon): brain-review cadence — produce AI-adjudication review units on schedule`.

### Task 4.3: PHASE-4 GATE — whole-loop safety + first live run

- [ ] Full suite green; ruff clean; final whole-branch code review (fresh subagent) focused on: every applier reversible + logged, guards intact, cap enforced, failure swallowed, no config auto-edit.
- [ ] First live run (or scratch-copy dry run) over the 172-finding queue: report open-findings before/after, verdict mix, and a 20-item spot-check with **zero** wrong mutations. Record in STATUS.

---

## Sequencing & dependencies

```
Phase 0 (packet + metric)
   └─> Phase 1 (duplicate loop, reuse merge_review) ── proves the loop end-to-end
         └─> Phase 2 (orphan suppress + missing_org)   [same rails, new appliers]
               └─> Phase 3 (ownerless + org-hygiene)
                     └─> Phase 4 (daemon cadence + cap + safety gate)
```

- Phase 1 first because `possible_duplicate` has existing apply infra and the strictest safety need — get the guards + reversibility right once, reuse everywhere.
- Each phase A/B-gated with a live/scratch dry-run + spot-check before its behavior is trusted; the cadence (Phase 4) only goes hot after every applier passed its gate.
- **Everything reversible + capped + conservative-default.** This loop mutates the graph unattended — the same failure shape as the C1 bug — so the rails are the point, not an afterthought.

## Do NOT build (trap list)

- Auto-editing `config.json` for `org_unrecognised` — surface a suggestion only.
- Any merge that bypasses `is_role_address` / `_NAME_MERGEABLE_TYPES`.
- Hard-deleting entities — suppression is reversible; merges are logged.
- Adjudicating `memory_promotion`/GTD nudges here — those are dismissed/handled elsewhere (out of scope; Session-4 is graph hygiene).
- A new units/pull/push mechanism — reuse the enrich block-unit pipeline.

## Self-review checklist (run before handoff)

- [ ] **Spec coverage:** evidence packet→0.1, producer/metric→0.2, verdict contract→1.1, duplicate apply→1.2, dup rules→1.3, orphan→2.1, missing_org→2.2, ownerless→3.1, org-hygiene→3.2, config→4.1, cadence→4.2, safety gate→4.3. All finding-types + the mechanism mapped.
- [ ] **No placeholders:** Phase 0/1 + store ops carry real test+impl code; per-kind appliers carry their specific op + verdict schema; gates carry concrete spot-check criteria.
- [ ] **Type consistency:** `build_review_packet`, `build_review_units`, `apply_*_verdicts`, `suppress_entity`, `assign_action_owner`, `validate_review_verdict`, `review_interval_s`, `review_max_apply_per_run`, `BLOCK_DRAINERS["review_*"]` names consistent across tasks and match the real `store`/`drain`/`resolve` signatures.
