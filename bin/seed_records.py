#!/usr/bin/env python3
"""One-time seeding script: creates a records repo from ops-brain source files.

Run once, then delete this script — it is a migration tool, not a mirror.
Re-runs are rejected if the destination already exists.

Usage:
    python3 bin/seed_records.py [--src PATH] [--dest PATH]

Defaults:
    --src   ~/ops-brain
    --dest  ~/records
"""
import argparse
import shutil
import stat
import subprocess
import sys
from pathlib import Path

# Files copied verbatim from ops-brain (relative paths)
_COPY_FILES = [
    "context/identity.md",
    "context/voice.md",
    "context/preferences.md",
    "reference/projects.md",
    "reference/systems.md",
    "reference/ministry-context.md",
    "state/hot.md",
    "state/decisions.md",
    "state/retired.md",
    "state/compliance.md",
    "bin/prune_hot_md.py",
]

# Whole directories copied from ops-brain
_COPY_DIRS = [
    "reference/examples",
    "templates",
]

# ---------------------------------------------------------------------------
# Generated file content
# ---------------------------------------------------------------------------

_GITIGNORE = """\
__pycache__/
*.pyc
.DS_Store
*.log
*.err
outputs/
projects/*/outputs/
"""

_CLAUDE_MD = """\
@context/identity.md
@context/voice.md
@context/preferences.md

---

<!-- GARDENER-PROTECTED-START: identity and core rules — gardener cannot modify this block -->

## Org tagging rules

<important if="this session involves people, organisations, or roles">
- List your orgs and roles in context/identity.md.
- Every person entity must include org affiliation in its first observation.
- Every `brain_ingest` call must name the org in body text so searches surface org context.
- When memory results appear, check the org tag before using them.
</important>

## Role attribution rules

<important if="this session involves people, roles, or attribution">
- Never attribute a role/title to a person based on text you wrote — including your own email signature or correspondence context.
- Only record a person's role if they stated it themselves, it's in their own email signature, or you explicitly confirm it.
- If role is uncertain, omit. Bad attribution is worse than no attribution.
</important>

<!-- GARDENER-PROTECTED-END -->

---

## Memory Protocol

**Load on demand:**
1. Active continuity → `state/hot.md` (auto-pruned to 14 days).
2. Historical context, prior emails, document content → brain_search first (summaries); brain_read only 2-3 most relevant. Never load full sets.
3. Task involves a project → `reference/projects.md`.
4. Task involves tools, automation, or the mcpbrain stack → `reference/systems.md`.
5. Task involves drafting → load the matching template + example before writing:

   | Artifact | Template | Example |
   |---|---|---|
   | Job description / PD | `templates/job-description.md` | `reference/examples/job-description.md` |
   | Meeting minutes | `templates/meeting-minutes.md` | `reference/examples/meeting-minutes.md` |
   | SOP | `templates/sop.md` | `reference/examples/sop.md` |

6. Related prior decision → `state/decisions.md`.
7. Compliance work → `state/compliance.md`.
8. Plan-writing, architecture work, rebuilding any named system → `state/retired.md`.

**Extended thinking:** use for risk assessments, strategic recommendations, financial analysis, compliance reviews, multi-stakeholder planning.

---

## Where Things Go

| Type | Destination | Format |
|---|---|---|
| Decision that supersedes earlier behaviour | `state/decisions.md` | Dated row + `Supersedes` column |
| Rule that should always apply going forward | `CLAUDE.md` (or `reference/*` if conditional) | Bullet under the right heading |
| Active in-progress work, < 7 days old | `state/hot.md` "Just decided" | 2-4 line entry, date-prefixed |
| One-shot session detail (what shipped, what bug) | Commit message | Don't write to hot.md |
| Cross-session learning from a mistake | `memory/feedback_*.md` | Standalone file + link from MEMORY.md |
| Context / project status change | `reference/projects.md` | Inline update |
| Tool / system / integration change | `reference/systems.md` | Inline update |

**hot.md discipline:** entries are 2-4 lines max with a `**YYYY-MM-DD:**` prefix. Anything older than 14 days is auto-pruned by `bin/prune_hot_md.py`.

---

## Output File Convention

- **Cross-cutting deliverables** → `~/records/outputs/`
- **Project-specific deliverables** → `~/records/projects/<project-name>/outputs/`

---

## Quality Standard

voice.md and preferences.md are loaded at session start. Apply them to all output. Before presenting any draft, run the voice.md self-check. Fix issues before presenting, not after.

---

## Subagents

Reach for subagents when a task genuinely needs an isolated context window — typically 3+ tool calls or a multi-section output. Single-fact lookups, short replies, and quick confirmations stay in the main session.

---

## Planning Before Action

- **Any request touching more than two files, or involving a new system/script:** propose a numbered plan and wait for confirmation before writing anything.
- **Don't add features that weren't explicitly requested.** Build the best long-term solution within scope.
- **Before drafting any plan: run a retirement and supersession check.** Read `state/retired.md` in full; grep `state/decisions.md` for Retired rows + last 30 days of Active rows.

---

## Proactive Behaviours

**Proactively `brain_ingest` at natural capture points:**
- After extended discussion of a topic with no explicit capture → ingest before closing.
- After producing a significant deliverable → ingest it.

**Run `brain_search` before answering from training data:**
- "What do we know about X", "have we dealt with X before" → search first.

**Use `brain_actions` for task/deadline questions.**

**Search discipline:** for factual lookups, allow up to 5 brain_search queries. If the answer hasn't surfaced by then, say "not in the index". Treat top score < 0.35 as no real match.

---

## Self-Evolution Protocol

Update files when things occur — don't defer to end of session. The routing rules above tell you the destination for each kind of change. Propose. Owner approves.

---

## Platform Notes

- **Claude Code (Mac):** Working directory is `~/records`. Edit files directly. mcpbrain MCP is registered; use `brain_*` tools for knowledge graph access.
- **Cowork:** Working folder is `~/records` with `~/.mcpbrain` as a connected folder. Project instructions are in `cowork/`.
- **Claude Desktop:** Reads context via MCP resources from `~/.mcpbrain/context/` (@-mentionable via the mcpbrain MCP server).
"""

