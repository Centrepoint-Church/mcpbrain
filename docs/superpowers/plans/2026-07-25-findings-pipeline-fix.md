# Proactive-findings Pipeline Fix — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the 200 stuck `proactive_findings` closable — repair the review-adjudication path that silently drops its work units, retire rows from a deleted lint check, and turn `memory_promotion` into a queue the weekly gardener acts on.

**Architecture:** Three independent repairs. (A) `mcpbrain/enrich_blocks.py` becomes the single source of truth for block-type sets, so the producer (`prepare.write_units`) emits review units and `brain_enrich_push` accepts review answers — the drainers in `drain.BLOCK_DRAINERS` already exist and start firing. (B1) `lint_graph.run()` closes rows belonging to retired checks. (B2) A new scoped `brain_finding_resolve` MCP tool plus a promotion-queue step in the gardener routine.

**Tech Stack:** Python 3, SQLite (`mcpbrain/store.py`), MCP (`mcp.server.lowlevel`), pytest (+ pytest-xdist, parallel by default).

**Spec:** `docs/superpowers/specs/2026-07-25-findings-pipeline-fix-design.md`

## Global Constraints

- **Do not push or release.** Commit locally only. Shipping is a separate, explicit instruction (`CLAUDE.md`).
- **Do not bump versions.** No changes to the five version files.
- **Scope test runs to edited modules and their direct dependents.** Josh runs the full suite himself.
- `ruff` must be clean on every file touched.
- Rows are **resolved** (`resolved_at` set), never `DELETE`d — history stays and the change is reversible.
- No new config flags. `review_max_apply_per_run` (default 50) already throttles applies.
- `MANUAL_RESOLVE_TYPES = ("memory_promotion",)` — the only finding type `brain_finding_resolve` may close.
- Outcome vocabulary, exact strings: `promoted` | `merged` | `dismissed`.
- Review block keys, exact strings: `review_orphan`, `review_missing_org`, `review_ownerless`, `review_org`, `org_merge_review`.

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `mcpbrain/enrich_blocks.py` | Modify — single source of truth for block sets; add `REVIEW_BLOCKS`, `PUSH_BLOCKS` | 1 |
| `tests/test_enrich_blocks.py` | Modify — registry invariants (two existing tests become wrong) | 1 |
| `tests/test_prepare.py` | Modify — `write_units` emits a review block unit | 1 |
| `mcpbrain/mcp_server.py` | Modify — push accepts/forwards `PUSH_BLOCKS`; generated schema; new `brain_finding_resolve` | 2, 4 |
| `tests/test_mcp_server.py` | Modify — push forwards review keys; `brain_finding_resolve` behaviour | 2, 4 |
| `tests/test_drain.py` | Modify — end-to-end push → drain → finding resolved | 2 |
| `mcpbrain/lint_graph.py` | Modify — `RETIRED_FINDING_TYPES` + sweep in `run()` | 3 |
| `tests/test_lint.py` | Modify — retirement sweep | 3 |
| `mcpbrain/routines/gardener.md` | Modify — promotion-queue step | 5 |
| `tests/test_gardener_routine.py` | Create — routine documents the queue contract | 5 |

---

### Task 1: Unify the block registry so review work becomes work units

**Files:**
- Modify: `mcpbrain/enrich_blocks.py` (whole file, 17 lines)
- Test: `tests/test_enrich_blocks.py` (replace two existing tests, add three)
- Test: `tests/test_prepare.py` (append one test)

**Interfaces:**
- Consumes: nothing (first task).
- Produces: `enrich_blocks.REVIEW_BLOCKS: tuple[str, ...]`, `enrich_blocks.PUSH_BLOCKS: tuple[str, ...]`, and a widened `enrich_blocks.UNIT_BLOCKS: tuple[str, ...]`. Task 2 imports `PUSH_BLOCKS`.

**Background:** `prepare.write_units` (`mcpbrain/prepare.py:726`) loops `for k in _UNIT_BLOCKS` where `_UNIT_BLOCKS` is imported from this module at `prepare.py:40`. Widening the tuple is the entire producer-side fix — no code change in `prepare.py`.

- [ ] **Step 1: Write the failing registry-invariant tests**

Replace the whole of `tests/test_enrich_blocks.py` with:

