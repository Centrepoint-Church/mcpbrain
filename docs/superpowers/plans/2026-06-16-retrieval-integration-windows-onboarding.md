# Retrieval + Calendar Graph + Windows + Onboarding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface a normalised relevance `score` from hybrid retrieval (with tunable RRF fusion + a recall/MRR eval harness), write calendar attendees directly into the person graph at sync time (pure structured data, no LLM), add cross-platform Windows schtasks assertions, and remove the onboarding working-folder friction.

**Architecture:** Part 1 adds a `score` to each `hybrid_search` result (RRF fusion score normalised against the top hit) behind tunable fusion parameters, threads it through `brain_search`, and adds a deterministic fixture-backed eval (`tests/eval/`) that guards recall@k/MRR. Part 2 adds `_apply_attendees_to_graph(store, event, owner)` to `sync/calendar.py`, called after each event's chunk upsert in both `sync_calendar` and `backfill_calendar_window`; it upserts a person entity per external attendee and an idempotent `attended` relation owner→attendee, reusing `graph_write` primitives — no enrich queue, no LLM. Parts 3–4 are validation/documentation: a new `tests/test_agents_windows_xplat.py` asserting the win32 schtasks arg lists, a Windows section appended to `docs/RELEASE-RUNBOOK.md`, and a setup.py confirmation (the working folder is already auto-created and resolvable, so onboarding is documentation + an optional path echo).

**Tech Stack:** Python 3.12, sqlite-vec, FTS5, pytest, ruff. Tests: `uv run pytest`; lint: `uv run ruff check mcpbrain/`.

