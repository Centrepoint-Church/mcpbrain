"""Create and scaffold the per-user records repo (local git, no remote).

The daemon writes structured records (decisions, continuity, memories) into this
repo via the write module, committing by name. The repo is a plain local git repo
under the user's app dir. This module creates it and stamps the minimal scaffold
the writers expect (the decisions/hot anchors, MEMORY.md, memory/, voice.md),
idempotently — existing files are never clobbered and an existing repo's git
identity is left as-is.
"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from mcpbrain import config

log = logging.getLogger(__name__)

_TEMPLATES = Path(__file__).parent / "records_templates"

# Relative target path in the repo -> template filename in records_templates/.
_TEMPLATE_FILES = {
    "CLAUDE.md": "CLAUDE.md",
    "context/identity.md": "context_identity.md",
    "context/preferences.md": "context_preferences.md",
    "reference/systems.md": "reference_systems.md",
    "reference/projects.md": "reference_projects.md",
    "reference/org-context.md": "reference_org_context.md",
}


def _render_template(name: str, profile: dict) -> str:
    text = (_TEMPLATES / name).read_text(encoding="utf-8")
    orgs = [str(o.get("name") or "").strip() for o in (profile.get("orgs") or [])
            if isinstance(o, dict) and str(o.get("name") or "").strip()]
    org_list = ", ".join(orgs) if orgs else "(none configured yet)"
    org_block = "\n".join(f"- Items for {o} must be tagged clearly and kept separate." for o in orgs)
    repl = {
        "{{OWNER_FULL_NAME}}": profile.get("owner_full_name") or "(your name)",
        "{{OWNER_ROLE}}": profile.get("owner_role") or "(your role)",
        "{{ORG_LIST}}": org_list,
        "{{ORG_BLOCK}}": org_block,
    }
    for k, v in repl.items():
        text = text.replace(k, v)
    return text


# Per-process cache: tracks repo paths that have been fully ensured this process
# lifetime.  A new daemon process re-verifies once; subsequent cycles are no-ops.
_ENSURED: set[str] = set()

_DECISIONS_MD = """# Decisions

Decisions that supersede earlier behaviour. Newest first.

Append new decisions at the top. One line per decision.

| Date | Decision | Rationale | Owner | Status | Supersedes |
|------|----------|-----------|-------|--------|------------|
"""

_HOT_MD = """# Hot — active continuity

## Just decided

## Open
"""

_MEMORY_MD = "# Memory Index\n"

_VOICE_MD = """\
# Voice & Style

## Write Like a Person

Write the way a competent colleague talks to another competent colleague. Get to the point, say what you mean, be direct without being blunt, warm without being performative. Vary sentence and paragraph length naturally to match the thought, not a formula. Use fragments when they land. Match formality to context. Have opinions where the evidence supports them. Hedge only when uncertainty is real, and make hedges specific.

## Cut Filler

If a sentence could appear in any document on any topic, remove it. Phrases like "in today's fast-paced world," "it's important to note that," "as we navigate the complexities of" exist to sound busy. Get to the subject within the first two sentences. End when you've said what needs saying. Don't restate the opening at the close. If specifics are missing, name the limitation plainly rather than inflating with adjectives.

## Banned Words

Do not use: crucial, pivotal, vital, vibrant, rich (figurative), tapestry, landscape (abstract), testament, underscore (verb), highlight (verb), foster, cultivate (figurative), garner, showcase, exemplify, delve, intricate, enduring, interplay, nestled, groundbreaking (figurative), renowned, utilize, leverage (verb), synergy, realm, transformative, streamline, robust (outside engineering), cutting-edge, holistic, multifaceted, elevate (figurative), empower, reimagine, spearhead, enhance

**Banned sentence-starters:** Additionally, Moreover, Furthermore, Consequently. Use and, but, so, still, though, because, then, instead.

**Also avoid:** serves as, stands as, boasts, features, offers (when "has" works), enhancing its, commitment to, reflects broader, evolving landscape, indelible mark, deeply rooted, setting the stage, valuable insights, align/resonate with.

**Use instead:** is, are, has, was, shows, means, helps, changes, works, matters, needs. Plain verbs, direct statements.

## Banned Phrases

