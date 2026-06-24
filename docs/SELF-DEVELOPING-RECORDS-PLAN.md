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
full-auto means **relaxing it** (Phase 4) — replaced by the git-revert safety net + a weekly digest.

---

## Target state per file

| File | Today | Target mechanism | Phase |
|---|---|---|---|
| `state/decisions.md` | 🟢 alive (`brain_decision`) | unchanged | — |
| `state/hot.md` | 🟢 alive (`brain_note` + prune) | unchanged | — |
| `context/voice.md` | 🔴 frozen (machinery OFF) | weekly analyse **+ auto-apply** from real drafts | 1 |
| `MEMORY.md` + `memory/*.md` | 🔴 empty | nightly consolidation **graduates** distilled notes → `memory/` | 2 |
| core identity tier | ⚫ never seeded | `seed_core_identity()` wired into nightly pass | 2 |
| `context/identity.md` | 🔴 frozen stub | gardener auto-edits from graph/identity evidence | 3 |
| `context/preferences.md` | 🔴 frozen stub | gardener auto-edits from observed feedback/lessons | 3 |
| `reference/projects.md` | 🟡 propose-only | gardener **auto-applies** drift + proposals | 3 |
| `reference/systems.md` | 🟡 propose-only | gardener auto-applies | 3 |
| `reference/org-context.md` (was `Ministry-context.md`) | ⚫ absent | scaffold + gardener-developed org/ministry reference | 5 |
| protected block in `records/CLAUDE.md` | guard rail | relaxed → weekly digest replaces approval gate | 4 |

---

## Phase 0 — turn on the dormant loops (config only, no code)

These subsystems are fully built and self-gating; they're just flagged off. Add to the runtime
config (`~/.mcpbrain/config.json` — confirm exact path in `config.py:read_config`):

```jsonc
{
  "tiered_memory":     true,   // core tier populated nightly (config.py:175)
  "decay":             true,   // nightly decay pass that also runs tier_pass (config.py:185)
  "consolidation":     true,   // nightly episodic→semantic distillation (config.py:195)
  "procedural_memory": true    // weekly voice analysis (config.py:205)
}
```

**Effect immediately:** `_run_decay_pass` (daemon.py:1339) starts calling `run_tier_pass` →
`recompute_core`, consolidation starts writing semantic notes, voice analysis starts queuing
suggestions. Nothing applies to the `context/`/`memory/` *files* yet — that's Phases 1–2.

**Validate (1 week):** check daemon logs for `tier_pass`, `consolidation: notes_written`,
`voice_analyse: queued N suggestions`. Confirm core block appears in `/api/recall`. No file
churn expected yet.

---

## Phase 1 — voice.md auto-applies

Today `_run_voice_analyse` (daemon.py:1389) only *queues* suggestions; `voice_apply.apply_suggestions()`
is manual. Going full-auto:

1. After `maybe_run_analysis` in `_run_voice_analyse`, call `apply_suggestions()` automatically,
   gated on a new `voice_auto_apply` flag (mirror `procedural_memory_enabled` in config.py).
2. Keep the existing safety rails already in `voice_apply.py`: **3-day cooldown + 20-line diff cap
   per apply** — those make auto-apply safe by construction.
3. Commit as `voice: weekly auto-update (N lines)`.

**Validate:** after 1–2 weekly cycles, `git log -- context/voice.md` shows dated voice commits;
diff each one for sanity; revert any that drift.

---

## Phase 2 — MEMORY.md / memory/ actually fill

Two wiring fixes:

**2a. Seed core identity (one missing call).** `seed_core_identity()` (memory_tier.py:98) is
implemented but never invoked. Call it from `run_tier_pass` (or once at daemon start when
`tiered_memory` is on) so identity/org facts from `config.json` + `identity.md` become an
always-injected `core` chunk that then *grows* via `recompute_core`.

**2b. Graduate distilled notes into durable `memory/*.md`.** Consolidation (consolidation.py:285)
writes `note-consolidated-*` chunks into the *search store* as `hot` tier, but never into the
`memory/` *files* — which is why `MEMORY.md` is still 15 bytes. Add a graduation step: when a
consolidated/distilled note crosses a confidence + recurrence bar (e.g. seen ≥N times, salience ≥X),
call `records_write.write_memory(slug, description, body, memory_type)` (records_write.py:82) so it
becomes a real `memory/<slug>.md` + a `MEMORY.md` pointer. The gardener (gardener.md, weekly) then
dedupes/expires as designed.

