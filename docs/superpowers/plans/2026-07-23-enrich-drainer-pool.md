# Enrichment Drainer Pool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut enrichment subagent startup + coordinator↔subagent comms overhead by replacing "one subagent per unit" with a work-stealing pool of looping drainers.

**Architecture:** Two new additive MCP tools — `brain_enrich_claim` (folds `units`+`pull` into one atomically-leased call) and `brain_enrich_pending` (non-claiming queue count). The `enrich-batch` subagent loops `claim → extract → push` up to K units; the coordinator routine spawns N such drainers per wave until `pending: 0`. Existing `units`/`pull`/`push`/`advance` are unchanged.

**Tech Stack:** Python 3.12, MCP server (`mcpbrain/mcp_server.py`), pytest, markdown agent/routine prompts.

## Global Constraints

- Existing enrich tools (`brain_enrich_units`, `brain_enrich_pull`, `brain_enrich_push`, `brain_enrich_advance`) MUST remain behaviourally unchanged — the general-purpose self-contained path (`units` → `pull(with_rules=True)`) still works.
- Lease TTL is `_LEASE_TTL_S` (15 min) — reuse, do not redefine.
- The `enrich-batch.md` Extraction-rules block (between `<!-- SHARED-EXTRACTION-RULES:BEGIN/END -->`) MUST stay byte-identical to `mcpbrain/_enrich_rules()` (`test_enrich_agent_rules_in_sync`). Only the protocol prose above it changes; do NOT hand-edit the rules block — if it ever drifts, run `python bin/sync_agents.py`.
- Defaults: pool size N=10, units-per-drainer K=5. K overridable via env `MCPBRAIN_ENRICH_UNITS_PER_DRAINER`.
- Run tests with `.venv/bin/python -m pytest`. Lint with `.venv/bin/ruff check mcpbrain/`.
- Test scope: run edited + directly-impacted test files only (the maintainer runs the full suite).

---

### Task 1: Shared lease helpers + `_unit_payload` extraction

Factor the lease logic and payload-building so `claim`/`pending` reuse them and `units`/`pull` stay DRY. No behaviour change yet — this is a pure refactor guarded by existing tests.

**Files:**
- Modify: `mcpbrain/mcp_server.py` (add helpers near `_claims_dir` at line 513; refactor `make_brain_enrich_units` ~518-561 and `make_brain_enrich_pull` ~564-612 to use them)
- Test: `tests/test_mcp_enrich_meeting_tools.py` (existing — must stay green)

**Interfaces:**
- Produces:
  - `_lease_is_live(claim_path, now: float) -> bool` — True iff a claim file exists and is younger than `_LEASE_TTL_S`.
  - `_atomic_claim(claims_dir, uid: str, now: float) -> bool` — acquire uid's lease atomically (`O_CREAT|O_EXCL`); reclaim a stale (≥TTL) lease via `utime`; return True iff acquired.
  - `_unit_payload(home, d: dict, unit_id: str, with_rules: bool) -> dict` — build the pull/claim response body from an already-parsed unit dict `d` (rules?/context/kind/unit_id/threads|block+items, with the `_PULL_SOFT_LIMIT` context trim).

- [ ] **Step 1: Add the helpers** after `_claims_dir` (line ~516)

```python
def _lease_is_live(claim_path, now: float) -> bool:
    """True iff claim_path exists and its lease has not expired."""
    try:
        return claim_path.exists() and now - claim_path.stat().st_mtime < _LEASE_TTL_S
    except OSError:
        return False


def _atomic_claim(claims_dir, uid: str, now: float) -> bool:
    """Acquire uid's lease atomically. Returns True iff acquired.

    Exclusive create (O_CREAT|O_EXCL) is atomic across processes on the local
    store, so two concurrent drainers can never both take the same fresh lease.
    A stale lease (>= _LEASE_TTL_S old — crashed worker) is reclaimed via utime;
    that reclaim is the one non-atomic window (two workers could both reclaim the
    same stale lease → an idempotent double-apply downstream, never corruption).
    """
    import os
    try:
        claims_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    claim = claims_dir / uid
    try:
        fd = os.open(str(claim), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.close(fd)
        return True
    except FileExistsError:
        try:
            if now - claim.stat().st_mtime >= _LEASE_TTL_S:
                os.utime(str(claim), (now, now))
                return True
        except OSError:
            pass
        return False
    except OSError:
        return False
```

