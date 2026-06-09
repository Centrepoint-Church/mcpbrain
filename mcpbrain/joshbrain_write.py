"""Daemon-side writer for structured records into the joshbrain repo.

Called only from drain (the daemon is the single writer). Each function
appends/creates one file and commits it BY NAME (never `git add -A`), keeping
the daemon's commits isolated from the gardener's hygiene commits.
"""
from __future__ import annotations
import subprocess
from datetime import datetime, timezone
from pathlib import Path

def _git(repo: str, *args: str) -> None:
    subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True)

def _commit_file(repo: str, relpath: str, message: str) -> None:
    _git(repo, "add", relpath)          # by name, never -A
    _git(repo, "commit", "-m", message)

def append_decision(repo: str, *, text: str, rationale: str = "", owner: str = "Josh",
                    supersedes: str = "", org: str = "") -> None:
    p = Path(repo) / "state" / "decisions.md"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = f"| {today} | {text} | {rationale or '-'} | {owner} | Active | {supersedes or '-'} |\n"
    original = p.read_text()
    anchor = "Append new decisions at the top. One line per decision."
    idx = original.find(anchor)
    insert = original.find("\n", idx) + 1 if idx != -1 else len(original)
    # skip the following blank line if present
    while insert < len(original) and original[insert] == "\n":
        insert += 1
    p.write_text(original[:insert] + row + original[insert:])
    _commit_file(repo, "state/decisions.md", f"decision: {text[:60]}")

def append_continuity(repo: str, *, text: str, today: str | None = None) -> None:
    p = Path(repo) / "state" / "hot.md"
    today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    entry = f"- **{today}:** {text}\n"
    original = p.read_text()
    anchor = "## Just decided"
    idx = original.find(anchor)
    insert = original.find("\n", idx) + 1 if idx != -1 else len(original)
    while insert < len(original) and original[insert] == "\n":
        insert += 1
    p.write_text(original[:insert] + entry + original[insert:])
    _commit_file(repo, "state/hot.md", f"continuity: {text[:60]}")

def write_memory(repo: str, *, slug: str, description: str, body: str,
                 memory_type: str = "project") -> None:
    mp = Path(repo) / "memory" / f"{slug}.md"
    mp.write_text(
        f"---\nname: {slug}\ndescription: {description}\nmetadata:\n  type: {memory_type}\n---\n\n{body}\n")
    index = Path(repo) / "MEMORY.md"
    idx_text = index.read_text()
    pointer = f"- [{description}]({slug}.md)\n"
    if pointer not in idx_text:
        index.write_text(idx_text.rstrip("\n") + "\n" + pointer)
    _git(repo, "add", f"memory/{slug}.md", "MEMORY.md")
    _git(repo, "commit", "-m", f"memory: add {slug}")