```python
from mcpbrain import enrich_blocks, mcp_server, prepare

# Importing these registers their BLOCK_DRAINERS entries, exactly as
# daemon.py:55-58 does at startup. Without them the registry is
# under-populated and the invariant test below passes vacuously.
import mcpbrain.profile_synth  # noqa: F401
import mcpbrain.community_synth  # noqa: F401
import mcpbrain.memory_distil  # noqa: F401
import mcpbrain.profile_audit  # noqa: F401
from mcpbrain.drain import BLOCK_DRAINERS


def test_unit_blocks_is_merge_review_plus_answer_and_review_blocks():
    assert enrich_blocks.UNIT_BLOCKS == (
        "merge_review", *enrich_blocks.ANSWER_BLOCKS, *enrich_blocks.REVIEW_BLOCKS)


def test_push_blocks_is_answer_plus_review_blocks():
    assert enrich_blocks.PUSH_BLOCKS == (
        *enrich_blocks.ANSWER_BLOCKS, *enrich_blocks.REVIEW_BLOCKS)


def test_consumers_derive_from_single_source():
    assert mcp_server._PUSH_BLOCKS == enrich_blocks.PUSH_BLOCKS
    assert prepare._UNIT_BLOCKS == enrich_blocks.UNIT_BLOCKS


def test_merge_review_is_a_unit_block_not_a_push_block():
    assert "merge_review" in enrich_blocks.UNIT_BLOCKS
    assert "merge_review" not in enrich_blocks.PUSH_BLOCKS


def test_every_registered_drainer_key_is_pushable():
    """A drainer whose key brain_enrich_push refuses can never fire. This
    drift is what stranded the review_* families: cadence produced the work,
    write_units dropped it, push would have refused the answer, and the
    drainers sat registered and never invoked."""
    unpushable = set(BLOCK_DRAINERS) - set(enrich_blocks.PUSH_BLOCKS)
    assert unpushable == set(), (
        f"drainers registered for keys push will not accept: {sorted(unpushable)}")


def test_every_review_block_has_a_drainer():
    missing = set(enrich_blocks.REVIEW_BLOCKS) - set(BLOCK_DRAINERS)
    assert missing == set(), f"review blocks with no drainer: {sorted(missing)}"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_enrich_blocks.py -v -p no:xdist`
Expected: FAIL — `AttributeError: module 'mcpbrain.enrich_blocks' has no attribute 'REVIEW_BLOCKS'`

- [ ] **Step 3: Rewrite `mcpbrain/enrich_blocks.py`**

Replace the whole file with:

```python
"""Single source of truth for the enrichment block-type sets.

ANSWER_BLOCKS — the optional answer blocks a subagent may push via
brain_enrich_push (beyond extractions + merge_answers), each drained by the
daemon.

REVIEW_BLOCKS — the review/curator families. The daemon's review and curator
cadences PRODUCE these as work units, the subagent answers under the SAME key
(unlike merge_review, whose answer key is merge_answers), and
drain.BLOCK_DRAINERS applies the verdicts.

UNIT_BLOCKS — the block-unit kinds the producer (prepare.write_units) emits.
PUSH_BLOCKS — the answer keys brain_enrich_push accepts and forwards.

INVARIANT: every drain.BLOCK_DRAINERS key must appear in PUSH_BLOCKS, and every
REVIEW_BLOCKS entry must have a drainer. A drainer whose key push refuses can
never fire. That exact drift stranded the review_* families for weeks — the
cadence produced the work, write_units silently dropped it because the keys
were absent from UNIT_BLOCKS, and push would have refused the answers anyway.
tests/test_enrich_blocks.py enforces both directions.
"""

ANSWER_BLOCKS = ("synthesis", "profile_synthesis", "community_synthesis",
                 "memory_distil", "profile_audit")

REVIEW_BLOCKS = ("review_orphan", "review_missing_org", "review_ownerless",
                 "review_org", "org_merge_review")

UNIT_BLOCKS = ("merge_review", *ANSWER_BLOCKS, *REVIEW_BLOCKS)

PUSH_BLOCKS = (*ANSWER_BLOCKS, *REVIEW_BLOCKS)
```

- [ ] **Step 4: Run the tests**

Run: `pytest tests/test_enrich_blocks.py -v -p no:xdist`
Expected: all PASS except `test_consumers_derive_from_single_source`, which still FAILs with `AttributeError: module 'mcpbrain.mcp_server' has no attribute '_PUSH_BLOCKS'`. That one is Task 2's job — leave it red.

- [ ] **Step 5: Write the producer test**

Append to `tests/test_prepare.py`:

```python
def test_write_units_emits_a_unit_per_review_block(tmp_path):
    """The review cadence's blocks must become real work units. They were
    silently dropped because write_units only iterated UNIT_BLOCKS and the
    review families were missing from it."""
    data = {
        "batch_id": "batch-review",
        "prepared_at": "2026-07-25T00:00:00Z",
        "context": {},
        "threads": [],
        "review_orphan": [
            {"finding_id": 1, "packet": {"finding_type": "lint:orphan_entity",
                                         "ref_id": "e-ghost"}},
        ],
        "org_merge_review": [
            {"pair_id": "p-1", "a": {"name": "ACC"}, "b": {"name": "ACCI"}},
        ],
    }

    summary = prepare.write_units(data, home=str(tmp_path))

    blocks = {}
    for path in (tmp_path / "enrich_queue" / "units").glob("*.json"):
        unit = json.loads(path.read_text())
        if unit["kind"] == "block":
            blocks[unit["block"]] = unit["items"]

    assert "review_orphan" in blocks, f"got block units: {sorted(blocks)}"
    assert "org_merge_review" in blocks, f"got block units: {sorted(blocks)}"
    assert blocks["review_orphan"][0]["finding_id"] == 1
    assert summary["units_written"] == 2
```

- [ ] **Step 6: Run the producer test**

Run: `pytest tests/test_prepare.py::test_write_units_emits_a_unit_per_review_block -v -p no:xdist`
Expected: PASS (Step 3 already widened `UNIT_BLOCKS`; this test proves the producer picks it up with no `prepare.py` change).

- [ ] **Step 7: Run the directly impacted modules' tests**

Run: `pytest tests/test_prepare.py tests/test_drain.py tests/test_integration_spool.py -q`
Expected: PASS. If anything fails, it is a real regression from widening `UNIT_BLOCKS` — fix it before committing.

- [ ] **Step 8: Lint**

