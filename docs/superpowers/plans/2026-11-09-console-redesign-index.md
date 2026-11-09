# Console redesign: plan index

**Spec:** `docs/superpowers/specs/2026-11-06-console-redesign-design.md` (approved; scope rule 2026-11-09)

## Staleness markers

- Last retirement check: 2026-11-09 against state/retired.md (52 entries scanned)
- Last cross-checked against state/decisions.md: 2026-11-09 (the archived ops-brain log, no rows after 2026-06-09, and the live records-repo log, 18 lines)
- Deviation banner: none blocking. The 2026-05-29 "Dark-mode support" retirement applies to the retired Nexus workstation (Svelte app), not this console; the console's light and dark modes do not revive it. The 2026-05-21 force-graph decision is consistent with keeping the graph engine. The 2026-05-16 voice-analyser revival is consistent with the voice row in Settings.

## Why seven plans

The spec spans independent subsystems: the stats layer, the undo and review backend, source controls, system controls, and three pages. Each phase below is its own plan and ships working software on its own, in this order. A plan is written against the code as it stands when the previous phase has landed, so later plans do not guess at interfaces earlier phases have not built yet.

| Phase | Plan | Depends on | Status |
|---|---|---|---|
| 1. Correct numbers | `2026-11-09-console-phase1-correct-numbers.md` | none | written |
| 2. Undo and review foundation | `…-console-phase2-undo-review.md` | 1 (health reads review counts) | to write |
| 3. Source controls | `…-console-phase3-source-controls.md` | 1 (freshness) | to write |
| 4. System controls | `…-console-phase4-system-controls.md` | none | to write |
| 5. Overview | `…-console-phase5-overview.md` | 1, 2, 3 (every action wired) | to write |
| 6. Settings and Setup | `…-console-phase6-settings.md` | 2, 3, 4 | to write |
| 7. Graph | `…-console-phase7-graph.md` | 5 (shared shell and tokens) | to write |

Phases 2, 3 and 4 do not depend on each other and can run in parallel once 1 has landed.

## Rules every phase carries

- **Scope rule:** every control ships with its server behaviour; no dead controls (spec, Delivery order).
- **Public repo:** no real person names, email addresses, organisation names or the owner's folder paths in code, tests, docs or commit messages. Use the repo's fictional cast (`Dana Okafor`, `Acme`, `Sam Chen`, `Pat Nguyen`, `example.org`). `tests/test_no_tenant_literals.py` must pass, with `mcpbrain/tenant_people.json` present.
- **Branch:** `console-redesign`. Commit per task. No push or release without the owner's go-ahead; releasing follows `docs/RELEASE-RUNBOOK.md`.
- **Checks before a phase is done:** `uv run pytest -q`, `uv run ruff check .`, and the phase's own live verification step.
- **Design-side findings to carry into later plans:** the Settings prototype says "My Drive and 2 shared drives"; the live store has 17 shared-drive cursors, one stale since July. Phase 6 shows the real count, and Phase 3 decides how a drive that stopped syncing (access removed) is reported.