- "it's worth noting," "let's delve into," "in the realm of," "navigate the complexities," "at the end of the day," "it goes without saying," "in an era of," "the landscape of"
- "in order to" (say "to"); "due to the fact that" (say "because"); "Whether it's X, Y, or Z" (just list them)

## Banned Patterns

- **Significance inflation** — scale language to actual importance, not "turning point" / "redefine".
- **Contrastive framing** — "This isn't just about X, it's about Y." If Y is the point, just say Y.
- **Ask-and-answer rhetorical questions** — state the point directly, don't pose then answer.
- **Inspirational pivot** — don't elevate practical topics into grand vagueness.
- **Trailing participles** — "...highlighting its importance in the broader context." Cut them.
- **"Despite challenges" sandwich** — negative-soften-positive formula that avoids honesty about what went wrong.
- **Balanced hedging overload** — "may potentially be effective in some circumstances." Take a position; make caveats specific.
- **Authority without sources** — "Studies show," "experts agree." Name the source or rewrite as your own claim. Never invent quotes. Verify named sources, quotes, dates and titles against a reliable source before publishing; correct misattributions.
- **Default rule-of-three triplets** — "Not for X. Not for Y. For Z." Fine occasionally, not as a default closer.
- **Elegant variation** — cycling synonyms to avoid repetition. Reuse the same word.
- **"Not only... but also"** and similar negative parallelisms.
- **Vague attributions** without naming sources.
- **Future prospects speculation** unless asked.
- **Unnecessary bold, Title Case headings, emoji** in professional writing.
- **Collaborative filler** — "I hope this helps," "Certainly!", "Would you like me to..."
- **Self-referential narration of approach** — announcing what you're doing or where the response is going instead of just doing it: "let's break it down", "here's the thing people miss", "let me bring it home". Just say the thing; the structure should be felt, not announced. One brief signpost at the start of a long document is fine; recurring narration is the pattern to cut.
- **Cleverness over sincerity** — drop contrarian hooks, rhetorical asides, and metaphors reached for effect when a plain, warm, true statement carries the point better.
- **Aphorism-and-tag ("drop and land")** — a polished fragment followed by a short declarative that re-labels it ("That's the deal." / "That's the point." / "That's the aim.") tells the reader what they just heard. If the line is good, let it stand without the verdict; cut the tag, or fold the whole thought into one plain sentence.
- **Performative trust or vulnerability appeals** — "We know the trust ask is real." State reasoned positions with confidence.
- **Closing flourishes that restate the prior paragraph** — if the previous paragraph made the point, stop.
- **Sector or peer-context preambles** — don't lead with "The X model has served the industry, but peers have moved to Z." State the position directly. One short clause if context is genuinely needed.
- **Dramatic stakes language** — don't reach for worst-case illustration to underline a point already made by argument.

## Punctuation and Formatting

Never use em dashes — they are one of the strongest AI-writing markers. Use commas, full stops, or parentheses. Prefer plain connectives (and, but, so, still, though, because, then, instead) over formal transitions. Use bold sparingly. Headings only when they aid navigation. Lists only for genuinely list-shaped content. No emoji in professional or academic writing unless the audience welcomes it.

## Be Specific

Replace vague claims with concrete details (number, timeframe, before/after). A category is not an example; a named, situated person or instance is. When referencing research, name the source — or say "in my experience" / "a common pattern is" rather than pretending there's a study.

**Name the actual work, not a vague descriptor.** Credit a scholar or practitioner with the real book, paper, or work, and the chapter where it helps: "[Author], in *[Title]*", never "a writer who explored this" or "a much-loved guide".

**Reach for a real, current, local figure.** A specific, sourced statistic — with the number, the year, and the place — beats a vague trend (e.g. "in [country] in [year], X% of [population] reported Y — verify before publishing"). Verify before it ships.

## Names

Use a person's **full name every time you name them**, not the surname alone. Write "Jane Smith", never a bare "Smith", even on second and later mentions. A bare surname reads cold and assumes the reader already knows who they are. The only exception is someone genuinely on first-name terms in an internal message (a teammate), where the first name is fine; even then, never a lone surname.

## Emotional Range