Run: `ruff check mcpbrain/enrich_blocks.py tests/test_enrich_blocks.py tests/test_prepare.py`
Expected: `All checks passed!`

- [ ] **Step 9: Commit**

```bash
git add mcpbrain/enrich_blocks.py tests/test_enrich_blocks.py tests/test_prepare.py
git commit -m "fix(enrich): emit review/curator blocks as work units

UNIT_BLOCKS omitted the five review families, so prepare.write_units
silently dropped every block the review and curator cadences produced.
Add REVIEW_BLOCKS + PUSH_BLOCKS to enrich_blocks and enforce the
drainer/push invariant that would have caught the drift."
```

Note: `tests/test_enrich_blocks.py::test_consumers_derive_from_single_source` is knowingly red at this commit and goes green in Task 2.

---

### Task 2: Accept review answers on push, and prove the round trip

**Files:**
- Modify: `mcpbrain/mcp_server.py:5` (import), `:685`, `:705`, `:1166-1183` (tool schema), `:1352`
- Test: `tests/test_mcp_server.py` (append), `tests/test_drain.py` (append)

**Interfaces:**
- Consumes: `enrich_blocks.PUSH_BLOCKS` from Task 1.
- Produces: `mcp_server._PUSH_BLOCKS` (the module-level alias `tests/test_enrich_blocks.py` asserts on). `brain_enrich_push` gains keyword arguments for all five review keys via its existing `**blocks` catch-all; the MCP `inputSchema` declares them.

**Background:** `brain_enrich_push` already accepts `**blocks`. Three sites filter that dict against `_ENRICH_ANSWER_BLOCKS`: the has-block-answer guard (`:685`), the inbox payload build (`:705`), and the MCP argument fan-out (`:1352`). The `inputSchema` at `:1167` hand-lists six properties, so the MCP client never sends review keys in the first place.

- [ ] **Step 1: Write the failing push test**

`tests/test_mcp_server.py` imports `asyncio` and `Store` but **not** `json`. Add `import json` to its imports at the top of the file, then append:

```python
# --- brain_enrich_push forwards review-block answers ---------------------

from mcpbrain.mcp_server import make_brain_enrich_push


def test_enrich_push_forwards_review_block_answers(tmp_path):
    """A review verdict must reach enrich_inbox under its own key. It used to
    be dropped: push filtered **blocks against ANSWER_BLOCKS only, so the
    registered review drainers could never fire."""
    push = make_brain_enrich_push(str(tmp_path))

    out = asyncio.run(push(
        unit_id="u-review1",
        review_orphan=[{"finding_id": 7, "ref_id": "e-ghost", "verdict": "keep"}],
    ))

    assert out["written"] is True, out
    payload = json.loads((tmp_path / "enrich_inbox" / "u-review1.json").read_text())
    assert payload["review_orphan"] == [
        {"finding_id": 7, "ref_id": "e-ghost", "verdict": "keep"}]
    assert payload["extractions"] == []


def test_enrich_push_review_answer_satisfies_the_block_unit_guard(tmp_path):
    """extractions may be omitted for a block unit. A review answer counts as
    a block answer, so the derailed-subagent guard must not reject it."""
    push = make_brain_enrich_push(str(tmp_path))

    out = asyncio.run(push(
        unit_id="u-review2",
        extractions=None,
        review_org=[{"finding_id": 9, "ref_id": "accі", "verdict": "skip"}],
    ))

    assert out["written"] is True, out


def test_enrich_push_schema_declares_every_push_block():
    """The inputSchema is what the MCP client is allowed to send. A key that
    is accepted by the handler but undeclared here is unreachable in
    practice."""
    from mcpbrain import enrich_blocks
    schema = _push_tool_schema()
    for key in enrich_blocks.PUSH_BLOCKS:
        assert key in schema["properties"], f"{key} missing from push inputSchema"
    assert "merge_answers" in schema["properties"]
```

Add this helper near the top of the same test file, below the existing imports:

```python
def _push_tool_schema():
    """Pull brain_enrich_push's declared inputSchema out of the module-level
    builder, so the test reads exactly what the MCP client is offered."""
    from mcpbrain.mcp_server import push_input_schema
    return push_input_schema()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_mcp_server.py -k "review_block or push_block or review_answer" -v -p no:xdist`
Expected: FAIL — the two payload tests fail on a missing `review_orphan` key / `"extractions is required for thread units"`, and the schema test fails with `ImportError: cannot import name 'push_input_schema'`.

- [ ] **Step 3: Extract the push schema into a named builder**

In `mcpbrain/mcp_server.py`, change the import on line 5:

```python
from mcpbrain.enrich_blocks import PUSH_BLOCKS as _PUSH_BLOCKS
```

Then add this function just above `def make_brain_enrich_push(home: str):` (currently line 651):

```python
def push_input_schema() -> dict:
    """brain_enrich_push's inputSchema, with one property per push block.

    Generated from _PUSH_BLOCKS rather than hand-listed so a block can never be
    added to the registry and forgotten here — an undeclared key is one the MCP
    client is not allowed to send, which makes its drainer unreachable.
    merge_answers stays explicit: it is the only block whose answer key differs
    from its unit key (merge_review).
    """
    return {"type": "object", "properties": {
        "unit_id": {"type": "string",
                    "description": "the unit you pulled (writes enrich_inbox/<unit_id>.json)"},
        "extractions": {"type": "array", "items": {"type": "object"},
                        "description": "one extraction object per thread (thread unit)"},
        "merge_answers": {"type": "array", "items": {"type": "object"},
                          "description": "answers for a merge_review block unit"},
        **{_k: {"type": "array", "items": {"type": "object"},
                "description": f"answers for a {_k} block unit"}
           for _k in _PUSH_BLOCKS},
    }, "required": ["unit_id"]}
```

