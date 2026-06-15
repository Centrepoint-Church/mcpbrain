---
name: mcpbrain-draft-reply
description: Draft a reply to an email using the 4-stage plan/draft/critique/voice pipeline, then save it to the brain.
---

# mcpbrain-draft-reply

Draft a contextual reply to an email using a four-stage pipeline.

## How to invoke

Provide an `email_id` (message_id from the brain) and optionally an `intent` hint.

## Pipeline

### Stage 1 — Plan

Call `brain_draft_context(email_id, intent)` to load the email context.

Analyse the context and determine:

- **Intent:** one of "reply", "acknowledge", "decline", "decide", "inform"
- **Audience tier:** one of "board", "staff_internal", "external", "unknown"
- **Key points:** 2–4 things the reply must address
- **Tone notes:** one sentence on tone (informed by `voice_rules` from the context)

Use the `voice_rules` excerpt returned by `brain_draft_context` to guide the tone notes. State the intent, audience tier, key points, and tone notes before proceeding.

### Stage 2 — Draft

Write the initial email reply from the account owner.

Use:
- `body` and `sender` from the context as the email you are replying to
- `key_points`, `audience_tier`, and `tone_notes` from Stage 1
- `voice_rules` for style guidance
- `samples` for thread context (recent messages in this thread)

Write only the email body. Start with a salutation. Do not include a subject line or a From: header.

### Stage 3 — Critique and revise

Review the Stage 2 draft for:
- **Tone** — appropriate for audience tier and the relationship
- **Length** — too long or too short for the intent
- **Clarity** — is the reply unambiguous?
- **Voice rule compliance** — does it match the owner's voice rules?

Write a 1–3 sentence critique, then produce a revised draft that addresses every issue raised.

### Stage 4 — Voice-check

Scan the revised draft for voice rule violations — banned phrases, wrong register, formulaic openers, incorrect length for the intent. List any violations found. Produce a clean final draft with all violations corrected.

## Persist and present

Present the final draft to the user.

Then call:

```
brain_draft_save(email_id, thread_id, intent, final_draft)
```

Report the `draft_record_id` returned from the result.

## Refinement

After presenting, offer: "To refine this draft, tell me: **warmer** / **shorter** / **firmer** / or describe a specific change — I'll re-run from Stage 3."

On receiving a refinement instruction, re-run Stages 3–4 on the existing draft, incorporating the instruction, then call `brain_draft_save` again with `parent_draft_id` set to the previous `draft_record_id`.

## MCP tools

This skill uses two MCP tools registered by mcpbrain:

- `brain_draft_context(email_id, intent="")` — returns `{subject, body, sender, thread_id, voice_rules, samples, intent}`
- `brain_draft_save(email_id, thread_id, intent, final_draft, parent_draft_id=None)` — returns `{draft_record_id}`