_BOOTSTRAP_MD = """\
# Records Bootstrap Checklist

Use this when setting up mcpbrain on a new machine. Steps are in dependency order.

## Prerequisites

```bash
xcode-select --install
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
brew install git python@3.12 node@22
```

Add SSH key to GitHub: `cat ~/.ssh/id_ed25519.pub` → GitHub Settings → SSH keys.

## 1. Clone repos

```bash
mkdir -p ~/Documents/GitHub
cd ~/Documents/GitHub
git clone git@github.com:<your-org>/mcpbrain.git
git clone git@github.com:<your-org>/records.git
```

## 2. Set up mcpbrain venv

```bash
cd ~/Documents/GitHub/mcpbrain
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

## 3. Initialise ~/.mcpbrain

```bash
mkdir -p ~/.mcpbrain/capture_inbox ~/.mcpbrain/context ~/.mcpbrain/enrich_queue ~/.mcpbrain/enrich_inbox
# Copy config.json from existing machine or adapt the example:
cp ~/Documents/GitHub/mcpbrain/config.json.example ~/.mcpbrain/config.json
# Edit: set repo_dir, enrich_mode, blocks_interval_s, audit_interval_s
```

## 4. Install launchd agents

Run with the venv python (`.venv/bin/python3`) from ~/Documents/GitHub/mcpbrain:

```python
from mcpbrain.agents import (
    launchd_plist, launchd_tray_plist,
    records_prune_plist, records_context_health_plist,
    records_gardener_plist, meeting_packs_plist,
)
import subprocess
from pathlib import Path

python_bin = subprocess.check_output(["which", "python3"], text=True).strip()
records_dir = str(Path.home() / ".mcpbrain" / "records")
mcpbrain_home = str(Path.home() / ".mcpbrain")
mcpbrain_bin = str(Path.home() / "Documents/GitHub/mcpbrain/.venv/bin/mcpbrain")
launchd = Path.home() / "Library" / "LaunchAgents"
launchd.mkdir(parents=True, exist_ok=True)

plists = {
    "com.mcpbrain": launchd_plist(mcpbrain_bin=mcpbrain_bin, home=mcpbrain_home),
    "com.mcpbrain.tray": launchd_tray_plist(mcpbrain_bin=mcpbrain_bin, home=mcpbrain_home),
    "com.mcpbrain.records.prune": records_prune_plist(
        python_bin=python_bin, records_dir=records_dir, mcpbrain_home=mcpbrain_home),
    "com.mcpbrain.records.context-health": records_context_health_plist(
        python_bin=python_bin, records_dir=records_dir, mcpbrain_home=mcpbrain_home),
    "com.mcpbrain.records.gardener": records_gardener_plist(
        records_dir=records_dir, mcpbrain_home=mcpbrain_home),
    "com.mcpbrain.records.meeting-packs": meeting_packs_plist(home=mcpbrain_home),
}

for label, content in plists.items():
    path = launchd / f"{label}.plist"
    path.write_text(content)
    subprocess.run(["launchctl", "load", "-w", str(path)], check=True)
    print(f"loaded {label}")
```

Then verify:

```bash
launchctl list | grep com.mcpbrain
# Expected: com.mcpbrain, com.mcpbrain.tray, com.mcpbrain.records.prune, com.mcpbrain.records.context-health, com.mcpbrain.records.gardener, com.mcpbrain.records.meeting-packs
```

## 5. Register mcpbrain MCP — Claude Code

Register the server with `claude mcp add` (not by hand-editing JSON):

```bash
claude mcp add --transport stdio --scope user mcpbrain-search \\
  -e MCPBRAIN_HOME=/home/user/.mcpbrain \\
  -- /home/user/Documents/GitHub/mcpbrain/.venv/bin/mcpbrain mcp
```

This writes to `~/.claude.json` (user scope). The `-e KEY=value` flag sets the environment variable. Verify with `claude mcp list`.

## 6. Register mcpbrain MCP — Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "mcpbrain-search": {
      "command": "/home/user/Documents/GitHub/mcpbrain/.venv/bin/mcpbrain",
      "args": ["mcp"],
      "env": {
        "MCPBRAIN_HOME": "/home/user/.mcpbrain"
      }
    }
  }
}
```

Restart Claude Desktop. Verify: in a chat, type `@memory.md` — should show memory.md content.

## 7. Claude Code plugins

```bash
# Install superpowers and any other plugins:
# From Claude Code: /install-superpowers (or follow plugin install docs)
```

## 8. Cowork projects

Create two projects in Cowork:

**Context project ("Records — Context"):**
- Working folder: `~/Documents/GitHub/records`
- Connected folder: `~/.mcpbrain`
- Instructions: paste the full content of `cowork/context-project.md`

**Memory Gardener ("Memory Gardener"):**
- Working folder: `~/Documents/GitHub/records`
- Connected folder: `~/.mcpbrain`
- Instructions: paste the full content of `cowork/memory-gardener.md`
- Schedule: weekly, Monday 08:00

## Verify everything

```bash
# Launchd agents running
launchctl list | grep com.mcpbrain

# mcpbrain daemon log
tail -20 ~/.mcpbrain/com.mcpbrain.log

# Records repo git healthy
git -C ~/Documents/GitHub/records log --oneline | head -5

# Brain search works
cd ~/Documents/GitHub/mcpbrain
.venv/bin/mcpbrain mcp  # should start without error (ctrl-C to exit)
```
"""

