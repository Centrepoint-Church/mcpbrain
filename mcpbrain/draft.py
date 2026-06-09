"""Email draft pipeline for mcpbrain.

Entry points:
  draft_email(store, home, email_id) → dict with draft_record_id and final draft
  refine_draft(store, home, draft_record_id, refinement) → dict

Pipeline (4 stages):
  1. pretrial_and_plan  (haiku)  — intent, audience tier, key points
  2. generate_draft     (sonnet) — initial reply
  3. critique_and_revise(sonnet) — merged critique + revised draft
  4. voice_check        (sonnet) — scan for banned patterns, produce final

Helpers: _find_claude, _call_llm, _load_voice_rules, _get_email_context, _get_samples
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

from mcpbrain import config

log = logging.getLogger(__name__)

_HAIKU = "claude-haiku-4-5-20251001"
_SONNET = "claude-sonnet-4-6"
_LEAN_FLAGS = [
    "--tools", "",
    "--strict-mcp-config",
    "--mcp-config", '{"mcpServers":{}}',
    # This is an internal tool call, not a user session: never fire user hooks
    # (e.g. the SessionEnd capture would otherwise ingest a junk note per draft).
    "--settings", '{"disableAllHooks":true}',
    "--dangerously-skip-permissions",
]
_TIMEOUT = 90


def _find_claude() -> str:
    """Locate claude CLI. Checks CLAUDE_BIN env → PATH → ~/.local/bin/claude."""
    import os
    env_path = os.environ.get("CLAUDE_BIN", "")
    if env_path:
        return env_path
    found = shutil.which("claude")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "claude"
    if fallback.exists():
        return str(fallback)
    raise RuntimeError("claude CLI not found; set CLAUDE_BIN or install Claude Code")


def _call_llm(prompt: str, model: str = _SONNET, timeout: int = _TIMEOUT) -> str:
    """Run claude -p with lean flags. Returns stripped stdout. Raises RuntimeError on error/timeout."""
    claude = _find_claude()
    try:
        result = subprocess.run(
            [claude, "-p", prompt, "--model", model, "--output-format", "text"]
            + _LEAN_FLAGS,
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"claude timed out after {timeout}s") from exc
    if result.returncode != 0:
        raise RuntimeError(f"claude exited {result.returncode}: {result.stderr[:200]}")
    return result.stdout.strip()


def _parse_json(raw: str) -> dict:
    """Parse model JSON output, tolerating ```json fences the model may add despite instructions."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(text)


def _load_voice_rules(home: str) -> str:
    """Read ~/joshbrain/context/voice.md. Returns empty string if not found."""
    # assumes `home` (e.g. ~/.mcpbrain) is a subdirectory of the root holding joshbrain/
    p = Path(home).parent / "joshbrain" / "context" / "voice.md"
    try:
        return p.read_text(encoding="utf-8") if p.exists() else ""
    except OSError:
        return ""


def _get_email_context(store, email_id: str) -> dict:
    """Return email_context row for email_id, or {} if not found."""
    try:
        with store._connect() as db:
            row = db.execute(
                "SELECT * FROM email_context WHERE message_id=?", (email_id,)).fetchone()
            return dict(row) if row else {}
    except Exception as exc:
        log.warning("_get_email_context failed for %s: %s", email_id, exc)
        return {}


def _get_samples(store, thread_id: str, n: int = 3) -> str:
    """Return recent thread context summaries as a formatted string."""
    if not thread_id:
        return ""
    try:
        with store._connect() as db:
            rows = db.execute(
                "SELECT date_iso, sender, summary FROM email_context "
                "WHERE thread_id=? ORDER BY date_iso DESC LIMIT ?",
                (thread_id, n),
            ).fetchall()
            if not rows:
                return ""
            lines = [f"[{r['date_iso']}] {r['sender']}: {r['summary']}" for r in rows]
            return "\n".join(lines)
    except Exception as exc:
        log.warning("_get_samples failed: %s", exc)
        return ""


def pretrial_and_plan(email_subject: str, email_body: str,
                      sender: str, voice_rules: str) -> dict:
    """Stage 1 (Haiku): extract intent, audience tier, key points to address."""
    voice_excerpt = f"\nVoice/tone guidance (excerpt):\n{voice_rules[:500]}" if voice_rules else ""
    prompt = f"""Analyse this email and return JSON with these exact keys:
- intent: one of "reply", "acknowledge", "decline", "decide", "inform"
- audience_tier: one of "board", "staff_internal", "external", "unknown"
- key_points: list of 2-4 strings (what the reply must address)
- tone_notes: one sentence on tone to use

Email subject: {email_subject}
Sender: {sender}
Body: {email_body}
{voice_excerpt}
Return ONLY valid JSON, no markdown fences."""
    try:
        raw = _call_llm(prompt, model=_HAIKU)
        return _parse_json(raw)
    except (json.JSONDecodeError, RuntimeError) as exc:
        log.warning("pretrial_and_plan failed: %s", exc)
        return {"intent": "reply", "audience_tier": "unknown",
                "key_points": [], "tone_notes": ""}


def generate_draft(email_subject: str, email_body: str, sender: str,
                   plan: dict, voice_rules: str, samples: str,
                   owner_full_name: str = "") -> str:
    """Stage 2 (Sonnet): produce initial draft reply."""
    kp = "\n".join(f"- {p}" for p in plan.get("key_points", []))
    voice_excerpt = (voice_rules or "")[:2000]
    samples_section = f"\n\nPrior context from this thread:\n{samples}" if samples else ""
    prompt = f"""Write an email reply from {owner_full_name or "the account owner"}.

Email to reply to:
Subject: {email_subject}
From: {sender}
Body: {email_body}

Intent: {plan.get('intent', 'reply')}
Audience: {plan.get('audience_tier', 'unknown')}
Key points to address:
{kp}
Tone notes: {plan.get('tone_notes', '')}
{samples_section}

Voice and style rules (excerpt):
{voice_excerpt}

Write only the email body (no subject line, no "From:" header). Start with a salutation."""
    return _call_llm(prompt, model=_SONNET)


def critique_and_revise(draft: str, email_subject: str,
                        plan: dict, voice_rules: str) -> dict:
    """Stage 3 (Sonnet): critique and revise in one call. Returns {critique, revised_draft}."""
    voice_excerpt = (voice_rules or "")[:1500]
    prompt = f"""Review this email draft and return JSON with these exact keys:
- critique: 1-3 sentence assessment (tone, length, clarity, voice rule compliance)
- revised_draft: the improved version of the draft

Email subject: {email_subject}
Intent: {plan.get('intent', 'reply')}

Draft to review:
{draft}

Voice rules (excerpt):
{voice_excerpt}

Return ONLY valid JSON, no markdown fences."""
    try:
        raw = _call_llm(prompt, model=_SONNET)
        result = _parse_json(raw)
        if "revised_draft" not in result:
            result["revised_draft"] = draft
        return result
    except (json.JSONDecodeError, RuntimeError) as exc:
        log.warning("critique_and_revise failed: %s", exc)
        return {"critique": "Review unavailable.", "revised_draft": draft}


def voice_check(draft: str, voice_rules: str) -> dict:
    """Stage 4 (Sonnet): scan for banned patterns and return clean final. Returns {issues, clean_draft}."""
    voice_excerpt = (voice_rules or "")[:2000]
    prompt = f"""Scan this email draft for voice rule violations and return JSON with:
- issues: list of strings describing each violation found (empty list if clean)
- clean_draft: the corrected version with all violations fixed

Voice rules:
{voice_excerpt}

Draft:
{draft}

Return ONLY valid JSON, no markdown fences."""
    try:
        raw = _call_llm(prompt, model=_SONNET)
        result = _parse_json(raw)
        if "clean_draft" not in result:
            result["clean_draft"] = draft
        if "issues" not in result:
            result["issues"] = []
        return result
    except (json.JSONDecodeError, RuntimeError) as exc:
        log.warning("voice_check failed: %s", exc)
        return {"issues": [], "clean_draft": draft}


def draft_email(store, home: str, email_id: str,
                intent: str = "") -> dict:
    """Run the full 4-stage pipeline. Returns {draft_record_id, final_draft, critique, voice_issues, audience_tier}.

    Raises ValueError if the email is not found in email_context.
    """
    ctx = _get_email_context(store, email_id)
    if not ctx:
        raise ValueError(f"email {email_id} not found in email_context")

    voice_rules = _load_voice_rules(home)
    samples = _get_samples(store, ctx.get("thread_id", ""))

    plan = pretrial_and_plan(
        email_subject=ctx.get("subject", ""),
        email_body=ctx.get("summary", ""),
        sender=ctx.get("sender", ""),
        voice_rules=voice_rules,
    )
    if intent:
        plan["intent"] = intent

    initial_draft = generate_draft(
        email_subject=ctx.get("subject", ""),
        email_body=ctx.get("summary", ""),
        sender=ctx.get("sender", ""),
        plan=plan, voice_rules=voice_rules, samples=samples,
        owner_full_name=config.owner_full_name(home),
    )

    revised = critique_and_revise(
        draft=initial_draft,
        email_subject=ctx.get("subject", ""),
        plan=plan, voice_rules=voice_rules,
    )

    final = voice_check(
        draft=revised["revised_draft"],
        voice_rules=voice_rules,
    )

    samples_count = len([line for line in samples.split("\n") if line.strip()]) if samples else 0
    draft_record_id = store.save_draft(
        email_id=email_id,
        thread_id=ctx.get("thread_id", ""),
        intent=plan.get("intent", ""),
        audience_tier=plan.get("audience_tier", ""),
        draft_text=final["clean_draft"],
        critique=revised.get("critique", ""),
        voice_issues=final.get("issues", []),
        samples_used=samples_count,
        model=_SONNET,
    )

    return {
        "draft_record_id": draft_record_id,
        "final_draft": final["clean_draft"],
        "critique": revised.get("critique", ""),
        "voice_issues": final.get("issues", []),
        "audience_tier": plan.get("audience_tier", ""),
    }


def refine_draft(store, home: str, draft_record_id: int,
                 refinement: str) -> dict:
    """Re-run critique+revise+voice with a refinement note. Saves a new draft_records row.

    refinement: one of "warmer", "shorter", "firmer", or "direct_about:<topic>"
    Returns same shape as draft_email.
    Raises ValueError if draft_record_id not found.
    """
    parent = store.get_draft(draft_record_id)
    if not parent:
        raise ValueError(f"draft record {draft_record_id} not found")

    voice_rules = _load_voice_rules(home)

    refinement_note = f"\n\nRefinement instruction: {refinement}"
    plan = {"intent": parent.get("intent", "reply"),
            "audience_tier": parent.get("audience_tier", "unknown"),
            "key_points": [], "tone_notes": ""}

    revised = critique_and_revise(
        draft=parent["draft_text"],
        email_subject="",
        plan=plan,
        voice_rules=voice_rules + refinement_note,
    )

    final = voice_check(
        draft=revised["revised_draft"],
        voice_rules=voice_rules,
    )

    child_id = store.save_draft(
        email_id=parent.get("email_id", ""),
        thread_id=parent.get("thread_id", ""),
        intent=parent.get("intent", ""),
        audience_tier=parent.get("audience_tier", ""),
        draft_text=final["clean_draft"],
        critique=revised.get("critique", ""),
        voice_issues=final.get("issues", []),
        samples_used=parent.get("samples_used", 0),
        model=_SONNET,
        parent_draft_id=draft_record_id,
        refinement=refinement,
    )

    return {
        "draft_record_id": child_id,
        "final_draft": final["clean_draft"],
        "critique": revised.get("critique", ""),
        "voice_issues": final.get("issues", []),
        "audience_tier": parent.get("audience_tier", ""),
    }