- [ ] **Step 4: Swap the three filter sites to `_PUSH_BLOCKS`**

In `mcpbrain/mcp_server.py`, replace `_ENRICH_ANSWER_BLOCKS` with `_PUSH_BLOCKS` at all three remaining occurrences:

Line ~685 (has-block-answer guard):
```python
        has_block_answer = (merge_answers is not None and merge_answers != []) or any(
            blocks.get(k) for k in _PUSH_BLOCKS
        )
```

Line ~705 (inbox payload):
```python
            for _k in _PUSH_BLOCKS:
                if blocks.get(_k):
                    payload[_k] = blocks[_k]
```

Line ~1352 (MCP argument fan-out):
```python
                **{k: arguments[k] for k in _PUSH_BLOCKS if arguments.get(k)},
```

- [ ] **Step 5: Use the generated schema in the tool listing**

Replace the `types.Tool(...)` entry for `brain_enrich_push` (lines ~1165-1184) with:

```python
            types.Tool(
                name="brain_enrich_push",
                description=(
                    "Submit a unit's enrichment result by unit_id → enrich_inbox/<unit_id>.json; "
                    "the daemon applies it, marks chunks enriched, and deletes the unit. Pass "
                    "`extractions` (one per thread, for a thread unit) and/or the block answer "
                    "field for a block unit: merge_answers (merge_review), or the block's own "
                    "name for " + ", ".join(_PUSH_BLOCKS) + "."
                ),
                inputSchema=push_input_schema(),
            ),
```

- [ ] **Step 6: Update the handler docstring**

In `brain_enrich_push`'s docstring (lines ~655-671), replace both hand-written block lists with the accurate one. Change:

```
        blocks (synthesis, profile_synthesis, community_synthesis, memory_distil,
        profile_audit) and forwards each. Returns {"written": bool, path|error}.
```
to:
```
        blocks — the synthesis/profile/community/memory/audit families and the
        review/curator families (see enrich_blocks.PUSH_BLOCKS) — and forwards
        each. Returns {"written": bool, path|error}.
```

And change:
```
            answer in a block field (merge_answers, synthesis, profile_synthesis,
            community_synthesis, memory_distil, profile_audit).  A push with no
```
to:
```
            answer in a block field (merge_answers or any enrich_blocks.PUSH_BLOCKS
            key).  A push with no
```

- [ ] **Step 7: Run the push tests**

Run: `pytest tests/test_mcp_server.py -k "review_block or push_block or review_answer" -v -p no:xdist`
Expected: PASS (3 tests).

- [ ] **Step 8: Confirm Task 1's deferred test is now green**

Run: `pytest tests/test_enrich_blocks.py -v -p no:xdist`
Expected: all PASS, including `test_consumers_derive_from_single_source`.

- [ ] **Step 9: Write the end-to-end round-trip test**

Append to `tests/test_drain.py` (it already provides the `store` and `home` fixtures used below):

```python
def test_review_orphan_round_trip_resolves_the_finding(store, home):
    """End-to-end proof of the repaired path: push a review verdict, drain it,
    and the finding closes. Every link in this chain existed before; the
    producer and push allowlists were the two broken ones."""
    from mcpbrain.mcp_server import make_brain_enrich_push

    store.record_finding(
        "lint:orphan_entity", "e-ghost",
        summary="orphan_entity: Ghost", severity="info",
    )
    finding_id = store.open_findings("lint:orphan_entity")[0]["id"]

    push = make_brain_enrich_push(str(home))
    out = asyncio.run(push(
        unit_id="u-e2e",
        review_orphan=[{"finding_id": finding_id, "ref_id": "e-ghost",
                        "verdict": "keep"}],
    ))
    assert out["written"] is True, out

    summary = drain.drain(store, home=home, apply=RecordingApply())

    assert summary["review_orphan_drained"] == 1
    assert store.get_finding(finding_id)["resolved_at"] != ""
```

`tests/test_drain.py` imports `json`, `pytest`, `drain`, and `Store` but **not** `asyncio`. Add `import asyncio` to its imports at the top of the file.

- [ ] **Step 10: Run the round-trip test**

Run: `pytest tests/test_drain.py::test_review_orphan_round_trip_resolves_the_finding -v -p no:xdist`
Expected: PASS. `verdict: "keep"` resolves the finding without mutating the graph (`review_apply.apply_orphan_verdicts`), so the test needs no entity fixture.

- [ ] **Step 11: Run the impacted suites**

Run: `pytest tests/test_mcp_server.py tests/test_mcp_server_stdio.py tests/test_mcp_enrich_with_rules.py tests/test_drain.py tests/test_enrich_blocks.py -q`
Expected: PASS.

- [ ] **Step 12: Lint**

Run: `ruff check mcpbrain/mcp_server.py tests/test_mcp_server.py tests/test_drain.py`
Expected: `All checks passed!`

- [ ] **Step 13: Commit**

