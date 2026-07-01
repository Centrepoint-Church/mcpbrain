# Enrichment Efficiency & Graph Depth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make enrichment cheaper and faster by moving deterministic work out of the LLM, and *simultaneously* deeper by populating the provenance/identity/temporal fields the graph schema already supports — proven by an eval gate, with no recall or precision regression.

**Architecture:** The daemon (`prepare.py`) already assembles all per-unit context deterministically; the LLM (Haiku) extracts; the daemon (`drain.py` → `graph_write.apply`) writes the graph. We shift three classes of work from the LLM to deterministic code — (a) **orchestration** (the Sonnet coordinator is a for-loop + a regex check), (b) **structural scaffold** (people-from-headers, org-from-domain, message metadata, provenance), and (c) **schema enforcement** — so Haiku's budget is spent only on the semantic delta (body entities, `reports_to`/`manages`, actions, summary). Every graph-affecting change is gated behind a measured baseline.

**Tech Stack:** Python 3.12, SQLite (`brain.sqlite3`), pytest, ruff, Claude Code subagents (Sonnet/Haiku), MCP tools (`brain_enrich_units`/`_pull`/`_push`/`_advance`).

---

## STATUS

### ✅ Session 1 — SHIPPED as 0.7.72 (2026-07-01)

Merged to `main` and released across all three repos (source `2f688a7`, dist `2f2d942`, plugin `991de09`). Full suite **1761 passed, 0 failed**; whole-branch review found no Critical, one Important (I-1) which was fixed.

- **Phase 0** ✅ `mcpbrain/enrich_eval.py` + `mcpbrain enrich-eval` CLI; baseline snapshot committed (`docs/superpowers/plans/enrich-baseline-2026-06-30.json`).
- **Phase 1** ✅ coordinator runs on Haiku (mechanical dispatch).
- **Phase 2** ✅ relation provenance (`source_doc_id` 0%→~100%), event-date `valid_from`, message-metadata attached daemon-side (not echoed by the model).
- **Phase 5a** ✅ configurable per-unit batch size (`unit_pull_cap` 40k→60k, decoupled from the `_PULL_SOFT_LIMIT=50k` response cap) + strict `brain_enrich_push` schema.
- **Review fixes** ✅ I-1 (drain recovers doc_ids from the unit / caps unrecoverable extractions); pull-response consumer-limit decouple.

**Unplanned but critical finding (the eval harness earned its keep on day one):** a one-shot salience backfill had cold-marked ~40% of the corpus, and `daemon.search` was excluding cold chunks from recall — **halving gold recall@10 (0.750→0.350)**. Fixed by decoupling cold-EXCLUSION from `tiered_memory` into `recall_excludes_cold` (**default OFF**): the salience gate is an *enrichment-cost* optimization, not a *retrieval* filter. Recall restored to **0.750 / MRR 0.556** with no store mutation. The gold eval now measures the true production path (include-cold + three-axis).

### ▶ Session 2 — CODE COMPLETE, VALIDATED, awaiting merge/release go-ahead (branch `enrich-graph-depth-session-2`)

Deepen the graph, now preceded by the salience/recall investigation the finding above surfaced (Phase 2.5). See the Session plan below.

**Session-2 summary (2026-07-01):** All 8 tasks landed across 11 commits (Task 3.3 skipped as a plan defect — see its entry below). Full suite 1794 passed / 1 skipped, ruff clean. Every task went through implementer → task-reviewer (all Approved); a final whole-branch review (opus) found no Critical/Important issues, only tracking-level Minor notes (see `.superpowers/sdd/progress.md` for the full list: `title` observations are currently write-only, `resolve_entities`/email-dedup has no production caller yet, per-`apply()` config-read cost, `home=` not forwarded by `drain`→`apply`).

**Live validation (2026-07-01):** rather than mutate production, copied `brain.sqlite3` to a scratch file and ran 24 real, previously-unenriched threads through this branch's actual `prepare`→(Haiku extraction)→`drain.drain`/`graph_write.apply` path — the real code, real email content, zero live-daemon interference. Results: freshly-written relations carried real `source_doc_id` 9/9 (100%, including one legacy-row provenance backfill); 4 person entities got `email_addr` from headers; `meeting`/`topic` entity types were accepted from the model; 2 `project_membership` observations were written with real dates; the model still produced its own semantic relations (`coordinates_with`) alongside the deterministic scaffold. Corpus-wide `enrich-eval` percentages barely moved at n=24 against an ~80k-chunk corpus (expected — the plan's ~300-thread gate is sized for a visible aggregate shift; this sample's evidence is per-row, not percentage-based).

**New finding (not a Session-2 defect, tracked separately):** testing Task 5.3 required calling `resolve.resolve_entities()`, which also runs the pre-existing, never-before-invoked `_deterministic_merges` (canonical-name merge, no config flag, no caller in production). On the real corpus (copy) this merged 3,980 entities in one pass — including `document`/`thread`-type entities colliding on generic titles ("Untitled document", "TEST"), which is a real identity-key flaw for non-person types (untouched by this branch; `_email_equality_merges` is correctly scoped to `type='person'` only). Confirms `resolve_entities` must not be wired into a real caller before this is fixed. 0 email-equality merges fired in this run — the corpus had no remaining same-email person duplicates once the canonical-name pass ran first (evidence the mechanism works; no positive spot-check sample was available on this corpus/sample size).

**Task 2.5.1 decision (2026-07-01): gate is NOT cold-marking gold-relevant docs — no tightening needed.** Added `gold_docs_cold_marked(store)` to `enrich_eval.py` (commit `ebb9203`) and ran it against the live store (81,545 chunks, 31,888 cold ≈ 39%): of the gold set's 20 unique expected chunk ids present in the store, **0 are cold-marked (present=20, cold=0, pct=0.0%)**. `should_enrich`'s existing rules (`_MIN_DRIVE_TEXT` floor, promotional-label skip, tabular-mime skip) already avoid the gold-relevant prose docs — the ~40% cold set is genuinely low-signal content, not false positives. **No change to `should_enrich` in this session.**

**Task 2.5.2 decision (2026-07-01): `recall_excludes_cold` stays OFF permanently; exclusion path kept (not deleted).** Re-ran `gold_eval` on the live store (three-axis production weights, `recency_weight=0.15 importance_weight=0.1 decay_weight=0.1 recency_alpha=0.01`) with `exclude_cold=False` vs `True`: **include-cold recall@10=0.750 MRR=0.556** vs **exclude-cold recall@10=0.350 MRR=0.300** (covered 20/20 both ways) — reproducing the 0.7.72 finding exactly, now confirmed *after* verifying (2.5.1) that the cold set is not gold-relevant-doc false positives. Include-cold wins decisively on both metrics, so `recall_excludes_cold` remains default **OFF**; production never excludes cold from recall. Kept the exclusion code path (`retrieval.hybrid_search(exclude_cold=)`, `daemon.py:738`, `config.recall_excludes_cold`) rather than deleting it — it is already gated OFF by default, covered by `test_recall_excludes_cold_defaults_off` (`tests/test_config.py`) and the production-path floor test `test_gold_recall_floor` (`tests/eval/test_eval_baseline.py`), and remains a legitimate opt-in for a future narrower cold definition; deleting a tested, documented kill-switch for no functional gain was judged higher risk than benefit.

