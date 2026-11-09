# Console redesign: Overview, Settings, Graph

Date: 2026-11-06
Status: approved by the owner 2026-11-06; layout, palette, settings and scope rule added 2026-11-09
Scope: `mcpbrain/wizard/{dashboard,index,graph}.html` and new shared CSS, `mcpbrain/dashboard.py`, `mcpbrain/control_api.py`, `mcpbrain/daemon.py`, `mcpbrain/config.py`, the automatic-change writers (`profile_synth`, `community_synth`, `consolidation`, ingest skips, gardener, voice), source sync, `doctor`, and tests. See Delivery order for the full backend scope.

## Why

The dashboard showed "153% digested into the graph". Investigating that turned up a wider problem. The local console (dashboard, setup/settings, graph) reports running totals that only go up, several of them wrong, and none of it tells you whether something needs attention. A week-long stall would look the same as a healthy day.

### What's wrong today (verified against the live store, 2026-11-06)

| Item | Shows | Problem | Correct value |
|---|---|---|---|
| Digest meter | 153% | Numerator counts every `enriched=1` chunk. Denominator excludes cold chunks. The 2026-10-26 ingestion clean-up marked 56,507 already-enriched chunks cold (mostly `source_digest` 42,492 and `source_session` 14,350), so they count on top and not below | 96% (96,595 / 100,173) |
| Queue state | not shown | `queue_state()` is computed but never rendered. Its input `indexed - cold - enriched` is negative, clamped to 0, so it can never say "starved" | 3,578 warm chunks waiting, 16 units queued |
| Daemon "Enrichment" | off | Reads `_enrich_client`, the retired in-process extractor. Enrichment now runs through scheduled Claude tasks, so it always reads off | Connections card says Running; last push 09:40 today, 54 pushes in 24h |
| Links / entity | 2.5, "graph density" | Relations ÷ entities. Each relation touches two entities, and "density" means something else | ~4.9 average links, and the number isn't useful anyway |
| Relations | 82,759 | Includes 1,445 invalidated (superseded) relations | 81,291 live |
| Entities | 33,185 "mapped" | 15,458 are `document` and `thread` bookkeeping nodes; 175 `unknown`; 10,385 have no live links | 6,786 people, 4,665 orgs, 2,645 projects, 2,857 topics, 591 meetings |
| Spool depth | under Sources | It's the enrichment work queue, not a source | Belongs with enrichment |
| Freshness | none | No last-synced or last-enriched time anywhere. Data exists: `sync_cursors.updated_at` per source, `enrich_payloads.at` | |
| Open findings | "2" | No way to see or resolve them (both are `agent_stderr` warnings for the context-health agent) | `/api/dashboard/findings/` exists |
| Re-chunking | done (13,335) | Finished one-off migration occupying a permanent row | Show only while running, blocked or halted |
| Sources | Gmail, Drive, Calendar | Meetings (`anarlog`) is synced but not listed | |
| Setup page once configured | hybrid | Unstyled `home-status` block with raw `google: ok` lines above the old setup cards; step 3 still numbered; raw unformatted counts; fleet folder IDs editable in the open | |
| Footer | "encrypted backup, if you enable it" | Contradicts the Backup card ("Automatic … no setting to choose") | |
| Pause | state shown, no control | `paused` var comments mention a toggle that doesn't exist; `/api/pause`, `/api/resume`, `/api/sync-now`, `/api/backup/run` all exist unused | |
| Graph types | 6 chips + legend | `thread` (4,701) and `unknown` (175) render grey with no legend or filter entry | |
| Suppress | "reversible" | No UI to see or restore suppressed entities | |
| Styling | 3 token sets | Each page defines its own colours and components; settings gear and graph back-link are the only navigation | |

## Intent

The console should answer three questions, in this order, within five seconds of opening it:

1. Is the brain healthy and current?
2. Does anything need me?
3. How complete is the brain, and is it growing?

Everything else is secondary detail or belongs in Settings.

## Goals

