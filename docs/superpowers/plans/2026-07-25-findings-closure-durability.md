# Findings Closure Durability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop resolved `proactive_findings` from silently reopening forever, and stop `memory_distil` from re-asking Haiku about notes it has already classified.

**Architecture:** A nullable `verdict` column on `proactive_findings` guards `record_finding`'s upsert — a finding whose row already carries a verdict stays resolved on re-detection instead of being forced back open. Every terminal review outcome (across four `review_apply.py` appliers, `brain_finding_resolve`, and the dashboard's manual dismiss route) is wired to pass its verdict through the same `store.resolve_finding` call. A parallel, independent fix stamps a `distilled_at` marker on memory-note chunk metadata so `memory_distil` stops resubmitting a note it has already classified.

**Tech Stack:** Python 3, SQLite (`mcpbrain/store.py`), pytest (parallel by default via pytest-xdist).

**Spec:** `docs/superpowers/specs/2026-07-25-findings-closure-durability-design.md`

## Global Constraints

- **Do not push or release.** Commit locally only.
- **Do not bump versions.** No changes to the five version files.
- **Scope test runs to edited modules and their direct dependents.** Josh runs the full suite himself.
- `ruff` must be clean on every file touched.
- A verdict, once recorded, is **permanent until manually cleared** — including `skip`/uncertain outcomes. No time-boxed re-review (e.g. "ask again after 90 days") — out of scope by design.
- No new "unack" tool. Reversal stays a direct DB action, matching the existing `entity_suppressions` convention.
- `resolve_finding`'s new `verdict` parameter **defaults to `None`** — any call site this plan does not touch keeps today's exact behavior (reopenable on redetection). Non-breaking by construction.
- `record_finding` itself never writes to the `verdict` column — only `resolve_finding` does.
- `memory_index.py`'s `note_chunks` call is untouched — a distilled note must still render in the memory index; only `memory_distil`'s own selection stops re-asking about it.

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `mcpbrain/store.py` | Modify — `verdict` column, `record_finding` upsert guard, `resolve_finding` verdict param, `get_finding` verdict field, `note_chunks` `exclude_distilled` param | 1, 4 |
| `tests/test_store_schema_p3.py` | Modify — reopen-guard tests, `note_chunks(exclude_distilled=...)` tests | 1, 4 |
| `mcpbrain/review_apply.py` | Modify — 14 call sites across 4 appliers pass their verdict | 2 |
| `tests/test_review_apply.py` | Modify — one round-trip-reopen test per applier | 2 |
| `mcpbrain/mcp_server.py` | Modify — `brain_finding_resolve` passes `outcome` as verdict | 3 |
| `tests/test_mcp_server.py` | Modify — round-trip-reopen test for `brain_finding_resolve` | 3 |
| `mcpbrain/control_api.py` | Modify — dashboard dismiss route passes `dismissed_by_human` | 3 |
| `tests/test_dashboard_digest.py` | Modify — dismiss route verdict + reopen-guard test | 3 |
| `mcpbrain/memory_distil.py` | Modify — `drain_distil` stamps `distilled_at` on all 3 verdicts; `build_distil_requests` passes `exclude_distilled=True` | 4 |
| `tests/test_memory_distil.py` | Modify — `distilled_at` stamping + exclusion tests | 4 |

---

### Task 1: Schema and core store methods

**Files:**
- Modify: `mcpbrain/store.py:586-597` (schema), `mcpbrain/store.py:2651-2683` (`record_finding`), `mcpbrain/store.py:2761-2775` (`get_finding`), `mcpbrain/store.py:2777-2784` (`resolve_finding`)
- Test: `tests/test_store_schema_p3.py` (append)

**Interfaces:**
- Consumes: nothing (first task).
- Produces: `Store.resolve_finding(self, finding_id: int, verdict: str | None = None) -> bool`; `Store.get_finding(...)` returns dicts with a `"verdict"` key; `record_finding`'s upsert leaves `resolved_at` untouched when the existing row's `verdict` is non-`NULL`. Tasks 2 and 3 call `resolve_finding` with a `verdict=` keyword.

**Background:** `mcpbrain/store.py:586-597`'s `CREATE TABLE IF NOT EXISTS proactive_findings` has no `verdict` column. Existing stores need it added via `ALTER TABLE ... ADD COLUMN`, following the exact pattern already used at `mcpbrain/store.py:198-206` (guard on `PRAGMA table_info`, then `ALTER TABLE`).

- [ ] **Step 1: Write the failing schema + reopen-guard tests**

Append to `tests/test_store_schema_p3.py` (it already has a `_store(tmp_path)` helper near the top of the file — reuse it):

```python
def test_proactive_findings_has_verdict_column(tmp_path):
    s = _store(tmp_path)
    with s._connect() as db:
        cols = {row["name"] for row in db.execute("PRAGMA table_info(proactive_findings)").fetchall()}
    assert "verdict" in cols


def test_resolve_finding_with_verdict_is_recorded(tmp_path):
    s = _store(tmp_path)
    s.record_finding("lint:orphan_entity", "e-1", summary="orphan")
    fid = s.open_findings("lint:orphan_entity")[0]["id"]

    assert s.resolve_finding(fid, verdict="keep") is True

    finding = s.get_finding(fid)
    assert finding["verdict"] == "keep"
    assert finding["resolved_at"] != ""


def test_record_finding_does_not_reopen_a_verdicted_finding(tmp_path):
    """The core fix: re-detecting the same signal must not force a settled
    finding back open. Before this fix, record_finding's upsert unconditionally
    cleared resolved_at on every re-detection, so a 'keep'/'skip' decision was
    thrown away the moment the lint check ran again."""
    s = _store(tmp_path)
    s.record_finding("lint:orphan_entity", "e-1", summary="orphan")
    fid = s.open_findings("lint:orphan_entity")[0]["id"]
    s.resolve_finding(fid, verdict="keep")

    # Re-detection: the lint check runs again, entity is still unchanged/orphan.
    s.record_finding("lint:orphan_entity", "e-1", summary="orphan (redetected)")

    assert s.open_findings("lint:orphan_entity") == []
    finding = s.get_finding(fid)
    assert finding["resolved_at"] != ""
    assert finding["verdict"] == "keep"
    # summary/detail still refresh even though resolved_at/verdict stay put —
    # get_finding doesn't expose summary, so check the row directly.
    with s._connect() as db:
        row = db.execute(
            "SELECT summary FROM proactive_findings WHERE id=?", (fid,)).fetchone()
    assert row["summary"] == "orphan (redetected)"


def test_record_finding_still_reopens_when_no_verdict_was_recorded(tmp_path):
    """Regression guard for the untouched default: a finding resolved WITHOUT
    a verdict (resolve_finding's default, and resolve_findings_not_in's path)
    must keep reopening on redetection exactly as before this change."""
    s = _store(tmp_path)
    s.record_finding("lint:orphan_entity", "e-2", summary="orphan")
    fid = s.open_findings("lint:orphan_entity")[0]["id"]
    s.resolve_finding(fid)  # no verdict — today's only path

    s.record_finding("lint:orphan_entity", "e-2", summary="orphan (redetected)")

    open_now = s.open_findings("lint:orphan_entity")
    assert len(open_now) == 1
    assert open_now[0]["id"] == fid
```