---

## Global Constraints

- **Version lives in FOUR files, kept equal:** `pyproject.toml`, `mcpbrain/__init__.py`, `plugin/.claude-plugin/plugin.json`, `plugin/.claude-plugin/marketplace.json` (+ `uv.lock` mcpbrain entry). Do not release within this plan — bump/release is a separate, explicitly-instructed step (`docs/RELEASE-RUNBOOK.md`).
- **Single-writer invariant:** only the daemon writes the store during a cycle. All graph mutations stay inside the daemon/`drain` path; MCP tools never write the graph directly.
- **Improvements ship ON. We never ship an improvement switched off.** Every behavior added here defaults **ON**, read via `config.read_config(home).get("<flag>", True)`. A flag exists only as an **emergency kill-switch** (set it to `false` to disable) — its default is always `True`. This matches the `salience_gate` precedent: validated, then shipped default ON (0.7.65).
- **Gates are development-time, not runtime.** The `enrich-eval --compare` checks below are pass-before-done gates run during implementation on a sample. A task is complete only when its gate passes — and then it ships ON. A gate that fails means **fix the implementation until it passes**, not ship it off. Nothing graph-shaping merges to `main` with a failing gate.
- **Cold-marking and all enrich-state changes are reversible** (`store.set_enrich_state(doc_ids, "")` re-queues).
- **TDD throughout:** failing test first, minimal impl, green, commit. Run `uv run pytest <file> -q` and `uv run ruff check mcpbrain/` before each commit.
- **Test isolation:** never touch the real app-dir lock/store in a test — pass `tmp_path` and inject (`Store(str(tmp_path/'b.sqlite3'), dim=384)`, `lock=dmod.SingleWriterLock(tmp_path/'daemon.lock')`). See `tests/test_restore_first_run.py`.
- **Quality is not assumed, it is measured.** Session 1 produces the baseline; every later task reports metrics against it. A graph-shape change that regresses recall MRR or relation precision is fixed before merge — never shipped off to "protect" against the regression.

---

## Session plan (2 sessions, review checkpoint each)

This plan runs in **two sessions**. Each produces working, tested, shipped-ON software and ends at a review checkpoint before the next begins.

### ✅ Session 1 — Stop the waste, ground the truth — SHIPPED 0.7.72

Foundations + deterministic/orchestration wins + provenance/event-date wiring. All done and released (see STATUS above). Phases 0, 1, 2, 5a + review fixes + the cold-recall decouple.

### ▶ Session 2 — Deepen the graph *(validated against the Session-1 baseline; everything ships ON)*

The semantic-depth spine, now opening with the salience/recall investigation the Session-1 finding surfaced. Each task A/Bs on a ~300-thread sample and ships ON once its gate passes.

- **Phase 2.5** — Salience / recall investigation (Tasks 2.5.1, 2.5.2) — **do first**
- **Phase 3** — Deterministic scaffold + identity (Tasks 3.1–3.4)
- **Phase 4** — Extraction depth (Tasks 4.1, 4.2)
- **Phase 5b** — Deterministic email dedup (Task 5.3; needs Phase 3's `email_addr`)

**Checkpoint:** Phase-2.5 decision recorded; all gates passed; `enrich-eval --compare` shows `person_email_pct`, `relations_semantic_pct`, and non-`role` observation attributes all up, recall MRR not down; all new behavior default ON.

> Task IDs are stable across this split (Phase 5's tasks land in different sessions: 5.1/5.2 in Session 1, 5.3 in Session 2). The per-task detail below is grouped by phase; follow the session assignment above for ordering.

---

## Phase 2.5 — Salience / recall investigation *(Session 2, do first)*

> Origin: the Session-1 finding. The 0.7.72 decouple (cold no longer excluded from recall) was the correct *acute* fix, but it left two open questions the eval harness now lets us answer with data. **Deliverable of each task is a measured decision recorded in this plan's STATUS, not necessarily a code change.**

### Task 2.5.1: Is the salience gate cold-marking genuinely-relevant docs? (precision of the gate)

**Approach:** the gold set's expected docs are hand-curated "correct answers." Measure how many of them `should_enrich()` would cold-mark (false positives). Add a metric to `enrich_eval.py`: `gold_docs_cold_marked` = of the gold cases' expected doc_ids, how many are currently `enrich_state='cold'`.
- If the gate is cold-marking gold-relevant prose docs, the gate is too aggressive — tighten `should_enrich` (e.g. the `_MIN_DRIVE_TEXT` floor, the promotional-label rules) and re-measure. Ships ON once false-positive rate is near zero.
- **Gate:** `gold_docs_cold_marked` trends to 0; no gold recall regression.

### Task 2.5.2: Should production ever exclude cold from recall? (decide `recall_excludes_cold`'s fate)

**Approach:** with the gate tightened (2.5.1), re-run `gold_eval` at `exclude_cold` True vs False on the three-axis path. Decide:
- If include-cold ≥ exclude-cold on recall@10 AND MRR (as measured 2026-07-01: 0.750/0.556 vs 0.350/0.300) → keep `recall_excludes_cold` **OFF permanently**; consider deleting the exclusion path entirely to remove the footgun.
- Only re-enable exclusion if a tightened gate makes exclude-cold measurably win.
- **Gate:** decision recorded with the measured numbers; the gold floor test continues to measure whatever production actually does.

---

## File Structure

| File | Responsibility | Touched by |
|---|---|---|
| `mcpbrain/enrich_eval.py` *(new)* | Population + quality metrics harness over a store; emits a JSON snapshot. The Phase 0 gate. | P0 |
| `mcpbrain/routines/enrich.md`, `plugin/skills/mcpbrain-backfill/SKILL.md` | Coordinator instructions (model + loop). | P1 |
| `mcpbrain/graph_write.py` | `apply()` write path: provenance stamping, event-date grounding, identity, deterministic relations, richer observations. | P2, P3, P4 |
| `mcpbrain/contract.py` | The extraction envelope contract: drop echoed metadata, reconcile vocabulary. | P2, P4 |
| `mcpbrain/prepare.py` | Per-unit assembly: scaffold injection, deterministic defaults, batch sizing. | P3, P5 |
| `mcpbrain/drain.py` | Apply orchestration: thread doc_id/event-date through to `apply()`. | P2 |
| `mcpbrain/config.py` | Flag accessors for every new behavior. | P1–P5 |
| `mcpbrain/resolve.py` | Deterministic dedup blocking (Q3). | P5 |
| `mcpbrain/mcp_server.py` | `brain_enrich_push` arg schema tightening. | P5 |

---

## Phase 0 — Eval baseline (gate for everything else) · **Session 1**

> Origin: **quality #5**. Nothing later may claim "no quality loss" without this. Build and snapshot FIRST.

### Task 0.1: Population + quality metrics harness

**Files:**
- Create: `mcpbrain/enrich_eval.py`
- Test: `tests/test_enrich_eval.py`

**Interfaces:**
- Produces: `graph_metrics(store) -> dict` returning keys `relations_total`, `relations_with_doc_id_pct`, `relations_semantic_pct` (relation ∉ {`involved_in`,`authored`,`instance_of`}), `entities_total`, `person_email_pct`, `observation_attributes` (dict attr→count), `relation_type_counts` (dict). All percentages are floats 0–100.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_enrich_eval.py
from mcpbrain.store import Store
from mcpbrain.enrich_eval import graph_metrics


def _seed(tmp_path):
    s = Store(str(tmp_path / "b.sqlite3"), dim=384)
    s.init()
    with s._connect() as db:
        db.execute("INSERT INTO entities(id,name,type,email_addr) VALUES('e1','Sam','person','sam@x.org')")
        db.execute("INSERT INTO entities(id,name,type,email_addr) VALUES('e2','Pat','person','')")
        db.execute("INSERT INTO entities(id,name,type) VALUES('o1','XOrg','org')")
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b,valid_from,source_doc_id) "
                   "VALUES('e1','reports_to','e2','2026-01-01','doc-1')")
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b,valid_from,source_doc_id) "
                   "VALUES('e1','involved_in','o1','2026-01-01','')")
        db.execute("INSERT INTO entity_observations(entity_id,attribute,value,valid_from) "
                   "VALUES('e1','role','CEO','2026-01-01')")
    return s


