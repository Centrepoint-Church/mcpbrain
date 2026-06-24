"""Daemon-side writer for structured records into the records repo.

Called only from drain (the daemon is the single writer). Each function
appends/creates one file and commits it BY NAME (never `git add -A`), keeping
the daemon's commits isolated from the gardener's hygiene commits.
"""
from __future__ import annotations
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

def _git(repo: str, *args: str) -> subprocess.CompletedProcess:
    # Force stable English git output (LC_ALL=C, LANGUAGE="") so a localized
    # launchd environment can't translate messages out from under any callers.
    env = {**os.environ, "LC_ALL": "C", "LANGUAGE": ""}
    return subprocess.run(["git", "-C", repo, *args], check=True,
                          capture_output=True, env=env)

def _has_staged(repo: str, relpath: str) -> bool:
    """True if there are staged changes for relpath. Exit-code based, locale-proof.

    `git diff --cached --quiet` exits 1 when there ARE staged changes (our signal
    to commit) and 0 when there are none.
    """
    env = {**os.environ, "LC_ALL": "C", "LANGUAGE": ""}
    result = subprocess.run(
        ["git", "-C", repo, "diff", "--cached", "--quiet", "--", relpath],
        check=False, capture_output=True, env=env)
    return result.returncode != 0

def _commit_file(repo: str, relpath: str, message: str) -> bool:
    """Stage relpath by name and commit only if something is staged.

    Returns True if a commit was made, False on a no-op (nothing staged).
    """
    _git(repo, "add", relpath)          # by name, never -A
    if not _has_staged(repo, relpath):
        return False  # nothing to commit; idempotent no-op
    _git(repo, "commit", "-m", message)
    return True

def append_decision(repo: str, *, text: str, rationale: str = "", owner: str = "",
                    supersedes: str = "") -> bool:
    p = Path(repo) / "state" / "decisions.md"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = f"| {today} | {text} | {rationale or '-'} | {owner} | Active | {supersedes or '-'} |\n"
    original = p.read_text()
    if row not in original:
        anchor = "Append new decisions at the top. One line per decision."
        idx = original.find(anchor)
        if idx == -1:
            log.warning("append_%s: anchor %r not found in %s; appending at EOF",
                        "decision", anchor, p)
        insert = original.find("\n", idx) + 1 if idx != -1 else len(original)
        # skip the following blank line if present
        while insert < len(original) and original[insert] == "\n":
            insert += 1
        p.write_text(original[:insert] + row + original[insert:])
    return _commit_file(repo, "state/decisions.md", f"decision: {text[:60]}")

def append_continuity(repo: str, *, text: str, today: str | None = None) -> bool:
    p = Path(repo) / "state" / "hot.md"
    today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    entry = f"- **{today}:** {text}\n"
    original = p.read_text()
    if entry not in original:
        anchor = "## Just decided"
        idx = original.find(anchor)
        if idx == -1:
            log.warning("append_%s: anchor %r not found in %s; appending at EOF",
                        "continuity", anchor, p)
        insert = original.find("\n", idx) + 1 if idx != -1 else len(original)
        while insert < len(original) and original[insert] == "\n":
            insert += 1
        p.write_text(original[:insert] + entry + original[insert:])
    return _commit_file(repo, "state/hot.md", f"continuity: {text[:60]}")

_APPROVED_ATTRIBUTION_SOURCES = frozenset({
    "owner_statement", "signature", "owner_confirmation"
})


def _changed_line_count(old: str, new: str) -> int:
    """Number of added + removed lines between old and new content.

    Used as a deterministic backstop for the gardener's per-run change cap so a
    single auto-apply can never silently rewrite a whole file.
    """
    import difflib
    n = 0
    for line in difflib.unified_diff(old.splitlines(), new.splitlines(), n=0):
        if line[:2] in ("++", "--") or line.startswith("@@"):
            continue  # diff headers, not content lines
        if line[:1] in ("+", "-"):
            n += 1
    return n


def _enforce_change_cap(p: Path, new_content: str, max_changed_lines: int | None) -> None:
    """Raise ValueError if applying new_content to p would change > the cap.

    No-op when max_changed_lines is None (cap disabled) or the file is absent.
    """
    if max_changed_lines is None or not p.exists():
        return
    changed = _changed_line_count(p.read_text(), new_content)
    if changed > max_changed_lines:
        raise ValueError(
            f"change cap exceeded for {p.name}: {changed} lines changed > "
            f"limit {max_changed_lines}. Apply fewer entries this run and leave "
            "the rest for the next."
        )