- [ ] **Step 2: Add `_unit_payload`** (lift the body-building out of `brain_enrich_pull`), placed after `_atomic_claim`

```python
def _unit_payload(home, d: dict, unit_id: str, with_rules: bool) -> dict:
    """Build the pull/claim response body from a parsed unit dict `d`.

    Rules (byte-stable) lead when included, then context, so a general-purpose
    caller's serialized prefix stays cacheable; variable per-unit fields trail.
    """
    import json as _json
    from pathlib import Path
    try:
        ctx = _json.loads((Path(home) / "enrich_queue" / "context.json").read_text())
    except (OSError, ValueError):
        ctx = {}
    out = {}
    if with_rules:
        out["rules"] = _enrich_rules()
    out["context"] = ctx
    out["kind"] = d.get("kind")
    out["unit_id"] = unit_id
    if d.get("kind") == "block":
        out["block"] = d.get("block")
        out["items"] = d.get("items") or []
    else:
        out["threads"] = d.get("threads") or []
    if len(_json.dumps(out)) > _PULL_SOFT_LIMIT:
        out["context"] = {k: ctx[k] for k in ("owner_name", "valid_orgs",
                                              "org_domain_map") if k in ctx}
    return out
```

- [ ] **Step 3: Refactor `brain_enrich_pull`** to delegate to `_unit_payload` (replace its body-building block, keeping the `unit_id` guard and the parse)

```python
def make_brain_enrich_pull(home: str):
    async def brain_enrich_pull(unit_id: str, with_rules: bool = True) -> dict:
        """(docstring unchanged)"""
        import json as _json
        if not unit_id:
            return {"empty": True}
        try:
            d = _json.loads((_units_dir(home) / f"{unit_id}.json").read_text())
        except (OSError, ValueError):
            return {"empty": True}
        return _unit_payload(home, d, unit_id, with_rules)
    return brain_enrich_pull
```

- [ ] **Step 4: Refactor `brain_enrich_units`** claim/lease-skip to use the helpers (replace the inline `claim.exists()...` check and `.touch()` with `_lease_is_live` skip + `_atomic_claim`)

```python
        ready, now = [], _time.time()
        for f in files:
            uid = f.stem
            if _lease_is_live(claims / uid, now):
                continue                              # still leased to another worker
            try:
                d = _json.loads(f.read_text())
            except (OSError, ValueError):
                continue                              # skip a half-written/garbage unit
            if not _atomic_claim(claims, uid, now):
                continue                              # lost the race to another caller
            ready.append({"unit_id": uid, "kind": d.get("kind"), "block": d.get("block"),
                          "count": len(d.get("threads") or d.get("items") or [])})
            if len(ready) >= batch:
                break
        return {"units": ready} if ready else {"empty": True}
```

- [ ] **Step 5: Run the existing enrich-tool tests to verify the refactor is behaviour-neutral**

Run: `.venv/bin/python -m pytest tests/test_mcp_enrich_meeting_tools.py tests/test_mcp_enrich_with_rules.py tests/test_integration_spool.py -q`
Expected: PASS (all previously-passing tests still pass; `test_pull_unit_leads_with_cacheable_prefix` confirms `_unit_payload` preserves key order).

- [ ] **Step 6: Ruff + commit**

```bash
.venv/bin/ruff check mcpbrain/mcp_server.py
git add mcpbrain/mcp_server.py
git commit -m "refactor(enrich): factor lease helpers + _unit_payload for reuse"
```

---

### Task 2: `brain_enrich_pending` tool

**Files:**
- Modify: `mcpbrain/mcp_server.py` (add `make_brain_enrich_pending` after `make_brain_enrich_advance` ~694)
- Test: `tests/test_enrich_pool.py` (create)