```bash
git add mcpbrain/mcp_server.py tests/test_mcp_server.py tests/test_drain.py
git commit -m "fix(enrich): accept review/curator answers on brain_enrich_push

push filtered **blocks against ANSWER_BLOCKS and hand-listed six schema
properties, so review verdicts were dropped and the MCP client was never
offered the keys. Filter against PUSH_BLOCKS and generate the schema from
it. Adds the push -> drain -> finding-resolved round trip."
```

---

### Task 3: Retire findings from deleted lint checks

**Files:**
- Modify: `mcpbrain/lint_graph.py` (module constant + two lines in `run()`)
- Test: `tests/test_lint.py` (append)

**Interfaces:**
- Consumes: nothing from Tasks 1-2.
- Produces: `lint_graph.RETIRED_FINDING_TYPES: tuple[str, ...]`. Nothing downstream reads it.

**Background:** `check_possible_duplicates` was deleted (`lint_graph.py:11`). `store.resolve_findings_not_in(finding_type, [], now)` closes every open row of a type when the live-ref list is empty — `store.py:2706-2714` takes the "no live refs" branch and updates all unresolved rows of that type.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_lint.py`:

```python
# ---------------------------------------------------------------------------
# Retired checks: their findings must not stay open forever
# ---------------------------------------------------------------------------

def test_lint_run_closes_findings_from_retired_checks(tmp_path):
    """A deleted check leaves its rows stranded: resolve_findings_not_in only
    runs for types the module still produces. 50 lint:possible_duplicate rows
    sat open on the live store for six weeks this way."""
    s = _store(tmp_path, name="retired.sqlite3")
    s.record_finding(
        "lint:possible_duplicate", "michael|michael-hollister",
        summary="Possible duplicate: Michael / Michael Hollister",
        severity="info",
    )
    assert len(s.open_findings("lint:possible_duplicate")) == 1

    run(s, now="2026-07-25T00:00:00Z", log_dir=tmp_path / "logs")

    assert s.open_findings("lint:possible_duplicate") == []


def test_lint_run_leaves_live_finding_types_alone(tmp_path):
    """The sweep is an explicit allowlist, not a 'close anything unfamiliar'
    heuristic: memory_promotion and org_unrecognised are produced elsewhere
    (memory_distil, drain) and must survive a lint run."""
    s = _store(tmp_path, name="live_types.sqlite3")
    s.record_finding("memory_promotion", "note-1",
                     summary="Memory note flagged for promotion", severity="info")
    s.record_finding("org_unrecognised", "acci",
                     summary="Unrecognised org 'ACCI'", severity="info")

    run(s, now="2026-07-25T00:00:00Z", log_dir=tmp_path / "logs")

    assert len(s.open_findings("memory_promotion")) == 1
    assert len(s.open_findings("org_unrecognised")) == 1
```

- [ ] **Step 2: Run the tests to verify the first fails**

Run: `pytest tests/test_lint.py -k retired -v -p no:xdist`
Expected: `test_lint_run_closes_findings_from_retired_checks` FAILs (`assert [{...}] == []`); `test_lint_run_leaves_live_finding_types_alone` PASSes already (it is the guard against over-reach).

- [ ] **Step 3: Add the constant**

In `mcpbrain/lint_graph.py`, add immediately above `def run(store, *, now: str, log_dir=None) -> dict:` (currently line 351):

```python
# Finding types this module used to produce. A retired check leaves its rows
# stranded open forever, because resolve_findings_not_in only runs for types
# still emitted below. Listing them here closes them out on the next run, on
# every install, with no manual SQL. Deliberately an explicit list and not a
# "close any type with no producer" sweep: memory_promotion (memory_distil)
# and org_unrecognised (drain) are produced outside this module.
RETIRED_FINDING_TYPES = ("lint:possible_duplicate",)
```

- [ ] **Step 4: Sweep in `run()`**

In `mcpbrain/lint_graph.py`, insert immediately before the closing `log.info(...)` line of `run()` (currently line 446):

```python
    # Close out rows left behind by checks this module no longer runs.
    for retired_type in RETIRED_FINDING_TYPES:
        store.resolve_findings_not_in(retired_type, [], now)

```

- [ ] **Step 5: Run the tests**

Run: `pytest tests/test_lint.py -k retired -v -p no:xdist`
Expected: both PASS.

- [ ] **Step 6: Run the full lint suite**

Run: `pytest tests/test_lint.py -q`
Expected: PASS.

- [ ] **Step 7: Lint**

Run: `ruff check mcpbrain/lint_graph.py tests/test_lint.py`
Expected: `All checks passed!`

- [ ] **Step 8: Commit**

```bash
git add mcpbrain/lint_graph.py tests/test_lint.py
git commit -m "fix(lint): close findings left behind by deleted checks

