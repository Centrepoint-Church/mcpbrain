@context/identity.md
@context/voice.md
@context/preferences.md

---

<!-- GARDENER-PROTECTED-START: identity — gardener cannot modify this block -->

## Identity

Your identity, voice and preferences are set in `context/` (imported above) — read
them, apply the voice to everything, and don't rewrite them. Classifying people,
orgs and relationships across the mail is handled automatically by background
enrichment; this is the workspace, not the classifier.

<!-- GARDENER-PROTECTED-END -->

---

## Memory Protocol

Read tools (mcpbrain MCP): `brain_search` (hybrid search → summaries), `brain_read` (full text of one chunk by doc_id), `brain_context` (profile an entity / list communities), `brain_actions` (open tasks/deadlines), `brain_graph` (entity relations), `brain_proactive` (surfaced findings), `brain_draft_reply` / `brain_draft_refine` (email drafts). Prefer these over loading whole files.

**Load on demand:**
1. Active continuity → `state/hot.md` (auto-pruned to 14 days).
2. Historical context, prior emails, document content → `brain_search` first (summaries); `brain_read` only the 2-3 most relevant doc_ids. Never load full sets.
3. Task involves a project → `reference/projects.md`.
4. Task involves tools, automation, or the mcpbrain stack → `reference/systems.md`.
5. Related prior decision → `state/decisions.md`.

**Extended thinking:** use for risk assessments, strategic recommendations, financial analysis, compliance reviews, multi-stakeholder planning.

---

## Where Things Go

Writes are **routed through MCP tools, not hand-edits.** Each write tool is **QUEUED**: the daemon owns the file write + commit and applies it on ~one daemon cycle (not instant). Do **not** call a write tool *and* also edit the underlying file — pick the tool for the routes below.

| Type | Route through | What the daemon does |
|---|---|---|
| Decision that supersedes earlier behaviour | `brain_decision(text, rationale, owner, supersedes, org)` | Appends a dated row to `state/decisions.md` + commits |
| Continuity / "just decided" note, active work | `brain_note(text)` | Prepends a dated entry to `state/hot.md` + commits |
| Durable memory (project/system/preference) | `brain_memory_write(slug, description, body, memory_type)` | Writes `memory/<slug>.md` + a `MEMORY.md` pointer + commits |
| Observational / entity fact, meeting outcome | `brain_ingest(title, content, tags, observation_type, org)` | Into the graph + memory index; searchable after the next sync (~5 min) |
| Rule that should always apply | Edit `CLAUDE.md` directly (or `reference/*` if conditional) | Hand-edit — this file is not daemon-owned |
| Project/system reference change | Edit `reference/projects.md` or `reference/systems.md` | Hand-edit |

**hot.md discipline:** entries are 2-4 lines max with a `**YYYY-MM-DD:**` prefix; anything older than 14 days is auto-pruned.

**Org tags on writes:** if something is clearly tied to one of your orgs, pass that `org` on the write; otherwise leave it blank — enrichment places people, orgs and relationships automatically. You don't need to tag anything to work.

---

## Output File Convention

- Cross-cutting deliverables → `outputs/`
- Project-specific deliverables → `projects/<project-name>/outputs/`

## Quality Standard

`voice.md` and `preferences.md` are loaded at session start. Apply them to all output. Before presenting any draft, run the voice self-check and fix issues before presenting, not after.

---

## Planning Before Action

- Any request touching more than two files, or involving a new system/script: propose a numbered plan and wait for confirmation before writing anything.
- Don't add features that weren't explicitly requested. Build the best long-term solution within scope.

---

## Proactive Behaviours

- Run `brain_search` before answering historical questions ("what do we know about X", "have we dealt with X before").
- Use `brain_actions` for task/deadline questions.
- `brain_ingest` at natural capture points: after extended discussion with no explicit capture, or after producing a significant deliverable.
- Search discipline: for factual lookups allow up to 5 `brain_search` queries; if nothing relevant surfaces, say "not in the index" rather than answer from training data.

---

## Session Capture

At natural capture points, call the matching write tool (capture is QUEUED — applied on ~one daemon cycle, searchable after the next sync ~5 min):
- Decisions that persist across sessions → `brain_decision`
- Key facts about people, projects, or systems → `brain_ingest`
- A continuity note for the next session → `brain_note`

---

## Self-Evolution Protocol

Capture things when they occur — don't defer to end of session. Use the "Where Things Go" routes (a write tool for daemon-owned files, a direct edit for `CLAUDE.md` / `reference/*`). Propose; the owner approves.

---

## Platform Notes

Backed by the mcpbrain MCP server and this working tree. The `brain_*` write tools are QUEUED on every surface — the daemon owns the file write + commit.

- **Cowork** — the primary interactive surface. Use the `brain_*` tools for reads and writes; let the daemon apply file changes.
- **Claude Desktop** — reads context via the mcpbrain MCP server and uses the `brain_*` tools.
- **Claude Code** — for editing the working tree directly (`CLAUDE.md`, `reference/*`, code).

**Where the files live:** this records repo is the working tree (decisions, hot, memory, context, reference). `~/.mcpbrain` is the runtime (index, daemon, connected runtime state).
