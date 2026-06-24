# Plan: make every records file self-developing

**Date:** 2026-06-24
**Goal:** Every file in the records store (`~/Library/Application Support/mcpbrain/records/`)
either develops itself from real evidence, or is auto-drafted and applied — none stay frozen
at the install scaffold.
**Owner decisions baked in:** full-auto on all files (incl. identity/voice/preferences);
`Ministry-context.md` → `reference/org-context.md`.

---

## Guiding principle: git is the safety net

The records store is a git repo and **every write tool / cadence commits**. That is what makes
"full auto on the constitution files" defensible: each auto-edit to `identity.md`, `voice.md`,
etc. is an isolated, attributed, revertible commit. So the design rule is:

> Auto-write freely, but **every autonomous edit must be its own commit with a clear author tag**
> (`gardener:`, `voice:`, `core:`, `consolidate:`) so any one can be reverted without losing the rest.

The existing `GARDENER-PROTECTED` block in `records/CLAUDE.md` was the old guard rail. Going
full-auto means **relaxing it** (Phase 2) — replaced by the git-revert safety net + a weekly digest.

---

## Target state per file

| File | Today | Target mechanism | Phase |
|---|---|---|---|
| `state/decisions.md` | 🟢 alive (`brain_decision`) | unchanged | — |
| `state/hot.md` | 🟢 alive (`brain_note` + prune) | unchanged | — |
| `context/voice.md` | 🔴 frozen (machinery OFF) | weekly analyse **+ auto-apply** from real drafts | 1 |
| `MEMORY.md` + `memory/*.md` | 🔴 empty | nightly consolidation **graduates** distilled notes → `memory/` | 1 |
| core identity tier | ⚫ never seeded | `seed_core_identity()` wired into nightly pass | 1 |
| `context/identity.md` | 🔴 frozen stub | gardener auto-edits from graph/identity evidence | 2 |
| `context/preferences.md` | 🔴 frozen stub | gardener auto-edits from observed feedback/lessons | 2 |
| `reference/projects.md` | 🟡 propose-only | gardener **auto-applies** drift + proposals | 2 |
| `reference/systems.md` | 🟡 propose-only | gardener auto-applies | 2 |
| `reference/org-context.md` (was `Ministry-context.md`) | ⚫ absent | scaffold + gardener-developed org/ministry reference | 2 |
| protected block in `records/CLAUDE.md` | guard rail | relaxed → weekly digest replaces approval gate | 2 |

---

## Phase 1 — turn on what's built; fill the empty files (low risk)

Everything here uses machinery that **already exists** — it's flags + a few missing wiring calls.
Self-gated and reversible. No change to the gardener's human-approval model yet.

### 1a. Flip the dormant loops on (config only)

Add to the runtime config (`~/.mcpbrain/config.json` — confirm path via `config.py:read_config`):

```jsonc
{
  "tiered_memory":     true,   // core tier populated nightly (config.py:175)
  "decay":             true,   // nightly decay pass that also runs tier_pass (config.py:185)
  "consolidation":     true,   // nightly episodic→semantic distillation (config.py:195)
  "procedural_memory": true    // weekly voice analysis (config.py:205)
}
```

Effect: `_run_decay_pass` (daemon.py:1339) starts calling `run_tier_pass` → `recompute_core`;
consolidation starts writing semantic notes; voice analysis starts queuing suggestions. No
`context/`/`memory/` *file* writes yet — that's 1b–1d.

### 1b. Seed the core identity tier (one missing call)

`seed_core_identity()` (memory_tier.py:98) is implemented but **never invoked**. Call it from
`run_tier_pass` (or once at daemon start when `tiered_memory` is on) so identity/org facts from
`config.json` + `identity.md` become an always-injected `core` chunk that then grows via
`recompute_core`. Commit tag `core:`.

### 1c. Graduate distilled notes into durable `memory/*.md`

