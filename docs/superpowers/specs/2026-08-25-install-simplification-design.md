# Install simplification — the install command should do the install

**Status:** design. Scope approved 2026-08-25 (all three stages, with the three
corrections in "What the first pass got wrong" folded in).

**Thesis.** The install *mechanism* is sound — uv tool install, a login agent, a
stdio MCP connector, a browser wizard. What is overcomplicated is everything
around it: a human is asked to hand-build four scheduled tasks that a Claude
session is documented to be able to create itself; the connector is written by
three code paths whose guidance contradicts each other; and roughly half of
`install.ps1` computes a plan nothing acts on. None of that is load-bearing.

---

## What the install costs today

A new user on macOS performs ~20 discrete actions across ~10 stages:

| # | Stage | Actions |
|---|---|---|
| 1 | Install the plugin (marketplace, or pre-installed) | 1–2 |
| 2 | Run `/mcpbrain:install` | 1 |
| 3 | Approve 3 shell commands | 3 |
| 4 | Wizard — Connect Google (OAuth) | 2 |
| 5 | Wizard — profile + timezone | 1 |
| 6 | Wizard — Download model button | 1 |
| 7 | Wizard — Connect & restart Claude Desktop | 1 |
| 8 | **Hand-build 4 Local routines** (name, description, instructions, model, permission mode, folder, schedule — ×4) + Run-now ×4 | **~10** |
| 9 | Settings → Desktop App → Run on startup | 1 |
| 10 | Optional `mcpbrain-bootstrap` interview (5 sections) | 5 |

Stage 8 alone is half the install, is the most error-prone step (a task created
as **Cloud** instead of **Local** silently does nothing forever), and is the one
step that cannot currently be scripted.

### Verified state on the author box, 2026-08-25

`mcpbrain` is registered **three times**, all with the identical absolute command:

```
~/.claude.json  mcpServers.mcpbrain                            (user scope)
~/.claude.json  projects[/Users/joshkemp/GitHub/mcpbrain]      (local scope)
~/Library/Application Support/Claude/claude_desktop_config.json
```

All three carry `command: /Users/joshkemp/.local/share/uv/tools/mcpbrain/bin/mcpbrain`,
`args: ["mcp-server"]`. Only the third is written by `mcpbrain setup`; the other
two accumulated by other means. Nothing reconciles them and nothing reports the
drift.

`claude_desktop_config.json` on this machine also holds `coworkUserFilesPath`,
`ccdScheduledTasksEnabled`, `sidebarMode` and 20 other Cowork/Code preferences,
so it is not the exclusive property of a separate chat app. The load-bearing
error in `setup.py:56-60` is not *which* file it names but the exclusion it
draws — "*not* Claude Code's `~/.claude.json`" — which is why setup writes one
file and leaves the other surface to chance. Stage 2 writes both rather than
adjudicating between them.

---

## What research settled