**Interfaces:**
- Consumes: `_units_dir`, `_claims_dir`, `_lease_is_live` (Task 1).
- Produces: `make_brain_enrich_pending(home: str)` → async `brain_enrich_pending() -> dict` returning `{"pending": int}` — count of unit files not under a live lease. Never claims.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_enrich_pool.py
import asyncio
import json
import os
import time

from mcpbrain import mcp_server


def _write_unit(home, uid, kind="thread", threads=None):
    d = home / "enrich_queue" / "units"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{uid}.json").write_text(json.dumps(
        {"kind": kind, "threads": threads or [{"thread_id": "t1", "body": "hi"}]}))


def _pending(home):
    return asyncio.run(mcp_server.make_brain_enrich_pending(str(home))())


def test_pending_counts_unleased_units_without_claiming(tmp_path):
    _write_unit(tmp_path, "u-a")
    _write_unit(tmp_path, "u-b")
    assert _pending(tmp_path) == {"pending": 2}
    # pending must NOT claim: a subsequent claim can still take both.
    claim = mcp_server.make_brain_enrich_claim(str(tmp_path))
    got = {asyncio.run(claim())["unit_id"], asyncio.run(claim())["unit_id"]}
    assert got == {"u-a", "u-b"}


def test_pending_excludes_live_leases(tmp_path):
    _write_unit(tmp_path, "u-a")
    _write_unit(tmp_path, "u-b")
    claims = tmp_path / "enrich_queue" / "claims"
    claims.mkdir(parents=True, exist_ok=True)
    (claims / "u-a").touch()  # live lease
    assert _pending(tmp_path) == {"pending": 1}
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_enrich_pool.py::test_pending_counts_unleased_units_without_claiming -v`
Expected: FAIL with `AttributeError: module 'mcpbrain.mcp_server' has no attribute 'make_brain_enrich_pending'` (and `make_brain_enrich_claim`; that arrives in Task 3 — this test also covers claim, so it stays red until Task 3. Run the `test_pending_excludes_live_leases` case for a claim-free RED here).

Run: `.venv/bin/python -m pytest tests/test_enrich_pool.py::test_pending_excludes_live_leases -v`
Expected: FAIL with `AttributeError: ... make_brain_enrich_pending`.

- [ ] **Step 3: Implement `make_brain_enrich_pending`** (after `make_brain_enrich_advance`, ~line 694)

```python
def make_brain_enrich_pending(home: str):
    async def brain_enrich_pending() -> dict:
        """Count enrichment units not under a live lease, WITHOUT claiming any.

        The coordinator calls this to decide whether to spawn another drainer
        wave — keeping done-ness a function of queue state, never reply text.
        Drainers self-serve work via brain_enrich_claim; this only observes.
        """
        import time as _time
        claims, now = _claims_dir(home), _time.time()
        try:
            files = sorted(_units_dir(home).glob("*.json"))
        except OSError:
            return {"pending": 0}
        n = sum(1 for f in files if not _lease_is_live(claims / f.stem, now))
        return {"pending": n}
    return brain_enrich_pending
```

- [ ] **Step 4: Run the lease-exclusion test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_enrich_pool.py::test_pending_excludes_live_leases -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/mcp_server.py tests/test_enrich_pool.py
git commit -m "feat(enrich): brain_enrich_pending — non-claiming queue count"
```

---

### Task 3: `brain_enrich_claim` tool (atomic lease + payload)

**Files:**
- Modify: `mcpbrain/mcp_server.py` (add `make_brain_enrich_claim` after `make_brain_enrich_pending`)
- Test: `tests/test_enrich_pool.py` (extend)