Consolidation (consolidation.py:285) writes `note-consolidated-*` chunks into the *search store*
as `hot` tier, but never into the `memory/` *files* — why `MEMORY.md` is still 15 bytes. Add a
graduation step: when a consolidated/distilled note crosses a confidence + recurrence bar
(e.g. seen ≥N times, salience ≥X), call `records_write.write_memory(slug, description, body,
memory_type)` (records_write.py:82) → real `memory/<slug>.md` + `MEMORY.md` pointer. Gardener
(gardener.md, weekly) dedupes/expires as designed. Commit tag `consolidate:`.

### 1d. voice.md auto-applies

`_run_voice_analyse` (daemon.py:1389) only *queues* suggestions; `voice_apply.apply_suggestions()`
is manual. Add an auto-apply call after `maybe_run_analysis`, gated on a new `voice_auto_apply`
flag (mirror `procedural_memory_enabled`). Keep the built-in **3-day cooldown + 20-line diff cap**
— they make auto-apply safe by construction. Commit tag `voice:`.

**Validate Phase 1 (≈1 week of real use):** daemon logs show `tier_pass`, `consolidation:
notes_written`, `voice_analyse: queued N`; core block appears in `/api/recall`; `memory/` is
non-empty and `MEMORY.md` lists pointers; `git log -- context/voice.md` shows dated voice commits.
Spot-check + `git revert` anything off.

---

## Phase 2 — full gardener autonomy + relax guard rails (broad)

Do after 1–2 weeks of trust in Phase 1. This removes the human-approval gate and brings the
constitution + reference + org-context files under autonomous development.

### 2a. Gardener auto-applies (reference + identity + preferences)

Today reference-gardener (reference-gardener.md) writes proposals to `reference/_proposals/` and
waits for a human. Going full-auto, two lanes, both auto-applied:
- **Drift lane** (status updates, well-evidenced new projects/systems): apply directly to
  `reference/*.md`, commit `gardener: apply drift (file)`.
- **Constitution lane** (`identity.md`, `preferences.md`): derive from `brain_context`/`brain_graph`
  + observed feedback/lessons, write directly, commit `gardener: update identity/preferences`.
- Still write the human-readable proposal file, now as a **changelog/digest**, not a gate.
- **Hard constraint:** enforce the existing role-attribution rule *inside* the identity writer
  ("never attribute a role from text you wrote") or it will assert wrong titles.

### 2b. Create reference/org-context.md (the "Ministry-context")

Scaffold `reference/org-context.md` in `records.py:ensure_records_repo` (TEMPLATE_FILES ~line 24–30)
+ `records_templates/reference_org_context.md` with sections for Centrepoint / Courageous / ACC
structure, governance, key people, org rules. reference-gardener already *reads* this file — extend
it to *develop* it from org evidence in the graph. Fold the org-tagging rules currently in the
protected `CLAUDE.md` block here as living content.

### 2c. Relax the protected block + weekly digest

Replace the `GARDENER-PROTECTED-START/END` block in `records_templates/CLAUDE.md` (and live
`records/CLAUDE.md`) with a note that these files are now auto-developed and git-revert is the
control. Add a weekly **"what changed in your brain" digest** prepended to `state/hot.md`: every
autonomous commit since last digest, one-line summaries + revert hints. This is the replacement for
the approval gate — review after, revert in one command.

**Validate Phase 2:** `git log` shows gardener commits to all four files weekly; `org-context.md`
populates; digest appears weekly and accurately lists the week's auto-commits; spot-check
identity/prefs edits for accuracy.

---

## Risks & caveats

- **Full-auto on identity/preferences can encode mistakes** — the role-attribution rule must live
  inside the gardener writer (Phase 2a). Backstop: git-revert + weekly digest.
- **Voice auto-apply can drift tone** on an atypical week — 20-line cap + 3-day cooldown limit blast
  radius; digest surfaces it.
- **Cost:** consolidation + voice + gardener shell out to the `claude` CLI; nightly/weekly cadence is
  modest but watch daemon logs the first fortnight.
- **These flags default OFF in `config.py` by design** — this is config + local code, it does **not**
  ship to other plugin users. Shipping is the separate explicit 3-repo release (mcpbrain →
  mcpbrain-dist → mcpbrain-plugin) and is out of scope here.

## Rollback

Every step is independently reversible: remove a config flag, or `git revert` in the records repo.
Nothing is shipped until a separate explicit release.