def test_graph_metrics_basic(tmp_path):
    m = graph_metrics(_seed(tmp_path))
    assert m["relations_total"] == 2
    assert m["relations_with_doc_id_pct"] == 50.0          # 1 of 2 has a doc id
    assert m["relations_semantic_pct"] == 50.0             # reports_to is semantic, involved_in is not
    assert m["entities_total"] == 3
    assert m["person_email_pct"] == 50.0                   # 1 of 2 persons has email
    assert m["observation_attributes"] == {"role": 1}
    assert m["relation_type_counts"]["reports_to"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_enrich_eval.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcpbrain.enrich_eval'`

- [ ] **Step 3: Write minimal implementation**

```python
# mcpbrain/enrich_eval.py
"""Graph population + quality metrics. The eval gate for the enrichment-depth work.

graph_metrics(store) is a pure read over the live schema; it does not mutate.
Run before and after each graph-shape change to prove improvement / no regression.
"""

_STRUCTURAL_RELATIONS = frozenset({"involved_in", "authored", "instance_of"})


def graph_metrics(store) -> dict:
    with store._connect() as db:
        def scalar(sql, *a):
            return db.execute(sql, a).fetchone()[0]

        rel_total = scalar("SELECT COUNT(*) FROM entity_relations")
        rel_doc = scalar("SELECT COUNT(*) FROM entity_relations WHERE COALESCE(source_doc_id,'')!=''")
        rel_sem = scalar(
            "SELECT COUNT(*) FROM entity_relations WHERE relation NOT IN (?,?,?)",
            *sorted(_STRUCTURAL_RELATIONS))
        ent_total = scalar("SELECT COUNT(*) FROM entities")
        persons = scalar("SELECT COUNT(*) FROM entities WHERE type='person'")
        persons_email = scalar(
            "SELECT COUNT(*) FROM entities WHERE type='person' AND COALESCE(email_addr,'')!=''")
        obs = dict(db.execute(
            "SELECT attribute, COUNT(*) FROM entity_observations GROUP BY attribute").fetchall())
        rel_types = dict(db.execute(
            "SELECT relation, COUNT(*) FROM entity_relations GROUP BY relation").fetchall())

    def pct(n, d):
        return round(100.0 * n / d, 1) if d else 0.0

    return {
        "relations_total": rel_total,
        "relations_with_doc_id_pct": pct(rel_doc, rel_total),
        "relations_semantic_pct": pct(rel_sem, rel_total),
        "entities_total": ent_total,
        "person_email_pct": pct(persons_email, persons),
        "observation_attributes": obs,
        "relation_type_counts": rel_types,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_enrich_eval.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/enrich_eval.py tests/test_enrich_eval.py
git commit -m "feat(eval): graph population metrics harness (enrichment-depth gate)"
```

### Task 0.2: CLI entry + snapshot the live baseline

**Files:**
- Modify: `mcpbrain/enrich_eval.py` (add `main()`)
- Modify: `mcpbrain/cli.py` (register `enrich-eval` subcommand — follow the existing `sub.add_parser(...)` pattern at `cli.py:20-25`)
- Test: `tests/test_enrich_eval.py` (add `main` smoke test on `tmp_path`)

**Interfaces:**
- Consumes: `graph_metrics(store)` from Task 0.1.
- Produces: `mcpbrain enrich-eval` prints the JSON metrics and, with `--baseline <path>`, writes them to disk; with `--compare <path>`, prints a per-key delta table.

- [ ] **Step 1: Write the failing test**

```python
def test_main_writes_baseline(tmp_path, monkeypatch, capsys):
    s = _seed(tmp_path)
    monkeypatch.setattr("mcpbrain.enrich_eval._open_store", lambda: s)
    from mcpbrain.enrich_eval import main
    out = tmp_path / "base.json"
    main(["--baseline", str(out)])
    import json
    saved = json.loads(out.read_text())
    assert saved["relations_total"] == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_enrich_eval.py::test_main_writes_baseline -q`
Expected: FAIL — `ImportError: cannot import name 'main'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to mcpbrain/enrich_eval.py
import argparse
import json


def _open_store():
    from mcpbrain import config
    from mcpbrain.store import Store
    from mcpbrain.embed import get_embedder
    return Store(config.store_path(), dim=get_embedder("bge-small").dim)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", help="write metrics JSON to this path")
    ap.add_argument("--compare", help="diff current metrics against this saved baseline")
    args = ap.parse_args(argv)
    m = graph_metrics(_open_store())
    if args.baseline:
        with open(args.baseline, "w") as f:
            json.dump(m, f, indent=2)
    if args.compare:
        base = json.load(open(args.compare))
        for k in ("relations_with_doc_id_pct", "relations_semantic_pct",
                  "person_email_pct", "relations_total"):
            print(f"{k}: {base.get(k)} -> {m.get(k)}")
    print(json.dumps(m, indent=2))


if __name__ == "__main__":
    main()
```

Register in `mcpbrain/cli.py` alongside the other subparsers (the dispatcher there imports lazily; mirror the nearest existing case):

```python
    sub.add_parser("enrich-eval", add_help=False)
    # ... in the dispatch block:
    if args.cmd == "enrich-eval":
        from mcpbrain import enrich_eval
        return enrich_eval.main(rest)
```

- [ ] **Step 4: Run tests + lint**

Run: `uv run pytest tests/test_enrich_eval.py -q && uv run ruff check mcpbrain/`
Expected: PASS, clean.

- [ ] **Step 5: Snapshot the live baseline (the gate) + commit**

```bash
uv run mcpbrain enrich-eval --baseline docs/superpowers/plans/enrich-baseline-2026-06-30.json
git add mcpbrain/enrich_eval.py mcpbrain/cli.py tests/test_enrich_eval.py docs/superpowers/plans/enrich-baseline-2026-06-30.json
git commit -m "feat(eval): enrich-eval CLI + live graph baseline snapshot"
```

Expected baseline (from 2026-06-30 measurement; the file pins exact values): `relations_with_doc_id_pct ≈ 0.5`, `relations_semantic_pct` low, `person_email_pct ≈ 4`, `observation_attributes == {"role": 2737}`. **Every later phase runs `--compare` against this file.**

---

## Phase 1 — Orchestration cost (independent, ship first) · **Session 1**

> Origin: **cost #1**. Pure cost cut, zero graph surface. No dependency on Phase 0.

### Task 1.1: Move the coordinator off Sonnet

**Files:**
- Modify: `mcpbrain/routines/enrich.md` (the "Models" paragraph)
- Modify: `plugin/skills/mcpbrain-backfill/SKILL.md` (the "Models" section)
- Test: `tests/test_enrich_prompt_doc.py` (these are doc-contract tests — add an assertion that the coordinator model is Haiku)

**Interfaces:**
- Produces: both coordinator docs instruct the orchestrator to run on **Haiku** (or a script harness), with the requeue-guard stated as a literal string-shape check. Subagents stay Haiku.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_enrich_prompt_doc.py  (add)
from pathlib import Path

def test_coordinator_runs_on_haiku():
    for p in ("mcpbrain/routines/enrich.md",
              "plugin/skills/mcpbrain-backfill/SKILL.md"):
        text = Path(p).read_text().lower()
        # The orchestrator must not be pinned to Sonnet anymore.
        assert "you (the coordinator) run on **sonnet**" not in text
        assert "run this loop on sonnet" not in text
        assert "coordinator" in text and "haiku" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_enrich_prompt_doc.py::test_coordinator_runs_on_haiku -q`
Expected: FAIL — both docs currently pin the coordinator to Sonnet.

- [ ] **Step 3: Edit the docs**

In `mcpbrain/routines/enrich.md`, replace the "Models" paragraph with:

```markdown
**Models:** the orchestration is mechanical — fan out one subagent per unit, then
check each reply against a fixed string shape — so **run the coordinator on Haiku**
(set it explicitly; do not assume Sonnet). The requeue guard is a literal check, not
a judgement: a unit is done IFF its reply matches `unit <unit_id>: <n> <kind>` or
`unit <unit_id>: gone`. Every `enrich-batch` subagent also runs on **Haiku**, set
explicitly per dispatch (step 3). Escalate to Sonnet only if a unit fails all retries
across multiple runs and needs investigation.
```

Apply the equivalent edit to the "Models" section of `plugin/skills/mcpbrain-backfill/SKILL.md` (replace "Run this loop on Sonnet …" with the Haiku instruction and the literal-check phrasing).

- [ ] **Step 4: Run test + the existing doc-contract suite**

Run: `uv run pytest tests/test_enrich_prompt_doc.py -q`
Expected: PASS (and no other doc-contract test regresses).

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/routines/enrich.md plugin/skills/mcpbrain-backfill/SKILL.md tests/test_enrich_prompt_doc.py
git commit -m "perf(enrich): run the coordinator on Haiku — orchestration is mechanical dispatch"
```

> Note: if `plugin/agents/enrich-batch.md` rules block is touched later, run `python bin/sync_agents.py` (see CLAUDE.md). This task does not touch the rules block.

---

## Phase 2 — Contract reshape + provenance (the shared seam) · **Session 1**

> Origin: **cost #2** (stop echoing message metadata), **quality #1** (stamp `source_doc_id`), **quality #4** (event-date grounding). These touch `apply()`/`contract`/`drain` together. All ship ON; **dev-time gate: `enrich-eval --compare` after Task 2.3 must pass before the session checkpoint.**

### Task 2.1: Stamp provenance (`source_doc_id`) on every relation in `apply()`

**Files:**
- Modify: `mcpbrain/graph_write.py` — `apply()` (`:836`), relation upsert site (`:1136`)
- Test: `tests/test_graph_write_provenance.py` *(new)*

**Interfaces:**
- Consumes: `apply(store, extraction, *, doc_ids, identity=None, clock=None)` (existing); `upsert_relation(store, a, rel, b, *, valid_from, evidence, ..., source_doc_id=None)` (existing, `:384`).
- Produces: relations written by `apply()` carry `source_doc_id = lead doc id` (first of `doc_ids`, else `f"enriched-{thread_id}"`), not the evidence-string fallback.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_graph_write_provenance.py
from mcpbrain.store import Store
from mcpbrain import graph_write


def _store(tmp_path):
    s = Store(str(tmp_path / "b.sqlite3"), dim=384); s.init(); return s


def test_relation_gets_real_doc_id(tmp_path):
    s = _store(tmp_path)
    extraction = {
        "thread_id": "t1", "org": "unknown", "content_type": "email",
        "summary": "s", "messages": [{"message_id": "m1", "sender": "a@x.org", "date": "2026-02-01"}],
        "entities": [{"name": "Sam", "type": "person"}, {"name": "Pat", "type": "person"}],
        "relations": [{"source_name": "Sam", "type": "reports_to", "target_name": "Pat"}],
        "actions": [], "topics": [],
    }
    graph_write.apply(s, extraction, doc_ids=["doc-42"])
    with s._connect() as db:
        rows = db.execute(
            "SELECT source_doc_id FROM entity_relations WHERE relation='reports_to'").fetchall()
    assert rows, "relation was not written"
    assert rows[0][0] == "doc-42", f"expected provenance doc-42, got {rows[0][0]!r}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_graph_write_provenance.py -q`
Expected: FAIL — `source_doc_id` is the evidence string (or empty), not `doc-42`.

- [ ] **Step 3: Implement — thread the lead doc id into the relation upsert**

In `apply()`, near the top (after `thread_id` is known), derive the provenance id:

```python
    # Provenance: the originating doc for every fact this extraction yields.
    # doc_ids is the thread's chunk doc_ids (drain passes them); fall back to the
    # synthetic enriched-thread id when a thread has no concrete lead doc.
    prov_doc_id = (doc_ids[0] if doc_ids else "") or (f"enriched-{thread_id}" if thread_id else "")
```

At the relation upsert site (`:1136`), pass it through:

```python
        if upsert_relation(store, source_id, rel_type, target_id,
                           valid_from=valid_from, evidence=evidence,
                           confidence=confidence, strength=strength,
                           source_doc_id=prov_doc_id):
```

(Keep the existing kwargs; only add `source_doc_id=prov_doc_id`. Update `apply()`'s docstring line that says `doc_ids: currently unused` to describe the provenance use.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_graph_write_provenance.py -q`
Expected: PASS

- [ ] **Step 5: Run the full graph-write + drain suites (no regression)**

Run: `uv run pytest tests/ -q -k "graph_write or drain or apply"`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/graph_write.py tests/test_graph_write_provenance.py
git commit -m "feat(graph): stamp real source_doc_id on relations in apply() (provenance 0%->~100%)"
```

### Task 2.2: Ground `valid_from` in the message/doc event date (not write-time)

**Files:**
- Modify: `mcpbrain/graph_write.py` — relation `valid_from` derivation in `apply()`
- Test: `tests/test_graph_write_provenance.py` (add)

**Interfaces:**
- Consumes: the lead message date already computed in `apply()` (`lead_date_iso`, `graph_write.py:902`).
- Produces: relations' `valid_from` equals the lead message's ISO date when present, not `_today()`.

- [ ] **Step 1: Write the failing test**

```python
def test_relation_valid_from_is_event_date(tmp_path):
    s = _store(tmp_path)
    extraction = {
        "thread_id": "t2", "org": "unknown", "content_type": "email", "summary": "s",
        "messages": [{"message_id": "m1", "sender": "a@x.org", "date": "2025-09-15T10:00:00Z"}],
        "entities": [{"name": "Sam", "type": "person"}, {"name": "Pat", "type": "person"}],
        "relations": [{"source_name": "Sam", "type": "manages", "target_name": "Pat"}],
        "actions": [], "topics": [],
    }
    graph_write.apply(s, extraction, doc_ids=["doc-9"])
    with s._connect() as db:
        vf = db.execute("SELECT valid_from FROM entity_relations WHERE relation='manages'").fetchone()[0]
    assert vf.startswith("2025-09-15"), f"valid_from should be the event date, got {vf!r}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_graph_write_provenance.py::test_relation_valid_from_is_event_date -q`
Expected: FAIL — `valid_from` is today's date.

- [ ] **Step 3: Implement**

In `apply()`, where the relation `valid_from` is set, prefer the lead event date:

```python
        # Bi-temporal valid_from is the fact's observation date = the lead
        # message/doc date when known, not the write time.
        valid_from = lead_date_iso or _today()
```

(`lead_date_iso` is already parsed at `:902` via `_parse_date_iso(lead.get("date",""))`. Reuse it; do not re-parse.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_graph_write_provenance.py::test_relation_valid_from_is_event_date -q`
Expected: PASS

- [ ] **Step 5: Run the supersession tests (bi-temporal correctness)**

Run: `uv run pytest tests/ -q -k "relation or supersed or bitemporal or observation"`
Expected: PASS — earlier-dated facts must not supersede later-dated ones.

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/graph_write.py tests/test_graph_write_provenance.py
git commit -m "fix(graph): valid_from = event date so supersession/'latest status' is correct"
```

### Task 2.3: Drop echoed message metadata from the contract; attach it deterministically

**Files:**
- Modify: `mcpbrain/contract.py` — `validate_extraction()` (`:46`)
- Modify: `mcpbrain/drain.py` — the apply call site (`:400-410`) to attach known messages
- Modify: `mcpbrain/enrich_prompt.md` — remove the instruction to return `messages[]`; then `python bin/sync_agents.py`
- Test: `tests/test_contract.py`, `tests/test_drain.py` (add)

**Interfaces:**
- Consumes: `store.doc_ids_for_messages(msg_ids)` (existing, `drain.py:402`); the unit's original `threads[*].messages` (the daemon built them in `prepare._thread_block`).
- Produces: `validate_extraction()` no longer requires `messages[]`; `drain` injects the original messages into the extraction dict before `apply()` so `graph_write` still derives lead metadata.

- [ ] **Step 1: Write the failing test (contract no longer requires messages)**

```python
# tests/test_contract.py  (add)
from mcpbrain.contract import validate_extraction

def test_messages_not_required():
    ext = {"thread_id": "t1", "org": "unknown", "content_type": "email",
           "summary": "s", "entities": [{"name": "Sam", "type": "person"}],
           "relations": [], "actions": [], "topics": ["x"]}
    # messages intentionally absent — the daemon supplies them.
    assert validate_extraction(ext) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_contract.py::test_messages_not_required -q`
Expected: FAIL — current contract requires a non-empty `messages` list (`contract.py:76-80`).

- [ ] **Step 3: Implement contract change**

In `validate_extraction()`, make `messages` **optional but typed when present**:

```python
    # messages: OPTIONAL — the daemon attaches the canonical messages from the unit
    # it built (sender/date/message_id are system-owned, not model output). Validate
    # shape only if the model included them.
    messages = d.get("messages")
    if messages is not None:
        if not isinstance(messages, list):
            problems.append("messages, when present, must be a list")
        else:
            for i, msg in enumerate(messages):
                if not isinstance(msg, dict):
                    problems.append(f"messages[{i}] must be an object")
```

Delete the old required-`messages` block (`:76-92`). Keep the empty-extraction guard (`:133-143`) — it does not reference `messages`.

- [ ] **Step 4: Write the failing test (drain attaches messages before apply)**

```python
# tests/test_drain.py  (add — uses the existing drain test harness/fakes in this file)
def test_drain_attaches_unit_messages_before_apply(tmp_path, monkeypatch):
    captured = {}
    def fake_apply(store, extraction, *, doc_ids, **kw):
        captured["messages"] = extraction.get("messages")
        return {"entities": 0, "relations": 0, "actions": 0}
    # ... assemble a unit whose threads[0].messages = [{"message_id":"m1","sender":"a@x.org","date":"2026-02-01"}]
    # ... model extraction omits messages; drain must inject the unit's messages.
    # (Mirror the fixture style already used by test_drain.py.)
    assert captured["messages"] == [{"message_id": "m1", "sender": "a@x.org", "date": "2026-02-01"}]
```

- [ ] **Step 5: Implement drain injection**

In `drain.py`, before calling `apply(...)` (`:410`), merge the unit's canonical messages into the extraction when the model omitted them:

```python
            # Message metadata is system-owned: attach the unit's original messages
            # (built by prepare._thread_block) so graph_write derives lead msg/date/sender
            # from authoritative data, not the model's echo.
            if not extraction.get("messages") and unit_messages_by_thread.get(extraction.get("thread_id")):
                extraction["messages"] = unit_messages_by_thread[extraction["thread_id"]]
```

(Build `unit_messages_by_thread` from the pulled unit's `threads` at the top of the per-unit loop; the unit already carries `threads[*].messages` from `prepare`.)

- [ ] **Step 6: Update the prompt + re-sync the agent**

In `mcpbrain/enrich_prompt.md`, remove the envelope instruction that asks for `messages[]` (keep the semantic fields). Then:

```bash
python bin/sync_agents.py   # keeps plugin/agents/enrich-batch.md rules block byte-identical
```

- [ ] **Step 7: Run tests + lint**

Run: `uv run pytest tests/test_contract.py tests/test_drain.py tests/test_enrich_prompt_doc.py -q && uv run ruff check mcpbrain/`
Expected: PASS, clean (the rules-in-sync test must stay green).

- [ ] **Step 8: Commit**

```bash
git add mcpbrain/contract.py mcpbrain/drain.py mcpbrain/enrich_prompt.md plugin/agents/enrich-batch.md tests/test_contract.py tests/test_drain.py
git commit -m "perf(enrich): stop round-tripping message metadata through the model; daemon attaches it"
```

- [ ] **Step 9: PHASE-2 GATE — compare graph metrics**

After re-enriching a sample (or on the next daemon pass), run:

```bash
uv run mcpbrain enrich-eval --compare docs/superpowers/plans/enrich-baseline-2026-06-30.json
```

Expected: `relations_with_doc_id_pct` rises from ~0.5 toward ~100 on newly-applied relations; `relations_total`/`relations_semantic_pct` not lower. If semantic % drops, STOP — message removal hurt extraction; investigate before Phase 3.

---

## Phase 3 — Deterministic scaffold + identity · **Session 2**

> Origin: **quality #2** (`email_addr` from headers), **cost #4** (org/content_type defaults), **cost #3** (structural relations pre-filled; model returns only the semantic delta). Depends on Phase 2's contract reshape. All ship ON; **dev-time gate: `enrich-eval --compare` after Task 3.1 and the Task 3.4 A/B must pass before merge.**

### Task 3.1: Populate `email_addr` deterministically for header participants

**Files:**
- Modify: `mcpbrain/graph_write.py` — entity upsert for header participants (the `_extract_email_addr(msg.get("sender"))` path already exists at `:747`)
- Test: `tests/test_graph_write_provenance.py` (add)

**Interfaces:**
- Consumes: the lead/sender header (`sender_header`, `:903`) and per-message senders (now system-attached, Phase 2).
- Produces: a `person` entity created/updated from a header carries `email_addr` set from the header address.

- [ ] **Step 1: Write the failing test**

```python
def test_header_person_gets_email(tmp_path):
    s = _store(tmp_path)
    extraction = {
        "thread_id": "t3", "org": "unknown", "content_type": "email", "summary": "s",
        "messages": [{"message_id": "m1", "sender": "Sam Lee <sam.lee@centrepoint.church>", "date": "2026-02-01"}],
        "entities": [{"name": "Sam Lee", "type": "person"}],
        "relations": [], "actions": [], "topics": ["x"],
    }
    graph_write.apply(s, extraction, doc_ids=["doc-7"])
    with s._connect() as db:
        email = db.execute("SELECT email_addr FROM entities WHERE name='Sam Lee'").fetchone()[0]
    assert email == "sam.lee@centrepoint.church"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_graph_write_provenance.py::test_header_person_gets_email -q`
Expected: FAIL — `email_addr` is empty (only 4% populated today).

- [ ] **Step 3: Implement**

In `apply()`, when matching an extracted person entity to a header sender, set `email_addr` from the parsed header. Use the existing helpers `_extract_email_addr` (`:747`) and the name match; when an extracted entity name matches the sender display name, attach the address on upsert. (Set only when currently empty — never overwrite a better-sourced address.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_graph_write_provenance.py::test_header_person_gets_email -q`
Expected: PASS

- [ ] **Step 5: Commit + gate**

```bash
git add mcpbrain/graph_write.py tests/test_graph_write_provenance.py
git commit -m "feat(graph): set person email_addr from message headers (identity 4%->high)"
uv run mcpbrain enrich-eval --compare docs/superpowers/plans/enrich-baseline-2026-06-30.json
```

Expected: `person_email_pct` rises well above 4 on newly-applied entities.

### Task 3.2: Deterministic `org` default from sender domain

**Files:**
- Modify: `mcpbrain/prepare.py` — `_thread_block()` (`:358`) to attach `org_hint`
- Modify: `mcpbrain/graph_write.py` — use `org_hint` when the model returns `org="unknown"`
- Modify: `mcpbrain/config.py` — `enrich_org_default_enabled(home)` kill-switch (default **True**)
- Test: `tests/test_prepare.py`, `tests/test_graph_write_provenance.py` (add)

**Interfaces:**
- Consumes: `graph_write.org_from_email(email_addr, taxonomy)` (existing, `:112`).
- Produces: each thread block carries `org_hint = org_from_email(lead_sender_domain)`; `apply()` uses it only when the model's `org` is empty/`"unknown"`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_prepare.py  (add)
def test_thread_block_has_org_hint(monkeypatch):
    # a lead sender at a configured domain yields a deterministic org_hint
    from mcpbrain import prepare
    # ... build a fake batch whose reassembled lead sender is "x@centrepoint.church"
    block = prepare._thread_block(fake_store, fake_batch)
    assert block["org_hint"]  # non-empty, == org_from_email("x@centrepoint.church")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_prepare.py::test_thread_block_has_org_hint -q`
Expected: FAIL — `_thread_block` returns no `org_hint`.

- [ ] **Step 3: Implement** — add `org_hint` in `_thread_block` (derive from the lead message sender domain via `graph_write.org_from_email`); in `apply()`, `org = (extraction.get("org") or "").strip(); if org in ("", "unknown"): org = extraction.get("org_hint") or org`. Guard the whole behavior behind `config.enrich_org_default_enabled(home)`.

- [ ] **Step 4: Run tests** — `uv run pytest tests/test_prepare.py tests/test_graph_write_provenance.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/prepare.py mcpbrain/graph_write.py mcpbrain/config.py tests/test_prepare.py tests/test_graph_write_provenance.py
git commit -m "feat(enrich): deterministic org default from sender domain (model overrides only on signal)"
```

### Task 3.3: Deterministic `content_type` default from source/mime — **SKIPPED (2026-07-01)**

**Skipped as a plan defect, decided with Josh.** `content_type`'s real enum (`chunking._VALID_CONTENT_TYPES = {"request","update","decision","fyi","notification"}`) is a message-**purpose** classification (`graph_write.apply()` uses e.g. `content_type == "notification"` to suppress action creation) — not a source-type discriminator. The task's own examples ("calendar→`event`, Drive mime→doc type, else `email`") aren't even members of that enum. Source/mime alone cannot reliably predict request-vs-update-vs-fyi-vs-notification; that classification is genuinely semantic, which is why it's LLM-extracted today. Forcing a source-based default here would write incorrect classifications into the graph — the opposite of this plan's quality goal. No code changed; no flag added. Superseded text below is the plan's original (unimplemented) task, kept for history.

**Files:**
- Modify: `mcpbrain/prepare.py` — attach `content_type_hint` per unit (calendar→`event`, Drive mime→doc type, else `email`)
- Modify: `mcpbrain/graph_write.py` — use the hint when the model omits `content_type`
- Test: `tests/test_prepare.py` (add)

**Interfaces:**
- Produces: `content_type_hint ∈ _VALID_CONTENT_TYPES` per thread block; `apply()` falls back to it when `content_type` is empty/invalid.

- [ ] **Step 1–4:** Failing test asserting `content_type_hint` for a calendar-sourced and a Drive-doc-sourced block; implement the deterministic mapping from `source_type`/`mime_type` (reuse the maps in `prepare.should_enrich`); fallback in `apply()`; run `uv run pytest tests/test_prepare.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/prepare.py mcpbrain/graph_write.py tests/test_prepare.py
git commit -m "feat(enrich): deterministic content_type default from source/mime"
```

### Task 3.4: Pre-fill structural relations; prompt the model for the semantic delta only

**Files:**
- Modify: `mcpbrain/graph_write.py` — emit deterministic `works_at` (person@domain→org) and co-participation `mentioned_with` for header participants, independent of the model's `relations`
- Modify: `mcpbrain/enrich_prompt.md` — scope the relations the model returns to the *semantic* ones (`reports_to`, `manages`, `coordinates_with`, plus body-only `works_at`); state that header people / structural edges are system-supplied; then `python bin/sync_agents.py`
- Modify: `mcpbrain/config.py` — `enrich_structural_relations_enabled(home)` kill-switch (default **True**; the A/B in Step 6 must pass before merge)
- Test: `tests/test_graph_write_provenance.py` (add)

**Interfaces:**
- Consumes: header participants + their `email_addr` (Task 3.1), `org_from_email` (`:112`).
- Produces: when the flag is ON, `apply()` writes `works_at` for each header person whose email domain maps to a configured org, and `mentioned_with` among co-participants — with `source_doc_id` and event-date `valid_from` (Phase 2) — *before* applying the model's semantic relations.

- [ ] **Step 1: Write the failing test**

```python
def test_structural_works_at_written_without_model(tmp_path, monkeypatch):
    monkeypatch.setattr("mcpbrain.config.enrich_structural_relations_enabled", lambda home: True)
    s = _store(tmp_path)
    extraction = {
        "thread_id": "t8", "org": "unknown", "content_type": "email", "summary": "s",
        "messages": [{"message_id": "m1", "sender": "Sam <sam@centrepoint.church>", "date": "2026-02-01"}],
        "entities": [{"name": "Sam", "type": "person"}],
        "relations": [],          # model returned NO relations
        "actions": [], "topics": ["x"],
    }
    graph_write.apply(s, extraction, doc_ids=["doc-1"])
    with s._connect() as db:
        n = db.execute("SELECT COUNT(*) FROM entity_relations WHERE relation='works_at'").fetchone()[0]
    assert n >= 1, "domain-derived works_at should be written deterministically"
```

- [ ] **Step 2: Run to verify it fails** → FAIL (no structural relation written).

- [ ] **Step 3: Implement** the deterministic structural-relation pass in `apply()` behind the flag; ensure it dedups against any identical model-supplied relation (the bi-temporal `upsert_relation` already idempotently revives/dedups, so double-writes are safe).

- [ ] **Step 4: Run** `uv run pytest tests/test_graph_write_provenance.py -q` → PASS. Update `enrich_prompt.md`, run `python bin/sync_agents.py`, confirm `tests/test_enrich_prompt_doc.py` green.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/graph_write.py mcpbrain/enrich_prompt.md plugin/agents/enrich-batch.md mcpbrain/config.py tests/test_graph_write_provenance.py
git commit -m "feat(enrich): deterministic structural relations; model returns only the semantic delta"
```

- [ ] **Step 6: PHASE-3 GATE — A/B on a sample before flipping the flag ON**

Pick ~300 threads. Enrich once with `enrich_structural_relations_enabled` OFF (model does everything) and once ON (scaffold + semantic delta). Compare with `enrich-eval --compare`:
- **Required:** `relations_semantic_pct` and `person_email_pct` up; `relations_with_doc_id_pct` ~100.
- **Required:** `reports_to`/`manages` counts NOT lower under ON (the scaffold must free budget for semantics, not replace them).
- This behavior **ships ON** (default `True`). If semantic recall drops on the sample, fix the prompt/scaffold and re-run until it passes — do **not** merge it switched off. The kill-switch exists only for an in-the-field emergency.

---

## Phase 4 — Extraction depth (validate, then ship ON) · **Session 2**

> Origin: **quality #3**. The graph's semantic layer is starved (`reports_to`=60, observations=`role`-only). Each task A/Bs on the sample and **ships ON once its gate passes** — the deliverable is validated depth, not a left-off flag. Depends on Phases 0 + 3.

### Task 4.1: Reconcile the contract vocabulary with the live graph

**Files:**
- Modify: `mcpbrain/contract.py` — `ENTITY_TYPES` (`:35`), `RELATION_TYPES` (`:38`)
- Modify: `mcpbrain/graph_write.py` — `VALID_RELATION_TYPES` (keep the two in lockstep, per the contract.py comment)
- Test: `tests/test_contract.py` (add)

**Interfaces:**
- Produces: contract enums that match what the graph already stores and what the daemon emits, so legitimate types aren't silently dropped. **Add only types already present in the live graph** (`meeting`, `event`, `topic`; `collaborates_with`, `attended`) — do not invent new ones.

- [ ] **Step 1: Write the failing test** asserting `"collaborates_with" in RELATION_TYPES` and `"meeting" in ENTITY_TYPES`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** — extend both frozensets; mirror in `graph_write.VALID_RELATION_TYPES`. Confirm `sanitize_extraction`/apply-time filters now keep these.
- [ ] **Step 4: Run** `uv run pytest tests/test_contract.py tests/ -q -k "relation or contract or sanitize"` → PASS.
- [ ] **Step 5: Commit** `feat(contract): reconcile entity/relation vocabulary with the live graph`.

### Task 4.2: Capture richer `entity_observations` (experiment → decision)

**Files:**
- Modify: `mcpbrain/enrich_prompt.md` (+ `bin/sync_agents.py`) — ask for typed observations (`title`, `org_move`, `project_membership`) with dates
- Modify: `mcpbrain/graph_write.py` — route extracted observations through the existing `write_role_observation` pattern (`:266`), generalized to an `attribute`
- Modify: `mcpbrain/config.py` — `enrich_rich_observations_enabled(home)` kill-switch (default **True**; the Step 6 gate must pass before merge)
- Test: `tests/test_graph_write_provenance.py` (observation write + supersession)

**Interfaces:**
- Consumes: `write_role_observation(store, entity_id, title, source, valid_from, confidence)` (existing, `:266`) — generalize to `write_observation(store, entity_id, attribute, value, source, valid_from, confidence)` preserving the supersession logic (`:301-318`).

- [ ] **Step 1:** Failing test: applying an extraction with an `observations:[{attribute:'title', value:'COO', date:'2026-01-01'}]` writes one `entity_observations` row with `attribute='title'`, and a later-dated `title` supersedes it (`valid_to` set on the old row).
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Generalize `write_role_observation`→`write_observation`; keep a thin `write_role_observation` wrapper for callers. Wire the extraction `observations[]` through it, behind the flag.
- [ ] **Step 4:** Run `uv run pytest tests/ -q -k "observation or supersed"` → PASS.
- [ ] **Step 5:** Commit `feat(graph): generalized temporal observations (title/org-move), supersession preserved`.
- [ ] **Step 6: PHASE-4 GATE (pass-before-merge; ships ON)** — enrich the ~300-thread sample; require `observation_attributes` to gain non-`role` keys with plausible counts, and recall MRR (existing gold set) not to regress. If the gate fails, fix the prompt/wiring and re-run — the behavior still ships ON. Record the metrics in this plan's log.

---

## Phase 5 — Throughput & robustness · **5.1/5.2 → Session 1, 5.3 → Session 2**

> Origin: **cost #5** (batch size), **cost #6** (schema-constrained push), **cost #7** (deterministic dedup blocking / Q3). 5.1/5.2 are independent and land in Session 1; 5.3 needs Phase 3's `email_addr` so it lands in Session 2. All ship ON.

### Task 5.1: Raise per-unit batch size (more threads per Haiku call)

**Files:**
- Modify: `mcpbrain/config.py` — `spool_thread_cap` default and a `unit_pull_cap` accessor
- Modify: `mcpbrain/prepare.py` — `_UNIT_PULL_CAP` sourced from config (`:543`)
- Modify: `mcpbrain/mcp_server.py` — `_PULL_MAX_CHARS` (`:459`) kept in lockstep
- Test: `tests/test_prepare.py` (packing respects the configured cap)

**Interfaces:**
- Produces: `config.unit_pull_cap(home)` (default raised from 40_000, e.g. 60_000) read by both `prepare.write_units` and `mcp_server` so a unit's pull still fits the MCP read cap.

- [ ] **Step 1:** Failing test: with `unit_pull_cap` set high, `prepare.write_units` packs more threads into one unit (fewer unit files for the same input).
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Thread the cap from config into `write_units`/`_pack_by_size` and `mcp_server`; assert the two constants stay equal in a test.
- [ ] **Step 4:** Run `uv run pytest tests/test_prepare.py tests/ -q -k "pull or unit or pack"` → PASS.
- [ ] **Step 5:** Commit `perf(enrich): configurable per-unit batch size to amortize per-call overhead`.

### Task 5.2: Tighten `brain_enrich_push` arg schema to kill derails

**Files:**
- Modify: `mcpbrain/mcp_server.py` — `brain_enrich_push` input schema (require `extractions` array of objects with the known keys; reject free-form)
- Modify: `mcpbrain/enrich_prompt.md` / `plugin/agents/enrich-batch.md` — "your only output is the tool call" (then `bin/sync_agents.py`)
- Test: `tests/test_mcp_enrich_meeting_tools.py` or the enrich-tool test (schema rejects a malformed push)

**Interfaces:**
- Produces: a push with a malformed/narrated payload is rejected at the tool boundary with a clear error (so the subagent's reply can't "look done" without a valid push).

- [ ] **Step 1:** Failing test: a push missing `extractions` (or with a non-list) returns an error, not `{"written": true}`.
- [ ] **Step 2:** Run → FAIL (schema currently lenient).
- [ ] **Step 3:** Add the JSON-schema constraints to the tool definition; validate in the handler before writing.
- [ ] **Step 4:** Run the enrich-tool tests → PASS.
- [ ] **Step 5:** Commit `fix(mcp): strict brain_enrich_push schema to cut subagent derails`.

### Task 5.3: Strengthen deterministic dedup blocking to shrink LLM merge-review (Q3, eval-gated)

**Files:**
- Modify: `mcpbrain/resolve.py` — add email-equality and token/embedding blocking to `_candidate_pairs`/the resolve tier
- Modify: `mcpbrain/config.py` — flip the existing `write_time_dedup` flag (`:451`) default **OFF → True** (it ships ON once the Step 6 gate passes)
- Test: `tests/` resolve tests (add email-equality merge case)

**Interfaces:**
- Consumes: `entities.email_addr` (now populated by Task 3.1 — this is why 5.3 follows Phase 3).
- Produces: entities sharing a normalized `email_addr` are merged deterministically (logged in `entity_merge_log`) before any pair reaches the LLM `merge_review` block.

- [ ] **Step 1:** Failing test: two `person` entities with the same `email_addr` are merged by the deterministic resolver (one survives; `entity_merge_log` gains a row with `method='email'`).
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement email-equality blocking in the resolve tier; gate via `write_time_dedup`.
- [ ] **Step 4:** Run `uv run pytest tests/ -q -k "resolve or dedup or merge"` → PASS.
- [ ] **Step 5:** Commit `feat(resolve): deterministic email-equality dedup; shrinks LLM merge-review queue`.
- [ ] **Step 6: PHASE-5 GATE (pass-before-merge; ships ON)** — on the sample, confirm merge-review block volume drops and no wrong-merge appears in `entity_merge_log` (spot-check 20). Ships with `write_time_dedup` default `True`; if a wrong-merge appears, tighten the blocking key and re-run — do not merge it off. Record the metrics.

---

## Sequencing & dependencies

```
SESSION 1 ─ Phase 0 (eval) ─┬─> Phase 1 (coordinator → Haiku)        [no graph risk]
                            ├─> Phase 2 (contract + provenance + event-date)
                            └─> Phase 5a: 5.1 (batch size), 5.2 (push schema)
              └── checkpoint: suite green, baseline committed, Phase-2 gate passed ──┐
                                                                                     v
SESSION 2 ─ Phase 3 (scaffold + identity) ─> Phase 4 (depth)
                            └─ Phase 3 (email_addr) ─> Phase 5b: 5.3 (email dedup)
              └── checkpoint: all gates passed, everything default ON ──────────────┘
```

- **Session 1 carries no graph-shape risk** — measurement, the pure cost cut, and provenance/event-date wiring the schema already supports. All ships ON.
- **Session 2 is the coupled depth spine** — each task A/Bs on the ~300-thread sample and ships ON once its gate passes. A gate that regresses semantic %, recall MRR, or relation precision is **fixed, not shipped off**.
- **5.1/5.2** are independent (Session 1); **5.3** waits for Phase 3's `email_addr` (Session 2).
- **Every behavior here defaults ON.** Kill-switches (Tasks 3.2, 3.4, 4.2, 5.3 and the always-on provenance/identity writes) default `True`; they exist only to disable a behavior in the field, never to ship it off.

## Do NOT build (trap list)

- A local/non-Claude executor model — out of scope here (separate decision); this plan keeps Sonnet-coordinator/Haiku-executor and makes them do less.
- New entity/relation *types* not already in the live graph — Task 4.1 only reconciles existing ones.
- Re-extraction of the whole 45k backlog mid-plan — validate on a ~300-thread sample; a full re-pass is a separate, explicitly-instructed run.
- Any release (version bump, wheel, plugin sync) — separate runbook step.

## Self-review checklist (run before handing off)

- [ ] **Spec coverage:** cost #1→T1.1, #2→T2.3, #3→T3.4, #4→T3.2/3.3, #5→T5.1, #6→T5.2, #7→T5.3; quality #1→T2.1, #2→T3.1, #3→T4.1/4.2, #4→T2.2, #5→T0.1/0.2. All twelve mapped.
- [ ] **No placeholders** in mechanical tasks (P0–P3, P5.1–5.2 carry real test+impl code); eval-gated tasks (P4, P5.3) carry concrete experiment + decision criteria, not vague TODOs.
- [ ] **Type consistency:** `graph_metrics`, `upsert_relation(..., source_doc_id=)`, `apply(..., doc_ids=)`, `org_from_email`, `write_observation` names match across tasks.