def write_gardener_reference(repo: str, filename: str, new_content: str, *,
                             max_changed_lines: int | None = None) -> bool:
    """Write reference/<filename> for the drift lane.

    Commits with tag 'gardener: apply drift (reference/<filename>)'.
    filename must be a plain basename — no path separators.
    The file must already exist (drift lane updates existing entries; it does not
    create new reference files). max_changed_lines caps the per-run change size
    (ValueError if exceeded). Returns True if a commit was made, False on no-op.
    """
    if "/" in filename or "\\" in filename:
        raise ValueError(f"filename must be a basename, not a path: {filename!r}")
    p = Path(repo) / "reference" / filename
    if not p.exists():
        raise FileNotFoundError(f"reference file not found: {p}")
    if not new_content.endswith("\n"):
        new_content += "\n"
    _enforce_change_cap(p, new_content, max_changed_lines)
    p.write_text(new_content)
    return _commit_file(repo, f"reference/{filename}",
                        f"gardener: apply drift (reference/{filename})")


def write_gardener_context(repo: str, filename: str, new_content: str, *,
                           asserts_person_role: bool = False,
                           attribution_source: str | None = None,
                           max_changed_lines: int | None = None) -> bool:
    """Write context/<filename> for the constitution lane.

    Role-attribution rule (scoped to actual role claims): when asserts_person_role
    is True — i.e. this update assigns a role/title to a *person* — attribution_source
    must be one of _APPROVED_ATTRIBUTION_SOURCES ("owner_statement", "signature",
    "owner_confirmation"); any other value raises ValueError. Never attribute a role
    from text you wrote or inferred from context; only from their own statement,
    signature, or owner confirmation. Updates that assert no person role (e.g. a
    preferences or own-responsibilities change) need no attribution_source.

    filename must be a plain basename. The file must already exist. max_changed_lines
    caps the per-run change size. Commits with tag 'gardener: update
    identity/preferences'. Returns True if a commit was made, False on no-op.
    """
    if asserts_person_role and attribution_source not in _APPROVED_ATTRIBUTION_SOURCES:
        raise ValueError(
            f"Role attribution source {attribution_source!r} is not permitted for a "
            f"person-role claim. Approved sources: {sorted(_APPROVED_ATTRIBUTION_SOURCES)}. "
            "Never attribute a role or title to a person from text you wrote; "
            "only from their own statement, signature, or owner confirmation."
        )
    if "/" in filename or "\\" in filename:
        raise ValueError(f"filename must be a basename, not a path: {filename!r}")
    p = Path(repo) / "context" / filename
    if not p.exists():
        raise FileNotFoundError(f"context file not found: {p}")
    if not new_content.endswith("\n"):
        new_content += "\n"
    _enforce_change_cap(p, new_content, max_changed_lines)
    p.write_text(new_content)
    return _commit_file(repo, f"context/{filename}",
                        "gardener: update identity/preferences")


def write_memory(repo: str, *, slug: str, description: str, body: str,
                 memory_type: str = "project") -> bool:
    """Write memory/<slug>.md and a MEMORY.md pointer, committing both by name.

    No-clobber: if the target file exists with DIFFERENT content (e.g. the weekly
    gardener curated it), it is left as-is and a warning is logged. Returns True
    only if a commit was made; False on a no-op (identical re-write, collision, or
    nothing-to-commit).
    """
    mp = Path(repo) / "memory" / f"{slug}.md"
    new_content = (
        f"---\nname: {slug}\ndescription: {description}\nmetadata:\n  type: {memory_type}\n---\n\n{body}\n")
    if not mp.exists():
        mp.write_text(new_content)
    elif mp.read_text() == new_content:
        pass  # idempotent: already exactly this content
    else:
        log.warning("memory slug collision: %s exists with different content; "
                    "not overwriting", slug)
    # Always ensure the MEMORY.md pointer exists, deduped on the PATH (slug),
    # not the description (FIX B).
    index = Path(repo) / "MEMORY.md"
    idx_text = index.read_text()
    path_ref = f"](memory/{slug}.md)"
    if path_ref not in idx_text:
        pointer = f"- [{description}](memory/{slug}.md)\n"
        index.write_text(idx_text.rstrip("\n") + "\n" + pointer)
    # Stage both by name, then exit-code no-op detection across both paths.
    _git(repo, "add", f"memory/{slug}.md", "MEMORY.md")
    if not _has_staged(repo, f"memory/{slug}.md") and not _has_staged(repo, "MEMORY.md"):
        return False
    _git(repo, "commit", "-m", f"memory: add {slug}")
    return True