**Interfaces:**
- Consumes: `_units_dir`, `_claims_dir`, `_lease_is_live`, `_atomic_claim`, `_unit_payload` (Task 1).
- Produces: `make_brain_enrich_claim(home: str)` → async `brain_enrich_claim(with_rules: bool = False) -> dict` returning a `_unit_payload` body for one atomically-leased unit, or `{"empty": True}` when none is claimable.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_enrich_pool.py`)

```python
def test_claim_returns_one_unit_and_leases_it(tmp_path):
    _write_unit(tmp_path, "u-a")
    claim = mcp_server.make_brain_enrich_claim(str(tmp_path))
    out = asyncio.run(claim())
    assert out["unit_id"] == "u-a"
    assert out["kind"] == "thread" and out["threads"]
    assert "rules" not in out                       # with_rules defaults False
    assert (tmp_path / "enrich_queue" / "claims" / "u-a").exists()  # leased


def test_claim_never_hands_out_the_same_unit_twice(tmp_path):
    _write_unit(tmp_path, "u-a")
    _write_unit(tmp_path, "u-b")
    claim = mcp_server.make_brain_enrich_claim(str(tmp_path))
    first = asyncio.run(claim())["unit_id"]
    second = asyncio.run(claim())["unit_id"]
    assert {first, second} == {"u-a", "u-b"}         # distinct
    assert asyncio.run(claim()) == {"empty": True}   # drained


def test_claim_with_rules_inlines_rules(tmp_path):
    _write_unit(tmp_path, "u-a")
    claim = mcp_server.make_brain_enrich_claim(str(tmp_path))
    out = asyncio.run(claim(with_rules=True))
    assert out.get("rules") and out["rules"] == mcp_server._enrich_rules()


def test_claim_reclaims_a_stale_lease(tmp_path):
    _write_unit(tmp_path, "u-a")
    claims = tmp_path / "enrich_queue" / "claims"
    claims.mkdir(parents=True, exist_ok=True)
    stale = claims / "u-a"
    stale.touch()
    old = time.time() - mcp_server._LEASE_TTL_S - 10
    os.utime(stale, (old, old))                      # lease older than TTL
    out = asyncio.run(mcp_server.make_brain_enrich_claim(str(tmp_path))())
    assert out["unit_id"] == "u-a"                   # stale lease reclaimed
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_enrich_pool.py -k claim -v`
Expected: FAIL with `AttributeError: ... make_brain_enrich_claim`.

- [ ] **Step 3: Implement `make_brain_enrich_claim`**

```python
def make_brain_enrich_claim(home: str):
    async def brain_enrich_claim(with_rules: bool = False) -> dict:
        """Atomically lease ONE ready unit and return its payload (units + pull,
        folded into a single call), or {"empty": true} when none is claimable.

        For the enrich-batch drain loop: each drainer calls this repeatedly.
        with_rules defaults False — the subagent carries the rules in its cached
        system prompt; a general-purpose caller may pass True to inline them.
        Lease acquisition is atomic (see _atomic_claim), so N concurrent drainers
        never take the same unit.
        """
        import json as _json
        import time as _time
        claims, now = _claims_dir(home), _time.time()
        try:
            files = sorted(_units_dir(home).glob("*.json"))
        except OSError:
            return {"empty": True}
        for f in files:
            uid = f.stem
            if _lease_is_live(claims / uid, now):
                continue
            try:
                d = _json.loads(f.read_text())
            except (OSError, ValueError):
                continue                              # skip garbage without leasing
            if not _atomic_claim(claims, uid, now):
                continue                              # lost the race; try the next
            return _unit_payload(home, d, uid, with_rules)
        return {"empty": True}
    return brain_enrich_claim
```

- [ ] **Step 4: Run the pool test file to verify all pass** (Task 2's claim-dependent test now passes too)

Run: `.venv/bin/python -m pytest tests/test_enrich_pool.py -v`
Expected: PASS (all pending + claim tests).

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/mcp_server.py tests/test_enrich_pool.py
git commit -m "feat(enrich): brain_enrich_claim — atomic one-shot lease+payload"
```

---

### Task 4: Register the two tools (list + dispatch + wiring)

**Files:**
- Modify: `mcpbrain/mcp_server.py` — factory wiring (~777-780), `list_tools` (~1099 after the advance entry), `_call` dispatch (~1253 after advance)
- Test: `tests/test_mcp_server_stdio.py` (existing tool-listing test — confirm the new tools appear; extend if it enumerates)

