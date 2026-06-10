"""Headless-claude cowork cadences (gardener, meeting-packs), cross-platform.

Reads a shipped prompt from mcpbrain/cowork/, appends runtime context, and runs
`claude -p` with the ops-brain-search MCP server pointing at `mcpbrain mcp-server`.
Prompt is piped via stdin (the prompts start with YAML `---`, which breaks -p).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from mcpbrain import config
from mcpbrain.draft import _find_claude

_PROMPT_DIR = Path(__file__).parent


def _mcpbrain_bin() -> str:
    return (shutil.which("mcpbrain")
            or str(Path(sys.executable).with_name("mcpbrain")))


def _mcp_config(home: str) -> str:
    return json.dumps({"mcpServers": {"ops-brain-search": {
        "command": _mcpbrain_bin(), "args": ["mcp-server"],
        "env": {"MCPBRAIN_HOME": home, "MCPBRAIN_EMBEDDER": config.EMBEDDER}}}})


def run_cowork(prompt_name: str, *, tools: str, extra_context: str,
               log_name: str, cwd: str | None = None, timeout: int = 1800) -> int:
    """Run a shipped cowork prompt via headless claude. Returns the claude rc."""
    home = str(config.app_dir())
    prompt = (_PROMPT_DIR / prompt_name).read_text() + "\n\n" + extra_context
    cmd = [_find_claude(), "-p", "--tools", tools,
           "--settings", '{"disableAllHooks":true}',
           "--strict-mcp-config", "--mcp-config", _mcp_config(home),
           "--dangerously-skip-permissions"]
    logs = Path(home) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                            timeout=timeout, cwd=cwd)
    stamp = datetime.now(timezone.utc).isoformat()
    with (logs / log_name).open("a") as f:
        f.write(f"[{stamp}] rc={result.returncode}\n{result.stdout}\n{result.stderr}\n")
    return result.returncode


def gardener_main(argv=None) -> int:
    home = str(config.app_dir())
    repo = config.records_dir(home)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ctx = (f"Weekly hygiene run. Working directory: {repo}\nToday's date: {today}\n"
           "When committing, use git add <specific-path> (never -A) and commit by name.")
    return run_cowork("memory-gardener.md", tools="Bash,Read,Edit,Write",
                      extra_context=ctx, log_name="memory_gardener.log", cwd=repo)


def meeting_packs_main(argv=None) -> int:
    home = str(config.app_dir())
    port = _read(Path(home) / "control_port")
    token = _read(Path(home) / "control_token")
    if not port or not token:
        return 0  # daemon not running — nothing to do
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ctx = (f"Control API base URL: http://127.0.0.1:{port}\nAuth token: {token}\n"
           f"Today's date: {today}\nRun now: check calendar, find events needing packs, build them.")
    return run_cowork("meeting-packs.md", tools="Bash", extra_context=ctx,
                      log_name="meeting_packs.log")


def _read(p: Path) -> str:
    try:
        return p.read_text().strip()
    except OSError:
        return ""