**Worktree & Dependencies:** This worktree OWNS and edits only: `mcpbrain/retrieval.py`, `mcpbrain/mcp_server.py`, `mcpbrain/sync/calendar.py`, `mcpbrain/setup.py`, new `tests/eval/*` files, new `tests/test_calendar_graph.py`, and the NEW file `tests/test_agents_windows_xplat.py`. It does NOT touch `plugin/skills/install/SKILL.md` (the onboarding copy is delegated to Spec 1) and does NOT extend `tests/test_agents_cadence_xplat.py` (Spec 1's file — Windows asserts live in the new file to avoid collision). It depends on NO other spec's new code; every part builds against the current 0.0.6 codebase. The 4 parts can be built in the spec's stated order: (1) Retrieval scores + eval, (2) Calendar→person, (3) Onboarding path echo, (4) Windows validation. Create an isolated worktree via superpowers:using-git-worktrees at execution.

---

## Verified codebase facts (re-read before coding)

- `mcpbrain/retrieval.py`: `_rrf(rankings, k=60) -> dict[str,float]`; `hybrid_search(store, embedder, query, limit=10) -> list[dict]` builds `sem = [d for d,_ in store.vec_knn(qv, limit*2)]`, `kw = [d for d,_ in store.fts_search(query, limit*2)]`, `fused = _rrf([sem, kw])`, sorts desc, calls `store.get_chunk(d)` (dict with `doc_id`/`text`/`metadata`), skips `meta.get("expired")`, appends until `limit`. **No score on results today.**
- `mcpbrain/mcp_server.py`: `make_brain_search(store, embedder)` → `async def brain_search(query, limit=10)` returns `hybrid_search(...)` directly (line ~86), wrapped in try/except returning `[]`.
- `mcpbrain/store.py`: `Store(path, dim, read_only=False)`; `.init()`; `upsert_chunk(doc_id, text, content_hash, metadata)`; `get_chunk(doc_id) -> {"doc_id","text","metadata"}`; `vec_knn(qv, k) -> list[(doc_id, distance)]`; `fts_search(query, k) -> list[(doc_id, rank)]`; `find_entity(query) -> dict|None`; `get_entity`.
- `mcpbrain/graph_write.py` (EXACT signatures — keyword-only args):
  - `upsert_entity(store, *, name, entity_type, org="", email_addr="", aliases="", notes="", taxonomy=None) -> str|None`
  - `upsert_relation(store, entity_a, relation, entity_b, *, valid_from, evidence="", confidence=1.0, strength=1, source_doc_id=None) -> int`
  - `is_junk_entity(name, entity_type) -> bool` (only acts on `"person"`/`"org"`)
  - `_is_owner(name, owner) -> bool`; `owner_identity_from_config() -> OwnerIdentity`; `OwnerIdentity(name, entity_id, aliases)`
  - `"attended"` is already in `ACCUMULATING_RELATIONS` (re-observation bumps the existing row, never duplicates; `degree` only increments on a NEW row → idempotent on re-sync).
- `mcpbrain/sync/calendar.py`: `normalise_calendar(event)` builds attendee text from `a.get("displayName") or a.get("email","")`; `sync_calendar(...)` and `backfill_calendar_window(...)` loop events and `store.upsert_chunk(...)` per chunk.
- `mcpbrain/agents.py`: `schtasks_args(*, mcpbrain_bin, home)`, `schtasks_tray_args(*, mcpbrain_bin, home)`, `prune_schtasks_args(*, mcpbrain_bin)`, `health_schtasks_args(*, mcpbrain_bin)`, `_cadence_schtasks_args(...)`. Daemon/tray use `/sc onlogon` with an embedded `cmd /c "set MCPBRAIN_HOME=... && <bin> <subcommand>"` action; cadences use `/sc daily|weekly`.
- `mcpbrain/setup.py`: `home = str(app_dir())`; `config.app_dir()` ALREADY does `d.mkdir(parents=True, exist_ok=True)` and returns the absolute path → the Cowork working folder is auto-created and resolvable today. Onboarding is documentation + (optional) an explicit path echo.
- `tests/eval/` already exists (`test_retrieval_quality.py`, fixtures `golden.json`, `baselines/`). New eval files sit ALONGSIDE these without modifying them.

---

# Part 1 — Retrieval: surfaced scores + tuned fusion (#4)

## Task 1.1 — Parameterise fusion + surface a normalised score on `hybrid_search`

- [ ] **Write failing test.** Append to `tests/test_retrieval.py` (reuses the existing `FakeEmbedder` and `_seed` helper at the top of that file):

```python
def test_hybrid_search_results_carry_normalised_score(tmp_path):
    s = _seed(tmp_path)
    results = hybrid_search(s, FakeEmbedder(), "budget", limit=2)
    assert results, "expected at least one hit"
    # Every result carries a float score in (0, 1].
    for r in results:
        assert "score" in r
        assert isinstance(r["score"], float)
        assert 0.0 < r["score"] <= 1.0
    # Normalisation: the top result's score is exactly 1.0.
    assert results[0]["score"] == 1.0
    # Scores are monotonically non-increasing (results stay rank-ordered).
    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True)


def test_hybrid_search_score_is_stable_when_single_hit(tmp_path):
    """A single-hit result set must not divide-by-zero; its score is 1.0."""
    s = Store(tmp_path / "one.sqlite3", dim=4)
    s.init()
    s.upsert_chunk("only", "the annual budget review", "h1", {})
    from mcpbrain.index import index_pending
    index_pending(s, FakeEmbedder())
    results = hybrid_search(s, FakeEmbedder(), "budget", limit=5)
    assert results[0]["score"] == 1.0


def test_rrf_weighting_is_tunable(tmp_path):
    """vec_weight / kw_weight scale each ranker's RRF contribution."""
    from mcpbrain.retrieval import _rrf
    sem = ["a", "b"]
    kw = ["b", "a"]
    base = _rrf([sem, kw])
    weighted = _rrf([sem, kw], vec_weight=2.0, kw_weight=0.0)
    # With kw zeroed, ordering follows the semantic ranking only.
    assert weighted["a"] > weighted["b"]
    # Base (equal weights) ties a and b (each appears once at rank 0 and once at rank 1).
    assert base["a"] == base["b"]
```

- [ ] **Run (expect FAIL):** `uv run pytest tests/test_retrieval.py -k "score or weighting" -q` — fails: results have no `score`, `_rrf` rejects `vec_weight`/`kw_weight`.

- [ ] **Implement minimally** in `mcpbrain/retrieval.py`. Replace `_rrf` and `hybrid_search`:

```python
# Default RRF constant and per-ranker fusion weights. Tunable via the eval
# harness (see tests/eval/run_eval.py). Equal weights = the historical
# behaviour; vec_weight/kw_weight scale each ranker's contribution before sum.
_RRF_K = 60
_VEC_WEIGHT = 1.0
_KW_WEIGHT = 1.0


def _rrf(rankings: list[list[str]], k: int = _RRF_K,
         vec_weight: float = _VEC_WEIGHT,
         kw_weight: float = _KW_WEIGHT) -> dict[str, float]:
    """Weighted Reciprocal Rank Fusion.

    rankings is [semantic_ranking, keyword_ranking] (the order hybrid_search
    passes). The two weights scale each ranker's reciprocal-rank contribution
    so the fusion can be tuned without changing call sites. A missing third+
    ranking falls back to weight 1.0.
    """
    weights = [vec_weight, kw_weight]
    scores: dict[str, float] = {}
    for idx, ranking in enumerate(rankings):
        w = weights[idx] if idx < len(weights) else 1.0
        for rank, doc_id in enumerate(ranking):
            scores[doc_id] = scores.get(doc_id, 0.0) + w / (k + rank + 1)
    return scores


def hybrid_search(store, embedder, query: str, limit: int = 10, *,
                  rrf_k: int = _RRF_K, vec_weight: float = _VEC_WEIGHT,
                  kw_weight: float = _KW_WEIGHT) -> list[dict]:
    qv = embedder.embed_query(query)
    sem = [d for d, _ in store.vec_knn(qv, limit * 2)]
    kw = [d for d, _ in store.fts_search(query, limit * 2)]
    fused = _rrf([sem, kw], k=rrf_k, vec_weight=vec_weight, kw_weight=kw_weight)
    ordered = sorted(fused, key=lambda d: -fused[d])
    # Normalise against the top fused score so the strongest hit is 1.0 and
    # callers can compare relevance across queries. Computed over the FULL
    # fused set (before expiry filtering) so dropping an expired top hit does
    # not silently rescale the survivors.
    top = fused[ordered[0]] if ordered else 0.0
    results = []
    for d in ordered:
        c = store.get_chunk(d)
        if not c:
            continue
        meta = c.get("metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        if meta.get("expired"):
            continue
        c["score"] = (fused[d] / top) if top > 0 else 0.0
        results.append(c)
        if len(results) == limit:
            break
    return results
```

- [ ] **Run (expect PASS):** `uv run pytest tests/test_retrieval.py -q` — all existing retrieval tests + the new ones pass. (The existing `test_hybrid_search_skips_expired_notes` and the quality gate `tests/eval/test_retrieval_quality.py` must remain green; they read `r["doc_id"]` and are unaffected by the added key.)
- [ ] **Lint:** `uv run ruff check mcpbrain/retrieval.py`
- [ ] **Commit:** `feat(retrieval): surface normalised score + tunable RRF weighting`

## Task 1.2 — Surface `score` through `brain_search`

- [ ] **Write failing test.** New file `tests/test_brain_search_score.py`:

```python
import asyncio

from mcpbrain.store import Store
from mcpbrain.mcp_server import make_brain_search


class FakeEmbedder:
    dim = 4

    def embed_passages(self, texts):
        return [[1.0, 0, 0, 0] if "budget" in t else [0, 1.0, 0, 0] for t in texts]

    def embed_query(self, text):
        return [1.0, 0, 0, 0] if "budget" in text else [0, 1.0, 0, 0]


def _seed(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    s.upsert_chunk("d-budget", "the annual budget review", "h1", {})
    s.upsert_chunk("d-roster", "the volunteer roster", "h2", {})
    from mcpbrain.index import index_pending
    index_pending(s, FakeEmbedder())
    return s


def test_brain_search_passes_score_through(tmp_path):
    s = _seed(tmp_path)
    search = make_brain_search(s, FakeEmbedder())
    results = asyncio.run(search("budget", 5))
    assert results, "expected hits"
    assert all("score" in r for r in results)
    assert results[0]["score"] == 1.0
```

- [ ] **Run (expect FAIL or PASS):** `uv run pytest tests/test_brain_search_score.py -q`. NOTE: `brain_search` returns the `hybrid_search` list verbatim, so after Task 1.1 the `score` key already flows through. If this test PASSES immediately, `make_brain_search` needs no change — record that in the commit body and move on (do NOT add redundant shaping). If `brain_search` re-shapes results into a subset of keys (re-read lines ~83–90 and ~355–395 of `mcp_server.py` to confirm), add `"score": r["score"]` to the shaped dict so it survives. Either way the test must end GREEN.
- [ ] **Run (expect PASS):** `uv run pytest tests/test_brain_search_score.py tests/test_retrieval.py -q`
- [ ] **Lint:** `uv run ruff check mcpbrain/`
- [ ] **Commit:** `feat(mcp): surface retrieval score through brain_search`

## Task 1.3 — Eval set + harness (recall@k + MRR)

- [ ] **Create the eval fixture** `tests/eval/retrieval_eval.jsonl` — a small, deterministic, hand-authored set. Each line: `{"query": "...", "expected_doc_ids": ["..."]}`. Keep it ~12 lines so it runs fast and stays reviewable. The harness builds its OWN fixture store (below) whose doc_ids these reference. Example (author the full set to cover semantic paraphrase + exact-keyword + multi-relevant cases):

```jsonl
{"query": "annual budget planning", "expected_doc_ids": ["doc-budget"]}
{"query": "money for next year", "expected_doc_ids": ["doc-budget"]}
{"query": "volunteer roster", "expected_doc_ids": ["doc-roster"]}
{"query": "who is serving on Sunday", "expected_doc_ids": ["doc-roster"]}
{"query": "youth camp logistics", "expected_doc_ids": ["doc-camp"]}
{"query": "summer retreat for teenagers", "expected_doc_ids": ["doc-camp"]}
{"query": "building maintenance request", "expected_doc_ids": ["doc-facilities"]}
{"query": "air conditioning broken in the hall", "expected_doc_ids": ["doc-facilities"]}
{"query": "staff meeting agenda", "expected_doc_ids": ["doc-staffmtg"]}
{"query": "leadership team gathering notes", "expected_doc_ids": ["doc-staffmtg"]}
{"query": "child safety policy", "expected_doc_ids": ["doc-safeguarding"]}
{"query": "safeguarding checks for kids ministry", "expected_doc_ids": ["doc-safeguarding"]}
```

- [ ] **Create the harness** `tests/eval/run_eval.py`. It builds a deterministic real-embedder fixture store keyed to the doc_ids above, runs `hybrid_search`, and reports recall@k + MRR. It is importable (so the regression test reuses it) and runnable as a script (so fusion can be swept).

```python
"""Retrieval eval harness: recall@k + MRR over tests/eval/retrieval_eval.jsonl.

Builds a small deterministic fixture store (real bge-small embeddings), runs
hybrid_search per query, and reports recall@k and MRR. Importable for the
regression test (run_eval) and runnable as a script to sweep fusion params:

    uv run python tests/eval/run_eval.py
    uv run python tests/eval/run_eval.py --rrf-k 30 --vec-weight 1.5 --kw-weight 1.0
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
EVAL = HERE / "retrieval_eval.jsonl"

# The fixture corpus. doc_ids MUST match expected_doc_ids in the jsonl. Text is
# deliberately varied so semantic paraphrase queries exercise the vector path
# and exact-term queries exercise FTS. Distractors stress precision.
FIXTURE_DOCS = {
    "doc-budget": "The annual budget review covers next year's ministry finances and spending plan.",
    "doc-roster": "Volunteer roster for Sunday services: who is serving on welcome, kids, and worship.",
    "doc-camp": "Youth summer camp logistics for the teenagers' retreat: transport, food, cabins.",
    "doc-facilities": "Building maintenance request: the air conditioning in the main hall is broken.",
    "doc-staffmtg": "Staff meeting agenda and leadership team gathering notes for this week.",
    "doc-safeguarding": "Child safety and safeguarding policy: background checks for kids ministry volunteers.",
    "doc-distract-1": "Coffee order for the cafe and the weekly grocery shopping list.",
    "doc-distract-2": "Car park resurfacing quote from the contractor for the south lot.",
}


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def build_fixture_store(tmp_dir: Path):
    """Return (store, embedder) seeded with FIXTURE_DOCS and fully indexed."""
    from mcpbrain.embed import get_embedder
    from mcpbrain.index import index_pending
    from mcpbrain.store import Store

    emb = get_embedder("bge-small")
    store = Store(tmp_dir / "eval.sqlite3", dim=emb.dim)
    store.init()
    for doc_id, text in FIXTURE_DOCS.items():
        store.upsert_chunk(doc_id, text, _hash(text), {})
    index_pending(store, emb)
    return store, emb


def load_cases() -> list[dict]:
    return [json.loads(line) for line in EVAL.read_text().splitlines() if line.strip()]


def run_eval(store, embedder, *, k: int = 5, rrf_k: int = 60,
             vec_weight: float = 1.0, kw_weight: float = 1.0) -> dict:
    """Return {"recall_at_k": float, "mrr": float, "k": k} over the eval set."""
    from mcpbrain.retrieval import hybrid_search

    cases = load_cases()
    recalls: list[float] = []
    rrs: list[float] = []
    for case in cases:
        expected = set(case["expected_doc_ids"])
        results = hybrid_search(store, embedder, case["query"], limit=k,
                                rrf_k=rrf_k, vec_weight=vec_weight, kw_weight=kw_weight)
        retrieved = [r["doc_id"] for r in results]
        hits = set(retrieved[:k]) & expected
        recalls.append(len(hits) / len(expected) if expected else 0.0)
        rr = 0.0
        for i, doc_id in enumerate(retrieved):
            if doc_id in expected:
                rr = 1.0 / (i + 1)
                break
        rrs.append(rr)
    n = len(cases) or 1
    return {"recall_at_k": sum(recalls) / n, "mrr": sum(rrs) / n, "k": k}


def main() -> None:
    import tempfile

    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--rrf-k", type=int, default=60)
    ap.add_argument("--vec-weight", type=float, default=1.0)
    ap.add_argument("--kw-weight", type=float, default=1.0)
    args = ap.parse_args()
    with tempfile.TemporaryDirectory() as td:
        store, emb = build_fixture_store(Path(td))
        m = run_eval(store, emb, k=args.k, rrf_k=args.rrf_k,
                     vec_weight=args.vec_weight, kw_weight=args.kw_weight)
        print(f"recall@{m['k']}={m['recall_at_k']:.3f}  MRR={m['mrr']:.3f}  "
              f"(rrf_k={args.rrf_k}, vec_weight={args.vec_weight}, kw_weight={args.kw_weight})")


if __name__ == "__main__":
    main()
```

- [ ] **Write the regression test** `tests/eval/test_eval_baseline.py`:

```python
"""Regression floor for retrieval quality over the hand-authored eval set.

Builds the fixture store once (module scope; loads bge-small once) and asserts
recall@5 and MRR stay above a conservative floor. The floor is intentionally
loose — it catches a fusion/scoring regression, not normal noise.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.eval.run_eval import build_fixture_store, run_eval

RECALL_FLOOR = 0.80
MRR_FLOOR = 0.70


@pytest.fixture(scope="module")
def fixture_store(tmp_path_factory):
    return build_fixture_store(tmp_path_factory.mktemp("eval_store"))


def test_recall_and_mrr_above_floor(fixture_store):
    store, emb = fixture_store
    m = run_eval(store, emb, k=5)
    print(f"\nretrieval eval: recall@5={m['recall_at_k']:.3f}  MRR={m['mrr']:.3f}")
    assert m["recall_at_k"] >= RECALL_FLOOR, (
        f"recall@5 {m['recall_at_k']:.3f} below floor {RECALL_FLOOR}")
    assert m["mrr"] >= MRR_FLOOR, (
        f"MRR {m['mrr']:.3f} below floor {MRR_FLOOR}")
```

- [ ] **Run (expect FAIL then calibrate):** `uv run pytest tests/eval/test_eval_baseline.py -q -s`. If recall/MRR land below the floors, FIRST sweep fusion to find the best deterministic setting:
  `uv run python tests/eval/run_eval.py` and a small grid, e.g.
  `for k in 30 60 90; do uv run python tests/eval/run_eval.py --rrf-k $k; done` and
  `uv run python tests/eval/run_eval.py --vec-weight 1.5 --kw-weight 1.0`.
  Pick the `(rrf_k, vec_weight, kw_weight)` that maximises recall@5 then MRR. If a non-default setting wins clearly, update the `_RRF_K`/`_VEC_WEIGHT`/`_KW_WEIGHT` module defaults in `retrieval.py` to that setting and re-run Task 1.1's tests. THEN set `RECALL_FLOOR`/`MRR_FLOOR` ~0.05 below the achieved numbers so the test is a true regression guard, not a tautology. Record the before/after numbers and the chosen params in the commit body.
- [ ] **Run (expect PASS):** `uv run pytest tests/eval/ tests/test_retrieval.py -q`
- [ ] **Lint:** `uv run ruff check mcpbrain/ tests/eval/`
- [ ] **Commit:** `test(eval): retrieval eval set + recall/MRR regression floor; record tuned fusion`

---

# Part 2 — Calendar attendees → person-entity context (#7)

## Task 2.1 — `_apply_attendees_to_graph`: external attendees become person entities + `attended` relations

- [ ] **Write failing test.** New file `tests/test_calendar_graph.py`. Uses a real `Store` (no embedder needed — graph writes are pure SQL) and a configured owner identity injected directly (avoids depending on `config.json`):

```python
"""Calendar attendees -> person graph (pure structured-data writes, no LLM)."""

from mcpbrain.graph_write import OwnerIdentity
from mcpbrain.store import Store
from mcpbrain.sync.calendar import _apply_attendees_to_graph


def _store(tmp_path):
    s = Store(tmp_path / "cal.sqlite3", dim=4)
    s.init()
    return s


# Owner is "Josh" at josh@centrepoint.church. Aliases are lowercased per
# OwnerIdentity contract; entity_id is the owner's slug (never upserted).
_OWNER = OwnerIdentity(
    name="Josh",
    entity_id="josh-kemp",
    aliases=frozenset({"josh", "josh kemp", "josh.k@centrepoint.church"}),
)


def _event(eid, attendees, start="2026-06-01T09:00:00Z"):
    return {
        "id": eid,
        "summary": "Project sync",
        "status": "confirmed",
        "start": {"dateTime": start},
        "end": {"dateTime": "2026-06-01T10:00:00Z"},
        "attendees": attendees,
    }


def test_two_external_attendees_create_entities_and_attended_relations(tmp_path):
    s = _store(tmp_path)
    ev = _event("evt1", [
        {"displayName": "Sam Chen", "email": "sam@partner.org"},
        {"displayName": "Dana Lee", "email": "dana@other.org"},
    ])
    n = _apply_attendees_to_graph(s, ev, _OWNER)
    assert n == 2

    sam = s.find_entity("sam@partner.org") or s.find_entity("Sam Chen")
    dana = s.find_entity("dana@other.org") or s.find_entity("Dana Lee")
    assert sam is not None and dana is not None
    assert sam["type"] == "person" and dana["type"] == "person"

    with s._connect() as db:
        rows = db.execute(
            "SELECT entity_a, relation, entity_b FROM entity_relations "
            "WHERE relation = 'attended' AND invalidated_at IS NULL").fetchall()
    pairs = {(r["entity_a"], r["entity_b"]) for r in rows}
    assert (_OWNER.entity_id, sam["id"]) in pairs
    assert (_OWNER.entity_id, dana["id"]) in pairs


def test_owner_self_attendee_is_excluded(tmp_path):
    s = _store(tmp_path)
    ev = _event("evt2", [
        {"displayName": "Josh", "email": "josh.k@centrepoint.church"},
        {"displayName": "Sam Chen", "email": "sam@partner.org"},
    ])
    n = _apply_attendees_to_graph(s, ev, _OWNER)
    assert n == 1  # only Sam
    assert s.find_entity("josh-kemp") is None


def test_junk_role_attendee_is_excluded(tmp_path):
    s = _store(tmp_path)
    # A long room-resource name is junk by is_junk_entity (>60 chars or bracket
    # chars); a bracketed resource name trips the structural junk patterns.
    ev = _event("evt3", [
        {"displayName": "Conference Room A [resource]", "email": "room-a@resource.calendar.google.com"},
        {"displayName": "Sam Chen", "email": "sam@partner.org"},
    ])
    n = _apply_attendees_to_graph(s, ev, _OWNER)
    assert n == 1
    assert s.find_entity("Sam Chen") is not None


def test_attendee_with_no_email_uses_display_name(tmp_path):
    s = _store(tmp_path)
    ev = _event("evt4", [{"displayName": "Pat Morgan"}])
    n = _apply_attendees_to_graph(s, ev, _OWNER)
    assert n == 1
    assert s.find_entity("Pat Morgan") is not None


def test_resync_same_event_is_idempotent(tmp_path):
    s = _store(tmp_path)
    ev = _event("evt5", [{"displayName": "Sam Chen", "email": "sam@partner.org"}])
    _apply_attendees_to_graph(s, ev, _OWNER)
    _apply_attendees_to_graph(s, ev, _OWNER)  # re-sync

    with s._connect() as db:
        ent_count = db.execute(
            "SELECT COUNT(*) c FROM entities WHERE type='person'").fetchone()["c"]
        rel_count = db.execute(
            "SELECT COUNT(*) c FROM entity_relations "
            "WHERE relation='attended' AND invalidated_at IS NULL").fetchone()["c"]
    assert ent_count == 1
    assert rel_count == 1
```

- [ ] **Run (expect FAIL):** `uv run pytest tests/test_calendar_graph.py -q` — fails: `_apply_attendees_to_graph` does not exist.

- [ ] **Implement** in `mcpbrain/sync/calendar.py`. Add imports at the top:

```python
from mcpbrain.graph_write import (
    is_junk_entity,
    upsert_entity,
    upsert_relation,
    _is_owner,
    owner_identity_from_config,
)
```

  Then add the function (place it after `normalise_calendar`):

```python
def _attendee_valid_from(event: dict) -> str:
    """YYYY-MM-DD for the event's start (the date the meeting was attended).

    Uses start.date or the date portion of start.dateTime; falls back to UTC
    today so a malformed/floating event still produces a valid bi-temporal
    valid_from (upsert_relation rejects an empty valid_from).
    """
    start = (event.get("start") or {})
    raw = start.get("dateTime") or start.get("date") or ""
    if raw[:10] and raw[4:5] == "-":
        return raw[:10]
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _apply_attendees_to_graph(store, event: dict, owner) -> int:
    """Write each external attendee as a person entity + an `attended` relation
    from the owner to that attendee. Pure structured-data: no LLM, no enrich.

    - Excludes the owner/self (by name aliases AND by email match).
    - Filters junk/role names via graph_write.is_junk_entity.
    - Idempotent on re-sync: upsert_entity dedups by email/name; upsert_relation
      bumps the existing `attended` row (accumulating relation) rather than
      duplicating it.

    Returns the number of attendees written (entities upserted).
    """
    attendees = event.get("attendees") or []
    if not attendees:
        return 0

    owner_email = ""
    for a in owner.aliases:
        if "@" in a:
            owner_email = a
            break

    valid_from = _attendee_valid_from(event)
    event_id = event.get("id", "")
    written = 0
    for a in attendees:
        email_addr = (a.get("email") or "").strip().lower()
        name = (a.get("displayName") or a.get("email") or "").strip()
        if not name:
            continue
        # Self-exclusion: by configured name/alias, or by owner email.
        if _is_owner(name, owner):
            continue
        if owner_email and email_addr == owner_email:
            continue
        # Skip room resources / junk names. Google marks rooms with
        # resource=True; treat that as junk regardless of the display name.
        if a.get("resource") is True:
            continue
        if is_junk_entity(name, "person"):
            continue

        entity_id = upsert_entity(
            store, name=name, entity_type="person", email_addr=email_addr)
        if not entity_id or entity_id == owner.entity_id:
            continue

        upsert_relation(
            store, owner.entity_id, "attended", entity_id,
            valid_from=valid_from,
            evidence=f"cal-{event_id}" if event_id else "",
            source_doc_id=f"cal-{event_id}" if event_id else None)
        written += 1
    return written
```

- [ ] **Run (expect PASS):** `uv run pytest tests/test_calendar_graph.py -q`
- [ ] **Lint:** `uv run ruff check mcpbrain/sync/calendar.py` (the `_is_owner`/`owner_identity_from_config` imports — keep only what is used; if `owner_identity_from_config` is unused until Task 2.2, import it there instead to avoid an F401).
- [ ] **Commit:** `feat(calendar): attendees -> person entities + attended relations (no LLM)`

## Task 2.2 — Wire `_apply_attendees_to_graph` into `sync_calendar` and `backfill_calendar_window`

- [ ] **Write failing test.** Append to `tests/test_calendar_graph.py` (drives the public sync entry point with the existing fake-service style from `test_calendar_sync.py`, but asserting the graph side-effect). Patch the owner identity so the test does not depend on `config.json`:

```python
def test_sync_calendar_applies_attendees_to_graph(tmp_path, monkeypatch):
    import mcpbrain.sync.calendar as calmod
    from tests.test_calendar_sync import FakeCalService, _event as _sync_event, _resp

    monkeypatch.setattr(calmod, "owner_identity_from_config", lambda: _OWNER)

    s = _store(tmp_path)
    ev = _sync_event("evtX", "Project sync", attendees=[
        {"displayName": "Sam Chen", "email": "sam@partner.org"},
    ])
    svc = FakeCalService(on_full=_resp([ev], next_sync_token="tok1"))

    result = calmod.sync_calendar(svc, s)
    assert result == 1
    # Chunk still written (unchanged behaviour) ...
    assert s.get_chunk("cal-evtX") is not None
    # ... AND the attendee is now in the graph.
    assert s.find_entity("Sam Chen") is not None
    with s._connect() as db:
        rel = db.execute(
            "SELECT COUNT(*) c FROM entity_relations WHERE relation='attended'"
        ).fetchone()["c"]
    assert rel == 1
```

- [ ] **Run (expect FAIL):** `uv run pytest tests/test_calendar_graph.py -k sync_calendar_applies -q` — fails: sync does not call the graph writer yet.

- [ ] **Implement.** In `mcpbrain/sync/calendar.py`, resolve the owner once per sync and call the writer right after each event's chunk upsert.

  In `sync_calendar`, after `cursor = store.get_cursor(source)` (or just before the event loop) add:
```python
    owner = owner_identity_from_config()
```
  Then in the event loop, after the `for ch in chunks: store.upsert_chunk(...)` block and the `if chunks: count += 1`, add the graph write (only for events that produced a chunk, i.e. non-cancelled):
```python
        if chunks:
            count += 1
            _apply_attendees_to_graph(store, ev, owner)
```

  Mirror the same change in `backfill_calendar_window`: resolve `owner = owner_identity_from_config()` before its loop, and call `_apply_attendees_to_graph(store, ev, owner)` inside the `if chunks:` branch.

  (Keep the `owner_identity_from_config` import from Task 2.1 — it is now used here.)

- [ ] **Run (expect PASS):** `uv run pytest tests/test_calendar_graph.py tests/test_calendar_sync.py -q` — the new graph wiring passes AND the existing calendar sync tests (cursor advance, 410 fallback, cancelled-skip) stay green. Cancelled events return `[]` from `normalise_calendar`, so `if chunks:` is false and the graph writer never runs on them — verify `test_cancelled_event_skipped` still passes.
- [ ] **Lint:** `uv run ruff check mcpbrain/sync/calendar.py`
- [ ] **Commit:** `feat(calendar): apply attendee graph writes in sync + backfill`

---

# Part 3 — Onboarding: working-folder existence guarantee + path echo (#9)

> **Delegation note (no code in `install/SKILL.md`):** Per the spec, ALL edits to `plugin/skills/install/SKILL.md` — including the onboarding "create the My Brain project" copy — are owned by Spec 1's worktree to keep the two parallel sessions collision-free. This worktree contributes only the setup.py path-echo (below) and the onboarding paragraph *content* (handed to Spec 1, not landed here). Do NOT edit `install/SKILL.md` in this worktree.
>
> **The working folder already exists.** `config.app_dir()` (called as `home = str(app_dir())` in `setup.py:main`) already does `d.mkdir(parents=True, exist_ok=True)` and returns the absolute path. So the existence guarantee the spec asks for is already satisfied by setup. The only gap is that setup does not print the path the user must paste into the Cowork project. Task 3.1 closes that — a one-line echo, no new directory or location invented.

## Task 3.1 — Echo the resolved working-folder path in `mcpbrain setup`

- [ ] **Write failing test.** New file `tests/test_setup_path_echo.py`:

```python
"""mcpbrain setup --dry-run prints the resolved Cowork working-folder path."""

from mcpbrain import setup


def test_setup_dry_run_echoes_working_folder(monkeypatch, tmp_path, capsys):
    home = tmp_path / "mcpbrain-home"
    monkeypatch.setattr(setup, "app_dir", lambda: home)
    # Make _ensure_daemon_running a no-op that yields a port, so --dry-run
    # reaches the echo without touching the daemon/browser.
    monkeypatch.setattr(setup, "_ensure_daemon_running", lambda h, dry_run=False: 8765)

    rc = setup.main(["--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert str(home) in out
    assert "working folder" in out.lower()
```

- [ ] **Run (expect FAIL):** `uv run pytest tests/test_setup_path_echo.py -q` — fails: no working-folder line is printed (and `--dry-run` returns before the echo).

- [ ] **Implement** in `mcpbrain/setup.py`. The working-folder echo must print on BOTH the `--dry-run` and the normal path, so place it right after `home = str(app_dir())` / before the dry-run early return. Edit `main`:

```python
    home = str(app_dir())
    # The Cowork "My Brain" project's working folder is mcpbrain home — already
    # created by app_dir(). Echo the absolute path so the user pastes a
    # known-good folder into the (manual) Cowork project setup rather than
    # browsing for it. Project creation itself is a manual Cowork step by design.
    print(f"Your Cowork project working folder is: {home}")

    port = _ensure_daemon_running(home, dry_run=args.dry_run)
    url = f"http://127.0.0.1:{port}/"

    if args.dry_run:
        print(f"would open {url}")
        return 0
```

- [ ] **Run (expect PASS):** `uv run pytest tests/test_setup_path_echo.py -q`
- [ ] **Lint:** `uv run ruff check mcpbrain/setup.py`
- [ ] **Commit:** `feat(setup): echo resolved Cowork working-folder path on setup`

## Task 3.2 — Hand the onboarding paragraph to Spec 1 (documentation only, no file edit here)

- [ ] **No code.** Record the exact onboarding copy for Spec 1 to land in `install/SKILL.md`. Add it to this worktree's PR/merge description (NOT to any tracked file in `install/`). Suggested paragraph content for Spec 1:
  - **Project name:** `My Brain`
  - **Working folder:** the path printed by `mcpbrain setup` ("Your Cowork project working folder is: …") — paste it verbatim; do not browse for a folder.
  - **Instructions block:** the agent-facing instructions for the My Brain project (carried by Spec 1's existing onboarding copy).
  - **Manual-reality note:** "Creating the Cowork project is a manual step in the desktop app by design — plugins cannot register a project. This is settled; do not re-investigate an auto-create path."
- [ ] **Verify** no file under `plugin/skills/install/` was modified by this worktree: `git -C <worktree> status --porcelain plugin/skills/install/` returns empty.

---

# Part 4 — Windows parity validation (#8)

## Task 4.1 — `tests/test_agents_windows_xplat.py`: assert win32 schtasks arg lists are well-formed

- [ ] **Write failing test.** New file `tests/test_agents_windows_xplat.py` (mirrors the assertion style of `tests/test_agents_cadence_xplat.py` but is a SEPARATE file — never extend the cadence file). Covers daemon, tray, prune, health:

```python
"""Cross-platform Windows schtasks generators produce well-formed arg lists.

Lives in its own file (NOT test_agents_cadence_xplat.py) so this worktree and
Spec 1 never collide. The live Windows validation is the manual runbook gate in
docs/RELEASE-RUNBOOK.md; these assertions exercise the pure generators in CI.
"""
from mcpbrain import agents


def _flag_value(args, flag):
    """Return the token immediately after `flag` in the arg list, or None."""
    for i, tok in enumerate(args):
        if tok == flag and i + 1 < len(args):
            return args[i + 1]
    return None


def test_daemon_schtasks_args_well_formed():
    a = agents.schtasks_args(mcpbrain_bin=r"C:\Tools\mcpbrain.exe", home=r"C:\Users\jo\mcpbrain")
    assert a[0] == "schtasks"
    assert "/create" in a and "/f" in a
    assert _flag_value(a, "/tn") == "mcpbrain"
    assert _flag_value(a, "/sc") == "onlogon"
    action = _flag_value(a, "/tr")
    assert action is not None
    # The daemon subcommand and the embedded home both appear in the action.
    assert "daemon" in action
    assert r"C:\Users\jo\mcpbrain" in action
    assert "MCPBRAIN_HOME" in action


def test_tray_schtasks_args_well_formed():
    a = agents.schtasks_tray_args(mcpbrain_bin=r"C:\Tools\mcpbrain.exe", home=r"C:\Users\jo\mcpbrain")
    assert a[0] == "schtasks"
    assert _flag_value(a, "/tn") == "mcpbrain-tray"
    assert _flag_value(a, "/sc") == "onlogon"
    assert "tray" in _flag_value(a, "/tr")


def test_schtasks_args_quote_paths_with_spaces():
    a = agents.schtasks_args(
        mcpbrain_bin=r"C:\Program Files\mcpbrain\mcpbrain.exe",
        home=r"C:\Users\Jo Smith\mcpbrain")
    action = _flag_value(a, "/tr")
    # Both the binary and the home (each containing a space) are quoted in the
    # embedded cmd action so schtasks parses them as single tokens.
    assert r'"C:\Program Files\mcpbrain\mcpbrain.exe"' in action
    assert r'"C:\Users\Jo Smith\mcpbrain"' in action


def test_prune_schtasks_args_well_formed():
    a = agents.prune_schtasks_args(mcpbrain_bin=r"C:\mcpbrain.exe")
    assert a[0] == "schtasks"
    assert _flag_value(a, "/tn") == "mcpbrain-records-prune"
    assert _flag_value(a, "/sc") == "daily"
    assert _flag_value(a, "/st") == "06:00"
    assert "records-prune" in _flag_value(a, "/tr")
    assert "/f" in a


def test_health_schtasks_args_well_formed():
    a = agents.health_schtasks_args(mcpbrain_bin=r"C:\mcpbrain.exe")
    assert a[0] == "schtasks"
    assert _flag_value(a, "/tn") == "mcpbrain-records-health"
    assert _flag_value(a, "/sc") == "weekly"
    assert _flag_value(a, "/d") == "MON"
    assert _flag_value(a, "/st") == "07:00"
    assert "records-health" in _flag_value(a, "/tr")


def test_health_and_prune_args_quote_binary_with_spaces():
    a = agents.prune_schtasks_args(mcpbrain_bin=r"C:\Program Files\mcpbrain\mcpbrain.exe")
    action = _flag_value(a, "/tr")
    assert action.startswith(r'"C:\Program Files\mcpbrain\mcpbrain.exe"')
```

- [ ] **Run (expect PASS):** `uv run pytest tests/test_agents_windows_xplat.py -q`. These assert against generators that ALREADY exist (verified: `schtasks_args`/`schtasks_tray_args` emit `/sc onlogon` + an embedded `cmd /c "set MCPBRAIN_HOME=… && <bin> <sub>"` action; `prune`/`health` emit `/sc daily|weekly`). They should pass immediately — they are a regression guard pinning the Windows arg shape so a future edit cannot silently break it. If any assertion fails, that is a REAL Windows-parity bug in `agents.py` — fix the generator (likely arg-quoting per the spec's "schtasks arg quoting" candidate), do not weaken the test.
- [ ] **Lint:** `uv run ruff check tests/test_agents_windows_xplat.py`
- [ ] **Commit:** `test(agents): cross-platform Windows schtasks arg assertions`

## Task 4.2 — Append the Windows clean-machine validation runbook to `docs/RELEASE-RUNBOOK.md`

- [ ] **No test (docs).** Append a new `## 6. Windows clean-machine validation (HARD GATE)` section to `docs/RELEASE-RUNBOOK.md`, mirroring the macOS C3 gate (Task C3 of `docs/superpowers/plans/2026-06-15-autonomous-cowork-scheduled-tasks.md`) and the spec's 9-step Part 3 checklist. The existing runbook already has a brief Windows bullet under "§4 Clean-machine validation"; the new section is the authoritative, numbered hard gate. Content:

```markdown
## 6. Windows clean-machine validation (HARD GATE — must pass before Windows rollout)

Mirrors the macOS C3 gate. The schtasks generators are unit-tested
(`tests/test_agents_windows_xplat.py`) but the live desktop flow has had zero
real-machine testing. Run this once on a clean Windows box with a **non-author**
`@centrepoint.church` Google account before any wider Windows rollout.

- [ ] **1. Install plugin → `/mcpbrain-install`** on a clean Windows machine.
- [ ] **2. uv + wheel install; PATH correct** — `irm https://itsjoshuakemp.github.io/mcpbrain-dist/install.ps1 | iex` runs; `mcpbrain --version` resolves in a fresh shell (validates uv shim + PATH).
- [ ] **3. `mcpbrain setup` registers daemon + tray via schtasks** — confirm both tasks exist: `schtasks /query /tn mcpbrain` and `schtasks /query /tn mcpbrain-tray` (or `schtasks /query | findstr mcpbrain`).
- [ ] **4. Wizard loads; non-author Google sign-in works** — the "Google hasn't verified this app → Advanced → Continue" path completes with a *different* Centrepoint account.
- [ ] **5. The four Cowork Desktop Scheduled Tasks can be created** with working folder = the path printed by `mcpbrain setup` ("Your Cowork project working folder is: …").
- [ ] **6. `/reload-plugins` connects MCP; `brain_search` returns** a result (with a `score` field, per Part 1).
- [ ] **7. Hourly enrich task drains `enrich_inbox`** — drop a pending batch and confirm it is consumed.
- [ ] **8. `mcpbrain restore` round-trips a snapshot.**
- [ ] **9. `mcpbrain doctor` runs and its auto-fixes work on Windows** — restart/re-register via schtasks (`schtasks /end`+`/run`, `/create /f`).

**Likely gap candidates to watch:** PATH / uv-shim differences, `mcpbrain home`
resolution (`%APPDATA%\mcpbrain`), and schtasks arg quoting for paths with
spaces (covered by `tests/test_agents_windows_xplat.py`). Fix any gap found in
`agents.py` / `setup.py` (both owned by this worktree) and add a regression
assertion to `tests/test_agents_windows_xplat.py`.

**Record results here.** Do not proceed with Windows rollout until this gate passes.
```

- [ ] **Verify the file is valid markdown** and the section renders: open it, confirm the heading numbering is consistent with the existing `## 5. Cutting a new release`.
- [ ] **Commit:** `docs(runbook): add Windows clean-machine validation hard gate`

---

## Final verification (before finishing the branch)

- [ ] **Full suite green:** `uv run pytest -q` (expect the existing suite + the new `tests/test_retrieval.py` additions, `tests/test_brain_search_score.py`, `tests/eval/test_eval_baseline.py`, `tests/test_calendar_graph.py`, `tests/test_setup_path_echo.py`, `tests/test_agents_windows_xplat.py` all passing; the existing `tests/eval/test_retrieval_quality.py` and `tests/test_calendar_sync.py` unaffected).
- [ ] **Lint clean:** `uv run ruff check mcpbrain/ tests/`
- [ ] **No out-of-scope edits:** `git status --porcelain` shows changes only under the owned files; `plugin/skills/install/` and `tests/test_agents_cadence_xplat.py` are untouched.
- [ ] **Sweep iCloud conflict-copies** (per RELEASE-RUNBOOK env hazard) before committing if any ` 2.py`/` 2.md` files appear.
- [ ] Use superpowers:finishing-a-development-branch to decide merge/PR. **Do not commit during planning; only at execution.**