**Interfaces:**
- Consumes: `make_brain_enrich_claim`, `make_brain_enrich_pending` (Tasks 2-3).
- Produces: `brain_enrich_claim` and `brain_enrich_pending` callable over MCP.

- [ ] **Step 1: Wire the closures** (after line 780, `enrich_advance = make_brain_enrich_advance(home)`)

```python
    enrich_claim = make_brain_enrich_claim(home)
    enrich_pending = make_brain_enrich_pending(home)
```

- [ ] **Step 2: Add `list_tools` entries** (after the `brain_enrich_advance` Tool, ~line 1099)

```python
            types.Tool(
                name="brain_enrich_claim",
                description="Atomically lease ONE enrichment unit and return its payload (kind + threads/items + context) in a single call — units+pull folded. For the enrich-batch drain loop: call it, extract per your system-prompt rules, brain_enrich_push, and repeat until it returns {\"empty\": true}. Concurrent drainers never get the same unit. Rules are omitted by default (they're in your prompt); pass with_rules=true only for a self-contained caller.",
                inputSchema={"type": "object", "properties": {
                    "with_rules": {"type": "boolean",
                                   "description": "inline the full extraction rules (default false; "
                                                  "enrich-batch workers carry them in their prompt)"},
                }},
            ),
            types.Tool(
                name="brain_enrich_pending",
                description="Count enrichment units still waiting (not under a live lease), WITHOUT claiming any. The coordinator calls this to decide whether to spawn another drainer wave: {\"pending\": N}. pending==0 means the queue is drained.",
                inputSchema={"type": "object", "properties": {}},
            ),
```

- [ ] **Step 3: Add `_call` dispatch** (after the `brain_enrich_advance` branch, ~line 1253)

```python
        if name == "brain_enrich_claim":
            out = await enrich_claim(with_rules=arguments.get("with_rules", False))
            return [types.TextContent(type="text", text=json.dumps(out))]
        if name == "brain_enrich_pending":
            out = await enrich_pending()
            return [types.TextContent(type="text", text=json.dumps(out))]
```

- [ ] **Step 4: Run the stdio/server test**

Run: `.venv/bin/python -m pytest tests/test_mcp_server_stdio.py -q`
Expected: PASS. If it asserts an exact tool count/set, update it to include `brain_enrich_claim` and `brain_enrich_pending`.

- [ ] **Step 5: Ruff + commit**

```bash
.venv/bin/ruff check mcpbrain/mcp_server.py
git add mcpbrain/mcp_server.py tests/test_mcp_server_stdio.py
git commit -m "feat(enrich): register brain_enrich_claim + brain_enrich_pending"
```

---

### Task 5: Rewrite the `enrich-batch` subagent as a bounded drain loop

**Files:**
- Modify: `plugin/agents/enrich-batch.md` (Protocol section, lines ~7-53; the frontmatter and the SHARED-EXTRACTION-RULES block stay unchanged)
- Test: `tests/test_mcp_enrich_meeting_tools.py::test_enrich_agent_is_haiku_and_skips_wire_rules` (update assertions)

**Interfaces:**
- Consumes: `brain_enrich_claim`, `brain_enrich_push` (over MCP).

- [ ] **Step 1: Update the agent-assertion test first** (it currently requires `brain_enrich_pull`; the loop uses `brain_enrich_claim`)

```python
def test_enrich_agent_is_haiku_and_loops_over_claims():
    # Model is set in frontmatter; the agent drains via a claim loop (claim → push),
    # not a single handed-in unit_id.
    text = _agent_file().read_text()
    assert "model: haiku" in text
    assert "brain_enrich_claim" in text and "brain_enrich_push" in text
    assert "brain_enrich_pull" not in text          # replaced by the claim loop
```

