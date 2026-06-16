# `mcpbrain doctor` + Automated End-to-End Test

> Spec date: 2026-06-16. Baseline: mcpbrain 0.0.6. Roadmap items #3 (install robustness / doctor) and #5 (automated e2e test). Grouped because both harden the install→sync→enrich seams that unit tests pass through.

---

## Part A — `mcpbrain doctor`

### Problem

The install chain has many failure points and opaque errors (uv missing, PATH unset, daemon down, scheduled tasks not created, MCP not connected, home split-brain). Non-technical users can't debug them, and several real bugs this cycle lived in these seams.

### Goal

A single `mcpbrain doctor` entrypoint that **diagnoses** every health dimension in plain language and **auto-fixes the idempotent local failures**, pointing at the exact next step for anything only Claude/Cowork can fix. Safe to re-run — same idempotent philosophy as the install skill.

### Design

`mcpbrain doctor` reuses `probes.all_connections()` (so CLI, wizard, monitor, and the recovery hook never disagree) and adds a repair layer.

Each probe result maps to one of three dispositions:

| Disposition | Meaning | doctor behaviour |
|---|---|---|
| **auto-fixable** | a local, idempotent fix exists | attempt the fix, then re-probe and report ✅/❌ |
| **guided** | only Claude/Cowork/the user can fix it | print the exact command or skill to run |
| **ok / not_started** | healthy, or deliberately unconfigured | report, do nothing |

### Auto-fixable repairs (local, idempotent)