check_possible_duplicates was removed but resolve_findings_not_in only
runs for live types, so its rows stayed open forever (50 on the live
store since 2026-06-15). Sweep an explicit RETIRED_FINDING_TYPES list."
```

---

### Task 4: `brain_finding_resolve` — a scoped way to close a finding

**Files:**
- Modify: `mcpbrain/mcp_server.py` (new factory near `make_brain_proactive` at `:197`; wiring at `:847`; tool listing after `:978`; dispatch after `:1258`)
- Test: `tests/test_mcp_server.py` (append)

**Interfaces:**
- Consumes: nothing from Tasks 1-3.
- Produces: `mcp_server.MANUAL_RESOLVE_TYPES: tuple[str, ...]`, `mcp_server.make_brain_finding_resolve(store)` returning an async `brain_finding_resolve(finding_id: int, outcome: str, note: str = "") -> dict`. Task 5's routine calls the tool.

**Background:** the tool must WRITE, so it is wired to `draft_store` — the writable handle created at `mcp_server.py:858` — not the read-only `store` used by `make_brain_proactive`. `make_brain_meeting_pack_upsert(draft_store)` is the existing precedent.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mcp_server.py`:

```python
# --- brain_finding_resolve MCP tool --------------------------------------

from mcpbrain.mcp_server import make_brain_finding_resolve


def _promotion_store(tmp_path, name="fr.sqlite3"):
    s = Store(tmp_path / name, dim=4)
    s.init()
    s.record_finding(
        "memory_promotion", "note-abc",
        summary="Memory note flagged for promotion: note-abc",
        detail="reason=durable preference target_hint=preferences",
        severity="info",
    )
    s.record_finding(
        "lint:orphan_entity", "e-ghost",
        summary="orphan_entity: Ghost", severity="info",
    )
    return s


def _finding_id(store, finding_type):
    return store.open_findings(finding_type)[0]["id"]


def test_finding_resolve_closes_a_memory_promotion(tmp_path):
    s = _promotion_store(tmp_path)
    fid = _finding_id(s, "memory_promotion")
    tool = make_brain_finding_resolve(s)

    out = asyncio.run(tool(finding_id=fid, outcome="promoted",
                           note="wrote memory/durable-preference.md"))

    assert out == {"resolved": True, "finding_id": fid, "outcome": "promoted"}
    assert s.open_findings("memory_promotion") == []


def test_finding_resolve_records_the_change(tmp_path):
    s = _promotion_store(tmp_path, name="fr_change.sqlite3")
    fid = _finding_id(s, "memory_promotion")
    tool = make_brain_finding_resolve(s)

    asyncio.run(tool(finding_id=fid, outcome="dismissed", note="not durable"))

    kinds = [c["change_type"] for c in s.recent_changes(limit=10)]
    assert "finding_resolved" in kinds


def test_finding_resolve_refuses_a_lint_finding(tmp_path):
    """lint types are owned by the review appliers. Closing one by hand is
    churn (the next lint run re-opens it) and would let any session quietly
    clear graph-hygiene work."""
    s = _promotion_store(tmp_path, name="fr_lint.sqlite3")
    fid = _finding_id(s, "lint:orphan_entity")
    tool = make_brain_finding_resolve(s)

    out = asyncio.run(tool(finding_id=fid, outcome="dismissed"))

    assert out["resolved"] is False
    assert "lint:orphan_entity" in out["error"]
    assert len(s.open_findings("lint:orphan_entity")) == 1


def test_finding_resolve_rejects_a_bad_outcome(tmp_path):
    s = _promotion_store(tmp_path, name="fr_outcome.sqlite3")
    fid = _finding_id(s, "memory_promotion")
    tool = make_brain_finding_resolve(s)

    out = asyncio.run(tool(finding_id=fid, outcome="done"))

    assert out["resolved"] is False
    assert "outcome" in out["error"]
    assert len(s.open_findings("memory_promotion")) == 1


def test_finding_resolve_rejects_unknown_and_already_resolved(tmp_path):
    s = _promotion_store(tmp_path, name="fr_missing.sqlite3")
    tool = make_brain_finding_resolve(s)

    missing = asyncio.run(tool(finding_id=99999, outcome="dismissed"))
    assert missing["resolved"] is False
    assert "not found" in missing["error"]

    fid = _finding_id(s, "memory_promotion")
    asyncio.run(tool(finding_id=fid, outcome="merged"))
    again = asyncio.run(tool(finding_id=fid, outcome="merged"))
    assert again["resolved"] is False
    assert "already resolved" in again["error"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_mcp_server.py -k finding_resolve -v -p no:xdist`
Expected: FAIL — `ImportError: cannot import name 'make_brain_finding_resolve'`

- [ ] **Step 3: Add the factory**

In `mcpbrain/mcp_server.py`, insert immediately after `make_brain_proactive` ends (after line 215, before `def _capture_envelope`):