Delete the old `test_enrich_agent_is_haiku_and_skips_wire_rules` (superseded).

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_mcp_enrich_meeting_tools.py::test_enrich_agent_is_haiku_and_loops_over_claims -v`
Expected: FAIL (`brain_enrich_claim` not yet in the agent file; `brain_enrich_pull` still present).

- [ ] **Step 3: Rewrite the Protocol section** of `plugin/agents/enrich-batch.md` (replace lines ~7-53, keep frontmatter and everything from `## Extraction rules` down). New body:

```markdown
# enrich-batch

Enrichment drain worker for the enrich routine (hourly, and re-run on demand to
backfill). You **drain a slice of the queue**: repeatedly claim a unit, extract it,
and push the result — nothing else, so the orchestrator's context stays flat no
matter how large the backlog is.

The FULL extraction protocol is in the **Extraction rules** section at the bottom of
this prompt. It is part of your system prompt on purpose: every enrich-batch worker
shares the identical prefix, so after the first one warms it the rules are served from
cache for the rest of the pool. Do not re-fetch the rules over the wire.

## Protocol

**A unit is only done once `brain_enrich_push` returns `{"written": true}`.** Nothing
you say completes it — the coordinator checks queue state, not your reply text, so
there is no required output format. What matters is that you make the real tool calls:
narrating alongside them is fine, but a run that never calls `brain_enrich_push` has
enriched nothing.

1. Load the tools once:
   `ToolSearch("select:mcp__mcpbrain__brain_enrich_claim,mcp__mcpbrain__brain_enrich_push")`.
2. **Drain loop — repeat up to 5 times:**
   a. Call `brain_enrich_claim` (no arguments — the rules are already in this prompt,
      so it omits them). If it returns `{"empty": true}`, the queue is drained — stop.
   b. The result carries `context` plus the work. Follow the **Extraction rules** below
      EXACTLY:
      - `kind` `"thread"`: produce one extraction object per thread in `threads`.
      - `kind` `"block"`: answer the block named in `block` for each item in `items`
        (`merge_review` → `merge_answers`; otherwise the field of the same name:
        `synthesis` / `profile_synthesis` / `community_synthesis` / `memory_distil` /
        `profile_audit`).
   c. Call `brain_enrich_push` with:
      - `unit_id=<the unit_id from the claim result>`
      - For a **thread unit**: `extractions=[…]` — one object per thread. Required for
        thread units; omitting it or passing a non-list is rejected.
      - For a **block unit**: pass the block answer field (`merge_answers`, `synthesis`,
        etc.); `extractions` may be omitted.
      Confirm the response is `{"written": true}`.
   d. Loop back to (a).
3. Stop after 5 successful units OR the first empty claim — whichever comes first. Do
   not keep going past 5; the coordinator spawns another wave if the queue isn't empty.
   Exiting with units still queued is fine and expected.

Use the MCP tools only. Do not read the spool via shell, and do not read skill or
command files — everything you need is in each claim response, and the rules are below.
```

- [ ] **Step 4: Verify the rules block is still byte-synced** (protocol edit must not have touched it)

Run: `.venv/bin/python -m pytest "tests/test_mcp_enrich_meeting_tools.py::test_enrich_agent_rules_in_sync" "tests/test_mcp_enrich_meeting_tools.py::test_enrich_agent_is_haiku_and_loops_over_claims" -v`
Expected: PASS both.

- [ ] **Step 5: Commit**

```bash
git add plugin/agents/enrich-batch.md tests/test_mcp_enrich_meeting_tools.py
git commit -m "feat(enrich): enrich-batch drains a claim loop (up to 5 units)"
```

---

### Task 6: Rewrite the coordinator routine as a drainer pool

**Files:**
- Modify: `mcpbrain/routines/enrich.md` (whole Loop section)
- Test: none (prose routine; behaviour is exercised by the tool tests above)

- [ ] **Step 1: Replace the routine body** of `mcpbrain/routines/enrich.md` with the pool loop:

```markdown
# Brain enrichment — work queue

Drain the pending enrichment work units through the mcpbrain MCP tools. You are the
**orchestrator**: you spawn a small pool of drainer subagents that self-serve units
from the queue, so your own context never holds email bodies — only counts. You keep
NO per-unit state. Self-contained — needs no skill or command file.

**Models:** you (the coordinator) run on **Sonnet** — the scheduled task runs in
**Auto permission mode**, which Claude Code only offers on Sonnet, so a Haiku
coordinator would stall on prompts. Every `enrich-batch` drainer runs on **Haiku**,
set **explicitly per dispatch** (the agent frontmatter is not always honored); that is
where the volume and the cost savings live.

## Loop

1. Call **`brain_enrich_pending`**. If it returns `{"pending": 0}`, stop and report
   `DONE: queue empty`. Otherwise note the count.
2. Spawn a **pool of 10 `enrich-batch` drainers** in a single message (Task tool,
   `subagent_type: enrich-batch`, **`model: haiku`** set explicitly on each). Give each
   drainer EXACTLY this one line (the agent already carries the drain protocol and the
   extraction rules — do not repeat them):

   > Drain up to 5 enrichment units: loop claim → extract → push until an empty claim or 5 done. Act autonomously; do not ask questions.

   Each drainer claims its own units via `brain_enrich_claim`, so you never hand out
   unit ids and never pull payloads into your own context.
3. When the wave's drainers return, **do not parse their replies**. Call
   **`brain_enrich_advance`** — the daemon drains every pushed result, applies it, and
   deletes those units from the queue.
4. Go back to step 1. Stop when `brain_enrich_pending` returns `{"pending": 0}`. If a
   full wave leaves `pending` **unchanged** (no progress — only live-leased or stuck
   units remain), stop and report `PARTIAL: units still pending — re-run to continue`;
   a stuck unit's 15-minute claim lease expires and the next run (or a re-run) sweeps
   it. **Backfill is just re-running this routine.** There is no wave cap.
5. Report: `DONE: queue empty` or `PARTIAL: units still pending — re-run to continue`.

Never pull unit payloads into your own context — each drainer claims and pushes its
own units (`brain_enrich_claim` → extract → `brain_enrich_push`). Use the MCP tools
only; do not read skill/command files or shell into the queue.
```

- [ ] **Step 2: Sanity-check the routine references only tools that exist**

