# Console Redesign Follow-ups Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. **Every task ends with a review gate:** a fresh reviewer checks the task's diff before the next task starts.

**Goal:** Close the findings the console redesign shipped with: the remaining raw codes, three correctness gaps, two concurrency gaps, the dropped maintenance history, and a reviewable list of unlinked entities.

**Architecture:** Small changes across the existing modules (`drain.py`, `console_stats.py`, `records_write.py`, `config.py`, `auto_changes.py`, `review.py`, `graph_view.py`, `control_api.py`) and the three console pages. A cross-process file lock replaces the in-process records lock. Config gains a cross-process write lock. Unlinked entities get a paged list endpoint and a list view in the graph page.

**Tech Stack:** Python 3 stdlib (`fcntl`/`msvcrt` for file locks), SQLite, pytest, ruff; plain HTML/CSS/JS on the existing `console.js` helpers. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-11-06-console-redesign-design.md`. Parent plan: `docs/superpowers/plans/2026-11-09-console-redesign.md` (its Global Constraints apply and are restated below).

## Staleness markers

- Last retirement check: 2026-11-09 against state/retired.md (52 entries scanned)
- Last cross-checked against state/decisions.md: 2026-11-09
- Deviation banner: none.

## Global Constraints

- Public repo: no real person names, emails, organisation names or owner paths anywhere. Fictional cast only (`Dana Okafor`, `Acme`, `Sam Chen`, `Pat Nguyen`, `example.org`). `uv run pytest tests/test_no_tenant_literals.py -q` passes with `mcpbrain/tenant_people.json` present.
- Branch `console-redesign`; commit per task after review; no push or release.
- Every control works end to end; no raw machine strings on any page (codes, ids, exception text); worded fallbacks for unknown values.
- Pages: textContent-only rendering of server strings; colours and fonts via tokens; mono only for measured values; no yellow, amber, orange, brown or violet; Australian English; no em dashes in UI copy; Hallmark sloplint 0 FAIL on every touched page (`node ~/.claude/skills/hallmark/scripts/sloplint.mjs <file> --genre modern-minimal --scope app`).
- Never touch the owner's real daemon, store, config or records repo; tests use tmp stores and tmp git repos.
- TDD: failing test first, shown RED, then GREEN; full suite once before commit; `uv run ruff check .` clean.

## Review Focus

1. **Two processes writing the records repo at once** (the daemon and the MCP server's gardener tool): one waits, then succeeds or refuses in words; neither corrupts the repo or leaves a stale lock after a crash. Test: Task 1.
2. **Two config writes at once from different paths** (`/api/settings` and `/api/config` or a daemon writer): both changes survive. Test: Task 1.
3. **An approval whose change lands but whose recording fails:** a second approval never applies the change twice. Test: Task 1.
4. **The unlinked list on a store with 10,000+ unlinked entities:** pages load in under a second and the total is stated. Test: Task 2.
5. **Unknown codes** reaching any page render as worded fallbacks. Test: Task 1.

---

### Task 1: Cleanup — words, locks, approvals, maintenance history

**Files:**
- Modify: `mcpbrain/drain.py:990`, `mcpbrain/console_stats.py` (`health`, stats `needs_you`), `mcpbrain/dashboard.py` (`findings_view`), `mcpbrain/records_write.py` (`_REPO_LOCK`, `repo_lock`, `RecordsBusy`, `BUSY`), `mcpbrain/config.py` (`write_config`, plus every other config writer it lists), `mcpbrain/settings_api.py`, `mcpbrain/auto_changes.py` (`_decide_claimed`, `_settle`, `_release`), `mcpbrain/review.py` / `mcpbrain/daemon.py` (`_run_review` off path), `mcpbrain/control_api.py` (restart and maintenance routes), `mcpbrain/tray.py`, `mcpbrain/wizard/dashboard.html`, `mcpbrain/wizard/settings.html`, `mcpbrain/wizard/console.js`
- Test: `tests/test_console_followups.py` plus the existing test files for each module touched

**Interfaces:**
- Produces:
  - `records_write.repo_lock()` becomes a cross-process lock: an exclusive OS file lock on `<records repo>/.git/mcpbrain-records.lock` (`fcntl.flock` on POSIX, `msvcrt.locking` on Windows) held together with the existing in-process `RLock`; the same 30 s timeout and `RecordsBusy`/`BUSY` refusal; the lock file is never deleted, so a crashed holder's OS lock is released by the OS and nothing is stale. `tools.make_brain_gardener_apply` and every records writer take it.
  - `config.write_config(home, updates)` takes a cross-process file lock on `<home>/config.lock` around its read-merge-write; `settings_api.apply_settings` and the `/api/config` path run inside that same lock. Signature unchanged.
  - `auto_changes`: an approval records `applied_change_id` on the `pending_changes` row in the same transaction as the change itself when the writer supports `record_change_tx`; if recording fails after the change landed, the row is settled `applied` with the error noted (never returned to `pending`). A second approval of an `applied` row returns `ALREADY_DECIDED`.
  - `GET /api/maintenance` → `{"reflow": store.reflow_status(), "ingestion_cleanup": ingestion_cleanup.status(store)}`, worded on the page under Settings > Advanced as "Maintenance history" (finished date, items handled, items given up on).
  - `GET /api/findings` count and `needs_you.findings` and the health reason all count grouped findings (the same grouping `findings_view` uses).
- Raw-string fixes:
  - `drain.py:990` summary becomes `Action "<action text, first 60 chars>" marked <worded status>`; `ref_id` stays the id.
  - Page fallbacks: every `X[code] || code` in `dashboard.html`, `settings.html` and `graph.html` (skip reasons, change types, cold reasons, the chunk list's Source column, finding types, kinds) gets a worded default ("an automatic change", "skipped for another reason", "another source"). Add `console.js` `worded(map, code, fallback)` and use it everywhere.
- Other:
  - With tidy-up `off`, the daily review pass resolves open tidy lint findings (verdict `tidy_off`) instead of leaving them open; switching tidy back on lets them be recorded again.
  - Hidden entities: a later failed restore clears the earlier success line in `#hidden-said`.
  - Undone rows inside grouped Review lists are struck through (same style as top-level undone rows).
  - Restart (`/api/restart`) and the tray's "Start mcpbrain" ignore a second request while one is in flight (202 `{"restarting": true, "already": true}` for the route; the tray item disables until the daemon answers or 60 s pass).