_COWORK_CONTEXT_PROJECT_MD = """\
# Records — Context Project

This Cowork project provides Claude with your complete working context across all sessions.

## Working setup

- **Working folder:** ~/records (identity, voice, preferences, state, reference, templates)
- **Connected folder:** ~/.mcpbrain (memory.md, config.json, brain.sqlite3)

## Identity

See `context/identity.md` in the working folder for your full profile.

Fill in your orgs, roles, and areas of responsibility there.

## Voice

See `context/voice.md` for the style guide.

## Preferences

See `context/preferences.md` for format defaults, collaboration style, and hard rules.

## Memory

The file `context/memory.md` in the connected ~/.mcpbrain folder contains the current memory index. Read it when cross-session context helps ("what do we know about X", "what was decided about Y").

`~/.mcpbrain/context/memory.md` is the daemon-maintained note index; `~/records/MEMORY.md` is the Claude Code auto-memory index maintained by the gardener. They are different files with different owners.

## Routing

When something worth recording happens this session:

| Type | Destination |
|---|---|
| Decision superseding prior behaviour | `state/decisions.md` — dated row |
| Active work < 7 days | `state/hot.md` — 2-4 line entry, date-prefixed |
| Project context change | `reference/projects.md` |
| Tool/system change | `reference/systems.md` |
| Cross-session learning | new `memory/feedback_*.md` + link in `MEMORY.md` |

## Proactive behaviours

- Run brain_search before answering historical questions
- Ingest new durable facts at natural capture points before closing
"""