Don't default flat. If something is frustrating, say so. If a result is good, say so directly. Express emotion naturally rather than describing it: "this was harder than we expected" not "this can be a challenging experience for many individuals."

## Voice Qualities

<!-- Replace with 4-6 adjectives that describe the owner's voice, plus 1-2 qualities to explicitly avoid. -->

Clear, direct, warm, structured, useful, non-performative.

## Audience Calibration

<!-- Replace these rows with the actual audiences the owner writes for and the register each requires. -->

- **[Primary audience]:** [tone/register — e.g. concise, ordered, decision-oriented]
- **[Secondary audience]:** [tone/register — e.g. clear, practical, lightly warm]
- **[External audience]:** [tone/register — e.g. accessible, concise, warm]

## Spoken and Presentation Register

For talks, training sessions, and other spoken material:

- **Stand with the room.** Prefer first-person-plural ("we look at this", "let's explore") over bare imperatives ("notice", "look") and over narrator signposts ("here's the thread").
- **Bold the one landing sentence per beat** — the line you most want to stick, not just the section labels.
- **Mark deliberate pauses for the ear.** An ellipsis to pace a line is welcome in spoken notes. Keep it off slide text.
- **Write for the ear, not the eye.** Read every draft aloud and rewrite anything that stumbles.

## Format-Specific Notes

- **Emails and messages:** open with purpose not pleasantries, put the action/decision in the first two lines, end with a concrete next step.
- **Reports and analysis:** natural voice, take positions and support them, cite real sources, active voice, say what something means not just what happened.
- **Governance and decision documents:** concise, ordered, accountable, decision-oriented; frameworks over narrative; clear recommendations.
- **Speeches and presentations:** write for the ear, shorter sentences, read aloud and rewrite anything stiff.
- **Documentation and guides:** skip the introduction, get to the task, plain language, test whether a new person could follow it.

<!-- Add further format-specific notes for any artifact types the owner produces regularly. -->

## Domain/Org Conventions

<!-- Add any house-style rules specific to your organisation or field:
- Capitalisation or pronoun conventions for particular proper nouns
- In-house terminology that differs from industry defaults
- Citation or attribution formats your field requires
- Mandatory disclaimers or disclosure language
-->

(No domain-specific conventions set yet — update this section during onboarding.)

## Structural Instincts

<!-- Describe how the owner naturally organises their thinking. Examples:
- Framework-first: clarify purpose → break into buckets → define outcomes → translate to format
- Narrative-first: context → tension → resolution
- Problem-solution: name the problem precisely → explore cause → propose fix → test
-->

