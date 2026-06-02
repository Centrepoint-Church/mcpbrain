#!/usr/bin/env bash
# mcpbrain installer (macOS, double-clickable). Installs uv if missing,
# installs the mcpbrain tool, warms the embedding model, registers the launchd
# login agent, and opens the setup wizard. Pass --dry-run to print the steps
# without running them.
set -euo pipefail

# Double-clicking runs from the user's home, not the repo. Move to this
# script's directory so `uv tool install --from .` finds pyproject.toml.
cd "$(dirname "${BASH_SOURCE[0]}")/.."
REPO="$(pwd)"

export MCPBRAIN_HOME="${MCPBRAIN_HOME:-$HOME/.mcpbrain}"

DRY="${1:-}"
run() { if [ "$DRY" = "--dry-run" ]; then echo "[dry-run] $*"; else "$@"; fi; }

command -v uv >/dev/null 2>&1 || run sh -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'

run uv tool install --from . mcpbrain --force

BIN="$(command -v mcpbrain || echo "$HOME/.local/bin/mcpbrain")"

# Register the MCP server entry in the Claude Desktop config.
run "$BIN" register || true

# Warm the model: the first daemon cycle downloads the ONNX embedding model.
# `|| true` so a credential-less first run can't fail the install.
run "$BIN" daemon --once || true

# `mcpbrain setup` installs and starts the launchd login agent itself (via
# _ensure_daemon_running), then opens the wizard.
if [ "$DRY" = "--dry-run" ]; then run "$BIN" setup --dry-run --repo-dir "$REPO"; else run "$BIN" setup --repo-dir "$REPO"; fi

echo "Done. If a browser didn't open, visit the URL above."