_COWORK_MEMORY_GARDENER_MD = """\
# Memory Gardener — Weekly Scheduled Task

**Schedule:** weekly, Monday 08:00
**Working folder:** ~/records
**Connected folder:** ~/.mcpbrain

## Purpose

Each week: review new memory captures, check context files for drift, and apply focused updates to records within defined boundaries. Commit all changes with a descriptive message. If nothing needs changing, log that instead — don't make cosmetic edits.

## What to read first

1. `context/memory.md` in the connected ~/.mcpbrain folder — current memory index (daemon-maintained)
2. `state/hot.md` — entries from the last 7 days
3. `MEMORY.md` in records — the Claude Code auto-memory index
4. Recent entries in `~/.mcpbrain/change_log` (if accessible) — system-applied changes since last gardener run

`~/.mcpbrain/context/memory.md` is the daemon-maintained note index; `~/records/MEMORY.md` is the Claude Code auto-memory index maintained by the gardener. They are different files with different owners.

## What you can update

- `MEMORY.md` — add new pointers, update descriptions, remove pointers for files that no longer exist
- `memory/*.md` files — update or add memory files when new durable facts emerge from recent captures
- `reference/projects.md` — update project status, add new projects when confirmed in recent captures

## What you cannot modify

- Anything between `<!-- GARDENER-PROTECTED-START -->` and `<!-- GARDENER-PROTECTED-END -->` in `CLAUDE.md`
- `context/identity.md`, `context/voice.md`, `context/preferences.md`
- `state/decisions.md`, `state/retired.md`, `state/compliance.md`
- `CLAUDE.md` itself (outside the non-protected routing sections — ask the owner for those changes)

## Caps per run

- Max 10 memory file updates (create or modify)
- Max 20 lines changed in any single reference file
- No changes to protected sections under any circumstances

## Commit format

Stage specific files by name (never `git add -A`):

```bash
git add state/decisions.md reference/projects.md memory/feedback_xyz.md MEMORY.md
git commit -m "gardener: [brief description of what changed and why]"
```

## If nothing needs changing

```bash
echo "$(date -I): gardener ran, no changes needed" >> ~/.mcpbrain/gardener.log
```

## Quality check before committing

Read every file you changed. Confirm no banned words, no em dashes, org tags correct.
"""

_MEMORY_MD = """\
# Memory Index

Populated by the Memory Gardener (weekly, Monday). One line per memory file in memory/.
"""

_CONTEXT_HEALTH_PY = """\
#!/usr/bin/env python3
\"\"\"Weekly context health check for the records repo.

Checks:
  - MEMORY.md line count (warn if approaching 200-line truncation limit)
  - state/hot.md for entries older than 14 days (prune_hot_md.py should clear these)
  - ~/.mcpbrain/context/memory.md exists and was updated recently
Writes warnings to stderr; exits 1 if any found, 0 if clean.
\"\"\"
import os
import re
import sys
from datetime import date, timedelta
from pathlib import Path

RECORDS = Path(__file__).parent.parent
MCPBRAIN_HOME = Path(os.environ.get("MCPBRAIN_HOME", Path.home() / ".mcpbrain"))

_WARN_MEMORY_LINES = 180
_WARN_HOT_DAYS = 14
_WARN_BRAIN_MEMORY_DAYS = 7


def _check_memory_md():
    path = RECORDS / "MEMORY.md"
    if not path.exists():
        return [f"MISSING: {path}"]
    n = len(path.read_text().splitlines())
    if n > _WARN_MEMORY_LINES:
        return [f"WARN: MEMORY.md has {n} lines (truncation limit 200 — prune old entries)"]
    return []


def _check_hot_md():
    path = RECORDS / "state" / "hot.md"
    if not path.exists():
        return []
    cutoff = date.today() - timedelta(days=_WARN_HOT_DAYS)
    warnings = []
    for line in path.read_text().splitlines():
        m = re.match(r"^\\s*[-*]\\s+(?:\\*\\*\\s*)?(\\d{4}-\\d{2}-\\d{2})", line)
        if m:
            entry_date = date.fromisoformat(m.group(1))
            if entry_date < cutoff:
                warnings.append(
                    f"WARN: hot.md entry {m.group(1)} is >{_WARN_HOT_DAYS} days old "
                    f"(prune_hot_md.py should have removed it)"
                )
    return warnings


def _check_brain_memory():
    path = MCPBRAIN_HOME / "context" / "memory.md"
    if not path.exists():
        return [f"WARN: {path} missing — mcpbrain daemon may not have run"]
    age = (date.today() - date.fromtimestamp(path.stat().st_mtime)).days
    if age > _WARN_BRAIN_MEMORY_DAYS:
        return [f"WARN: {path} not updated in {age} days — check daemon is running"]
    return []


def main() -> int:
    warnings = _check_memory_md() + _check_hot_md() + _check_brain_memory()
    if warnings:
        for w in warnings:
            print(w, file=sys.stderr)
        return 1
    print(f"context health OK ({date.today()})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
"""