- Every number on screen is correct, bounded (no >100%, no negatives) and labelled in plain words.
- A single health verdict at the top, derived server-side, that turns amber or red when something is actually wrong.
- Freshness on everything that moves: "last synced 3 min ago", "last enriched 09:40".
- Things that need action are listed with the action next to them.
- One app with three pages (Overview, Graph, Settings), shared navigation and one stylesheet.
- First-run setup is a clean linear flow that hands off to the Overview once done.

## Non-goals

- No new framework, bundler or build step. Plain HTML/CSS/JS served by the daemon, as now.
- No change to the force-graph rendering engine or the entity drawer's edit/merge flows.
- No "Today" view (actions, calendar, inbox). `/api/dashboard/today` stays for the plugin and is untouched.
- No historical charts or time series beyond 24h counts.

## Anti-goals (failure even if it technically works)

- Any percentage that can exceed 100% or any count that can go negative.
- Anything showing green while sync or enrichment has stalled.
- Decorative motion that implies activity when nothing is moving.
- A number with no plain-language label of what it counts.

## Design

### 1. Shared shell

- Top bar on every page: wordmark, nav (Overview · Graph · Settings), health pill (same verdict as the Overview), as-of time.
- One stylesheet served at `/static/console.css` holding tokens and shared components (card, row, badge, button, stat). Pages keep only page-specific CSS.
- Routes: `/` redirects to `/dashboard` once configured, otherwise serves Setup. New `/settings`. Existing `/dashboard`, `/graph` keep their paths.
- Australian English, AWST times via the configured timezone (not hard-coded Perth in JS).

### 2. Overview (`/dashboard`)

Top to bottom:

**a. Health strip.** One line verdict plus the facts behind it, and controls.

> Healthy · email synced 2 min ago · enrichment pushed 09:40 · backup 18 min ago  [Sync now] [Pause]

Verdict states, computed by a new pure function `health(stats) -> {state, reasons[]}`:

| State | When |
|---|---|
| Paused | daemon paused |
| Stalled (red) | `stalled` set, watchdog limit reached, or any source not synced for > 6h |
| Needs you (amber) | any connection `needs_action`, open findings, or pending corrections |
| Behind (amber) | warm chunks waiting and no enrichment push for > 6h (the "starved" case) |
| Healthy | none of the above |

Thresholds are constants in one place, picked as my starting point (6h covers an overnight gap in the hourly enrichment task). The pulse dot animates only when Healthy.

**b. Needs you.** Shown only when non-empty. One row per item, with its action inline:

- Connection needing action → Reconnect / Fix in Settings
- Open findings → summary, severity, Resolve (existing findings endpoint). Duplicate findings with the same type and summary collapse to one row with a count
- Pending graph corrections → Apply / Decline (moved up from the bottom card)
- Enrichment behind → "3,578 chunks waiting, last push 14h ago" with a pointer to the scheduled task

**c. Pipeline.** Three stages left to right, each with one headline number and a plain sub-line:

| Stage | Headline | Sub-line |
|---|---|---|
| Captured | 258,764 chunks | Gmail, Drive, Calendar, Meetings |
| Worth enriching | 100,173 | 158,591 low-signal skipped · 57 duplicates |
| In the graph | 96% | 96,595 done · 3,578 waiting · 16 units queued · last push 09:40 · 54 in 24h |

The meter uses `enriched_eligible / eligible`, clamped to 0–100. The raw `enriched=1` total is no longer shown.

**d. Graph.** Counts of what a person would call knowledge:

People 6,786 · Organisations 4,665 · Projects 2,645 · Topics 2,857 · Meetings 591 · Circles 56
Second line: 81,291 live links · 34,634 facts · 15,458 documents and threads (linked as sources)
Hygiene line: "10,385 entities with no links" linking to the Graph pre-filtered to them.

Links/entity is dropped.

**e. Sources.** One row per source including Meetings: last synced (relative time), history status ("full history from Jun 2021" or "backfilling, reached Mar 2023"), and a warning dot if older than the stale threshold.

**f. System footer.** Version, backup restore point age, daemon uptime. Re-chunking appears here only while running, blocked or halted.

### 3. Settings (`/settings`)

*Superseded by section 9, which is the current Settings design. Kept for the reasoning.*

Replaces the configured state of `index.html`. Sections:

1. **Profile**: name, email, role, organisations with domains, timezone. Same fields and save semantics as today (post only filled fields).
2. **Connections**: Google (account, scopes, Reconnect), Claude Desktop (Reconnect and restart, with the existing warning), Backup (state, last restore point, Back up now via `/api/backup/run`), Records, Enrichment (last push, pushes in 24h; no on/off since it's driven by scheduled tasks).
3. **Data**: Suppressed entities list with Restore (needs a small new endpoint pair: list suppressed, unsuppress).
4. **Advanced** (collapsed): fleet folder and escrow folder IDs, read-only until "Edit" is pressed; maintenance info (re-chunk history, ingestion clean-up result).

Footer copy corrected to match automatic backup.

### 4. Setup (`/` when not configured)

Linear: 1 Connect Google → 2 About you → 3 Connect Claude Desktop → done screen with "Open the console". Backup runs automatically after step 1 as now, shown as a status line in step 1, not its own card. No status card; no hybrid `home-status` block.

### 5. Graph (`/graph`)

The existing graph stays: `vendor/d3.min.js` + `vendor/force-graph.min.js`, the force setup, layouts (emergent, radial), hover rings, right-click focus, search-and-jump, and the drawer's edit, merge and suppress flows. The prototype's static drawing is a stand-in for that canvas, nothing more. The redesign changes only what surrounds it, and its colours through the CSS variables `graph.html` already reads at load (`--graph-bg`, `--graph-link-*`, `--graph-person` and the other type tokens), remapped to the Console palette.

- Shared shell and nav.
- Type chips and legend cover every type present, driven from the server's type list, not a hard-coded six. `document` and `thread` are off by default (my call: they're bookkeeping and crowd out people and projects); one click turns them on.
- URL state: `?entity=<id>` opens that entity's drawer, `?unlinked=1` shows entities with no live links (needs the canvas endpoint to accept it). The Overview links use these.
- Header count reads "N shown of M entities".
- The graph background token is `--graph-ground` (renamed from `--graph-bg` for Hallmark rule 41, which pairs a `*-bg` token with an ink token on the same rule).

### 6. Data layer

`dashboard.stats()` returns a reshaped payload. All counts come from one read-only connection in one function so they're consistent with each other.

```
index:   total, eligible, cold, duplicate, enriched_eligible, waiting, pct
enrich:  queue_state, units_pending, last_push_at, pushes_24h
graph:   by_type{person,org,project,topic,meeting,document,thread,unknown},
         relations_live, observations_live, circles, unlinked
sources: [{name, label, last_synced_at, history:{done, reached}}]   (adds anarlog)
health:  {state, reasons[]}
needs_you: {connections[], findings[], corrections_count}
system:  version, backup_age, uptime, reflow (only when active)
```

Definitions (the single source of truth, documented in the function):