Note: `get_finding`'s returned dict has fixed keys (`id`, `finding_type`, `ref_id`, `resolved_at`, and — after Step 6 below — `verdict`); it does not include `summary`, which is why the last test above checks the row directly via `db.execute(...)` rather than through `get_finding`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_store_schema_p3.py -k "verdict or reopen" -v -p no:xdist`
Expected: FAIL — `test_proactive_findings_has_verdict_column` fails with `AssertionError: assert 'verdict' in {...}`; the others fail with `TypeError: resolve_finding() got an unexpected keyword argument 'verdict'`.

- [ ] **Step 3: Add the schema migration**

In `mcpbrain/store.py`, immediately after the `CREATE TABLE IF NOT EXISTS proactive_findings (...)"""` block closes (currently ending at line 597 with `)"""`), before the next `db.execute("CREATE INDEX ...")` line, insert:

```python
            _pf_cols = {row["name"] for row in
                        db.execute("PRAGMA table_info(proactive_findings)").fetchall()}
            if "verdict" not in _pf_cols:
                db.execute("ALTER TABLE proactive_findings ADD COLUMN verdict TEXT")
```

- [ ] **Step 4: Update `record_finding`'s upsert**

In `mcpbrain/store.py`, `record_finding` (currently lines 2651-2683) has this `DO UPDATE SET` clause:

```python
                "ON CONFLICT(finding_type, ref_id) DO UPDATE SET "
                "  org         = excluded.org, "
                "  summary     = excluded.summary, "
                "  detail      = excluded.detail, "
                "  severity    = excluded.severity, "
                "  detected_at = excluded.detected_at, "
                "  resolved_at = NULL",
```

Replace the `resolved_at = NULL` line with:

```python
                "  resolved_at = CASE WHEN verdict IS NOT NULL THEN resolved_at ELSE NULL END",
```

Also update the docstring immediately above (currently: *"resolved_at is cleared on upsert so a previously resolved finding resurfaces if it is re-detected."*) to:

```python
        """Insert or update a proactive finding.

        Upserts on the UNIQUE(finding_type, ref_id) constraint so re-detecting
        the same signal updates the existing row rather than accumulating
        duplicates. resolved_at is cleared on upsert UNLESS the existing row
        already carries a verdict (set by resolve_finding) — a settled review
        decision must not be thrown away just because the same unchanged
        signal was detected again. record_finding itself never writes the
        verdict column; only resolve_finding does.
        """
```

- [ ] **Step 5: Update `resolve_finding`**

In `mcpbrain/store.py`, `resolve_finding` (currently lines 2777-2784) reads:

```python
    def resolve_finding(self, finding_id: int) -> bool:
        """Dismiss one finding (sets resolved_at). True if a row changed."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._connect() as db:
            cur = db.execute(
                "UPDATE proactive_findings SET resolved_at=? "
                "WHERE id=? AND resolved_at IS NULL", (now, finding_id))
            return cur.rowcount > 0
```

Replace with:

```python
    def resolve_finding(self, finding_id: int, verdict: str | None = None) -> bool:
        """Dismiss one finding (sets resolved_at). If `verdict` is given, it is
        stored too, and record_finding's upsert will not reopen this finding on
        a later re-detection of the same (finding_type, ref_id) — a settled
        review decision (keep/skip/dismissed/etc.) should not resurface just
        because the same unchanged signal was detected again. Leave verdict
        None for a resolution that SHOULD still be reopenable (today's only
        path, and resolve_findings_not_in's "the underlying ref genuinely
        disappeared" case, which is a real new occurrence if it reappears).
        True if a row changed."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._connect() as db:
            cur = db.execute(
                "UPDATE proactive_findings SET resolved_at=?, verdict=? "
                "WHERE id=? AND resolved_at IS NULL", (now, verdict, finding_id))
            return cur.rowcount > 0
```

- [ ] **Step 6: Update `get_finding`**

In `mcpbrain/store.py`, `get_finding` (currently lines 2761-2775) reads:

```python
    def get_finding(self, finding_id) -> dict | None:
        """Return one finding as {id, finding_type, ref_id, resolved_at} or None.

        The review appliers use this to target the finding's OWN stored ref_id
        and finding_type rather than trusting the adjudicator's verdict payload —
        so a malformed verdict can't redirect an unattended mutation onto an
        arbitrary entity or resolve an unrelated finding (defense-in-depth)."""
        with self._connect() as db:
            r = db.execute(
                "SELECT id, finding_type, ref_id, resolved_at FROM proactive_findings WHERE id=?",
                (finding_id,)).fetchone()
        if r is None:
            return None
        return {"id": r["id"], "finding_type": r["finding_type"],
                "ref_id": r["ref_id"], "resolved_at": r["resolved_at"] or ""}
```

