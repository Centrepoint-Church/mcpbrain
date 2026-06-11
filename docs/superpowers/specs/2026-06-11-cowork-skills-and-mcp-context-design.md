# Cowork skills + MCP-resource context, and the working-project rewrite

**Date:** 2026-06-11
**Status:** design — approved in conversation, building now
**Supersedes parts of:** `2026-06-10-settings-profile-and-status-design.md` (the "two Cowork projects" step, the records-`CLAUDE.md` classification sections, and the scheduled-dir enrichment-skill write). Builds on `~/joshbrain/docs/superpowers/specs/2026-06-09-cowork-memory-architecture-design.md`.

## Problem

The shipped onboarding gets the **separation of concerns wrong** and makes setup more manual than it needs to be:

1. **Two project shells.** The wizard asks the user to create two Cowork projects. A scheduled task only needs a working folder, not a project, and the enrichment skill is self-contained — a second project wrapper adds a manual step and would make the narrow enrichment task inherit project-instruction noise.
2. **Classification leaked into the workspace.** The working-project `CLAUDE.md`/instructions carry org-tagging + role-attribution rules. Classifying people/orgs/relationships is **enrichment's** job (systematic, background). The project is the user's desk; it should record decisions/continuity/memory, not re-classify the corpus.
3. **Context bound to filesystem paths.** The working project was specified to read context from `~/.mcpbrain/records` files. The mcpbrain MCP server already serves `~/.mcpbrain/context/*.md` as resources but **not** the records repo (there is a `mcp_server.py:34` NOTE flagging this exact misalignment), so context isn't path-independent.
4. **Enrichment setup is fragile + manual.** The enrichment `SKILL.md` is written into a guessed Cowork scheduled-tasks dir, and the user must create the schedule by hand (the daemon can't set the cadence).

## What changes (and what is deliberately preserved)

**Preserved (cross-checked against both repos):**
- **Enrichment still runs in Cowork as an hourly scheduled task** — the pipeline that drains `~/.mcpbrain/enrich_queue/pending.json` → `enrich_inbox/` is unchanged (cowork-memory-architecture spec, "enrichment project … left exactly as it runs today"). Only its *packaging* and *setup* change.
- **Memory determinism layers** (hooks > MCP tools > instructions) and the **write-routing** (`brain_decision`→`state/decisions.md`, `brain_note`→`state/hot.md`, `brain_memory_write`→`memory/`+`MEMORY.md`, daemon-owned, queued) — unchanged.
- **SessionStart/SessionEnd hooks** (`hooks.py`, `session_hooks.py`) and the **gardener** (weekly, launchd, memory hygiene, protected sections) — untouched.
- **Records repo location** (`records_dir = <home>/records`, per-user local git, no remote).
- **Classification rules live in `cowork/enrichment.md`** (already there: `valid_orgs` = configured orgs + `external` + `unknown`, per-entity own-domain rule, role-attribution). This stays the single home for classification.

**Changed:**
1. **MCP resources surface the records repo** (path-independent context). 
2. **Enrichment + setup ship as personal skills** in `~/.claude/skills/`, replacing the scheduled-dir write.
3. **One working project ("My Brain")**, created from scratch, work-focused instructions.
4. **Records `CLAUDE.md` slimmed** — classification sections removed, memory/self-evolution kept.

## Design

### 1. MCP server: surface records context as resources (`mcp_server.py`)

`list_context_resources()` / `read_context_resource()` currently serve `app_dir/context/*.md`. Extend them to ALSO serve, from `records_dir` (`config.records_dir(app_dir())`):
- `CLAUDE.md`
- `context/*.md` (identity, voice, preferences)
- `reference/*.md` (systems, projects)
- `state/decisions.md`
- `MEMORY.md`

Implementation:
- A `_resource_files()` helper returns a list of `(uri, path, description)` for both roots. Resource URIs use a stable scheme, e.g. `mcpbrain://records/CLAUDE.md`, `mcpbrain://records/context/identity.md`, and keep the existing `mcpbrain://context/memory.md` (app_dir) entries.
- `read_context_resource(uri)` resolves the uri back to its path with the SAME containment guard already present (resolved path must sit under one of the two allowed roots; reject anything else — defence in depth against traversal).
- Missing files are simply absent from the list (degrade, never raise). This also closes the `mcp_server.py:34` NOTE.

### 2. Personal skills in `~/.claude/skills/` (`mcpbrain/skills.py`, new)

Replace `cowork_tasks.write_enrichment_skill` (scheduled-dir write) with two personal skills written under `~/.claude/skills/` (honouring `CLAUDE_CONFIG_DIR`):

- **`mcpbrain-enrichment/SKILL.md`** — front-matter (`name`, `description`) + the body from package data `cowork/enrichment.md` (the canonical extraction prompt, unchanged; it already owns classification). As a personal skill it is invokable anywhere and a scheduled task can run it.
- **`mcpbrain-setup/SKILL.md`** — a short skill the user runs once in Cowork. Its body instructs Claude to:
  1. Resolve the mcpbrain home (`~/.mcpbrain` if present, else the OS default app dir; it can `cat <home>/config.json`).
  2. Confirm the `mcpbrain-enrichment` skill is installed.
  3. **Create an hourly scheduled task** named `mcpbrain-enrichment` whose prompt is "run the mcpbrain-enrichment skill on the pending batch", working folder = the mcpbrain home. (Claude can create scheduled tasks when asked — this sets the cadence the daemon can't.)
  4. Report what it created.

New module `mcpbrain/skills.py`:
- `skills_dir() -> Path` = `<CLAUDE_CONFIG_DIR or ~/.claude>/skills`.
- `write_personal_skills() -> list[Path]` — writes both SKILL.md files (front-matter + body), `mkdir -p`, atomic write, idempotent. Degrades to `[]` on OSError (never raises).
- `enrichment_skill_present() -> bool` and `setup_skill_present() -> bool`.
- `cowork_tasks.py` is retired/trimmed: remove `scheduled_dir`/`write_enrichment_skill`/`enrichment_skill_present` (or keep the module only if something else imports it — grep first). The Fix-2 scheduled-dir resolution is no longer needed.

### 3. Probes (`probes.py`)

- `probe_enrichment(home)` now: `not_started` if `skills.enrichment_skill_present()` is False; `ok` ("Running") if the skill is present AND an `enrich_inbox/*.json` is < 2 days old; else `needs_action` ("Run /mcpbrain-setup in Cowork to start the hourly schedule"). (Same 2-day proxy as before, message updated.)
- `probe_records`, `probe_memory_hooks`, `probe_claude` unchanged.

### 4. Daemon (`daemon.py`)

`apply_config` materialise block: call `skills.write_personal_skills()` and `records.scaffold_records(home)` (best-effort, warn-on-fail). Drop the `cowork_tasks.write_enrichment_skill` call.

Also write the skills on daemon start (so a fresh install has them before the user opens Cowork), via the same best-effort path the daemon already uses for records ensure.

### 5. Control API + wizard

- **`config_profile()`** gains a `project_instructions` string (server-rendered with the owner's name + orgs) so the wizard's Copy button is exact and path-independent. Also already exposes `home_dir`/`records_dir` (kept).
- **Wizard step rewrite** (`wizard/index.html`): replace the "two Cowork projects" step and the old standalone enrichment step with ONE consolidated step:
  - **A. Create "My Brain"** — Projects → + → **New from scratch** (default `~/Documents/Claude/Projects`). Copy buttons: **project name** (`My Brain`) and **project instructions** (the work-focused block below).
  - **B. Start enrichment** — in any Cowork chat, run `/mcpbrain-setup` (Copy button for the phrase). The skill creates the hourly schedule.
  - Remove the old `#step-enrich` giant inline spec and the duplicate enrichment expander. Renumber steps.
- **Project instructions block** (rendered with `{name}` / `{orgs}`), work-focused, capture loop, no classification section:

```
You're {name}'s assistant, working from here on. Memory + tools come from the
mcpbrain MCP server:
- brain_search / brain_context / brain_actions — recall by meaning, profile a
  person/org, see what's open
- brain_draft_reply / brain_draft_refine — draft email in my voice

Read my identity, voice, preferences, reference and decisions from the mcpbrain
@-resources; apply my voice to everything. Run brain_search before answering
from memory.

Keep my brain current as we work — this is the point, it should get better:
- A decision that changes how things are done → brain_decision
- A "just decided / where we're up to" note → brain_note
- A durable learning, preference, or fact worth keeping → brain_memory_write
- When a system or project materially changes, propose an edit to the matching
  reference file and I'll approve it.
Captures are queued (the daemon writes them to my records repo within ~a minute;
don't hand-edit those files). If something is clearly tied to one of my orgs
({orgs}) pass that org on a write; otherwise leave it — classifying people, orgs
and relationships is automatic background enrichment, you don't tag anything.
```

### 6. Records `CLAUDE.md` template slim (`records_templates/CLAUDE.md`)

- **Remove** the `## Org tagging rules` and `## Role attribution rules` sections (lines 9–26). The GARDENER-PROTECTED block stays but now wraps only the identity intro / core working rules (keep the markers so the gardener still honours them; if nothing else is protected, keep a one-line "identity is set in context/identity.md; don't rewrite it" inside).
- **Keep** Memory Protocol, Where Things Go (write-routing), Output convention, Quality Standard, Planning, Proactive Behaviours, Session Capture, Self-Evolution, Platform Notes.
- Add the same one-line org clause to "Where Things Go" (pass org on a write if obviously one of mine; else enrichment places it).

## Data flow

```
daemon start / apply_config ─► skills.write_personal_skills()  → ~/.claude/skills/{mcpbrain-enrichment,mcpbrain-setup}/SKILL.md
                            └► records.scaffold_records()       → records repo (CLAUDE.md slimmed, context/reference)

My Brain project (created from scratch) ─► reads identity/voice/preferences/reference/decisions via mcpbrain MCP @-resources
                                        └► records via brain_decision/note/memory (daemon-owned, queued)

/mcpbrain-setup (run once in Cowork) ─► Claude creates hourly scheduled task → runs mcpbrain-enrichment skill → drains pending.json
```

## Testing

- `tests/test_skills.py` — `skills_dir` honours CLAUDE_CONFIG_DIR; `write_personal_skills` writes both SKILL.md with correct front-matter + body (enrichment body matches `cowork/enrichment.md`); idempotent; degrades to `[]`; `enrichment_skill_present`/`setup_skill_present`.
- `tests/test_mcp_resources.py` — records files are listed as resources; `read_context_resource` returns their content; a uri outside the allowed roots is rejected; missing records repo degrades to just the app_dir resources.
- `tests/test_probes.py` — `probe_enrichment` three states keyed on `skills.enrichment_skill_present()` (update the monkeypatch target).
- `tests/test_records.py` — slimmed `CLAUDE.md` no longer contains "Role attribution"/"Every person"; still contains "Where Things Go" + "Self-Evolution".
- `tests/test_wizard_serve.py` — served HTML has the single "My Brain" step, the `/mcpbrain-setup` phrase, no `#step-enrich` giant spec; `project_instructions` present in `/api/config`.
- `tests/test_daemon_profile.py` — `config_profile` includes `project_instructions` rendered with orgs; no secret.
- Update/retire `tests/test_cowork_tasks.py` to match the module's removal/trim.

## Out of scope

- Creating the Cowork **project shell** programmatically (no API; the user creates it from scratch — one paste of name + instructions).
- Changing the enrichment extraction prompt (classification rules stay in `cowork/enrichment.md`).
- The gardener, SessionStart/SessionEnd hooks, ClickUp sync, backups — untouched.
- Screenshots (still text-only until captured).