# ---------------------------------------------------------------------------
# Seeding logic
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="One-time script: seed a records repo from ops-brain source files."
    )
    parser.add_argument("--src", default=str(Path.home() / "ops-brain"),
                        help="Path to ops-brain checkout (default: ~/ops-brain)")
    parser.add_argument("--dest", default=str(Path.home() / "records"),
                        help="Destination path for records repo (default: ~/records)")
    args = parser.parse_args()

    src = Path(args.src)
    dest = Path(args.dest)

    if not src.exists():
        print(f"ERROR: source not found: {src}", file=sys.stderr)
        sys.exit(1)

    if dest.exists():
        print(
            f"ERROR: destination already exists: {dest}\n"
            "This is a one-time seeding script — re-runs are not supported.\n"
            "If you need to re-seed, remove the destination first.",
            file=sys.stderr,
        )
        sys.exit(1)

    dest.mkdir(parents=True)

    copied = 0
    for rel in _COPY_FILES:
        src_file = src / rel
        if not src_file.exists():
            print(f"WARN: {src_file} not found, skipping", file=sys.stderr)
            continue
        dest_file = dest / rel
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dest_file)
        copied += 1

    for rel in _COPY_DIRS:
        src_dir = src / rel
        if not src_dir.exists():
            print(f"WARN: {src_dir} not found, skipping", file=sys.stderr)
            continue
        shutil.copytree(src_dir, dest / rel)
        copied += sum(1 for f in (dest / rel).rglob("*") if f.is_file())

    generated = 0
    generated_files = {
        "CLAUDE.md": _CLAUDE_MD,
        "BOOTSTRAP.md": _BOOTSTRAP_MD,
        ".gitignore": _GITIGNORE,
        "cowork/context-project.md": _COWORK_CONTEXT_PROJECT_MD,
        "cowork/memory-gardener.md": _COWORK_MEMORY_GARDENER_MD,
        "bin/context_health.py": _CONTEXT_HEALTH_PY,
        "MEMORY.md": _MEMORY_MD,
        "memory/.gitkeep": "",
    }
    for rel, content in generated_files.items():
        dest_file = dest / rel
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        dest_file.write_text(content)
        generated += 1

    # Make scripts executable
    for script in ["bin/context_health.py", "bin/prune_hot_md.py"]:
        p = dest / script
        if p.exists():
            p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)

    # Git init and initial commit
    subprocess.run(["git", "init"], cwd=dest, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=dest, check=True, capture_output=True)
    # Resolve author identity: prefer global git config, fall back to env.
    import shlex as _shlex  # noqa: F401
    def _git_cfg(key: str, fallback: str) -> str:
        r = subprocess.run(["git", "config", "--global", key],
                           capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else fallback

    git_name = _git_cfg("user.name", "Records Owner")
    git_email = _git_cfg("user.email", "owner@example.org")

    subprocess.run(
        ["git",
         "-c", f"user.name={git_name}",
         "-c", f"user.email={git_email}",
         "commit", "-m",
         "feat: seed records repo from ops-brain\n\n"
         "Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"],
        cwd=dest, check=True, capture_output=True,
    )

    print(
        f"records repo seeded at {dest} "
        f"({copied} files copied, {generated} files generated, 1 commit)"
    )


if __name__ == "__main__":
    main()