Run: `grep -o 'brain_enrich_[a-z]*' mcpbrain/routines/enrich.md | sort -u`
Expected: `brain_enrich_advance`, `brain_enrich_claim`, `brain_enrich_pending` (all registered in Task 4; `brain_enrich_push` is named in the drainer's own prompt, not the routine).

- [ ] **Step 3: Commit**

```bash
git add mcpbrain/routines/enrich.md
git commit -m "feat(enrich): coordinator spawns a drainer pool (pending/spawn/advance loop)"
```

---

### Task 7: K env override + defaults note

Make units-per-drainer (K) overridable via env, mirroring `MCPBRAIN_ENRICH_UNITS_BATCH`, and document the pool defaults. K is enforced in the drainer's prompt (the "up to 5" cap), so the env override adjusts the number the coordinator substitutes into the dispatch line.

**Files:**
- Modify: `mcpbrain/mcp_server.py` (add `_units_per_drainer()` reader near `_units_batch` ~492)
- Modify: `mcpbrain/routines/enrich.md` (note that the "5" is the default `MCPBRAIN_ENRICH_UNITS_PER_DRAINER`)
- Test: `tests/test_enrich_pool.py` (extend)

**Interfaces:**
- Produces: `_units_per_drainer() -> int` — reads `MCPBRAIN_ENRICH_UNITS_PER_DRAINER`, default 5, floored at 1.

- [ ] **Step 1: Write the failing test** (append to `tests/test_enrich_pool.py`)

```python
def test_units_per_drainer_default_and_override(monkeypatch):
    monkeypatch.delenv("MCPBRAIN_ENRICH_UNITS_PER_DRAINER", raising=False)
    assert mcp_server._units_per_drainer() == 5
    monkeypatch.setenv("MCPBRAIN_ENRICH_UNITS_PER_DRAINER", "8")
    assert mcp_server._units_per_drainer() == 8
    monkeypatch.setenv("MCPBRAIN_ENRICH_UNITS_PER_DRAINER", "0")
    assert mcp_server._units_per_drainer() == 1      # floored
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_enrich_pool.py::test_units_per_drainer_default_and_override -v`
Expected: FAIL with `AttributeError: ... _units_per_drainer`.

- [ ] **Step 3: Implement `_units_per_drainer`** (after `_units_batch`, ~line 506)

```python
_UNITS_PER_DRAINER_DEFAULT = 5  # max units one enrich-batch drainer processes before exiting


def _units_per_drainer() -> int:
    """Max units a single enrich-batch drainer processes per spawn (K).

    Bounds a looping drainer's context growth (it accumulates each unit's bodies)
    while amortising subagent cold-start over several units. K=1 reproduces the old
    one-subagent-per-unit behaviour. Override with MCPBRAIN_ENRICH_UNITS_PER_DRAINER.
    """
    import os
    try:
        return max(1, int(os.environ.get("MCPBRAIN_ENRICH_UNITS_PER_DRAINER",
                                         _UNITS_PER_DRAINER_DEFAULT)))
    except (TypeError, ValueError):
        return _UNITS_PER_DRAINER_DEFAULT
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_enrich_pool.py::test_units_per_drainer_default_and_override -v`
Expected: PASS.

- [ ] **Step 5: Add a one-line note** to `mcpbrain/routines/enrich.md` step 2 (after the dispatch line):

```markdown
   The "5" and the pool size "10" are the defaults; the per-drainer cap is
   `MCPBRAIN_ENRICH_UNITS_PER_DRAINER` (default 5).
```

- [ ] **Step 6: Ruff + commit**

```bash
.venv/bin/ruff check mcpbrain/mcp_server.py
git add mcpbrain/mcp_server.py mcpbrain/routines/enrich.md tests/test_enrich_pool.py
git commit -m "feat(enrich): MCPBRAIN_ENRICH_UNITS_PER_DRAINER (K) override, default 5"
```

---

### Task 8: Full impacted-suite pass + lint

**Files:** none (verification).

- [ ] **Step 1: Run all impacted tests together**

Run: `.venv/bin/python -m pytest tests/test_enrich_pool.py tests/test_mcp_enrich_meeting_tools.py tests/test_mcp_enrich_with_rules.py tests/test_integration_spool.py tests/test_mcp_server_stdio.py tests/test_drain.py -q`
Expected: PASS (all).

- [ ] **Step 2: Lint the changed module**

Run: `.venv/bin/ruff check mcpbrain/`
Expected: `All checks passed!`

- [ ] **Step 3: Confirm no dangling `brain_enrich_pull` reference in the drain path prompts**

Run: `grep -rn "brain_enrich_pull\|brain_enrich_units" mcpbrain/routines/enrich.md plugin/agents/enrich-batch.md`
Expected: no matches (the routine and drainer use `claim`/`pending`; `pull`/`units` remain only as registered tools for the general-purpose path).

---

## Release (after implementation approved)

Follow `docs/RELEASE-RUNBOOK.md`. Bump the five version files + `uv.lock` to the next patch, update the CLAUDE.md "Current state" block to describe the drainer pool, run `bin/sync_agents.py` is **not** needed (rules block unchanged) but run `test_enrich_agent_rules_in_sync` to confirm. Push source → dist wheel (mind the stale-wheel gotcha) → plugin (`git archive HEAD:plugin`). Reinstall this machine with `uv tool install --force ".[daemon]"` and restart the daemon.

## Notes for the implementer

- The scheduled task's coordinator is Sonnet in Auto mode; drainers are Haiku set explicitly per dispatch — do not change these.
- `brain_enrich_advance` and `brain_enrich_push` are unchanged — do not touch them.
- The `plugin/` tree is mirrored to the public `mcpbrain-plugin` repo at release; the `enrich-batch.md` change ships to all users. `mcpbrain/routines/enrich.md` is the coordinator prompt used by the scheduled task.
