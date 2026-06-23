"""Draft critic: voice, coverage, and grounding check for email drafts — S5.

Adapted from ops-brain/src/draft_critic.py for mcpbrain's local-only stack.

Design:
  - Inline voice rules (em-dash, banned words) — no YAML dependency.
  - One LLM call via claude CLI (never Anthropic SDK).
  - Falls back to empty-violations report on any failure — never blocks the
    draft pipeline.
  - Gated on `draft_critic_enabled` config flag (default False).
  - composite_confidence() blends drafter self-score with critic findings.

Public API:
    from mcpbrain.draft_critic import critique, composite_confidence
    report = critique(draft_text, ctx, home=home)   # never raises
    conf   = composite_confidence(drafter_score, report)
"""
from __future__ import annotations

import json
import logging
import re
import subprocess

log = logging.getLogger("mcpbrain.draft_critic")

_CRITIC_TIMEOUT = 30   # seconds — generous for a structured-output call

# ---------------------------------------------------------------------------
# Inline voice rules (ported from ops-brain draft_critic._INLINE_VOICE_RULES)
# ---------------------------------------------------------------------------

_INLINE_VOICE_RULES = [
    {
        "name": "em-dash",
        "severity": "high",
        "description": (
            "Em dashes (U+2014) and en dashes (U+2013) are forbidden. "
            "Replace with comma, full stop, or parentheses."
        ),
        "exemplar_bad": "This is important — the deadline matters.",
    },
    {
        "name": "banned-word",
        "severity": "high",
        "description": (
            "Banned words must not appear: crucial, pivotal, vital, vibrant, tapestry, "
            "testament, underscore (verb), highlight (verb), foster, cultivate, garner, "
            "showcase, exemplify, delve, intricate, enduring, interplay, nestled, "
            "groundbreaking, renowned, utilize, leverage, synergy, realm, transformative, "
            "streamline, robust (non-engineering), cutting-edge, holistic, multifaceted, "
            "elevate (fig.), empower, reimagine, spearhead, enhance."
        ),
        "exemplar_bad": "This is crucial to delivering a robust outcome.",
    },
]


def _fmt_rules(rules: list[dict]) -> str:
    lines: list[str] = []
    for r in rules:
        sev = (r.get("severity") or "medium").upper()
        name = r.get("name") or ""
        desc = (r.get("description") or "").strip().replace("\n", " ")
        bad = (r.get("exemplar_bad") or "").strip()
        lines.append(f"[{sev}] {name}: {desc}")
        if bad:
            lines.append(f'  Bad: "{bad}"')
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# LLM call (subscription-only — claude CLI, never Anthropic SDK)
# ---------------------------------------------------------------------------