**Validate:** within a week of real usage, `memory/` is non-empty and `MEMORY.md` lists pointers;
gardener log shows tidy passes; no duplicate slugs.

---

## Phase 3 — gardener auto-applies (reference + identity + preferences)

Today reference-gardener (reference-gardener.md) writes proposals to `reference/_proposals/` and
**waits for a human** — that's why projects/systems only moved when proposals were applied by hand,
and identity/preferences never moved at all.

Going full-auto, split by risk but auto-apply both lanes:

- **Low-risk drift** (status updates, new projects/systems with strong evidence): gardener applies
  directly to `reference/*.md`, commits `gardener: apply drift (file)`.
- **Constitution files** (`identity.md`, `preferences.md`): extend the gardener to derive updates
  from the graph (`brain_context`/`brain_graph`) and observed feedback/lessons, and **write them
  directly**, commit `gardener: update identity/preferences`.
- Keep writing the human-readable proposal file too (now as a *changelog/digest*, not a gate).

**Validate:** `git log` shows gardener commits to all four files weekly; spot-check identity/prefs
edits for accuracy (bad role attribution is the main risk — see Risks).

---

## Phase 4 — relax the protected block, add a weekly digest

1. In `records_templates/CLAUDE.md` (and the live `records/CLAUDE.md`), replace the
   `GARDENER-PROTECTED-START/END` block with a note that these files are now auto-developed and
   git-reverting is the control.
2. Add a **weekly "what changed in your brain" digest** prepended to `state/hot.md` (or emailed):
   lists every autonomous commit since last digest with one-line summaries + revert hints. This is
   the replacement for the approval gate — you review *after*, not *before*, and revert is one
   command.

**Validate:** digest appears weekly and accurately lists the week's auto-commits.

---

## Phase 5 — create reference/org-context.md (the "Ministry-context")

1. Scaffold `reference/org-context.md` in `records.py:ensure_records_repo` (TEMPLATE_FILES around
   line 24–30) + a matching `records_templates/reference_org_context.md` with sections for
   Centrepoint / Courageous / ACC structure, governance, key people, org-specific rules.
2. reference-gardener already *reads* `reference/org-context.md` — extend it to *develop* it from
   org evidence in the graph (people→org affiliations, governance decisions, recurring entities),
   under the same auto-apply rule as Phase 3.
3. Fold the org-tagging rules currently in the protected `CLAUDE.md` block here as living content.

**Validate:** file scaffolds on next `ensure_records_repo`; gardener populates it; org tags in
recall reference it.

---

## Risks & honest caveats

- **Full-auto on identity/preferences can encode mistakes.** The existing role-attribution rule
  ("never attribute a role from text you wrote") must be enforced *inside* the gardener's identity
  writer, or it will assert wrong titles. Mitigation: keep that rule as a hard constraint in the
  gardener prompt; rely on git-revert + weekly digest.
- **Voice auto-apply can drift your tone** if drafts in a given week are atypical. Mitigation: the
  20-line cap + 3-day cooldown already limit blast radius; digest surfaces it.
- **Cost / LLM calls:** consolidation + voice + gardener all shell out to the `claude` CLI. Nightly
  + weekly cadence is modest, but watch the daemon logs the first fortnight.
- **These flags default OFF in `config.py` by design** — this plan is *config + local code*, it does
  **not** ship to other plugin users. Per `CLAUDE.md`, shipping is a separate, explicit 3-repo
  release (mcpbrain → mcpbrain-dist → mcpbrain-plugin) and is out of scope here.

---

## Sequencing & rollback

1. **Phase 0** (config flags) — observe a week, zero file churn, fully reversible by removing flags.
2. **Phase 1 + 2** (voice apply + memory graduation + core seed) — the highest-value file-filling.
3. **Phase 3** (gardener auto-apply) — the broadest autonomy step; do after 1–2 weeks of trust.
4. **Phase 4 + 5** (relax guard, digest, org-context) — finalize.

Every phase is independently revertible (config flag off, or `git revert` in the records repo).
Nothing here is shipped until a separate explicit release.