- [ ] **Step 1: Write the failing tests** in `tests/test_console_followups.py`:
  - the action-update summary contains the action text and no raw id;
  - a node-run test feeds unknown codes to every page's word maps through `worded()` and gets the fallback text;
  - two identical `agent_stderr` findings give `needs_you.findings == 1` and the reason "1 open finding";
  - two processes (`multiprocessing`) committing to one tmp records repo both succeed or one gets `RecordsBusy`, the repo stays valid (`git fsck` clean), and a killed holder does not block the next writer;
  - two processes writing different config keys at once both survive;
  - an approval where recording raises after APPLY lands leaves the row `applied`, and a second approval returns `ALREADY_DECIDED` with APPLY called once;
  - `/api/maintenance` returns both blocks on a tmp store;
  - with tidy off, a review pass resolves `lint:*` findings;
  - a second `/api/restart` within one restart reports `already`;
  - page asset tests for the maintenance block, the `#hidden-said` reset and the grouped undone style.
- [ ] **Step 2: Run** `uv run pytest tests/test_console_followups.py -q` and expect failures.
- [ ] **Step 3: Implement** as listed.
- [ ] **Step 4: Run** the new file plus `tests/test_records_write.py tests/test_settings_api.py tests/test_auto_changes.py tests/test_console_stats.py tests/test_dashboard*.py tests/test_settings_page.py tests/test_graph_page.py tests/test_system_ops.py tests/test_tray.py tests/test_phase2_gardener.py tests/test_config*.py`, then the full suite, ruff, the tenant check and sloplint on the three pages.
- [ ] **Step 5: Review gate, then commit** `fix(console): worded fallbacks, cross-process locks, safe approvals, maintenance history`.

### Task 2: Unlinked entities as a reviewable list

**Files:**
- Modify: `mcpbrain/graph_view.py` (new `list_unlinked`), `mcpbrain/control_api.py` (new route), `mcpbrain/wizard/graph.html` (list view for `?show=unlinked`), `mcpbrain/wizard/dashboard.html` (the "Review unlinked entities" link)
- Test: `tests/test_graph_routes.py`, `tests/test_graph_page.py`

**Interfaces:**
- Produces:
  - `graph_view.list_unlinked(store, *, page: int = 1, page_size: int = 50, kinds: tuple[str, ...] = (), q: str = "") -> {"items": [{id, name, type, org, first_seen, last_seen, mentions}], "total": int, "page": int, "page_size": int}`, reusing `_unlinked_sql` (live relations only, suppressed excluded), ordered `last_seen DESC, id`, `page_size` clamped 10 to 200, `kinds` filtering on the same type mapping as the canvas (empty, NULL and `unknown` together).
  - `GET /api/graph/unlinked?page=&page_size=&type=&q=`.
- Page: `?show=unlinked` opens a list view in the graph page's main area (the canvas view stays available through a "Show on the map" switch). Each row: name, kind, organisation, last seen (measured), and actions Open (drawer), Merge with… (existing merge preview flow), Hide (existing suppress, undoable). A pager (previous / next, "Page 2 of 185, 9,205 entities") and a name filter. The rail's kind filters apply. The Overview's "Review unlinked entities" link opens this list.

- [ ] **Step 1: Write the failing tests:**
  - `list_unlinked` paging, ordering, kind filter, name filter, and invalidated or suppressed exclusion on a tmp store;
  - 12,000 unlinked fixture entities return page 1 in under 1 s with `total == 12000`;
  - the route's 400 on a bad page;
  - page asset tests for the list view, the pager wording and the three row actions wired to existing endpoints.
- [ ] **Step 2: Run** the tests and expect failures.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest tests/test_graph_routes.py tests/test_graph_page.py -q`, then the full suite, ruff, the tenant check and sloplint on `graph.html` and `dashboard.html`. Check it live on a tmp store with 2,000 unlinked fictional entities: list, page, filter, merge one, hide one and undo it. Take screenshots in light and dark.
- [ ] **Step 5: Review gate, then commit** `feat(console): unlinked entities as a reviewable list`.

### After both tasks (controller, not a subagent task)

Render all four pages (Overview, Settings, Graph, Setup) on the final commit against a tmp store with fictional data, in light, dark and 390 px, read every screenshot, and fix anything wrong through a scoped fix dispatch before handing back.