def _call_claude(prompt: str, timeout: int = _CRITIC_TIMEOUT) -> str:
    """Call claude CLI; return stdout or '' on any failure."""
    from mcpbrain import config
    try:
        claude = config.find_claude()
    except RuntimeError:
        return ""
    try:
        result = subprocess.run(
            [claude, "-p", prompt, "--output-format", "text"],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            log.debug("draft_critic: claude returned %d: %s",
                      result.returncode, (result.stderr or "")[:200])
            return ""
        return (result.stdout or "").strip()
    except subprocess.TimeoutExpired:
        log.debug("draft_critic: timed out after %ds", timeout)
        return ""
    except Exception as exc:  # noqa: BLE001
        log.debug("draft_critic: claude call failed: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_questions(email_summary: str) -> list[str]:
    """Extract implied questions/requests from the inbound email summary."""
    if not email_summary:
        return []
    imperative_re = re.compile(
        r"\b(please|can you|could you|let me know|send me|provide|confirm|share|update me)\b",
        re.I,
    )
    seen: set[str] = set()
    questions: list[str] = []
    for sent in re.split(r"(?<=[.!?])\s+", email_summary):
        s = sent.strip()
        if not s or len(s) < 10:
            continue
        if ("?" in s or imperative_re.search(s)) and s not in seen:
            seen.add(s)
            questions.append(s)
    return questions[:5]


# ---------------------------------------------------------------------------
# Critic prompt
# ---------------------------------------------------------------------------

_CRITIC_PROMPT = """\
You are a voice critic evaluating an email draft.

VOICE RULES (HIGH violations require revision):
{rules_block}

INBOUND EMAIL:
Subject: {subject}
Questions/requests in the inbound email: {questions}

DRAFT TO CRITIQUE:
---
{draft}
---

CONTEXT (for grounding verification):
{context_summary}

Evaluate the draft for exactly three things:
1. Voice violations — check against every rule above.
2. Coverage — does the draft address all the implied questions/requests?
3. Grounding — are any specific claims (dates, numbers, amounts, commitments)
   unsupported by the context?

Return JSON only, no other text:
{{
  "voice_violations": [
    {{"pattern": "rule-name", "excerpt": "shortest offending phrase", "severity": "high|medium|low"}}
  ],
  "coverage_issues": ["description of unanswered question or unaddressed request"],
  "grounding_issues": ["specific claim that lacks support in the context provided"],
  "independent_confidence": 0.0,
  "revise_needed": false,
  "summary": "one sentence"
}}

Rules:
- Only report genuine violations, not debatable style preferences.
- independent_confidence: your 0–1 quality score, independent of the drafter's score.
- revise_needed: true automatically if any HIGH voice violation OR any grounding_issue.
- excerpt: shortest span capturing the violation.
- Empty arrays and revise_needed=false when nothing is wrong.
"""


def _parse_report(raw: str) -> dict | None:
    try:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end < start:
            return None
        report: dict = json.loads(raw[start:end + 1])
        report.setdefault("voice_violations", [])
        report.setdefault("coverage_issues", [])
        report.setdefault("grounding_issues", [])
        report.setdefault("independent_confidence", 0.5)
        report.setdefault("revise_needed", False)
        report.setdefault("summary", "")
        # Enforce: any HIGH violation or grounding issue → revise_needed
        has_high = any(
            (v.get("severity") or "").lower() == "high"
            for v in report["voice_violations"]
        )
        if has_high or report["grounding_issues"]:
            report["revise_needed"] = True
        return report
    except Exception as exc:  # noqa: BLE001
        log.debug("draft_critic: parse failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_EMPTY_REPORT: dict = {
    "voice_violations": [],
    "coverage_issues": [],
    "grounding_issues": [],
    "independent_confidence": 0.5,
    "revise_needed": False,
    "summary": "Critic unavailable — skipped.",
}


def critique(draft_text: str, ctx: dict, home: str = "") -> dict:
    """Evaluate draft against voice/coverage/grounding rules.

    ctx: {
      "subject": str,
      "body": str,       # inbound email summary
      "sender": str,
      "voice_rules": str,
    }

    Returns a CritiqueReport dict.  Never raises.
    """
    from mcpbrain import config
    if home and not config.draft_critic_enabled(home):
        return dict(_EMPTY_REPORT, summary="draft_critic_enabled=false")

    try:
        subject = ctx.get("subject") or ""
        email_summary = ctx.get("body") or ""
        questions = _extract_questions(email_summary)
        context_summary = (
            f"Sender: {ctx.get('sender', '')}\n"
            f"Email summary: {email_summary[:300]}"
        ) if email_summary else "No additional context."

        prompt = _CRITIC_PROMPT.format(
            rules_block=_fmt_rules(_INLINE_VOICE_RULES),
            subject=subject[:200],
            questions="\n  - ".join([""] + questions) if questions else "None detected.",
            draft=draft_text[:3000],
            context_summary=context_summary[:700],
        )

        raw = _call_claude(prompt)
        if not raw:
            return dict(_EMPTY_REPORT)

        report = _parse_report(raw)
        if report is None:
            log.warning("draft_critic: no valid JSON in response")
            return dict(_EMPTY_REPORT)

        return report
    except Exception as exc:  # noqa: BLE001
        log.debug("draft_critic: critique failed: %s", exc)
        return dict(_EMPTY_REPORT)


def composite_confidence(drafter_confidence: float, report: dict) -> float:
    """Blend drafter self-score with critic findings into a composite confidence.

    Penalty model (ported from ops-brain composite_confidence):
      0.10 per HIGH voice violation
      0.03 per MEDIUM voice violation
      0.05 per grounding issue
      0.02 per coverage issue
    Blended base: 0.7 * drafter_confidence + 0.3 * critic independent_confidence.
    Result clipped to [0.10, 1.0].
    """
    violations = report.get("voice_violations") or []
    high_n = sum(1 for v in violations if (v.get("severity") or "").lower() == "high")
    medium_n = sum(1 for v in violations if (v.get("severity") or "").lower() == "medium")
    grounding_n = len(report.get("grounding_issues") or [])
    coverage_n = len(report.get("coverage_issues") or [])
    penalty = 0.10 * high_n + 0.03 * medium_n + 0.05 * grounding_n + 0.02 * coverage_n
    base = 0.7 * float(drafter_confidence) + 0.3 * float(
        report.get("independent_confidence") or 0.5
    )
    return round(max(0.10, min(1.0, base - penalty)), 4)