[Describe the owner's default structuring approach here.]

## Before You Finish — Self-Check

1. Does every paragraph earn its place?
2. Could any sentence appear in a generic document on any topic? If yes, make it specific or cut it.
3. Are there em dashes? Replace them. (Em dashes are a strong AI-writing marker.)
4. Any banned words? Replace them.
5. Does the opening reach the point within two sentences?
6. Does the closing just repeat the opening? Rewrite or remove it.
7. Would the reader know what to do, think, or understand afterwards?
8. Does it sound like a real person wrote it?
9. Are there sentences that narrate the approach instead of doing it? Cut them.
10. Does the close add new content or just sign off with sentiment? If only sentiment, replace with a short, direct close.
11. Have you volunteered clarifications about things the reader didn't ask? Cut them. Stay scoped to what was actually raised.

## What Strong Output Feels Like

Clear, structured, proportionate, audience-fit, warm enough, direct enough, immediately usable. The reader finishes knowing what matters and can act. Another person could pick it up without confusion.

## What Weak Output Feels Like

Generic enough to apply to any organisation. Too broad, not audience-aware, not actionable. Padded to sound substantial but not grounded in context. Performs professionalism instead of carrying it.

## Quality Order

Useful first. Clear second. Polished third.

## Litmus Test

Would the owner actually use this output — in their most common artifact types — without substantially rebuilding it?

## Anti-Overfitting Guide

This file captures the owner's taste. It is not a rigid checklist. The goal is to internalise the instincts underneath their decisions, not mechanically copy patterns.

**Hard rules:** Do not be vague, fluffy, generic, or impractical. Do not write outputs that cannot survive real use.

**Strong tendencies:** Use structure. Clarify audience. Make work actionable. Prefer reusable formats. Write with warm clarity.

**Context matters.** Voice adapts by audience and artifact. Not every piece needs a table, formal headings, an executive summary, or explicit next steps. But every piece should feel like it came from someone who values clarity, usefulness, and structure.

**What matters most:**
1. Make it useful
2. Make it clear
3. Make it usable in the real world
"""


_BIN_README = (
    "# bin/\n\nPlace cadence scripts here (prune_hot_md.py, context_health.py, "
    "run_memory_gardener.sh, build_meeting_packs.sh).\n"
)

# Relative path -> initial content. memory/ is created as a directory separately.
_SCAFFOLD = {
    "state/decisions.md": _DECISIONS_MD,
    "state/hot.md": _HOT_MD,
    "MEMORY.md": _MEMORY_MD,
    "context/voice.md": _VOICE_MD,
    "bin/README.md": _BIN_README,
}


def _git(repo: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = {**os.environ, "LC_ALL": "C", "LANGUAGE": ""}
    try:
        return subprocess.run(["git", "-C", repo, *args], check=check,
                              capture_output=True, env=env)
    except FileNotFoundError:
        raise RuntimeError(
            "git is required but was not found in PATH — install git and ensure it is on the PATH used by launchd"
        )


def ensure_records_repo(repo_dir: str, *, git_name: str = "mcpbrain",
                        git_email: str = "mcpbrain@localhost",
                        profile: dict | None = None) -> str:
    """Ensure repo_dir is a git repo with the scaffold the writers expect.

    git-inits the directory if absent, sets a local git identity only if none is
    configured (never overrides the user's), stamps any missing scaffold files
    (never clobbers existing ones), and commits the scaffold on first creation.
    Idempotent; safe to call every cycle. Returns repo_dir.
    """
    repo = Path(repo_dir).resolve()
    repo_key = str(repo)
    if repo_key in _ENSURED:
        return repo_dir
    repo.mkdir(parents=True, exist_ok=True)
    fresh = not (repo / ".git").is_dir()
    if fresh:
        _git(repo_dir, "init")
    if _git(repo_dir, "config", "--local", "user.name", check=False).returncode != 0:
        _git(repo_dir, "config", "--local", "user.name", git_name)
    if _git(repo_dir, "config", "--local", "user.email", check=False).returncode != 0:
        _git(repo_dir, "config", "--local", "user.email", git_email)
    (repo / "memory").mkdir(exist_ok=True)
    newly_written: list[str] = []
    for rel, content in _SCAFFOLD.items():
        p = repo / rel
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            newly_written.append(rel)
    if profile is not None:
        for rel, tmpl in _TEMPLATE_FILES.items():
            p = repo / rel
            if not p.exists():
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(_render_template(tmpl, profile), encoding="utf-8")
                newly_written.append(rel)
    if fresh:
        _git(repo_dir, "add", "-A")
        staged = _git(repo_dir, "diff", "--cached", "--quiet", check=False).returncode != 0
        if staged:
            _git(repo_dir, "commit", "-m", "scaffold: initialize records repo")
    elif newly_written:
        _git(repo_dir, "add", *newly_written)
        staged = _git(repo_dir, "diff", "--cached", "--quiet", check=False).returncode != 0
        if staged:
            _git(repo_dir, "commit", "-m", "scaffold: add missing scaffold files")
    _ENSURED.add(repo_key)
    return repo_dir


def scaffold_records(home: str) -> list[str]:
    """Ensure + stamp the records repo from the saved profile. Degrades to [].

    Best-effort: any failure (no git, unwritable dir) returns [] and never raises,
    so a settings POST is never failed by scaffolding.
    """
    try:
        repo = config.records_dir(home)
        profile = {
            "owner_full_name": config.owner_full_name(home),
            "owner_role": config.owner_role(home),
            "orgs": config.read_config(home).get("orgs") or [],
        }
        _ENSURED.discard(str(Path(repo).resolve()))  # force a re-stamp pass
        ensure_records_repo(repo, profile=profile)
        return [str(Path(repo) / rel) for rel in _TEMPLATE_FILES]
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.debug("scaffold_records degraded: %s", exc)
        return []