| Question | Answer | Source |
|---|---|---|
| Can a session create Local scheduled tasks? | **Yes.** "You can also create a task by describing what you want in any session"; "You can also list, create, edit, and pause tasks by asking Claude in any Desktop session." | [desktop-scheduled-tasks](https://code.claude.com/docs/en/desktop-scheduled-tasks) |
| Can we ship task definitions as files instead? | **No.** `~/.claude/scheduled-tasks/<name>/SKILL.md` carries `name`/`description`/`model` only. Schedule, folder and enabled state live in Desktop's private store — verified absent from `~/.claude/*.json` on this machine. | same + local inspection |
| Does Claude Desktop clobber `claude_desktop_config.json`? | **Yes, known and widespread** — including wiping it to a preferences-only stub. Editing while it runs in the background loses the edit. | [#32345](https://github.com/anthropics/claude-code/issues/32345), [#291](https://github.com/robotmcp/ros-mcp-server/issues/291) |
| Is the Windows connector path correct? | **No.** MSIX installs virtualise `%APPDATA%\Claude\` to `%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\`. `setup.py:63-65` only writes the former, so the write lands where nothing reads it — silently. | [#26073](https://github.com/anthropics/claude-code/issues/26073), [#29100](https://github.com/anthropics/claude-code/issues/29100), [#38830](https://github.com/anthropics/claude-code/issues/38830) |
| Does uv fail on an unknown extra? | **No — it warns and proceeds.** Measured 2026-08-25: `warning: The package … does not have an extra named 'nosuchextra'`, then a successful resolve. | direct measurement |

---

## What the first pass got wrong

Three items were proposed for removal that are load-bearing. Recorded because the
reasoning matters more than the conclusion.

1. **`setup`'s `_register_desktop_mcp()` call (`setup.py:241,263`) is NOT removable.**
   It is the only connector write that happens if the user never reaches the
   wizard's last step. It stays; only its output changes.

2. **Wizard steps 5–7 are NOT removable.** Step 6 (`index.html:165-183`) holds the
   live Fleet folder / escrow-folder controls and `saveFleet()`; step 7
   (`index.html:185-193`) is the live status panel. Only the *numbering* is
   cosmetic. Likewise step 3's Download-model button doubles as the
   re-download/repair control — auto-fire it, do not delete it.

3. **OCR must MOVE, not be removed.** `ocr.install_tesseract` has exactly two
   callers: `setup.py:144` and `doctor.py:128` (the manual `--repair` path).
   Dropping the setup call without a replacement automatic caller reinstates the
   exact regression its own docstring records — "nothing installed it and so every
   install had OCR silently off".

A fourth claim was overstated: removing the `[daemon]` extra was said to break the
fleet's daily auto-update, because every deployed `update.py` has
`"mcpbrain[daemon]"` baked into its command line. Measured, uv only warns. The
plan is unchanged (an empty declared extra costs one line and keeps it silent),
but the risk was not what was claimed.

---

## Stage 1 — the install command creates the scheduled tasks

**Change.** `plugin/commands/install.md` step 4 stops being a table for a human to
retype and becomes an instruction to the assistant running the command: create
these four Local scheduled tasks, then verify and Run-now each.

The four tasks are unchanged in content — same names, prompts, models, cadences.
What changes is who types them.

| Task | Schedule | Model | Permission mode | Instructions |
|---|---|---|---|---|
| `brain-enrich-hourly` | Hourly | Sonnet 4.6 | Auto | Call the `brain_routine` tool with name `enrich` and follow the instructions it returns exactly. |
| `brain-meeting-packs-hourly` | Hourly | Sonnet 4.6 | Auto | …name `meeting-packs`… |
| `brain-gardener-weekly` | Weekly | Sonnet 4.6 | Auto | …name `gardener`… |
| `brain-reference-gardener-weekly` | Weekly | Sonnet 4.6 | Auto | …name `reference-gardener`… |

**The Local-not-Cloud constraint becomes a constraint on the assistant**, stated
in the command: these must be Local scheduled tasks created through the Routines
surface, never `/schedule` (a cloud routine runs from a fresh clone on Anthropic's
servers and cannot reach the local daemon — enrichment would silently do nothing).

**Verification is part of the step**, not an afterthought: after creating them,
list the tasks back and confirm four exist and are Active; then Run-now each once
so the permission prompts are answered while the user is present, per the
scheduled-tasks docs' own guidance on avoiding stalls.

**Fallback stays.** The existing table is retained in `plugin/INSTALL.md` as the
manual procedure, for the case where the assistant cannot create tasks (older
Desktop build, org policy disabling Routines). The install command says so rather
than failing.

### `[daemon]` extra

`fastembed` moves into `[project.dependencies]`. `daemon = []` remains declared,
permanently, as a no-op alias so the `"mcpbrain[daemon]"` command lines already
baked into deployed `update.py` installs stay warning-free. Every install
instruction drops the extra and the shell quoting it required.

Rationale for the merge: the extra is one package; `mcp-server` itself dies
without the embedder weights (`doctor.py:277-281`); and the carve-out's stated
justification in `DISTRIBUTION.md:130` is the `.mcpb` bridge removed 2026-08-24.
Meanwhile `pystray`, `pillow`, `pyobjc`, `igraph` and `leidenalg` all ship in base
deps on the reasoning that "the daemon and its tray are one system". The split is
inconsistent and, per Josh's own local-reinstall experience, an active footgun.

### Documentation single-sourcing

`plugin/commands/install.md` becomes the single source of install instructions.

- `README.md:11-17` is **wrong today** — it says clone a repo named
  `mcp-ops-brain` and run `./install/setup.sh`. Verified: `install/` does not
  exist in this repo, and `DISTRIBUTION.md:97` states those scripts were removed.
  Replaced with a link.
- `DISTRIBUTION.md:104` omits `[daemon]`; after the merge above the line is
  simply correct, and gains a link rather than a duplicated command.
- `RELEASE-RUNBOOK.md` §6's note that "`INSTALL.md` is currently macOS-worded" is
  stale — it has had a Windows section since 0.7.97. Corrected.

---

## Stage 2 — one connector path, correct on both surfaces

Scope decision: **both surfaces matter** (some staff use the standalone Claude
chat app as well as Claude Code Desktop), so the `claude_desktop_config.json`
write stays. This stage makes it correct and adds the second surface, rather than
replacing it.

### 2a. One function, two destinations

A single `register_connector()` merge-writes the identical stdio entry to:

1. **`claude_desktop_config.json`** — the chat app. On Windows, resolve
   MSIX-first: if
   `%LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude_desktop_config.json`
   exists, that is the file the app reads; otherwise fall back to
   `%APPDATA%\Claude\`. Write **both** when both exist — an MSIX machine that
   also has a non-MSIX install is cheap to serve and impossible to distinguish
   reliably.
2. **`~/.claude.json`** (honouring `CLAUDE_CONFIG_DIR` when set) under top-level
   `mcpServers` — user scope, which per the [MCP docs](https://code.claude.com/docs/en/mcp)
   loads in every project. This file is not Desktop-owned and does not exhibit the
   clobber-on-quit behaviour.

Both writes are merge-preserving (other servers and, critically, the
`preferences` block survive) and **atomic** (tempfile + `os.replace`, mirroring
`config.write_config`). A file that fails to parse is left untouched and reported,
never overwritten — `~/.claude.json` holds the user's entire project history and
a truncating write there is the worst outcome in this design.

Not using `claude mcp add --scope user`: it requires the `claude` CLI on PATH,
which is not guaranteed on a Desktop-only install, and adds a subprocess failure
mode to replace a JSON merge we already perform correctly elsewhere.

### 2b. Ordering fix

`control_api.py:332-334` currently does write → quit → launch. Claude Desktop
rewrites its config on quit, so the write is inside the window where it can be
lost. Reorder to **quit → wait for exit → write → launch**, which is the ordering
`setup.py:100-103`'s own warning text already identifies as the reliable one.

`desktop.relaunch_claude_desktop()` gains an exit-wait (poll for the process to
disappear, bounded — ~10s, then proceed regardless) and is split so the caller
can interleave the write. Best-effort semantics are unchanged: it still never
raises.

### 2c. Binary path

`_mcpbrain_bin()` calls `Path.resolve()`, which follows the uv shim into the tool
venv (`~/.local/share/uv/tools/mcpbrain/bin/mcpbrain`). The stable public entry
point is the shim itself, `~/.local/bin/mcpbrain`. Prefer the shim; keep
`resolve()` only as the fallback when `shutil.which` returns nothing.

### 2d. Messaging

`setup.py:98-110`'s five-line IMPORTANT block is deleted. It contradicts both
`install.md` step 3 and wizard step 4, and after 2b the flow it recommends is no
longer the better one. `setup` keeps writing the connector (see correction 1) but
says one line about it and defers to the wizard's final step.

`mcpbrain connect` remains the documented manual fallback and gains the same
quit→write→launch behaviour, so the two paths cannot diverge again.

### 2e. Doctor

A connector check: for each config file this machine actually has, is the
`mcpbrain` entry present, and does its `command` path exist on disk? Repairable
via `--repair`, reusing `register_connector()`. This is what would have caught
the MSIX bug before a hardware gate did.

---

## Stage 3 — installer symmetry, dead code, wizard framing

### 3a. `install.ps1`

Delete `Get-InstallPlan`'s `persistence-*` entries and `Test-Scheduler`. Verified
inert: `Invoke-InstallPlan`'s switch reaches `default { }` for both persistence
actions, and `Test-Scheduler` is duplicated by `agents._scheduler_available()` —
which is the copy whose result is actually used, at `mcpbrain setup` time. Note
that `Test-Scheduler` is not a passive probe: it creates and deletes a real
scheduled task, so today that side effect runs twice per install.

`install.tests.ps1:10,25,26` assert on those plan entries; they are tests of
inert output and go with it.

Result: ensure uv → ensure x64 VC++ redist → `uv tool install` (with its ARM64
uv-link fallback, which is real and stays) → `mcpbrain setup`. ~35 lines.

### 3b. Publish `install.ps1` from the release script

`plugin/scripts/install.ps1` is the source of truth but is *served* from
`mcpbrain-dist`, hand-copied per `RELEASE-RUNBOOK.md` §1b.1. Nothing guards the
copy, so the published installer can silently go stale. `bin/release.py --dist`
copies it alongside the wheel index, and a test asserts the dist copy matches the
source when the dist repo is present.

### 3c. Wizard

Renumber to three real steps — **1 Connect Google, 2 About you, 3 Connect Claude
Desktop** — and demote steps 5–7 to unnumbered panels ("You're set up", "Backup &
recovery", "Status"). All controls in them are retained unchanged (correction 2).

The model download auto-starts when Google connects, showing progress in the
existing badge; the button remains as the manual re-download/repair control.

Fleet folder and escrow IDs come from `/api/config` instead of being hardcoded at
`index.html:175,178`, where they duplicate `org_defaults.py:13,16`.

### 3d. OCR placement

`setup.py:144` runs a `brew install tesseract` — minutes, possibly prompting —
*before* `webbrowser.open(url)`, i.e. at the exact moment the user is waiting on a
blank screen. Move it behind the wizard opening, onto a daemon first-run task that
runs once and records its outcome, so it remains automatic (correction 3) while
leaving the critical path. `doctor --repair` continues to retry it.

---

## Deliberately not done

- **`brain_routine("due")` consolidation.** The first pass proposed collapsing the
  four tasks into one daemon-scheduled "what's due" call. Research killed it: once
  the install command creates the tasks, four tasks cost the user nothing, so this
  would buy new daemon state plus a breaking change to every existing fleet task's
  prompt in exchange for nothing.
- **Writing scheduled-task definitions to disk.** Schedule/folder/enabled live in
  Desktop's private store; writing there would be fragile and unsupported.
- **Removing `claude_desktop_config.json` support.** Both surfaces are in use.
- **The `mcpbrain-bootstrap` interview.** Optional and out of scope.
- **Windows ARM64 native install.** Unchanged and still not viable — several deps
  ship no `win_arm64` wheels.

---

## Testing

Stage 1 is mostly prose and packaging; Stages 2–3 are code and get TDD'd.

| Area | Test |
|---|---|
| `[daemon]` alias | `daemon` extra still declared and empty; `update.py`'s command line unchanged (`test_update_index.py:31` keeps passing untouched) |
| Install docs | one canonical command; `test_plugin_assets.py` asserts `install.md` no longer carries `[daemon]`; a new assertion that README/DISTRIBUTION reference no second copy |
| MSIX path | `_desktop_config_paths()` returns the virtualised path first when it exists, `%APPDATA%` when it does not, both when both do (monkeypatched `LOCALAPPDATA`/`APPDATA`) |
| Merge safety | writing into a config holding `preferences` + other `mcpServers` preserves every key; an unparseable file is left byte-identical and reported |
| Atomicity | a write interrupted before `os.replace` leaves the original intact |
| `~/.claude.json` | entry lands under top-level `mcpServers`; `projects` is untouched; `CLAUDE_CONFIG_DIR` honoured |
| Ordering | `/api/connect-desktop` calls quit, then write, then launch — asserted on call order, not just outcome |
| Binary path | shim preferred over the resolved venv path; `resolve()` fallback when `which` is empty |
| Doctor | connector check reports missing entry, stale command path, and repairs both |
| `install.ps1` | Pester suite shrinks to uv / vcredist / install actions; no `persistence-*` assertions remain |
| Release | dist copy of `install.ps1` matches source |
| Wizard | fleet IDs render from `/api/config`; model download auto-fires once on Google connect and is idempotent |
| OCR | the first-run task calls `install_tesseract` exactly once and records its outcome; `setup` no longer calls it |

Per project convention, Claude runs edited-and-directly-impacted tests only; Josh
runs the full suite.

## Risks and rollback

- **Task creation may not be available** on an older Desktop build or under an org
  policy that disables Routines. Mitigation: the manual table stays in
  `plugin/INSTALL.md` and the command falls back to it rather than failing.
- **`~/.claude.json` is high-value.** Mitigated by parse-check, atomic replace, and
  never writing on a parse failure. This is the single riskiest write in the
  design and its tests come first.
- **MSIX path is unverified on real hardware.** It is derived from three
  independent bug reports, not from a machine we control. It stays behind the open
  Windows hardware QA gate; shipping it does not close that gate.
- **Rollback** is per-stage: each stage is independently shippable and none
  changes stored data. Stage 2 is the only one that touches a file the user owns,
  and its writes are additive and idempotent.
