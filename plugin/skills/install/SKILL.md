---
name: mcpbrain-install
description: Bootstrap the mcpbrain daemon onto this machine. Installs uv, installs the mcpbrain daemon from the Centrepoint wheel index, registers the launchd (macOS) or Task Scheduler (Windows) background agent, and opens the setup wizard for Google sign-in. Idempotent — safe to run again.
---

# Install mcpbrain

Run this once in Cowork. If Cowork is running in full VM-sandbox mode (it cannot write to your home directory), run this skill in Claude Code instead, then return to Cowork.

## Steps

### 0. Check host access
```bash
touch ~/.local/.mcpbrain_probe 2>/dev/null && rm ~/.local/.mcpbrain_probe && echo HOST_OK || echo SANDBOX
```
If `SANDBOX`: stop and tell the user to run this skill in Claude Code, then return to Cowork. If `HOST_OK`: continue.

### 1. Install uv (if missing)
```bash
command -v uv || curl -LsSf https://astral.sh/uv/install.sh | sh
[ -f "$HOME/.local/bin/uv" ] && export PATH="$HOME/.local/bin:$PATH"
```

### 2. Install the mcpbrain daemon
```bash
uv tool install --python 3.12 --index "mcpbrain=https://centrepoint.github.io/mcpbrain-dist/simple/" mcpbrain --force
export PATH="$HOME/.local/bin:$PATH"
```

### 3. Register the background agent + open the setup wizard
`mcpbrain setup` registers the right background agent for your OS — **launchd** on macOS, **Task Scheduler** on Windows — installs the periodic cadences, starts the daemon, and opens the setup wizard in a browser tab:
```bash
mcpbrain setup
```
Complete Google sign-in, identity, and timezone in the wizard. (You do not run a separate scheduler command — `mcpbrain setup` detects the OS and does the right thing.)

### 4. Reload plugins
Run `/reload-plugins` in Cowork so the mcpbrain MCP server connects.

## Idempotency
Each step is safe to re-run: `uv tool install` is a no-op at the same version, agent registration is idempotent, and the wizard skips already-filled fields.
