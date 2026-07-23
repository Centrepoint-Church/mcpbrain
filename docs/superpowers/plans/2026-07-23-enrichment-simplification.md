# Enrichment Simplification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove accreted overcomplication and a half-finished queue migration from the enrichment stack, and collapse the hourly + backfill orchestration into a single stateless, queue-driven loop.

**Architecture:** Eight independent tasks. Tasks 1–4 are behaviour-preserving refactors (shared helpers, single sources, call-time config). Task 5 fixes a token leak. Task 6 deletes the dead `pending.json` path. Tasks 7–8 rewrite the one orchestration prompt and delete the redundant backfill skill. Each task is TDD with its own commit.

**Tech Stack:** Python 3, pytest. No new dependencies.

## Global Constraints

- **Design source of truth:** `docs/superpowers/specs/2026-07-23-enrichment-simplification-design.md`. Every task traces to it.
- **Behaviour-preserving except two:** Task 5 (#1) reduces worker token cost; Task 4 (#7) makes `unit_pull_cap` config take effect next cycle instead of on restart. Everything else must not change observable behaviour.
- **Test scope:** run only the edited + directly-impacted test files (the operator runs the full suite separately). Never assert a wave count or subagent budget anywhere.
- **`brain_enrich_push` validation is untouched** — it is the store-write safety boundary, not a duplicate of the deleted reply-match.
- **Keep `enrich_mode="spool"` config value** for backward compat; only remove misleading comments.
- **`mcpbrain/maintenance/*` is dev-only** (excluded from the wheel via pyproject `exclude`), so deleting files there is release-safe.
- Commit message trailer (every commit):
  ```
  Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01259MwdcBMPrAzm4snYioGu
  ```

---

## File Structure

- `mcpbrain/thread_enrich.py` — Task 1: gains `message_identity()`; `_group_key` + `reassemble_thread` call it.
- `mcpbrain/drain.py` — Task 2: gains `_give_up_or_bump()`; two sites call it.
- `mcpbrain/enrich_blocks.py` — Task 3: **new**, single source for the synthesis answer-block set.
- `mcpbrain/mcp_server.py` — Tasks 3, 4, 5: import block set; cap read at call time; `with_rules` wired through.
- `mcpbrain/prepare.py` — Tasks 3, 4, 6: import block set; `write_units` reads cap at call time; delete `prepare()` + `_write_pending()`.
- `mcpbrain/config.py` — Task 4: docstring update only.
- `mcpbrain/daemon.py` — Task 6: docstring + stale-comment fixes.
- `bin/drain_backlog.py`, `mcpbrain/maintenance/extractor_io.py` — Task 6: **deleted**.
- `mcpbrain/routines/enrich.md` — Task 7: rewritten queue-driven loop.
- `plugin/skills/mcpbrain-backfill/` — Task 8: **deleted**; refs in `plugin/agents/enrich-batch.md`, `plugin/commands/install.md`, `docs/ARCHITECTURE.md` updated.
- Tests: `tests/test_thread_enrich.py` (or existing), `tests/test_drain*.py`, `tests/test_prepare.py`, `tests/test_mcp_enrich*.py`, `tests/test_plugin_assets.py`, `tests/test_package_data.py`.

---

## Task 1: Shared `message_identity` helper (#3)

**Files:**
- Modify: `mcpbrain/thread_enrich.py:47-65` (`_group_key`), `:109-124` (`reassemble_thread` loop)
- Test: `tests/test_thread_enrich.py` (create if absent)

**Interfaces:**
- Produces: `thread_enrich.message_identity(meta: dict, doc_id: str) -> str` — the per-message identity `file_id → message_id → doc_id`. `_group_key(chunk)` returns `meta.thread_id or message_identity(meta, doc_id)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_thread_enrich.py
from mcpbrain import thread_enrich


def test_message_identity_precedence():
    assert thread_enrich.message_identity({"file_id": "F", "message_id": "M"}, "D") == "F"
    assert thread_enrich.message_identity({"message_id": "M"}, "D") == "M"
    assert thread_enrich.message_identity({}, "D") == "D"


def test_group_key_layers_thread_id_over_message_identity():
    # thread_id wins for batching...
    assert thread_enrich._group_key(
        {"metadata": {"thread_id": "T", "file_id": "F"}, "doc_id": "D"}) == "T"
    # ...but with no thread_id it falls to the shared message identity (file_id first)
    assert thread_enrich._group_key(
        {"metadata": {"file_id": "F", "message_id": "M"}, "doc_id": "D"}) == "F"


def test_reassemble_uses_message_identity_for_drive_and_email():
    chunks = [
        {"doc_id": "gdrive-F-0", "text": "a",
         "metadata": {"file_id": "F", "chunk_index": 0, "file_name": "Doc"}},
        {"doc_id": "gdrive-F-1", "text": "b",
         "metadata": {"file_id": "F", "chunk_index": 1, "file_name": "Doc"}},
        {"doc_id": "m1", "text": "hi",
         "metadata": {"message_id": "m1", "date": "2026-01-01"}},
    ]
    msgs = thread_enrich.reassemble_thread(chunks)
    ids = {m["message_id"] for m in msgs}
    assert ids == {"F", "m1"}  # Drive collapses to file_id; email keeps message_id
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_thread_enrich.py -v`
Expected: FAIL — `AttributeError: module 'mcpbrain.thread_enrich' has no attribute 'message_identity'`

- [ ] **Step 3: Add the helper and route both consumers through it**

In `mcpbrain/thread_enrich.py`, add above `_group_key` (after the `_CHUNK_JOIN` constant, ~line 28):

```python
def message_identity(meta: dict, doc_id: str) -> str:
    """Per-message identity shared by reassemble_thread (which emits it as a
    message's `message_id`) and store.doc_ids_for_messages (which resolves it back
    to chunks). Precedence: Drive `file_id` (whole doc = one message) → email
    `message_id` → `doc_id` fallback. Keeping this in ONE place is the invariant
    that the 0.7.98 Drive-enrichment bug violated (the emitted id must be
    resolvable back to the same chunks)."""
    return meta.get("file_id") or meta.get("message_id") or doc_id
```

Replace `_group_key` body (lines 63-65) with:

```python
    meta = chunk.get("metadata") or {}
    return meta.get("thread_id") or message_identity(meta, chunk["doc_id"])
```

In `reassemble_thread`, replace the identity derivation (lines 116-120) with:

```python
        mid = message_identity(meta, chunk["doc_id"])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_thread_enrich.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Run the existing enrichment-grouping tests to confirm no regression**

Run: `pytest tests/test_prepare.py tests/ -k "group or reassemble or drive or thread_enrich" -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/thread_enrich.py tests/test_thread_enrich.py
git commit -m "refactor(enrich): single message_identity helper for group/reassemble (#3)

<trailer>"
```

---

## Task 2: `_give_up_or_bump` helper in drain (#5)

**Files:**
- Modify: `mcpbrain/drain.py:368-381` (invalid-extraction site), `:435-442` (matched-no-chunk site)
- Test: `tests/test_drain_giveup.py` (create)

**Interfaces:**
- Produces: `drain._give_up_or_bump(store, doc_ids: list[str], summary: dict) -> None` — bump `enrich_attempts`; at `>= _EMPTY_ATTEMPT_CAP` call `store.mark_enriched(doc_ids)` and increment `summary["gave_up"]`. Never raises (bookkeeping must not break drain).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_drain_giveup.py
from mcpbrain import drain


class _FakeStore:
    def __init__(self, attempts):
        self._attempts = attempts
        self.marked = []
    def bump_enrich_attempts(self, doc_ids):
        return self._attempts
    def mark_enriched(self, doc_ids):
        self.marked.extend(doc_ids)


def test_give_up_marks_when_cap_reached():
    s = _FakeStore(attempts=drain._EMPTY_ATTEMPT_CAP)
    summary = {}
    drain._give_up_or_bump(s, ["d1", "d2"], summary)
    assert s.marked == ["d1", "d2"]
    assert summary["gave_up"] == 1


def test_give_up_only_bumps_below_cap():
    s = _FakeStore(attempts=1)
    summary = {}
    drain._give_up_or_bump(s, ["d1"], summary)
    assert s.marked == []
    assert "gave_up" not in summary


def test_give_up_swallows_store_errors():
    class _Boom:
        def bump_enrich_attempts(self, d):
            raise RuntimeError("db locked")
    drain._give_up_or_bump(_Boom(), ["d1"], {})  # must not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_drain_giveup.py -v`
Expected: FAIL — `AttributeError: module 'mcpbrain.drain' has no attribute '_give_up_or_bump'`

- [ ] **Step 3: Add the helper**

In `mcpbrain/drain.py`, after `_EMPTY_ATTEMPT_CAP = 3` (line 51):

```python
def _give_up_or_bump(store, doc_ids, summary) -> None:
    """Bump the empty-attempt counter for these chunks; once it reaches
    _EMPTY_ATTEMPT_CAP, consume them (mark_enriched) so a genuinely
    un-extractable doc stops re-queuing forever. Best-effort — bookkeeping must
    never break a drain run."""
    if not doc_ids:
        return
    try:
        attempts = store.bump_enrich_attempts(doc_ids)
        if attempts >= _EMPTY_ATTEMPT_CAP:
            store.mark_enriched(doc_ids)
            summary["gave_up"] = summary.get("gave_up", 0) + 1
    except Exception as exc:  # noqa: BLE001 — bookkeeping must not break drain
        log.debug("drain: attempt-cap bookkeeping failed: %s", exc)
```

- [ ] **Step 4: Replace the two inline blocks with calls**

At the invalid-extraction site (currently `drain.py:368-380`, the `try/except` that computes `_mids`/`_dids` and bumps), replace with:

```python
                _mids = [m.get("message_id") for m in (extraction.get("messages") or [])
                         if m.get("message_id")]
                _dids = store.doc_ids_for_messages(_mids) if _mids else []
                _give_up_or_bump(store, _dids, summary)
```

At the matched-no-chunk site (currently `drain.py:435-442`, the `if _unit_dids:` try/except), replace that block with:

```python
                _give_up_or_bump(store, _unit_dids, summary)
```

(Leave the surrounding `_unit_mids`/`_unit_dids` computation and the `log.warning` + `summary["skipped"]` lines intact.)

- [ ] **Step 5: Run tests to verify pass + no drain regression**

Run: `pytest tests/test_drain_giveup.py tests/ -k drain -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/drain.py tests/test_drain_giveup.py
git commit -m "refactor(drain): extract _give_up_or_bump; dedupe two give-up sites (#5)

<trailer>"
```

---

## Task 3: Single source for the synthesis answer-block set (#6)

**Context:** `_ENRICH_ANSWER_BLOCKS` (mcp_server, 5 keys) and `_UNIT_BLOCKS` (prepare, `merge_review` + the same 5) are hand-kept and can drift. `BLOCK_DRAINERS` (drain) is a **separate** review/curator registry — NOT the same set — and stays independent (unifying it would flatten a real distinction). Single-source only the shared answer blocks.

**Files:**
- Create: `mcpbrain/enrich_blocks.py`
- Modify: `mcpbrain/mcp_server.py:492-493`, `mcpbrain/prepare.py:680-681`
- Test: `tests/test_enrich_blocks.py` (create)

**Interfaces:**
- Produces: `enrich_blocks.ANSWER_BLOCKS: tuple[str, ...]` (synthesis, profile_synthesis, community_synthesis, memory_distil, profile_audit); `enrich_blocks.UNIT_BLOCKS: tuple[str, ...]` (`("merge_review", *ANSWER_BLOCKS)`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_enrich_blocks.py
from mcpbrain import enrich_blocks, mcp_server, prepare


def test_unit_blocks_is_merge_review_plus_answer_blocks():
    assert enrich_blocks.UNIT_BLOCKS == ("merge_review", *enrich_blocks.ANSWER_BLOCKS)


def test_consumers_derive_from_single_source():
    assert mcp_server._ENRICH_ANSWER_BLOCKS == enrich_blocks.ANSWER_BLOCKS
    assert prepare._UNIT_BLOCKS == enrich_blocks.UNIT_BLOCKS


def test_merge_review_is_a_unit_block_not_an_answer_block():
    assert "merge_review" in enrich_blocks.UNIT_BLOCKS
    assert "merge_review" not in enrich_blocks.ANSWER_BLOCKS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_enrich_blocks.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcpbrain.enrich_blocks'`

- [ ] **Step 3: Create the single source**

```python
# mcpbrain/enrich_blocks.py
"""Single source of truth for the enrichment block-type sets.

ANSWER_BLOCKS — the optional answer blocks a subagent may push via
brain_enrich_push (beyond extractions + merge_answers), each drained by the
daemon. UNIT_BLOCKS — the block-unit kinds the producer emits: merge_review
plus every answer block. Keeping both here means adding a block type is a
one-line change instead of editing mcp_server + prepare in lockstep.

NOTE: drain.BLOCK_DRAINERS (review_*/org_merge_review) is a SEPARATE registry
for review/curator blocks and is intentionally not derived from here.
"""

ANSWER_BLOCKS = ("synthesis", "profile_synthesis", "community_synthesis",
                 "memory_distil", "profile_audit")

UNIT_BLOCKS = ("merge_review", *ANSWER_BLOCKS)
```

- [ ] **Step 4: Point both consumers at it**

In `mcpbrain/mcp_server.py`, replace the `_ENRICH_ANSWER_BLOCKS = (...)` definition (lines 492-493) with:

```python
from mcpbrain.enrich_blocks import ANSWER_BLOCKS as _ENRICH_ANSWER_BLOCKS
```

(Place the import with the other module-level imports if the linter prefers; a local alias assignment at the same spot is acceptable. Keep the explanatory comment above it.)

In `mcpbrain/prepare.py`, replace the `_UNIT_BLOCKS = (...)` definition (lines 680-681) with:

```python
from mcpbrain.enrich_blocks import UNIT_BLOCKS as _UNIT_BLOCKS
```

- [ ] **Step 5: Run tests + affected consumers**

Run: `pytest tests/test_enrich_blocks.py tests/ -k "enrich or prepare or block" -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/enrich_blocks.py mcpbrain/mcp_server.py mcpbrain/prepare.py tests/test_enrich_blocks.py
git commit -m "refactor(enrich): single source for answer/unit block sets (#6)

<trailer>"
```

---

## Task 4: Size caps read config at call time (#7)

**Context:** `_UNIT_PULL_CAP` (prepare) and `_PULL_MAX_CHARS` (mcp_server) freeze `config.unit_pull_cap()` at import, so a config change needs a restart. `_PULL_MAX_CHARS` is used only by a lockstep test; the only real consumer is `write_units`'s `pull_cap` default.

**Files:**
- Modify: `mcpbrain/prepare.py:678` + `:706-707` (`write_units` signature/body), `mcpbrain/mcp_server.py:458` (remove `_PULL_MAX_CHARS`)
- Modify: `tests/test_prepare.py:851-862` (lockstep test → call-time test)

**Interfaces:**
- Produces: `write_units(data, *, home=None, pull_cap=None, window=600)` — when `pull_cap is None`, reads `config.unit_pull_cap(home)` at call time.

- [ ] **Step 1: Write the failing test** (replace the existing lockstep test in `tests/test_prepare.py`, lines ~851-862)

```python
def test_write_units_reads_cap_at_call_time(tmp_path, monkeypatch):
    from mcpbrain import prepare, config
    calls = {"n": 0}
    def _fake_cap(home=None):
        calls["n"] += 1
        return 12_345
    monkeypatch.setattr(config, "unit_pull_cap", _fake_cap)
    prepare.write_units({"context": {}, "threads": []}, home=str(tmp_path))
    assert calls["n"] >= 1, "write_units must read unit_pull_cap at call time, not import"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_prepare.py::test_write_units_reads_cap_at_call_time -v`
Expected: FAIL — `unit_pull_cap` not called (cap was frozen at import), `calls["n"] == 0`.

- [ ] **Step 3: Make `write_units` read config at call time**

In `mcpbrain/prepare.py`, delete the `_UNIT_PULL_CAP = config.unit_pull_cap()` line (678) and change the `write_units` signature (706-707) and the budget line (729):

```python
def write_units(data: dict, *, home=None, pull_cap=None,
                window: int = 600) -> dict:
```

Immediately after the docstring, before `from pathlib import Path`, resolve the cap:

```python
    if pull_cap is None:
        pull_cap = config.unit_pull_cap(home)
```

(The existing `budget = max(2000, pull_cap - _UNIT_RULES_RESERVE - ctx_len - 1500)` now uses the resolved value.) Update the frozen-at-import comment block (670-677) to state the cap is read at call time.

- [ ] **Step 4: Remove the dead frozen constant in mcp_server**

In `mcpbrain/mcp_server.py`, delete `_PULL_MAX_CHARS = config.unit_pull_cap()` (line 458) and its comment (453-457). Update `config.unit_pull_cap`'s docstring (config.py:607-610) to drop the "`mcp_server._PULL_MAX_CHARS`" reference and the "FROZEN AT IMPORT" framing.

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_prepare.py -k "cap or write_units or units" -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/prepare.py mcpbrain/mcp_server.py mcpbrain/config.py tests/test_prepare.py
git commit -m "refactor(enrich): read unit_pull_cap at call time, not import (#7)

<trailer>"
```

---

## Task 5: Wire `with_rules` through `brain_enrich_pull` (#1)

**Context:** `brain_enrich_pull` accepts `with_rules` but the tool schema omits it and the dispatch never forwards it, so it is permanently `True` — every pull re-sends the ~11 KB rules block uncached even though workers already carry it in their cached system prompt.

**Files:**
- Modify: `mcpbrain/mcp_server.py:1071-1074` (schema), `:1240` (dispatch)
- Test: `tests/test_mcp_enrich_with_rules.py` (create)

**Interfaces:**
- Consumes: `make_brain_enrich_pull(home)` returning `brain_enrich_pull(unit_id, with_rules=True)` (unchanged signature — inner behaviour is already tested in `tests/test_mcp_enrich_meeting_tools.py:190`).
- Produces: the `brain_enrich_pull` **tool schema** declares `with_rules`, and the **dispatch** forwards it. This is the gap — the inner function already honours the arg; the schema omits it and dispatch drops it, so over MCP it's permanently `True`.

- [ ] **Step 1: Write the failing test** (dispatch-level, via the stdio session harness — mirror the setup in `tests/test_mcp_server_stdio.py`, which already spins up a client session and calls `session.list_tools()` / `session.call_tool(...)`)

```python
# tests/test_mcp_enrich_with_rules.py
# Reuse the client-session fixture/setup from tests/test_mcp_server_stdio.py
# (same import of ClientSession + stdio server construction over a seeded home).
import json


def _seed_unit(home):
    from pathlib import Path
    q = Path(home) / "enrich_queue"
    (q / "units").mkdir(parents=True, exist_ok=True)
    (q / "context.json").write_text(json.dumps({"owner_name": "Josh"}))
    (q / "units" / "u-abc.json").write_text(json.dumps(
        {"unit_id": "u-abc", "kind": "thread", "threads": [{"thread_id": "t1"}]}))


async def test_pull_schema_declares_with_rules(mcp_session):
    # mcp_session == the connected ClientSession built exactly as in
    # test_mcp_server_stdio.py's harness.
    tools = (await mcp_session.list_tools()).tools
    pull = next(t for t in tools if t.name == "brain_enrich_pull")
    assert "with_rules" in pull.inputSchema["properties"]


async def test_pull_dispatch_forwards_with_rules(mcp_session, home):
    _seed_unit(home)
    res = await mcp_session.call_tool("brain_enrich_pull",
                                      {"unit_id": "u-abc", "with_rules": False})
    payload = json.loads(res.content[0].text)
    assert "rules" not in payload   # dispatch honoured with_rules=False
    res2 = await mcp_session.call_tool("brain_enrich_pull", {"unit_id": "u-abc"})
    assert "rules" in json.loads(res2.content[0].text)   # default stays True
```

> Implementer note: lift the exact session/home fixtures from `tests/test_mcp_server_stdio.py` (it already constructs the server over a temp home and yields a `ClientSession`). The two assertions above are the load-bearing TDD checks.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_mcp_enrich_with_rules.py -v`
Expected: FAIL — `test_pull_schema_declares_with_rules` fails (schema has no `with_rules`), and `test_pull_dispatch_forwards_with_rules` fails (dispatch drops the arg, so `rules` is present even with `with_rules=False`).

- [ ] **Step 3: Add `with_rules` to the schema and forward it in dispatch**

In `mcpbrain/mcp_server.py`, the `brain_enrich_pull` tool `inputSchema` (1071-1074) becomes:

```python
                inputSchema={"type": "object", "properties": {
                    "unit_id": {"type": "string",
                                "description": "the unit to fetch (from brain_enrich_units)"},
                    "with_rules": {"type": "boolean",
                                   "description": "include the full extraction rules in the "
                                                  "response (default true). enrich-batch workers "
                                                  "pass false — they already carry the rules in "
                                                  "their cached system prompt, so re-sending here "
                                                  "would pay for them twice."},
                }, "required": ["unit_id"]},
```

The dispatch (1240) becomes:

```python
            out = await enrich_pull(unit_id=arguments.get("unit_id", ""),
                                    with_rules=arguments.get("with_rules", True))
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_mcp_enrich_with_rules.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/mcp_server.py tests/test_mcp_enrich_with_rules.py
git commit -m "fix(enrich): forward with_rules through brain_enrich_pull; stop double-sending rules (#1)

<trailer>"
```

---

## Task 6: Delete the legacy `pending.json` / `batch_id` path (#2)

**Context:** Production uses the per-unit queue (`prepare_units`/`write_units`). `prepare.prepare()` + `_write_pending()` (test-only), `bin/drain_backlog.py`, and `mcpbrain/maintenance/extractor_io.py` still speak the dead `pending.json`/`batch_id` dialect.

**Files:**
- Delete: `bin/drain_backlog.py`, `mcpbrain/maintenance/extractor_io.py`
- Modify: `mcpbrain/prepare.py` (remove `prepare()` 842-862 and `_write_pending()` 633-644), `mcpbrain/daemon.py:344` (docstring) + `:490` (stale comment)
- Modify/redirect tests: `tests/test_prepare.py`, `tests/test_integration_spool.py`, `tests/test_daemon_p3.py`, `tests/test_package_data.py`, `tests/helpers/stub_extractor.py`, `tests/e2e/test_full_loop.py`

- [ ] **Step 1: Find every live reference to the doomed symbols**

Run:
```bash
grep -rn "extractor_io\|drain_backlog\|prepare\.prepare(\|_write_pending\|\.prepare(store" mcpbrain bin tests
```
Expected: references only in the files listed above. If any *production* module (not `bin/`, not `tests/`, not `maintenance/`) imports them, STOP and reconcile before deleting.

- [ ] **Step 2: Delete the dead files**

```bash
git rm bin/drain_backlog.py mcpbrain/maintenance/extractor_io.py
```

- [ ] **Step 3: Remove `prepare()` and `_write_pending()` from prepare.py**

Delete `def _write_pending(...)` (lines ~633-644) and `def prepare(...)` (lines ~842-862) from `mcpbrain/prepare.py`. Leave `prepare_units`, `build_pending`, `write_units`, `attach_extra_blocks` intact.

- [ ] **Step 4: Fix daemon docstring + stale comment**

In `mcpbrain/daemon.py`, the `run_cycle` docstring (line ~344) currently says `"spool": prepare.prepare writes pending.json ...`. Replace with:

```
      - "spool": prepare.prepare_units writes immutable work units to
        enrich_queue/units/, then drain.drain applies whatever an out-of-band
        extractor session has pushed to enrich_inbox/ since last cycle.
```

At line ~490, replace the comment `# Enrichment source: spool | gemini | off.` with:

```python
        # Enrichment source: "spool" (the per-unit work queue) or "off". Defaults
        # to "off" so a newly-constructed daemon enriches nothing until configured.
```

- [ ] **Step 5: Redirect the tests off the deleted symbols**

For each test file, replace `prepare.prepare(...)`/`_write_pending` usage with `prepare.prepare_units(...)` or `prepare.build_pending(...)` (they exercise noise-filter / merge-review / assembly, which survive). In `tests/test_package_data.py`, remove the `extractor_io` reference. Delete `tests/helpers/stub_extractor.py`'s `drain_backlog`/`extractor_io` couplings (or the helper entirely if unused after). Concretely, run the suite for these files and fix each failure to the surviving API:

```bash
pytest tests/test_prepare.py tests/test_integration_spool.py tests/test_daemon_p3.py tests/test_package_data.py tests/e2e/test_full_loop.py -x -v
```

Fix import/attribute errors by pointing at `prepare_units`/`build_pending`. Any test whose sole purpose was the legacy `pending.json` shape is deleted (note each deletion in the commit body).

- [ ] **Step 6: Run the redirected tests to green**

Run: `pytest tests/test_prepare.py tests/test_integration_spool.py tests/test_daemon_p3.py tests/test_package_data.py tests/e2e/test_full_loop.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor(enrich): delete dead pending.json/batch_id path (#2)

Remove prepare.prepare()/_write_pending (test-only), bin/drain_backlog.py,
and mcpbrain/maintenance/extractor_io.py; redirect tests to prepare_units/
build_pending. Fix daemon docstring + stale 'gemini' comment.

<trailer>"
```

---

## Task 7: Rewrite the enrich routine as a stateless queue-driven loop (#4, #8, #9)

**Files:**
- Rewrite: `mcpbrain/routines/enrich.md`
- Test: `tests/test_enrich_routine.py` (create) — asserts the routine is queue-driven and free of wave/budget/reply-string language.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_enrich_routine.py
from pathlib import Path

_ROUTINE = Path(__file__).parent.parent / "mcpbrain" / "routines" / "enrich.md"


def test_routine_is_queue_driven_and_capless():
    text = _ROUTINE.read_text()
    low = text.lower()
    # Terminator is the empty queue, not a wave/budget cap.
    assert "brain_enrich_units" in text
    assert "empty" in low
    assert "15 wave" not in low and "10 wave" not in low
    assert "budget" not in low
    # No reply string-match contract.
    assert "unit <unit_id>:" not in text
    assert "requeue guard" not in low
    # Still fans out one Haiku subagent per unit and nudges the daemon.
    assert "enrich-batch" in text
    assert "haiku" in low
    assert "brain_enrich_advance" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_enrich_routine.py -v`
Expected: FAIL (current routine contains the wave cap + `unit <unit_id>:` contract).

- [ ] **Step 3: Rewrite `mcpbrain/routines/enrich.md`**

Replace the whole file with:

```markdown
# Brain enrichment — work queue

Drain the pending enrichment work units through the mcpbrain MCP tools. You are the
**orchestrator**: you hand each unit to a fresh subagent, so your own context never
holds email bodies — only unit ids. You keep NO per-unit state. Self-contained —
needs no skill or command file.

**Models:** you (the coordinator) run on **Sonnet** — the scheduled task runs in
**Auto permission mode**, which Claude Code only offers on Sonnet, so a Haiku
coordinator would stall on prompts. Every `enrich-batch` subagent runs on **Haiku**,
set **explicitly per dispatch** (the agent frontmatter is not always honored); that
is where the volume and the cost savings live.

## Loop

1. Call **`brain_enrich_units`**. If it returns `{"empty": true}`, stop and report
   `DONE: queue empty`.
2. Otherwise it returns `units` — a list of `{unit_id, kind, block, count}`. For
   **each unit**, spawn the **`enrich-batch`** subagent (Task tool,
   `subagent_type: enrich-batch`, **`model: haiku`** set explicitly). Fan out up to
   ~12 in parallel per message. Give each subagent EXACTLY this one line, with the
   unit's `unit_id` substituted (the agent already carries the extraction protocol —
   do not repeat it):

   > Enrich unit `<unit_id>`. Act autonomously; do not ask questions.

3. When the wave's subagents return, **do not parse their replies**. Call
   **`brain_enrich_advance`** — the daemon drains every pushed result, applies it,
   and deletes that unit from the queue.
4. Go back to step 1. A unit that was enriched is gone from the queue; a unit that
   was NOT (its subagent derailed, or is still running under its lease) simply
   re-appears on a later list once its 15-minute claim lease expires, and you
   dispatch it again. Done-ness is queue state, never reply text.
5. Stop when `brain_enrich_units` returns `{"empty": true}`. There is no wave cap
   and no subagent budget — if this session runs out of subagent capacity before the
   queue empties, that is fine: report what remains and the next run (or a re-run)
   continues. **Backfill is just re-running this routine.**
6. Report: `DONE: queue empty` or `PARTIAL: units still pending — re-run to continue`.

Never pull unit payloads into your own context — each subagent pulls its own unit
(`brain_enrich_pull(unit_id=…, with_rules=false)`) and pushes its own result
(`brain_enrich_push(unit_id=…)`). Use the MCP tools only; do not read skill/command
files or shell into the queue.
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_enrich_routine.py -v`
Expected: PASS

- [ ] **Step 5: Update any test asserting the OLD routine contract**

Run:
```bash
grep -rln "unit <unit_id>\|requeue guard\|15 wave\|wave cap" tests/
```
For each hit (e.g. a `test_plugin_assets`/`test_mcp_enrich_meeting_tools` assertion tied to the removed contract), delete or update it to the queue-driven language. Do NOT touch `test_enrich_agent_rules_in_sync` (rules sync is unrelated and stays).

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/routines/enrich.md tests/test_enrich_routine.py tests/
git commit -m "refactor(enrich): stateless queue-driven routine; drop reply-match + wave cap (#4,#8,#9)

<trailer>"
```

---

## Task 8: Delete the backfill skill and update references (#9)

**Files:**
- Delete: `plugin/skills/mcpbrain-backfill/` (whole directory)
- Modify: `plugin/agents/enrich-batch.md:9`, `plugin/commands/install.md` (backfill mention), `docs/ARCHITECTURE.md` (backfill mention)
- Modify tests: `tests/test_plugin_assets.py:71-99` (remove `test_backfill_skill_exists` + `test_backfill_skill_orchestrates_loop`; add gone-assertion)

- [ ] **Step 1: Write the failing test** — in `tests/test_plugin_assets.py`, delete `test_backfill_skill_exists` (71-72) and `test_backfill_skill_orchestrates_loop` (~90-99), and add:

```python
def test_backfill_skill_removed():
    # Backfill is now just re-running the enrich routine; the separate skill is gone.
    assert not (_PLUGIN / "skills" / "mcpbrain-backfill").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_plugin_assets.py::test_backfill_skill_removed -v`
Expected: FAIL — the directory still exists.

- [ ] **Step 3: Delete the skill directory**

```bash
git rm -r plugin/skills/mcpbrain-backfill
```

- [ ] **Step 4: Update the references**

- `plugin/agents/enrich-batch.md:9` — change "the hourly enrich routine and the backfill skill" to "the enrich routine (hourly, and re-run on demand to backfill)".
- `plugin/commands/install.md` — find the backfill-skill line (`grep -n backfill plugin/commands/install.md`) and remove/adjust it so it no longer references a `/backfill` skill; backfilling is "re-run the `Brain — enrich (hourly)` task."
- `docs/ARCHITECTURE.md` — `grep -n backfill docs/ARCHITECTURE.md`; update the prose to describe backfill as re-running the enrich routine, not a separate skill.

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_plugin_assets.py -v`
Expected: PASS (no backfill tests remain; `test_backfill_skill_removed` passes)

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "refactor(plugin): delete mcpbrain-backfill skill; backfill = re-run enrich routine (#9)

<trailer>"
```

---

## Final verification (not a task — a checkpoint)

After all eight tasks, before any release:

```bash
ruff check mcpbrain tests
pytest tests/ -q     # operator runs the full suite
```

Confirm: no import of `extractor_io`, `drain_backlog`, `prepare.prepare`, `_write_pending`, `_PULL_MAX_CHARS`, or `_UNIT_PULL_CAP` remains (`grep -rn` each). The enrich routine is the only orchestration prompt. **This is a `plugin/` change — releasing requires the normal three-repo sync (see `docs/RELEASE-RUNBOOK.md`); do not release without an explicit instruction.**

---

## Self-review notes

- **Spec coverage:** #1→T5, #2→T6, #3→T1, #4/#8/#9→T7+T8, #5→T2, #6→T3, #7→T4. All findings mapped.
- **Deviation from spec #6:** the spec's wording implied `BLOCK_DRAINERS` would also derive from the single source; it is a genuinely different set (review/curator blocks), so it stays independent. Documented in Task 3 context and `enrich_blocks.py`.
- **Task 5 nuance:** the inner `brain_enrich_pull` already honoured `with_rules`; the real fix is schema+dispatch. Tests assert the dispatch path, and the schema declares the field.
- **Ordering:** pure/behaviour-preserving refactors (T1–T4) and the token fix (T5) land before the deletions (T6) and prose (T7–T8), so a reviewer can reject the risky prose/deletion tasks without unwinding the safe refactors.