Replace with:

```python
    def get_finding(self, finding_id) -> dict | None:
        """Return one finding as {id, finding_type, ref_id, resolved_at, verdict}
        or None.

        The review appliers use this to target the finding's OWN stored ref_id
        and finding_type rather than trusting the adjudicator's verdict payload —
        so a malformed verdict can't redirect an unattended mutation onto an
        arbitrary entity or resolve an unrelated finding (defense-in-depth)."""
        with self._connect() as db:
            r = db.execute(
                "SELECT id, finding_type, ref_id, resolved_at, verdict "
                "FROM proactive_findings WHERE id=?",
                (finding_id,)).fetchone()
        if r is None:
            return None
        return {"id": r["id"], "finding_type": r["finding_type"],
                "ref_id": r["ref_id"], "resolved_at": r["resolved_at"] or "",
                "verdict": r["verdict"] or ""}
```

- [ ] **Step 7: Run the tests**

Run: `pytest tests/test_store_schema_p3.py -k "verdict or reopen" -v -p no:xdist`
Expected: all 4 PASS.

- [ ] **Step 8: Run the impacted suites**

Run: `pytest tests/test_store_schema_p3.py tests/test_change_log.py tests/test_lint.py -q`
Expected: PASS. (`test_change_log.py::test_resolve_finding` and `test_lint.py`'s reopen tests call `resolve_finding`/`record_finding` without a verdict — this proves the default-`None` path is unchanged.)

- [ ] **Step 9: Lint**

Run: `ruff check mcpbrain/store.py tests/test_store_schema_p3.py`
Expected: `All checks passed!`

- [ ] **Step 10: Commit**

```bash
git add mcpbrain/store.py tests/test_store_schema_p3.py
git commit -m "fix(store): a verdicted finding stays resolved on re-detection

record_finding's upsert unconditionally cleared resolved_at on every
re-detection of the same (finding_type, ref_id), so any review verdict
that didn't mutate the graph (keep/skip/etc.) was silently discarded
the moment the same signal was detected again. Add a verdict column:
record_finding now only reopens a finding when it has no verdict
recorded. resolve_finding gains a verdict= parameter (default None,
fully backward compatible)."
```

---

### Task 2: Wire every `review_apply.py` terminal outcome through its verdict

**Files:**
- Modify: `mcpbrain/review_apply.py:54`, `:59`, `:63`, `:116`, `:121`, `:126`, `:177`, `:182`, `:185`, `:190`, `:285`, `:296`, `:305`, `:311`
- Test: `tests/test_review_apply.py` (append)

**Interfaces:**
- Consumes: `store.resolve_finding(finding_id, verdict=...)` and `store.get_finding(...)["verdict"]` from Task 1.
- Produces: nothing consumed by later tasks — this task's own tests are the proof.

**Background:** All 14 call sites currently read `store.resolve_finding(finding_id)`. Each becomes `store.resolve_finding(finding_id, verdict="<outcome>")` per the table below. The "missing" branches (ref vanished between detection and verdict) are untouched — they never call `resolve_finding` at all.

| Line | Function | Branch | verdict string |
|---|---|---|---|
| 54 | `apply_orphan_verdicts` | suppress success | `"suppress"` |
| 59 | `apply_orphan_verdicts` | keep | `"keep"` |
| 63 | `apply_orphan_verdicts` | skip / unrecognised | `"skip"` |
| 116 | `apply_missing_org_verdicts` | assign success | `"assign"` |
| 121 | `apply_missing_org_verdicts` | external | `"external"` |
| 126 | `apply_missing_org_verdicts` | skip / invalid assign | `"skip"` |
| 177 | `apply_ownerless_verdicts` | owner success | `"owner"` |
| 182 | `apply_ownerless_verdicts` | waiting_on | `"waiting_on"` |
| 185 | `apply_ownerless_verdicts` | unowned | `"unowned"` |
| 190 | `apply_ownerless_verdicts` | skip / invalid owner | `"skip"` |
| 285 | `apply_org_verdicts` | canonicalize (ambiguous_org) | `"canonicalize"` |
| 296 | `apply_org_verdicts` | canonicalize (duplicate_org) | `"canonicalize"` |
| 305 | `apply_org_verdicts` | add_to_config | `"add_to_config"` |
| 311 | `apply_org_verdicts` | skip / anything else | `"skip"` |

- [ ] **Step 1: Write the failing round-trip tests**

Append to `tests/test_review_apply.py` (it already has `_seed(tmp_path)` and `_write_config` helpers — reuse `_seed`):

```python
# ---------------------------------------------------------------------------
# Closure durability: every terminal verdict must survive a re-detection
# ---------------------------------------------------------------------------

def test_orphan_keep_verdict_survives_redetection(tmp_path):
    s = _seed(tmp_path)
    s.record_finding("lint:orphan_entity", "e2", summary="probably fine")
    finding = s.open_findings("lint:orphan_entity")[0]

    apply_orphan_verdicts(
        s, [{"finding_id": finding["id"], "ref_id": "e2", "verdict": "keep"}], cap=50)
    assert s.get_finding(finding["id"])["verdict"] == "keep"

    # Re-detection: the lint check runs again, e2 is still unchanged/orphan.
    s.record_finding("lint:orphan_entity", "e2", summary="probably fine (redetected)")
    assert s.open_findings("lint:orphan_entity") == []


def test_orphan_skip_verdict_survives_redetection(tmp_path):
    s = _seed(tmp_path)
    s.record_finding("lint:orphan_entity", "e3", summary="unclear")
    finding = s.open_findings("lint:orphan_entity")[0]

    apply_orphan_verdicts(
        s, [{"finding_id": finding["id"], "ref_id": "e3", "verdict": "maybe???"}], cap=50)
    assert s.get_finding(finding["id"])["verdict"] == "skip"

    s.record_finding("lint:orphan_entity", "e3", summary="unclear (redetected)")
    assert s.open_findings("lint:orphan_entity") == []


def test_missing_org_external_verdict_survives_redetection(tmp_path):
    s = _seed(tmp_path)
    s.record_finding("lint:missing_org", "e3", summary="no org")
    finding = s.open_findings("lint:missing_org")[0]

    cfg_home = _write_config(tmp_path, ACME_CFG)
    apply_missing_org_verdicts(
        s, [{"finding_id": finding["id"], "ref_id": "e3", "verdict": "external"}],
        cap=50, home=cfg_home)
    assert s.get_finding(finding["id"])["verdict"] == "external"

    s.record_finding("lint:missing_org", "e3", summary="no org (redetected)")
    assert s.open_findings("lint:missing_org") == []


def test_ownerless_waiting_on_verdict_survives_redetection(tmp_path):
    # _seed_ownerless is this file's existing helper for apply_ownerless_verdicts
    # tests (defined above, around line 325) — it returns (store, {"a1": id, "a2": id}).
    s, action_ids = _seed_ownerless(tmp_path)
    aid = action_ids["a1"]
    s.record_finding("lint:ownerless_action", str(aid), summary="no owner")
    finding = s.open_findings("lint:ownerless_action")[0]

    apply_ownerless_verdicts(
        s, [{"finding_id": finding["id"], "ref_id": aid, "verdict": "waiting_on"}], cap=50)
    assert s.get_finding(finding["id"])["verdict"] == "waiting_on"

    s.record_finding("lint:ownerless_action", str(aid), summary="no owner (redetected)")
    assert s.open_findings("lint:ownerless_action") == []


def test_org_skip_verdict_survives_redetection(tmp_path):
    # _seed_org is this file's existing helper for apply_org_verdicts tests
    # (defined above, around line 461) — two entities tagged org='external'.
    home = _write_config(tmp_path, ACME_CFG)
    s = _seed_org(tmp_path)
    s.record_finding("org_unrecognised", "rotary club", summary="unrecognised org")
    finding = s.open_findings("org_unrecognised")[0]

    apply_org_verdicts(
        s, [{"finding_id": finding["id"], "finding_type": "org_unrecognised",
             "ref_id": "rotary club", "verdict": "skip"}], cap=50, home=home)
    assert s.get_finding(finding["id"])["verdict"] == "skip"

    s.record_finding("org_unrecognised", "rotary club", summary="unrecognised org (redetected)")
    assert s.open_findings("org_unrecognised") == []
```

Note: the `missing_org` test above uses `_seed`'s `e3` (created with `org=""`), not `e2` (which has `org="Acme"` and would never match `check_missing_org`'s `WHERE org IS NULL OR org=''` in real use — here we're calling `record_finding` directly rather than the real check, but `e3` keeps the fixture honest). The `ownerless` test uses `_seed_ownerless`, a separate helper defined further down in this same file (around line 325) purpose-built for the `apply_ownerless_verdicts` tests — it returns `(store, {"a1": id, "a2": id})`, not the `_seed` store. The `org_skip` test uses `_seed_org` (around line 461) plus `_write_config`/`ACME_CFG`, both already defined earlier in this file, matching `test_org_skip_resolves_without_mutation_for_each_kind`'s existing pattern.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_review_apply.py -k redetection -v -p no:xdist`
Expected: FAIL — each assertion on `get_finding(...)["verdict"]` fails (`AssertionError: assert '' == 'keep'` etc.), since Step 1 of this task hasn't changed `review_apply.py` yet.

- [ ] **Step 3: Update `apply_orphan_verdicts`**

In `mcpbrain/review_apply.py`, within `apply_orphan_verdicts` (the suppress/keep/skip branches around lines 48-64):

```python
        if verdict_str == "suppress":
            if result["suppressed"] >= cap:
                result["capped"] += 1
                continue
            if store.suppress_entity(ref_id, reason=verdict.get("reason", "")):
                store.resolve_finding(finding_id, verdict="suppress")
                result["suppressed"] += 1
            else:
                result["missing"] += 1
        elif verdict_str == "keep":
            store.resolve_finding(finding_id, verdict="keep")
            result["kept"] += 1
        else:
            # "skip" and any unrecognised verdict string.
            store.resolve_finding(finding_id, verdict="skip")
            result["skipped"] += 1
```

- [ ] **Step 4: Update `apply_missing_org_verdicts`**

Within `apply_missing_org_verdicts` (the assign/external/skip branches around lines 110-127):

```python
        if verdict_str == "assign" and org and org in valid_orgs:
            if result["assigned"] >= cap:
                result["capped"] += 1
                continue
            if store.update_entity_org(ref_id, org):
                store.resolve_finding(finding_id, verdict="assign")
                result["assigned"] += 1
            else:
                result["missing"] += 1
        elif verdict_str == "external":
            store.resolve_finding(finding_id, verdict="external")
            result["external"] += 1
        else:
            # "skip", an "assign" with a missing/invalid org, and any
            # unrecognised verdict string.
            store.resolve_finding(finding_id, verdict="skip")
            result["skipped"] += 1
```

- [ ] **Step 5: Update `apply_ownerless_verdicts`**

Within `apply_ownerless_verdicts` (the owner/waiting_on/unowned/skip branches around lines 170-191):

```python
        if verdict_str == "owner" and owner:
            if result["owner_assigned"] >= cap:
                result["capped"] += 1
                continue
            if store.assign_action_owner(ref_id, owner, owner_entity_id=verdict.get("owner_entity_id", "")):
                store.resolve_finding(finding_id, verdict="owner")
                result["owner_assigned"] += 1
            else:
                result["missing"] += 1
        elif verdict_str == "waiting_on":
            store.resolve_finding(finding_id, verdict="waiting_on")
            result["waiting_on"] += 1
        elif verdict_str == "unowned":
            store.resolve_finding(finding_id, verdict="unowned")
            result["unowned"] += 1
        else:
            # "skip", an "owner" verdict with a missing `owner` field, and any
            # unrecognised verdict string.
            store.resolve_finding(finding_id, verdict="skip")
            result["skipped"] += 1
```

- [ ] **Step 6: Update `apply_org_verdicts`**

Within `apply_org_verdicts` (the canonicalize/add_to_config/skip branches around lines 278-312):

```python
        if verdict_str == "canonicalize" and canonical_org and canonical_org in valid_orgs \
                and finding_type == "lint:ambiguous_org":
            if _budget_used() >= cap:
                result["capped"] += 1
                continue
            if store.update_entity_org(ref_id, canonical_org):
                store.resolve_finding(finding_id, verdict="canonicalize")
                result["canonicalized"] += 1
            else:
                result["missing"] += 1
        elif verdict_str == "canonicalize" and canonical_org and canonical_org in valid_orgs \
                and finding_type == "lint:duplicate_org":
            if _budget_used() >= cap:
                result["capped"] += 1
                continue
            updated = store.rewrite_org_field(ref_id, canonical_org)
            if updated > 0:
                store.resolve_finding(finding_id, verdict="canonicalize")
                result["canonicalized"] += 1
            else:
                result["missing"] += 1
        elif verdict_str == "add_to_config" and finding_type == "org_unrecognised":
            if _budget_used() >= cap:
                result["capped"] += 1
                continue
            store.suggest_org_mapping(ref_id, reason=verdict.get("reason", ""))
            store.resolve_finding(finding_id, verdict="add_to_config")
            result["suggested"] += 1
        else:
            # "skip", "add_to_config" on a finding_type it isn't defined for,
            # a "canonicalize" with a missing/invalid `canonical_org`, and any
            # unrecognised verdict string.
            store.resolve_finding(finding_id, verdict="skip")
            result["skipped"] += 1
```

- [ ] **Step 7: Run the tests**

Run: `pytest tests/test_review_apply.py -k redetection -v -p no:xdist`
Expected: all 5 PASS.

- [ ] **Step 8: Run the full review_apply suite**

Run: `pytest tests/test_review_apply.py -q`
Expected: PASS — every existing test in this file (suppress/assign/owner/canonicalize behaviors) is unaffected, since none of them asserted on the (previously nonexistent) `verdict` field.

- [ ] **Step 9: Lint**

Run: `ruff check mcpbrain/review_apply.py tests/test_review_apply.py`
Expected: `All checks passed!`

- [ ] **Step 10: Commit**

```bash
git add mcpbrain/review_apply.py tests/test_review_apply.py
git commit -m "fix(review): every terminal review verdict survives re-detection

All 14 resolve_finding call sites across the four appliers now pass
their actual outcome (keep/skip/external/waiting_on/unowned/
canonicalize/add_to_config/suppress/assign/owner) as the verdict, so
record_finding's upsert guard (added in the prior commit) keeps them
closed instead of reopening on the next lint run."
```

---

### Task 3: `brain_finding_resolve` and the dashboard dismiss route

**Files:**
- Modify: `mcpbrain/mcp_server.py:249`
- Modify: `mcpbrain/control_api.py:423`
- Test: `tests/test_mcp_server.py` (append), `tests/test_dashboard_digest.py` (append)

**Interfaces:**
- Consumes: `store.resolve_finding(finding_id, verdict=...)` from Task 1.
- Produces: nothing consumed by later tasks.

**Background:** `brain_finding_resolve` (`mcp_server.py`, added in the prior branch) already computes `outcome` as one of `"promoted"`/`"merged"`/`"dismissed"` before calling `store.resolve_finding(finding_id)` at line 249 — passing it through as the verdict needs no new vocabulary. The dashboard's `/api/dashboard/findings/<id>/dismiss` route (`control_api.py:423`) is a human clicking dismiss; it gets a distinct constant, `dismissed_by_human`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mcp_server.py` (reuse the existing `_promotion_store`/`_finding_id` helpers from the `brain_finding_resolve` test section added in the prior branch):

```python
def test_finding_resolve_verdict_survives_redetection(tmp_path):
    """The closure-durability fix: a memory_promotion finding closed via
    brain_finding_resolve must not reopen if memory_distil re-flags the same
    doc_id on a later run."""
    s = _promotion_store(tmp_path, name="fr_redetect.sqlite3")
    fid = _finding_id(s, "memory_promotion")
    tool = make_brain_finding_resolve(s)

    asyncio.run(tool(finding_id=fid, outcome="dismissed", note="not durable"))
    assert s.get_finding(fid)["verdict"] == "dismissed"

    # Re-detection: memory_distil flags the same note again on a later run.
    s.record_finding(
        "memory_promotion", "note-abc",
        summary="Memory note flagged for promotion: note-abc",
        detail="reason=durable preference target_hint=preferences",
        severity="info",
    )
    assert s.open_findings("memory_promotion") == []
```

Append to `tests/test_dashboard_digest.py`, immediately after `test_post_dismiss_finding` (this file already imports `json`, `urllib.error`, `urllib.request`, `mock`, `ControlServer`, `Store`, and defines the `_store(tmp_path)` and `FakeDaemon` helpers this test reuses):

```python
def test_dismiss_records_a_verdict_that_blocks_reopening(tmp_path):
    """A human's manual dismiss must stick, same as an AI verdict: the
    dashboard's dismiss route should not leave a finding reopenable by the
    next re-detection of the same signal."""
    s = _store(tmp_path)
    s.record_finding("org_unrecognised", ref_id="x", summary="s")
    fid = s.open_findings()[0]["id"]
    srv = ControlServer(FakeDaemon(), home=str(tmp_path), store=s)
    srv.start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{srv.port}/api/dashboard/findings/{fid}/dismiss",
            data=b"{}", method="POST")
        req.add_header("Authorization", f"Bearer {srv.token}")
        out = json.loads(urllib.request.urlopen(req).read())
        assert out["dismissed"] is True

        assert s.get_finding(fid)["verdict"] == "dismissed_by_human"

        # Re-detection: org_unrecognised is re-flagged for the same raw org string.
        s.record_finding("org_unrecognised", ref_id="x", summary="s (redetected)")
        assert s.open_findings("org_unrecognised") == []
    finally:
        srv.stop()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_mcp_server.py -k redetection -v -p no:xdist`
Expected: FAIL — `AssertionError: assert '' == 'dismissed'`.

Run: `pytest tests/test_dashboard_digest.py -k verdict -v -p no:xdist`
Expected: FAIL — `AssertionError: assert '' == 'dismissed_by_human'`.

- [ ] **Step 3: Update `brain_finding_resolve`**

In `mcpbrain/mcp_server.py`, inside `make_brain_finding_resolve`'s factory function, the line currently reading (around line 249):

```python
            if not store.resolve_finding(finding_id):
```

becomes:

```python
            if not store.resolve_finding(finding_id, verdict=outcome):
```

- [ ] **Step 4: Update the dashboard dismiss route**

In `mcpbrain/control_api.py`, the dismiss route currently reads (around line 423):

```python
                ok = self.store.resolve_finding(finding_id)
```

becomes:

```python
                ok = self.store.resolve_finding(finding_id, verdict="dismissed_by_human")
```

- [ ] **Step 5: Run the tests**

Run: `pytest tests/test_mcp_server.py -k redetection -v -p no:xdist tests/test_dashboard_digest.py -k verdict -v -p no:xdist`
Expected: all PASS.

- [ ] **Step 6: Run the impacted suites**

Run: `pytest tests/test_mcp_server.py tests/test_mcp_server_stdio.py tests/test_dashboard_digest.py -q`
Expected: PASS.

- [ ] **Step 7: Lint**

Run: `ruff check mcpbrain/mcp_server.py mcpbrain/control_api.py tests/test_mcp_server.py tests/test_dashboard_digest.py`
Expected: `All checks passed!`

- [ ] **Step 8: Commit**

```bash
git add mcpbrain/mcp_server.py mcpbrain/control_api.py tests/test_mcp_server.py tests/test_dashboard_digest.py
git commit -m "fix(mcp,dashboard): finding resolution outcomes survive re-detection

brain_finding_resolve now passes its outcome (promoted/merged/dismissed)
as the verdict; the dashboard's manual dismiss route records
dismissed_by_human. Both now rely on the prior commit's record_finding
upsert guard to stay closed on redetection."
```

---

### Task 4: `memory_distil` incrementality

**Files:**
- Modify: `mcpbrain/store.py:1589-1618` (`note_chunks`)
- Modify: `mcpbrain/memory_distil.py` (imports, `drain_distil`, `build_distil_requests`)
- Test: `tests/test_store_schema_p3.py` (append), `tests/test_memory_distil.py` (append)

**Interfaces:**
- Consumes: nothing from Tasks 1-3 (independent fix).
- Produces: `Store.note_chunks(..., exclude_distilled: bool = False, ...)`. Nothing downstream consumes this — this task's tests are the proof.

**Background:** `memory_distil.build_distil_requests` re-submits the same live notes every run because `note_chunks` only excludes `expired` chunks, never anything based on prior distillation. `drain_distil`'s `"keep"` branch currently does nothing but `continue`; `"expire"` and `"promote"` each touch the chunk/store but never mark "this note has been distilled."

- [ ] **Step 1: Write the failing `note_chunks` test**

Append to `tests/test_store_schema_p3.py`:

```python
def test_note_chunks_exclude_distilled(tmp_path):
    s = _store(tmp_path)
    s.upsert_chunk(doc_id="note-a", text="A\n\nbody", content_hash="note-a",
                   metadata={"source": "note", "title": "A",
                             "observation_type": "memory",
                             "captured_at": "2026-06-01T00:00:00Z"})
    s.upsert_chunk(doc_id="note-b", text="B\n\nbody", content_hash="note-b",
                   metadata={"source": "note", "title": "B",
                             "observation_type": "memory",
                             "captured_at": "2026-06-01T00:00:00Z",
                             "distilled_at": "2026-07-01T00:00:00Z"})

    # Default (memory_index.py's call shape): both still show.
    ids = {c["doc_id"] for c in s.note_chunks(observation_type="memory")}
    assert ids == {"note-a", "note-b"}

    # exclude_distilled=True (memory_distil's call shape): only the
    # not-yet-distilled note shows.
    ids = {c["doc_id"] for c in s.note_chunks(observation_type="memory", exclude_distilled=True)}
    assert ids == {"note-a"}


def test_note_chunks_exclude_distilled_applies_before_limit(tmp_path):
    """A distilled note must not occupy a slot in the capped result set — it
    should be filtered out before `limit` truncates, exactly like the
    existing `expired` filter. Otherwise a store with many already-distilled
    notes could starve build_distil_requests of the genuinely fresh ones."""
    s = _store(tmp_path)
    s.upsert_chunk(doc_id="note-old", text="Old\n\nbody", content_hash="note-old",
                   metadata={"source": "note", "title": "Old",
                             "observation_type": "memory",
                             "captured_at": "2026-06-01T00:00:00Z",
                             "distilled_at": "2026-06-02T00:00:00Z"})
    s.upsert_chunk(doc_id="note-new", text="New\n\nbody", content_hash="note-new",
                   metadata={"source": "note", "title": "New",
                             "observation_type": "memory",
                             "captured_at": "2026-07-01T00:00:00Z"})

    ids = {c["doc_id"] for c in
           s.note_chunks(observation_type="memory", exclude_distilled=True, limit=1)}
    assert ids == {"note-new"}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_store_schema_p3.py -k distilled -v -p no:xdist`
Expected: FAIL — `TypeError: note_chunks() got an unexpected keyword argument 'exclude_distilled'`.

- [ ] **Step 3: Add `exclude_distilled` to `note_chunks`**

In `mcpbrain/store.py`, `note_chunks` (currently lines 1589-1618) reads:

```python
    def note_chunks(self, *, observation_type: str | None = None,
                    include_expired: bool = False, limit: int = 500) -> list[dict]:
        """Return capture-note chunks (doc_id starting with 'note-'), with parsed metadata.

        Excludes expired chunks (meta["expired"] is truthy) unless include_expired=True.
        Filters by observation_type if provided. Returns the newest `limit` live
        results (ORDER BY rowid DESC). The limit is applied AFTER the Python-side
        expired/observation_type filter, so a store full of expired notes never
        truncates live ones — we iterate the cursor and stop once `limit` live
        rows are collected rather than pre-truncating in SQL.
        """
        sql = ("SELECT doc_id, text, metadata FROM chunks "
               "WHERE doc_id LIKE 'note-%' ORDER BY rowid DESC")
        results = []
        with self._connect() as db:
            for r in db.execute(sql):
                try:
                    meta = json.loads(r["metadata"])
                except Exception:
                    continue
                if not include_expired and meta.get("expired"):
                    continue
                if observation_type is not None and meta.get("observation_type") != observation_type:
                    continue
                results.append({
                    "doc_id": r["doc_id"],
                    "text": r["text"],
                    "metadata": meta,
                })
                if len(results) == limit:
                    break
        return results
```

Replace with:

```python
    def note_chunks(self, *, observation_type: str | None = None,
                    include_expired: bool = False, exclude_distilled: bool = False,
                    limit: int = 500) -> list[dict]:
        """Return capture-note chunks (doc_id starting with 'note-'), with parsed metadata.

        Excludes expired chunks (meta["expired"] is truthy) unless include_expired=True.
        Excludes chunks memory_distil has already classified (meta["distilled_at"] is
        truthy) when exclude_distilled=True — pass this only from memory_distil's own
        selection query; callers like memory_index.py that render the live memory set
        must NOT set it, since a distilled note still belongs in the index. Filters by
        observation_type if provided. Returns the newest `limit` live results (ORDER BY
        rowid DESC). The limit is applied AFTER the Python-side expired/distilled/
        observation_type filter, so a store full of expired or already-distilled notes
        never truncates live/fresh ones — we iterate the cursor and stop once `limit`
        live rows are collected rather than pre-truncating in SQL.
        """
        sql = ("SELECT doc_id, text, metadata FROM chunks "
               "WHERE doc_id LIKE 'note-%' ORDER BY rowid DESC")
        results = []
        with self._connect() as db:
            for r in db.execute(sql):
                try:
                    meta = json.loads(r["metadata"])
                except Exception:
                    continue
                if not include_expired and meta.get("expired"):
                    continue
                if exclude_distilled and meta.get("distilled_at"):
                    continue
                if observation_type is not None and meta.get("observation_type") != observation_type:
                    continue
                results.append({
                    "doc_id": r["doc_id"],
                    "text": r["text"],
                    "metadata": meta,
                })
                if len(results) == limit:
                    break
        return results
```

- [ ] **Step 4: Run the `note_chunks` tests**

Run: `pytest tests/test_store_schema_p3.py -k distilled -v -p no:xdist`
Expected: both PASS.

- [ ] **Step 5: Write the failing `memory_distil` tests**

Append to `tests/test_memory_distil.py` (it already has `_store` and `_note` helpers):

```python
def test_drain_stamps_distilled_at_on_keep(tmp_path):
    s = _store(tmp_path)
    _note(s, "note-a", "Keep me")
    memory_distil.drain_distil(s, {"memory_distil": [
        {"doc_id": "note-a", "verdict": "keep"},
    ]})
    chunk = s.get_chunk("note-a")
    assert chunk["metadata"].get("distilled_at")


def test_drain_stamps_distilled_at_on_expire(tmp_path):
    s = _store(tmp_path)
    _note(s, "note-b", "Expire me")
    memory_distil.drain_distil(s, {"memory_distil": [
        {"doc_id": "note-b", "verdict": "expire", "reason": "stale"},
    ]})
    chunk = s.get_chunk("note-b")
    assert chunk["metadata"].get("distilled_at")
    assert chunk["metadata"].get("expired") is True


def test_drain_stamps_distilled_at_on_promote(tmp_path):
    s = _store(tmp_path)
    _note(s, "note-c", "Promote me")
    memory_distil.drain_distil(s, {"memory_distil": [
        {"doc_id": "note-c", "verdict": "promote",
         "reason": "stated 4 times", "target_hint": "preferences.md"},
    ]})
    chunk = s.get_chunk("note-c")
    assert chunk["metadata"].get("distilled_at")


def test_build_distil_requests_excludes_already_distilled_notes(tmp_path):
    s = _store(tmp_path)
    _note(s, "note-a", "Already distilled")
    _note(s, "note-b", "Fresh note")
    memory_distil.drain_distil(s, {"memory_distil": [
        {"doc_id": "note-a", "verdict": "keep"},
    ]})

    reqs = memory_distil.build_distil_requests(s, cap=30)

    assert {r["doc_id"] for r in reqs} == {"note-b"}
```

- [ ] **Step 6: Run the tests to verify they fail**

Run: `pytest tests/test_memory_distil.py -k "distilled_at or excludes_already" -v -p no:xdist`
Expected: FAIL — `assert None` / `assert chunk["metadata"].get("distilled_at")` fails, and the last test fails with `assert {'note-a', 'note-b'} == {'note-b'}`.

- [ ] **Step 7: Update `memory_distil.py`**

In `mcpbrain/memory_distil.py`, add the datetime import at the top of the file (currently only `json` and `logging` are imported):

```python
from datetime import datetime, timezone
```

Then, in `drain_distil`, the current loop body reads:

```python
        if verdict == "keep":
            continue

        # Verify the chunk exists before acting.
        chunk = store.get_chunk(doc_id)
        if chunk is None:
            log.debug("memory_distil: doc_id=%s not found, skipping", doc_id)
            continue

        if verdict == "expire":
            ok = store.patch_chunk_metadata(doc_id, expired=True)
            if ok:
                reason = item.get("reason", "")
                store.record_change(
                    "memory_expired",
                    ref_id=doc_id,
                    summary=f"Memory note expired: {doc_id}",
                    detail=reason,
                    source="memory_distil",
                )
                expired_count += 1

        elif verdict == "promote":
            reason = item.get("reason", "")
            target_hint = item.get("target_hint", "")
            # get_chunk returns metadata already parsed to a dict; guard for a
            # raw JSON string defensively.
            meta = chunk.get("metadata") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    meta = {}
            store.record_finding(
                "memory_promotion",
                ref_id=doc_id,
                org=meta.get("org", ""),
                summary=f"Memory note flagged for promotion: {doc_id}",
                detail=f"reason={reason} target_hint={target_hint}",
            )
            promoted_count += 1
```

Replace with:

```python
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        if verdict == "keep":
            # Stamp distilled_at even on a no-op verdict: nothing about an
            # unchanged note will produce a different answer on the next
            # distil run, so re-asking Haiku about it forever is pure
            # recurring cost. patch_chunk_metadata's own existence guard
            # (returns False, harmlessly) covers a doc_id gone stale between
            # listing and draining, so no separate get_chunk check is needed
            # for this branch.
            store.patch_chunk_metadata(doc_id, distilled_at=now)
            continue

        # Verify the chunk exists before acting.
        chunk = store.get_chunk(doc_id)
        if chunk is None:
            log.debug("memory_distil: doc_id=%s not found, skipping", doc_id)
            continue

        if verdict == "expire":
            ok = store.patch_chunk_metadata(doc_id, expired=True, distilled_at=now)
            if ok:
                reason = item.get("reason", "")
                store.record_change(
                    "memory_expired",
                    ref_id=doc_id,
                    summary=f"Memory note expired: {doc_id}",
                    detail=reason,
                    source="memory_distil",
                )
                expired_count += 1

        elif verdict == "promote":
            reason = item.get("reason", "")
            target_hint = item.get("target_hint", "")
            # get_chunk returns metadata already parsed to a dict; guard for a
            # raw JSON string defensively.
            meta = chunk.get("metadata") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    meta = {}
            store.record_finding(
                "memory_promotion",
                ref_id=doc_id,
                org=meta.get("org", ""),
                summary=f"Memory note flagged for promotion: {doc_id}",
                detail=f"reason={reason} target_hint={target_hint}",
            )
            store.patch_chunk_metadata(doc_id, distilled_at=now)
            promoted_count += 1
```

Then update `build_distil_requests` — currently:

```python
    chunks = store.note_chunks(observation_type="memory", limit=cap)
```

becomes:

```python
    chunks = store.note_chunks(observation_type="memory", exclude_distilled=True, limit=cap)
```

Finally, update the module docstring's verdict summary (currently lines 8-11):

```python
  drain_distil(store, inbox_obj) -> {"expired": N, "promotions_flagged": N}
      Applies verdicts:
        "keep"    — no-op
        "expire"  — patches chunk metadata expired=True, records memory_expired
        "promote" — records a memory_promotion finding; keeps the note live
      Unknown doc_id or unknown verdict: silently skipped.
```

to:

```python
  drain_distil(store, inbox_obj) -> {"expired": N, "promotions_flagged": N}
      Applies verdicts (all three stamp distilled_at so build_distil_requests
      stops re-submitting an already-classified note):
        "keep"    — stamps distilled_at only
        "expire"  — patches chunk metadata expired=True, records memory_expired
        "promote" — records a memory_promotion finding; keeps the note live
      Unknown doc_id or unknown verdict: silently skipped.
```

- [ ] **Step 8: Run the tests**

Run: `pytest tests/test_memory_distil.py -v -p no:xdist`
Expected: all PASS, including the pre-existing `test_drain_expires_and_promotes` (its `live = {...}` assertion uses `note_chunks(observation_type="memory")` with no `exclude_distilled`, so it is unaffected by this change).

- [ ] **Step 9: Run the impacted suites**

Run: `pytest tests/test_memory_distil.py tests/test_store_schema_p3.py -q`
Expected: PASS.

Also run the one other caller of `note_chunks` to confirm it is genuinely untouched:

Run: `grep -rn "note_chunks(" mcpbrain/memory_index.py` and confirm the call still reads `store.note_chunks(observation_type="memory")` with no `exclude_distilled` argument — if it does, no test changes are needed there; if a prior task somehow changed it, stop and report back before proceeding.

- [ ] **Step 10: Lint**

Run: `ruff check mcpbrain/store.py mcpbrain/memory_distil.py tests/test_store_schema_p3.py tests/test_memory_distil.py`
Expected: `All checks passed!`

- [ ] **Step 11: Commit**

```bash
git add mcpbrain/store.py mcpbrain/memory_distil.py tests/test_store_schema_p3.py tests/test_memory_distil.py
git commit -m "fix(memory_distil): stop re-asking about already-classified notes

build_distil_requests resubmitted the same live notes to Haiku on
every distil run regardless of the prior verdict — only 'expire'
marked the chunk so it stopped resurfacing. Add a distilled_at
metadata stamp on all three verdicts (keep/expire/promote) and a
symmetric note_chunks(exclude_distilled=) filter, applied before the
result cap exactly like the existing expired filter. memory_index.py's
call is untouched — a distilled note still belongs in the memory index."
```

---

## Done criteria

- `pytest tests/test_store_schema_p3.py tests/test_review_apply.py tests/test_mcp_server.py tests/test_mcp_server_stdio.py tests/test_dashboard_digest.py tests/test_memory_distil.py tests/test_change_log.py tests/test_lint.py -q` passes.
- `ruff check mcpbrain/ tests/` is clean.
- Four commits on `main`, unpushed.
- Report to Josh so he can run the full suite, then decide separately about restarting the local daemon and about releasing.

## Deliberately not in this plan

- Time-boxed re-review of a settled verdict (spec: out of scope by design).
- An "unack" tool — reversal stays a direct DB action.
- Any version bump or release step.
- Any manual SQL against the live store.