```python
# Finding types brain_finding_resolve may close. Deliberately narrow: every
# other type is owned by an automated resolver (the review appliers via
# drain.BLOCK_DRAINERS, or lint's own resolve_findings_not_in). Closing one of
# those by hand is churn — the next lint run re-opens it because the underlying
# entity is still there — and a general tool would let any session quietly clear
# the graph-hygiene queue. The dashboard route
# /api/dashboard/findings/<id>/dismiss remains the human override for any type.
MANUAL_RESOLVE_TYPES = ("memory_promotion",)

_RESOLVE_OUTCOMES = ("promoted", "merged", "dismissed")


def make_brain_finding_resolve(store):
    async def brain_finding_resolve(finding_id: int, outcome: str,
                                    note: str = "") -> dict:
        """Close one proactive finding the caller has acted on.

        Only types in MANUAL_RESOLVE_TYPES may be closed this way. outcome is
        one of promoted | merged | dismissed and is recorded in the change log
        alongside `note`. Returns {"resolved": bool, ...} — never raises.
        """
        try:
            if outcome not in _RESOLVE_OUTCOMES:
                return {"resolved": False,
                        "error": f"outcome must be one of {list(_RESOLVE_OUTCOMES)}, "
                                 f"got {outcome!r}"}
            finding = store.get_finding(finding_id)
            if finding is None:
                return {"resolved": False, "error": f"finding {finding_id} not found"}
            if finding["resolved_at"]:
                return {"resolved": False,
                        "error": f"finding {finding_id} is already resolved"}
            ftype = finding["finding_type"]
            if ftype not in MANUAL_RESOLVE_TYPES:
                return {"resolved": False,
                        "error": f"{ftype} is resolved automatically and cannot be "
                                 f"closed by hand; only {list(MANUAL_RESOLVE_TYPES)} "
                                 f"may be"}
            if not store.resolve_finding(finding_id):
                return {"resolved": False,
                        "error": f"finding {finding_id} could not be resolved"}
            store.record_change(
                "finding_resolved", ref_id=str(finding_id),
                summary=f"{ftype} {outcome}: {finding['ref_id']}",
                detail=note, source="mcp")
            return {"resolved": True, "finding_id": finding_id, "outcome": outcome}
        except Exception as exc:  # noqa: BLE001 — a tool must return, not raise
            _log.exception("brain_finding_resolve failed")
            return {"resolved": False, "error": str(exc)}
    return brain_finding_resolve
```

- [ ] **Step 4: Run the tests**

Run: `pytest tests/test_mcp_server.py -k finding_resolve -v -p no:xdist`
Expected: all 5 PASS.

- [ ] **Step 5: Wire the tool into the server**

Three edits in `mcpbrain/mcp_server.py`.

(a) After `meeting_pack_upsert = make_brain_meeting_pack_upsert(draft_store)` (line ~872), add — note `draft_store`, because the read-only handle cannot UPDATE:

```python
    # Writable handle: resolving a finding UPDATEs proactive_findings.
    finding_resolve = make_brain_finding_resolve(draft_store)
```

(b) In the `_tools()` list, immediately after the `brain_proactive` entry (which closes at line ~978), add:

```python
            types.Tool(
                name="brain_finding_resolve",
                description=(
                    "Close one proactive finding you have acted on. Only "
                    "memory_promotion findings may be closed this way — every other "
                    "type is resolved automatically. outcome: 'promoted' (a memory "
                    "file was written), 'merged' (folded into an existing memory "
                    "file), or 'dismissed' (not durable)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "finding_id": {"type": "integer",
                                       "description": "the finding's id, from brain_proactive"},
                        "outcome": {"type": "string",
                                    "enum": list(_RESOLVE_OUTCOMES),
                                    "description": "what you did about it"},
                        "note": {"type": "string",
                                 "description": "short free text for the change log"},
                    },
                    "required": ["finding_id", "outcome"],
                },
            ),
```

(c) In the dispatch chain, immediately after the `brain_proactive` branch (which ends at line ~1258), add:

```python
        if name == "brain_finding_resolve":
            out = await finding_resolve(
                finding_id=arguments.get("finding_id", 0),
                outcome=arguments.get("outcome", ""),
                note=arguments.get("note", ""),
            )
            return [types.TextContent(type="text", text=json.dumps(out))]
```

- [ ] **Step 6: Verify the tool is listed over the wire**

Run: `pytest tests/test_mcp_server_stdio.py -q`
Expected: PASS. This test opens a real stdio session and calls `list_tools()`, so it catches a syntax error or a broken `_tools()` entry.

- [ ] **Step 7: Run the impacted suites**

Run: `pytest tests/test_mcp_server.py tests/test_mcp_server_no_native.py tests/test_mcp_server_stdio.py -q`
Expected: PASS.

- [ ] **Step 8: Lint**

Run: `ruff check mcpbrain/mcp_server.py tests/test_mcp_server.py`
Expected: `All checks passed!`

- [ ] **Step 9: Commit**

```bash
git add mcpbrain/mcp_server.py tests/test_mcp_server.py
git commit -m "feat(mcp): brain_finding_resolve for hand-actioned findings

Scoped to MANUAL_RESOLVE_TYPES (memory_promotion): every other type is
owned by an automated resolver, so closing one by hand is churn and would
let any session clear the graph-hygiene queue. Records the outcome in the
change log."
```

---

### Task 5: Give the gardener the promotion queue

**Files:**
- Modify: `mcpbrain/routines/gardener.md`
- Test: `tests/test_gardener_routine.py` (create)

**Interfaces:**
- Consumes: `brain_finding_resolve(finding_id, outcome, note)` from Task 4; the existing `brain_proactive(finding_type=...)`, `brain_read(doc_id)`, and `brain_memory_write(slug, description, body, memory_type)` tools.
- Produces: nothing consumed by other tasks.

**Background:** `mcpbrain/routines/gardener.md` is served through the `brain_routine` MCP tool from `mcpbrain/routines/` and has no `plugin/` mirror, so this is a single-file change. The routine already lists promotion as job #3 of its hygiene pass but has never had a way to see the queue. `tests/test_enrich_routine.py` is the pattern for asserting on routine content.

- [ ] **Step 1: Write the failing routine test**

Create `tests/test_gardener_routine.py`:

```python
# tests/test_gardener_routine.py
from pathlib import Path

_ROUTINE = Path(__file__).parent.parent / "mcpbrain" / "routines" / "gardener.md"


def test_gardener_works_the_promotion_queue():
    """The gardener must ACT on memory_promotion findings, not just read them:
    the finding type was write-only until this step existed."""
    text = _ROUTINE.read_text()
    assert "memory_promotion" in text
    assert 'brain_proactive(finding_type="memory_promotion")' in text
    assert "brain_read" in text


def test_gardener_closes_every_promotion_finding():
    """All three outcomes resolve the finding. If any path left it open the
    queue would silently refill, which is the bug being fixed."""
    text = _ROUTINE.read_text()
    assert "brain_finding_resolve" in text
    for outcome in ("promoted", "merged", "dismissed"):
        assert outcome in text, f"outcome {outcome} not documented"


def test_gardener_promotes_through_the_write_tool():
    """Promotion must go through brain_memory_write -> daemon. The routine's
    standing rule is that it never hand-authors a new memory file."""
    text = _ROUTINE.read_text()
    assert "brain_memory_write" in text
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/test_gardener_routine.py -v -p no:xdist`
Expected: FAIL — `assert 'memory_promotion' in text`

- [ ] **Step 3: Add the promotion-queue section to the routine**

In `mcpbrain/routines/gardener.md`, the numbered hygiene list currently ends at item 4 ("**Fix drift** — …"). Add a fifth item to that list:

```markdown
5. **Work the promotion queue** — act on every open `memory_promotion` finding (see the section below). This is the one part of the pass that closes work items, not just files.
```

Then add this section immediately before the `## Caps per run` heading:

```markdown
## Promotion queue (memory_promotion findings)

`memory_distil` flags a session note when it judges the note durable enough to
belong in the records repo as a real `memory/*.md` file. Each flag is an open
`memory_promotion` finding. Work every one of them, and close every one of them
— a finding you leave open is one nobody will look at again.

Read the queue:

```
brain_proactive(finding_type="memory_promotion")
```

Each finding gives you:

- `id` — pass this to `brain_finding_resolve`
- `ref_id` — **this is the note's `doc_id`**; read the full note with `brain_read(ref_id)`
- `detail` — the distiller's `reason=` and `target_hint=`
- `org` — the note's org tag, if it had one

For each finding, read the note, compare it against the existing `memory/*.md`
files and the `MEMORY.md` index, then take exactly one of three actions:

**promote** — the fact is durable and not already recorded. Write it through the
tool, never by hand:

```
brain_memory_write(slug="<short-kebab-slug>", description="<one-line hook>",
                   body="<the fact, plus Why: / How to apply: for feedback and project types>",
                   memory_type="user|feedback|project|reference")
```

The daemon writes `memory/<slug>.md` and the `MEMORY.md` pointer and commits it
within a cycle. Do not also create the file yourself.

**merge** — the fact already lives in an existing memory file. Fold it into that
file, fix the `MEMORY.md` description if it has drifted, and do not create a new
file.

**dismiss** — the note is not durable after all (one-off status, superseded, or
already captured somewhere better). Change nothing.

Then close the finding, in all three cases:

```
brain_finding_resolve(finding_id=<id>, outcome="promoted|merged|dismissed",
                      note="<one line on what you did>")
```

`brain_finding_resolve` only accepts `memory_promotion`. Every other finding type
is resolved automatically by the enrichment pipeline — leave those alone.

Promotions and merges are memory file updates and count against the cap below.
Dismissals touch no files, so they are uncapped: clear the whole queue of them in
one run.
```

- [ ] **Step 4: Run the routine test**

Run: `pytest tests/test_gardener_routine.py -v -p no:xdist`
Expected: all 3 PASS.

- [ ] **Step 5: Check the routine still loads through `brain_routine`**

Run:
```bash
python3 -c "
from mcpbrain.mcp_server import _routine_instructions
text = _routine_instructions('gardener')
assert text and 'memory_promotion' in text, 'routine did not load'
print('gardener routine loads,', len(text), 'chars')
"
```
Expected: `gardener routine loads, <N> chars`

- [ ] **Step 6: Run the gardener + routine suites**

Run: `pytest tests/test_gardener_routine.py tests/test_phase2_gardener.py tests/test_enrich_routine.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add mcpbrain/routines/gardener.md tests/test_gardener_routine.py
git commit -m "feat(gardener): work the memory_promotion queue

memory_distil recorded memory_promotion findings that nothing ever read
or resolved. The weekly gardener now reads the queue, promotes through
brain_memory_write, merges, or dismisses, and closes every finding."
```

---

## Done criteria

- `pytest tests/test_enrich_blocks.py tests/test_prepare.py tests/test_drain.py tests/test_mcp_server.py tests/test_mcp_server_stdio.py tests/test_lint.py tests/test_gardener_routine.py tests/test_phase2_gardener.py -q` passes.
- `ruff check mcpbrain/ tests/` is clean.
- Five commits on `main`, unpushed.
- Report to Josh so he can run the full suite, then decide separately about restarting the local daemon and about releasing.

## Deliberately not in this plan

- The `LIMIT 50` / `LIMIT 30` lint caps (spec: out of scope). The dashboard will keep reading 50 orphans against a true 404 until that is tackled.
- Any version bump or release step.
- Any manual SQL against the live store — Task 3's sweep closes the stranded rows on the next lint run by itself.
