#!/usr/bin/env bash
# mcpbrain installer (Linux/macOS). Installs uv if missing, installs mcpbrain
# from the wheel index, registers the login agent, and opens the setup wizard.
# Pass --dry-run to print the steps without running them.
set -euo pipefail

export MCPBRAIN_HOME="${MCPBRAIN_HOME:-$HOME/.mcpbrain}"
INDEX_URL="${MCPBRAIN_INDEX_URL:-https://itsjoshuakemp.github.io/mcpbrain-dist/simple/}"

DRY="${1:-}"
run() { if [ "$DRY" = "--dry-run" ]; then echo "[dry-run] $*"; else "$@"; fi; }

command -v uv >/dev/null 2>&1 || run sh -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
run uv tool install --python 3.12 --index "mcpbrain=$INDEX_URL" mcpbrain --force

BIN="$(command -v mcpbrain || echo "$HOME/.local/bin/mcpbrain")"
run "$BIN" register || true
run "$BIN" daemon --once || true
run "$BIN" setup

echo "Done. If a browser didn't open, visit the URL above."
