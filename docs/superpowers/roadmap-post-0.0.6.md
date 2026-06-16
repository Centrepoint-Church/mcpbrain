# mcpbrain — Post-0.0.6 Roadmap / Backlog

> Captured 2026-06-15. These are the items deliberately **left out of the 0.0.6 "autonomous, subscription-only" milestone** (see `plans/2026-06-15-autonomous-cowork-scheduled-tasks.md`). Backup-wiring and the reference-gardener were folded INTO 0.0.6 and are NOT here. This file preserves the context/learnings so specs can be written later **once 0.0.6 is live**. Each item: what it is, why it matters, what we learned, a rough approach, and dependencies.

## How 0.0.6 leaves the product (the baseline these build on)
- **Architecture:** local daemon (launchd/schtasks) does sync (Gmail/Calendar/Drive) + embed (bge-small, local) + graph cadences + spool prepare/drain. It **never calls an LLM**. All recurring LLM work runs as **Cowork Desktop Scheduled Tasks** on the user's subscription (enrich hourly, gardener weekly, meeting-packs 2×/day, reference-gardener weekly), created by the install skill driving Claude (the only supported path — plugins can't register tasks; schedule lives in the desktop app DB; cloud "Routines" have no local FS; `/loop` is session-bound).
- **Self-development after 0.0.6:** graph (people/orgs/roles/relations/communities), state (decisions/continuity/memories), and — newly — reference/context world-model (bootstrap interview + weekly propose-not-overwrite reference-gardener).
- **Known accepted tradeoff:** scheduled tasks only run while the Claude desktop app is open + the machine awake (open-at-login is an instruction; keep-awake not configured).
- **Two repos required:** `Centrepoint-Church/mcpbrain-plugin` (Claude integration glue) + `Centrepoint-Church/mcpbrain-dist` (PEP 503 wheel index via Pages). Daemon source is the dev repo.

---

## 1. Proactive surfacing (push, don't pull)
- **What:** a daily "morning brief" delivered to the user (e.g. emailed to themselves, or injected at Cowork session-start) — actions due, who's waiting on them, what changed, today's meetings — instead of requiring them to open the dashboard.
- **Why:** non-technical users won't open a dashboard daily; the brain's value should *come to them*. The data already exists (dashboard `assemble`, meeting-packs, `brain_proactive` lint findings).
- **Learned:** `mcpbrain/dashboard.py` already computes the digest server-side; `session_hooks.session_start` already injects continuity + open actions into a session. The gap is a *push* channel and a curated brief.
- **Approach:** a scheduled "morning-brief" Cowork task that composes + delivers the brief (email-to-self via a draft, or a written file the session-start hook surfaces). Reuse `dashboard.assemble`.
- **Depends on:** 0.0.6 scheduled-task pattern.

## 2. In-context failure recovery
- **What:** when Google auth expires or sync errors, surface "Reconnect Google →" **inside Cowork** at the moment it breaks, with a one-action fix — not just a dashboard pill / `mcpbrain monitor` line the user never sees.
- **Why:** today failures are passive; a non-technical user won't notice sync silently stopped. The monitor + `probe_*` states exist but aren't pushed into the user's flow.
- **Learned:** `probes.py` already classifies states (`needs_action`/`not_started`/`ok`); `monitor.py` reports them; the gap is delivering them into a Cowork session and making the fix one step (re-run Connect Google).
- **Approach:** a SessionStart hook addition (or a monitor→notification bridge) that, when a probe is `needs_action`, prints a clear in-session prompt + the exact remedy.
- **Depends on:** none beyond 0.0.6.

## 3. Install robustness — `mcpbrain doctor` / repair
- **What:** a diagnose-and-repair path for when the bootstrap half-fails (uv missing, PATH not set, daemon not running, scheduled tasks not created, MCP not connected).
- **Why:** the install chain has many failure points and opaque errors; non-technical users can't debug. Several real bugs this cycle lived in these seams (home split-brain, dead dispatch).
- **Learned:** `probes.all_connections` + `monitor.run_monitor` already assess health; what's missing is a guided *fix* for each failure mode and a single entrypoint.
- **Approach:** `mcpbrain doctor` CLI + a `/mcpbrain-fix` skill that runs checks and repairs (re-register agent, re-create a missing scheduled task, re-run setup) — idempotent, like the install skill.
- **Depends on:** none.

## 4. Retrieval quality — rerank + surfaced relevance
- **What:** improve `brain_search` beyond the current hybrid (vector + BM25 fusion) with a rerank pass and/or surfaced relevance scores; measure quality honestly.
- **Why:** "recall by meaning" is the core value; it should be as strong as possible.
- **Learned:** `mcpbrain/retrieval.py:hybrid_search` already fuses `vec_chunks` (distance) + FTS `bm25`. It works but is unranked-beyond-fusion and exposes no score to the caller. This is *enhancement*, not a missing feature.
- **Approach:** add an optional rerank (cross-encoder or an LLM-judge rerank in a Cowork task for top-k), expose a score field, and build a tiny eval set to measure before/after.
- **Depends on:** none.