- `eligible` = chunks where `enrich_state` is NULL, `''` or `duplicate` (all 57 current duplicates are enriched, so including them doesn't move the number)
- `enriched_eligible` = eligible chunks with `enriched=1`
- `waiting` = `eligible - enriched_eligible`
- `relations_live` = `invalidated_at IS NULL`
- `unlinked` = entities with no live relation on either side

`daemon.status()` gains wall-clock `last_*_at` fields alongside the existing monotonic `progress` values, so freshness can be shown without converting monotonic clocks. `enrich_enabled` is removed from the dashboard payload (kept in status for anything else that reads it).

### 7. Every figure and change has an action (added 2026-11-06, the owner)

Anything the console shows, the owner can act on from where it's shown. Reversible actions use undo in place, never a confirm dialog.

| Shown | Action | Backend today |
|---|---|---|
| Open finding | Show the log, Dismiss (undoable), Mark fixed | `/api/dashboard/findings/<id>/dismiss` exists; resolve-as-fixed and log view are new |
| Pending graph correction | Apply, Decline (rows in Needs you; panel notes "none waiting" when empty) | `/api/corrections/<id>/apply\|decline` exist |
| Sync, backup, pause | Sync now (all, or per source), Back up now, Pause/Resume, Refresh | `/api/sync-now`, `/api/backup/run`, `/api/pause`, `/api/resume` exist; per-source sync is new |
| Pipeline stages | See sources, Review what's skipped (cold chunks, with re-include), See what's waiting | Cold-chunk list and re-include are new |
| "What changed" trail (from `change_log`) | Per entry: open the item, Undo, plus type actions (Rename a circle, Merge a person, Open a skipped Drive item) | **No recent change type writes `revert_ref`** (`profile_updated`, `community_titled`, `ingest_skip`, `consolidation_pass`, `capture_*` all empty). Undo needs each writer to record the prior value, and a generic `/api/changes/<id>/undo` |
| Graph counts | Each kind opens the Graph filtered to it; unlinked opens the review | Graph URL state (`?type=`, `?show=unlinked`, `?entity=`, `?view=circles`) is new |
| Entity detail | Edit, Merge, Hide, Add a fact; relation groups and people open in the graph | Edit/merge/hide exist; add-a-fact and relation filter are new |

### 8. Visual system

Overview layout: option A, reading-led (chosen by the owner, 2026-11-06). The 96% reading leads on the left on its own deepest-step panel; the one thing that needs the owner sits beside it in the only accent-bordered panel, with the sync times and Sync/Pause at its foot; "What changed" runs full width as Today and Yesterday columns; Knowledge graph and Sources (with System folded in) share the last row. Colour and type: variation 1, "Neutral" (chosen by the owner, 2026-11-06). Host Grotesk with Atkinson Hyperlegible Mono for measured values. No yellow, amber, orange or brown anywhere, in either mode (owner decision, 2026-11-06). Grounds are true neutral greys (chroma 0.001 to 0.007, hue 250). "Needs you" and the one primary button are raspberry (light: fill oklch 55% 0.19 355 with white text, text oklch 52% 0.18 355; dark: fill oklch 72% 0.16 355 with dark text). Measured values are teal (hue 205). Status green stays; status red moves to hue 30 so it never reads as raspberry. Graph kind colours avoid yellow, brown, olive and violet (person neutral, organisation blue-grey 235, project sea-green 165, topic pale cyan 200, meeting coral 20, documents and threads grey). Token values live in the prototype's `skins.css` (`--room-light-*`, `--room-dark-*`) and `console.css` (`--type-*`).


Built with the Hallmark skill (theme Console, graphite colourway), prototype kept privately outside this repo because it renders the owner's real data (pages: `variation-1.html` Overview, `settings.html`, `graph.html`, with `console.css` and `skins.css`), linted clean (before: 66 FAIL, 70 REFLEX; after: 0). Rules carried into the build: amber means a person needs to act (and the one primary button per page); cool blue means a value the system measured or wrote (times, versions); green and red are status only; graph kind colours stay off those three. Measured values in Atkinson Hyperlegible Mono, everything else Host Grotesk. Both fonts are vendored under `wizard/vendor/` (no Google Fonts call; the console promises nothing leaves the Mac). Tables, not tile walls; one large reading per page.

Light and dark modes: dark is Console's graphite colourway, light is its paper colourway, from one token set (`[data-mode="light"|"dark"]` plus a `prefers-color-scheme` block when no choice is saved). A sun/moon icon button in the top bar of every page switches between them; the choice is saved in `localStorage` (`mcpbrain-mode`) and applied by a few lines in `<head>` before first paint, so there is no flash. Until the owner picks one, the console follows macOS appearance. Amber text in light mode uses its own darker token (`--color-accent-text`) so chips and links pass 4.5:1. The graph page reads its colours once at load (`readPalette()`), so the toggle must call it again and refresh force-graph's colour accessors (`fg.backgroundColor(...)`, `fg.nodeColor(fg.nodeColor())`, and the same for links).

### 9. Settings, expanded (added 2026-11-09, the owner)

Section order, with a jump list under the page heading: Profile, Appearance, Sources, Automatic changes, Connections, Hidden entities, System, Advanced. One filled primary on the page (Save profile, because profile is free text); every switch and choice elsewhere saves as it changes and says so inline.

| Section | Controls | Backed by today | New backend |
|---|---|---|---|
| Profile: names you go by | Derived names shown fixed (`dana`, `dana okafor`, derived from the owner name); add or remove extra names | `owner_aliases` in config; `config.owner_aliases()` derives the rest | none (write via `/api/config`) |
| Appearance | Mode: match my Mac, light, dark. Main colour: six presets (raspberry default, crimson, magenta, cobalt, teal, slate) or any colour. The hue and strength drive every accent shade in both modes at fixed lightness, so contrast holds. A colour near teal moves measured values to blue; a colour near red moves the status red | none needed | none for a per-browser choice (saved in `localStorage`); store in config if it should follow the owner across browsers |
| Sources | Per source: on/off switch, Sync, history start, last synced. Note that off stops new items and keeps what's indexed. Keep-out lists: own automated mail subject starts, Drive folders, senders and Gmail labels | `anarlog.enabled`; `ingest.generated_gmail_subject_prefixes`; `ingest.generated_drive_folders` | on/off for Gmail, Drive, Calendar; per-source sync; sender and label exclusions |
| Automatic changes | Per kind: Apply (shown in What changed with Undo), Ask me first (goes to Needs you), Off. Rows: people profiles, circle names, reference and context files (cap 20 lines per change), voice file (every 3 days at most), memory consolidation, draft style tuning (Apply, Advise only, Off). Most changes per pass (default 50) | `gardener_auto_apply` (on), `gardener_max_changed_lines` (20), `voice_auto_apply` (on), `consolidation` (on/off), `bandit_auto_apply` (off), `review_max_apply_per_run` (50) | an apply switch for `profile_synth` and `community_synth`; "ask me first" routing into Needs you for every kind; undo records (`revert_ref`) per section 7 |
| System | mcpbrain status, version, watchdog restarts, Check for update, Restart; Start when I log in; search model status and re-download; catch-up enrichment start/cancel; Diagnostics (runs `doctor`, lists each line with its fix), Open logs, Copy a report | `/api/status`, `/api/model/status`, `/api/model/ensure`, `/api/enrich-backfill/start\|cancel`, `doctor.run_doctor`, LaunchAgent `com.mcpbrain.plist` | restart endpoint, login-item toggle, update check endpoint, doctor endpoint, log/report export |

Later phases, not built in the prototype: Schedule (the `cadences` block: sync frequency, daily passes, the hourly enrichment task's last run) and Data and privacy (forget a document, thread or person everywhere; storage used). The ~40 internal feature switches stay out of the main page; Advanced may list them read-only.

### 10. Plan-time decisions (2026-11-09)

Mapping the code for the plan changed five things (1 to 5); the final review recorded two more (6 and 7). These override earlier sections where they disagree.

1. **Undo storage.** Nothing reads `revert_ref` today, and `prune_change_log` keeps only the newest 500 rows each cycle. Undoable changes store `{"before": …, "after": …}` JSON in `revert_ref`; the prune keeps any row with a non-empty `revert_ref` for 30 days. Undo refuses with a plain reason when the item has changed again since (`after` no longer matches).
2. **Graph tidy-up is an automatic change.** The daily review (orphans hidden, missing organisations set, owners assigned, duplicates merged) applies AI verdicts with no change record and no switch. It gets its own row in Automatic changes (Apply, Ask me first, Off), and "Most changes applied in one pass" (`review_max_apply_per_run`, 50) sits under it, not as a general cap.
3. **Catch-up enrichment is removed, not built.** `/api/enrich-backfill/start|cancel` import a module deleted in an earlier commit, and the backlog is already queued; the limit is the hourly task's drain rate. The two endpoints and the Settings row are removed.
4. **Keep-out wording matches behaviour.** Subject starts and Drive folders keep today's behaviour: indexed and searchable, kept out of the graph. Sender and Gmail-label exclusions are new and skip indexing entirely, each skip recorded. Settings says which is which.
5. **Restart and start-at-login.** The daemon picks a new port and token on every start, so the open page cannot follow a restart; the Restart control says so and points at the menu bar. Start at login rewrites the daemon's launch agent and takes effect at next login; the menu bar app gains "Start mcpbrain" for when the daemon is not running. Settings writes go through a new allowlisted `/api/settings` that deep-merges nested blocks (today's `/api/config` replaces a nested block wholesale and accepts any key).

Deviations recorded at the final whole-branch review (2026-11-09). These override section 6 and the system footer in section 2f:

6. **Freshness comes from the store, not `daemon.status()`.** The planned wall-clock `last_*_at` fields on `daemon.status()` were not added. Source freshness reads each source's `sync_cursors` row and enrichment freshness reads `enrich_payloads` (last push, pushes in 24 h), so the figures survive a daemon restart and need no in-memory clock.
7. **No daemon uptime in the system footer.** Uptime was dropped from the footer and from `stats().system`; the footer shows the version, watchdog restarts and how enrichment runs, plus re-chunking while it runs. The backup restore point age is not in the footer either; backup state shows on the Encrypted backup connection row in Settings. An uptime figure resets on every restart and said nothing the health strip does not.

## Delivery order

**Scope rule (owner decision, 2026-11-09):** everything the prototype shows works end to end as part of this spec. Every "new backend" item in sections 6, 7 and 9 is in scope, not deferred. No control ships without the server behaviour behind it: a control's backend lands in the same phase as the control or an earlier one, and the "Needs backend" chips exist only in the prototype. The only items out of scope are the two named later phases in section 9 (Schedule, and Data and privacy), which the prototype does not show.

Each phase ships on its own and leaves the console working.

1. **Correct numbers.** New `stats()` payload, `health()`, freshness fields (`last_*_at`), tests. The current page reads the corrected fields, which fixes the 153%.
2. **Undo and review foundation.** Every automatic-change writer records the value it replaced in `change_log.revert_ref` (`profile_synth`, `community_synth`, `consolidation`, `ingest_skip`, `capture_*`, gardener, voice). Generic `POST /api/changes/<id>/undo`. An apply switch for `profile_synth`, `community_synth` and consolidation, each with three modes (apply, ask first, off), where ask-first queues the change as a pending review that appears in Needs you with Apply and Decline. Findings: mark fixed and a log view endpoint.
3. **Source controls.** On/off for Gmail, Drive and Calendar (Meetings already has one). Per-source sync. Sender and Gmail-label exclusions alongside the existing subject and folder lists. Cold-chunk review list with re-include, server-paged. "What it added" (entities and links from the last enrichment pushes). Priority re-queue for named chunks ("Queue them first" for the 11 items re-chunking could not carry).
4. **System controls.** Restart endpoint, start-at-login toggle (the `com.mcpbrain.plist` LaunchAgent), update check, a `doctor` endpoint returning each check with its repair, log export and a support report.
5. **Overview.** Layout A, shared shell and stylesheet, the Neutral palette with the accent picker tokens, light and dark, every action from section 7 wired.
6. **Settings and Setup.** All of section 9 wired to phases 2 to 4. Appearance (mode, main colour), the setup flow and the `/` redirect.
7. **Graph.** Shell and palette (the existing force-graph engine kept), kind list from the server, URL state (`?type=`, `?entity=`, `?show=unlinked`, `?view=circles`, `?since=`), unlinked filter, hidden-entity list and restore, add a fact, relation filter, and the colour re-read on mode change.

## Testing

- `tests/test_dashboard_stats.py`: fixture store with cold-and-enriched chunks, duplicates, `''` and NULL states, invalidated relations, unlinked entities. Asserts every number, and that `pct` stays within 0–100 and `waiting` ≥ 0.
- `health()` table test covering each state and precedence.
- Control API route tests for `/settings` and the `/` redirect.
- Manual check of each page against the live store before release (the `run` skill).

## Left to the builder

- Exact visual styling within the existing look (keep the current palette and type).
- Copy wording beyond what's specified here.
- Whether `enrich_state=''` gets normalised to NULL (a separate one-line migration; counting treats both the same either way).

## Calls made in drafting (accepted with the spec, 2026-11-06)

- Stale threshold 6h for sources and enrichment.
- Documents and threads hidden by default on the Graph.
- Links/entity removed rather than corrected.
- Duplicates counted as eligible.