| Failure | Repair |
|---|---|
| Daemon not running (`claude`/heartbeat stale but agent installed) | restart the daemon (`agents.restart_agent(platform)`) |
| launchd/schtasks agent missing | re-register it (`agents.install_agent(...)`) |
| Records repo missing / not a git repo | recreate from `records_templates` + `git init` (reuse setup's records bootstrap) |
| Control port/token file missing while daemon should be up | restart daemon (regenerates them) |
| Home split-brain (config in an unexpected home) | detect + report the resolved home; do **not** silently move data — print the mismatch and the canonical path |

### Guided (print remedy, don't attempt)

| Failure | Remedy printed |
|---|---|
| Google expired | `Run: mcpbrain auth` |
| A scheduled task missing | `Run /mcpbrain-fix in Cowork to recreate the enrich/gardener/meeting-packs/reference-gardener tasks` |
| ClickUp key invalid | `Re-enter your ClickUp key in the mcpbrain wizard` |
| MCP not connected (plugin not installed) | `Install the mcpbrain plugin and run /reload-plugins` |

### Output shape

```
$ mcpbrain doctor
mcpbrain doctor — 2026-06-16 12:01 AWST   (home: /Users/josh/.mcpbrain)

✅ Google           Connected
❌ Daemon           Not running → restarting... ✅ fixed
⚠️  Enrichment       No enrichment in 48h → open Claude or run /mcpbrain-fix
✅ Records          OK
⚠️  Scheduled tasks  enrich task not detectable → run /mcpbrain-fix in Cowork
✅ Backup           OK

2 fixed automatically, 2 need your action (see ↑).
```

Exit code: `0` if nothing needs user action after auto-fix; `1` otherwise (so it composes with scripts/monitors).

> **Note on scheduled-task detection:** the daemon cannot read the Cowork app DB, so doctor cannot *verify* the four scheduled tasks directly. It infers their health from `probe_enrichment` (enrichment fresh ⇒ enrich task is firing). doctor states this honestly rather than claiming to check the task list. Recreating tasks is therefore always a *guided* step (`/mcpbrain-fix`), never auto.

### Components

**New: `mcpbrain/doctor.py`**
- `run_doctor(home) → tuple[int, str]` — orchestrates: probe → classify disposition → attempt auto-fixes → re-probe fixed ones → format report. Pure-ish: the repair calls are injected (the actual `agents.*` / records-bootstrap functions) so the logic is unit-testable with stubs.
- `_DISPOSITIONS: dict[str, str]` mapping probe key → `"auto" | "guided"` and the repair/remedy.

**Modified: `mcpbrain/cli.py`** — add `doctor` subcommand → `doctor.run_doctor`.

**Optional, separate from this spec's core:** a `/mcpbrain-fix` Cowork skill that recreates the four scheduled tasks (it can drive the Cowork UI the way the install skill does). Listed here as the referenced remedy; its full body can be specced with the doctor implementation plan or folded into the existing install skill as a "repair" mode.

### Testing (doctor)

`tests/test_doctor.py`:
- daemon-down + agent-installed → repair called, re-probe ✅, reported fixed
- agent-missing → install_agent called
- records-missing → records bootstrap called
- google-expired → no repair attempted, guided remedy in output
- everything ok → exit 0, no repairs
- a repair that fails → reported ❌, exit 1
- repair functions are injected stubs (no real launchd/git side effects in tests)

---

## Part B — Automated end-to-end test

### Problem

1300+ unit tests, near-zero integration coverage at the daemon↔Cowork↔plugin boundary. Every real bug this cycle (home split-brain, dead ClickUp dispatch, enrichment-packaging gap) lived in seams that unit tests passed through.

### Goal

A repeatable, CI-runnable test of the real loop: **sync → prepare spool → (stubbed Cowork extractor) → drain → graph + dashboard**, asserting that known input produces the expected graph rows and dashboard output.

### Design

A pytest test (`tests/e2e/test_full_loop.py`) that exercises the real modules end-to-end with only the two external boundaries stubbed:

1. **Stub Google** — a fake Drive/Gmail/Calendar service object returning a small fixed fixture (2–3 email threads, 1 calendar event, 1 doc). No network.
2. **Run the real sync** → chunks land in a temp store.
3. **Run the real `prepare`** → assert a `pending.json` spool is written with the expected threads.
4. **Stub the Cowork extractor** — instead of invoking Claude, write a known-good `enrich_inbox/<batch>.json` (a hand-authored extraction matching the spool, valid against the contract). This is exactly what the enrich skill would produce.
5. **Run the real `drain`** → `graph_write.apply` runs for real.
6. **Assert:** expected person/org entities + relations exist in the graph; `dashboard.assemble` returns the expected actions; `brain_search` finds a known chunk.

The only fakes are the two things we genuinely can't run in CI (Google's API, Claude's enrichment). Everything between them is the real code path.

### CI

- Runs in GitHub Actions on every push (new job in the existing workflow, or a `pytest -m e2e` marker).
- Fast (seconds — small fixture, local sqlite, no network).
- Gates the branch: a red e2e fails the build.

### Components

**New: `tests/e2e/test_full_loop.py`** + `tests/e2e/fixtures/` (fixed Google payloads + the canned `enrich_inbox` extraction).
**New helper:** a `FakeGoogleService` in `tests/e2e/conftest.py` returning the fixtures (shaped like the real `googleapiclient` resource so sync code needs no changes).
**Modified:** CI workflow to run the e2e job.

### Testing (the test)

The e2e test *is* the test. Guard against it silently passing on a no-op: assert non-empty graph + non-empty dashboard so a broken pipeline that produces nothing fails loudly.

---

## Sequencing

Doctor and e2e are independent and can be built in either order. Suggested: doctor first (smaller, immediately useful for support), then the e2e harness (locks the seams doctor diagnoses).

---

## Dependencies (for parallel-worktree execution)

**Files this worktree owns exclusively:** `mcpbrain/doctor.py` (new), new `tests/test_doctor.py`, new `tests/e2e/*` (incl. `conftest.py` + fixtures), and the CI workflow file (no other spec edits CI).

**Files SHARED with another spec (expect a small merge conflict):**
- `mcpbrain/cli.py` — Spec 3 adds the `doctor` subcommand; **Spec 1 adds `fleet-report`** to the same registration tuple + dispatch dict. Whichever merges second resolves a ~2-line conflict. No logic dependency.

**Depends on other specs' new code:** none. `doctor` reuses `probes` + `agents` + the records-bootstrap helper, all of which exist in current 0.0.6. Builds without Specs 1/2/4.

**Provides to other specs:** the `mcpbrain doctor` subcommand + `/mcpbrain-fix` skill that **Spec 2's remedy strings reference**. Important: this is a one-way, text-only reference — Spec 2 does **not** import or call anything here, so Spec 3 has **no** reciprocal dependency on Spec 2 and the two can merge in any order. (If Spec 3 lands first, Spec 2's remedies are immediately runnable; if second, they light up on merge.)

**Shared read-only:** `probes.all_connections` (also read by Specs 1 + 2; none modify `probes.py`).

**Merge note:** only collision is the `cli.py` 2-liner with Spec 1.