## 5. Automated end-to-end test
- **What:** a repeatable test of install → sync → spool → enrich → drain → surface, beyond the one-time manual clean-machine gate (C3 in the 0.0.6 plan).
- **Why:** every real bug this cycle (home split-brain, dead ClickUp dispatch, the enrichment-packaging gap) lived in the seams that unit tests passed through. 1300+ unit tests, near-zero integration coverage at the daemon↔Cowork↔plugin boundary.
- **Approach:** an integration harness that stubs Google + the Cowork extractor (write a known `enrich_inbox` file) and asserts the full loop produces graph + dashboard output; run in CI.
- **Depends on:** none.

## 6. The org "platform" layer (the largest gap vs. the word *platform*)
This is what turns "a tool each person installs" into "something Centrepoint operates for its staff."
- **6a. Admin / fleet view:** which staff installed, who's healthy, whose enrichment stalled. Today every install is an island; the maintainer has zero cross-user visibility.
- **6b. Per-user lifecycle:** onboarding/offboarding at scale, revoking access, central policy/config (e.g. push a cadence/config change to all users).
- **6c. Opt-in support telemetry:** so the maintainer can help a non-technical user whose daemon broke without remoting into their Mac.
- **6d. Subscription-quota awareness:** Cowork scheduled tasks spend each user's Claude quota; if they hit a limit, enrichment stalls **silently**. No detection/surfacing today.
- **Why:** the productization goal is an org-managed multi-user app; none of this exists. This is the highest-leverage *platform* work after the world-model.
- **Learned:** everything is per-user-local (config, store, OAuth token, records repo). There is no server-side component or admin surface. Building this likely needs a small opt-in central service or a shared-Drive-based status rollup.
- **Approach:** start with the lightest thing — a per-user health beacon written to a shared org Drive that an admin dashboard reads — before any central service.
- **Depends on:** the org OAuth/shared-Drive model (also used by backup escrow).

## 7. Integration depth
- **What:** Drive *document* parsing (currently metadata-level only), Google Calendar attendee context, and meeting capture (Zoom/in-person notes).
- **Why:** in-person/Zoom meetings and Google Docs are "dark" to the brain unless emailed — a large class of the user's world isn't captured.
- **Learned:** `sync/drive.py` + `sync/calendar.py` exist but operate at metadata level; no doc-content extraction, no attendee graph, no meeting-notes ingestion.
- **Approach:** extend sync extractors to pull Doc text (for owned/shared docs) and calendar attendees into the graph; consider a Zoom/recording ingestion path.
- **Depends on:** OAuth scopes (may need broader Drive read), enrichment capacity.

## 8. Windows parity
- **What:** full validation of the Task Scheduler install path, `mcpbrain setup`, and scheduled-task creation on Windows.
- **Why:** the build is Mac-centric; if any Centrepoint staff are on Windows, this is required, and it's its own validation effort.
- **Learned:** `agents.py` has a `win32` schtasks path; the desktop scheduled-task mechanism + wizard are unvalidated on Windows.
- **Approach:** a Windows clean-machine validation mirroring C3; fix gaps found.
- **Depends on:** 0.0.6 shipping on Mac first.

## 9. Onboarding friction — the "My Brain" project
- **What:** creating the Cowork "My Brain" project + pasting project instructions is still a manual step in the wizard.
- **Why:** it's the one bit of non-frictionless onboarding; a smoother/automated flow would help non-technical users.
- **Learned:** the wizard provides copy-paste name + instructions; there's no auto-create.
- **Approach:** investigate whether a skill can create/configure the Cowork project programmatically (same class of question as scheduled tasks — likely a guided/instructed step, not an API).
- **Depends on:** Cowork capabilities.

## Non-code prerequisite (tracked, not a spec)
- **Claude Team admin marketplace registration:** an org admin must add `mcpbrain-plugin` in Claude Team settings (claude.ai/settings) and set it available. Only an admin can do this; it's a hand-off step in the 0.0.6 publish (C4).

---

## Suggested sequencing after 0.0.6
1. **#6 platform layer** (admin/fleet visibility + quota awareness) — without it, supporting more than a couple of users is blind. Start with the health-beacon-to-Drive MVP.
2. **#2 in-context recovery + #1 proactive surfacing** — make the brain reach the user (high UX leverage, low build cost; reuse existing data).
3. **#3 doctor + #5 e2e test** — harden the seams that keep breaking.
4. **#4 retrieval rerank**, **#7 integration depth**, **#8 Windows**, **#9 onboarding** — quality/expansion.
