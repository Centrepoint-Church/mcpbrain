# UserPromptSubmit auto-recall (brain-grounded prompts) — design

**Date:** 2026-06-19
**Status:** spec (not yet implemented) — ships behind a default-OFF flag
**Owner:** Josh Kemp

## Goal

Make every prompt in a brain-connected session automatically grounded in the
brain, without the user having to ask. On `UserPromptSubmit`, run a fast
semantic recall over the brain and inject the top few hits as `additionalContext`
alongside the prompt — so Claude answers from the user's actual memory/people/
decisions by default, not from a cold model plus whatever it happens to call.

This is the single highest-leverage hook for a "brain" plugin: it turns the
brain from a set of tools Claude *may* call into ambient retrieval-augmentation
on *every* turn. It is also the easiest to get wrong (latency, noise, misleading
context), so it ships **opt-in** with hard guardrails and a measured rollout.

## Why a hook (and not "just let Claude call brain_search")

`brain_search` already exists, but it's discretionary: the model calls it when
it thinks to. In practice that means many turns answer without consulting the
brain at all, or consult it a beat too late. A `UserPromptSubmit` hook makes
recall *unconditional and pre-emptive* — the relevant memory is in context
before the first token of reasoning. The MCP tools remain for deliberate,
deeper lookups; the hook covers the "always at least glance at the brain" floor.

## Non-goals

- Not a replacement for `brain_search`/`brain_graph` — those stay for explicit,
  iterative retrieval.
- Not a writer. This hook is read-only recall; capture stays in session-end /
  pre-compact.
- Not a reranker/LLM step. The hook must be cheap and synchronous; any model
  work would blow the latency budget.

## Architecture

A `UserPromptSubmit` hook process is spawned per prompt by Claude Code. It must
be fast and must not load heavy state. The embedder (bge-small, 384-dim) costs
hundreds of ms+ to load — **far** too slow to initialise per prompt — so the
hook must NOT open its own store/embedder. Instead it reaches the already-warm
daemon over the loopback control API, exactly as `session_start` already does
for `/api/dashboard/today`.

```
Claude Code ──UserPromptSubmit(stdin JSON: {prompt,…})──▶ `mcpbrain user-prompt-submit`
                                                              │  (reads control_port/control_token)
                                                              ▼
                                              POST 127.0.0.1:<port>/api/recall  (Bearer token)
                                                              │
                                                              ▼
                                       daemon.search(query, limit)  ──▶ hybrid_search(self._store, self._embedder, …)
                                                              │
                                                              ▼
                                              {results:[{snippet,score,kind,source}…]}
                                                              │
   additionalContext  ◀──exit 0 + JSON hookSpecificOutput─────┘
```

The daemon already holds `self._store` + `self._embedder` (daemon.py:375–376)
and `hybrid_search` is the existing entry (retrieval.py:154). The endpoint is a
thin wrapper; no new retrieval logic.

### New surfaces

**1. `daemon.search(query, limit=5)`** (mcpbrain/daemon.py) — read-only:

```python
def search(self, query: str, limit: int = 5) -> list[dict]:
    """Semantic recall for the UserPromptSubmit hook. Read-only, never raises
    into the control API (returns [] on any failure)."""
    try:
        return hybrid_search(self._store, self._embedder, query, limit)
    except Exception:
        log.warning("recall search failed", exc_info=True)
        return []
```

**2. `POST /api/recall`** (mcpbrain/control_api.py) — loopback + Bearer, mirrors
the existing `/api/dashboard/*` handlers:

```python
if h.path == "/api/recall":
    q = (body.get("query") or "").strip()
    limit = min(int(body.get("limit") or 5), 10)
    return h_json(h, 200, {"results": d.search(q, limit) if q else []})
```

**3. `mcpbrain user-prompt-submit`** (new CLI subcommand → new
`mcpbrain/prompt_recall.py`, kept out of session_hooks.py so the per-prompt hot
path imports as little as possible). Sketch:

```python
def user_prompt_submit(home, stdin=None, out=None) -> None:
    stdin, out = stdin or sys.stdin, out or sys.stdout
    if not config.prompt_recall_enabled(home):      # flag OFF -> instant no-op
        return
    try:
        hook = json.loads(stdin.read() or "{}")
    except (ValueError, OSError):
        return
    prompt = (hook.get("prompt") or "").strip()
    if not _worth_recalling(prompt):                # slash cmd / too short / ack
        return
    results = _recall(home, prompt, limit=_LIMIT, timeout=_TIMEOUT_S)
    block = _format_context(results)                # thresholded, capped
    if block:
        out.write(json.dumps({"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": block}}))
```

**4. `plugin/hooks/hooks.json`** — add:

```json
"UserPromptSubmit": [
  { "hooks": [ { "type": "command", "command": "mcpbrain user-prompt-submit" } ] }
]
```

**5. `config.prompt_recall_enabled(home)`** — reads `prompt_recall` (default
`False`). Later surfaced as a wizard toggle.

## Guardrails (the part that makes this safe)

These are not optional polish — they are the difference between "ambient
grounding" and "every prompt drowns in irrelevant context."

1. **Opt-in.** Flag defaults OFF. When off the hook returns before any I/O —
   zero latency, zero behaviour change. Dogfood first; default-ON only if
   quality holds (rollout phase 3).
2. **Hard latency budget.** Connect+read timeout ~1.2s. On timeout/daemon-down/
   any error → emit nothing (fail-open). A prompt must never feel slower because
   the brain was sluggish. (The hook event default timeout is 30s; we ignore it
   and self-bound far tighter.)
3. **Relevance threshold.** Only inject results scoring above a floor, so an
   off-topic or generic prompt ("ok thanks") yields *nothing*. Better to inject
   nothing than noise. Threshold is tuned in phase 2 from real scores.
4. **Tight caps.** Top `_LIMIT` = 3–5 hits, each snippet ~200 chars, total
   injected text hard-capped (~1200 chars). The block is a *pointer set*, not a
   document dump.
5. **Skip non-substantive prompts.** No recall for: slash commands (`/…`),
   prompts under ~12 chars, or bare acknowledgements. These never benefit and
   only add cost/noise.
6. **Never block.** Always exit 0. This hook *can* block a prompt
   (`decision:"block"`); we never use that. Recall is additive or absent.
7. **Framed as optional.** The injected block carries a header that tells the
   model it's discretionary, e.g.:
   `## From your brain (possibly relevant — ignore if off-topic)`
   so a weak match can't hijack the answer.
8. **De-dup vs SessionStart (phase 2).** SessionStart already injected hot.md +
   open actions at session start. Recall hits are semantic doc matches, so
   overlap is usually low — but to be safe, phase 2 keeps a per-session
   seen-set in `MCPBRAIN_HOME/recall_seen/<session_id>.json` (cleaned on
   SessionEnd) and filters already-shown doc_ids.

## Privacy

Everything stays on-device: the prompt is POSTed only to the loopback daemon
(127.0.0.1, Bearer-token gated, same trust boundary as the existing dashboard
calls). No prompt text leaves the machine; nothing is persisted by the hook.

## Failure modes & fallbacks

| Condition | Behaviour |
|---|---|
| Flag OFF | Instant no-op, no I/O |
| Daemon down / port file missing | Emit nothing (fail-open) |
| Recall > timeout | Emit nothing |
| Zero hits above threshold | Emit nothing |
| Malformed hook JSON | Emit nothing |
| Endpoint/search raises | `daemon.search` returns `[]`; hook emits nothing |

The invariant: **a recall failure is indistinguishable from "nothing relevant"
— the prompt proceeds normally either way.**

## Testing plan

- **Unit (hook):** builds correct `additionalContext` from a fake recall
  response; skips slash commands and short prompts; respects flag-OFF without
  touching the network; fails open on timeout and on connection refused;
  enforces snippet/total caps and the relevance threshold.
- **Unit (endpoint/daemon):** `daemon.search` returns hits and returns `[]` on a
  raising store; `/api/recall` rejects without the Bearer token; empty query →
  `[]`.
- **Integration:** seed a small store, run the hook end-to-end against a live
  loopback daemon, assert relevant snippet present and irrelevant prompt yields
  nothing.
- **Latency guard:** a stubbed slow endpoint must cause the hook to return within
  the budget with empty output.

## Rollout

1. **Phase 1 — land dark.** Endpoint + hook + flag, default OFF. Author enables
   locally and dogfoods. Measure: p50/p95 hook latency, hit rate, subjective
   relevance.
2. **Phase 2 — tune.** Set the relevance threshold and caps from real score
   distributions; add per-session de-dup; consider mixing in prompt-relevant
   open actions.
3. **Phase 3 — default ON.** Add a wizard toggle; flip default to ON if quality
   holds. Keep the off switch forever.

## Open questions

- **Main session only, or subagents too?** Default to main session (no matcher
  scoping needed; subagents inherit their own retrieval via tools). Revisit if
  subagents would benefit.
- **Mix in open actions?** A prompt like "what's left on the launch?" might want
  today's actions, not just doc hits. Possibly a second recall mode in phase 2.
- **Cold-daemon latency.** First call after daemon start may be slower (warm
  caches). The fail-open budget covers correctness; worth measuring whether a
  tiny warm-up matters.
- **Threshold portability.** hybrid_search scores aren't calibrated across
  corpora; the floor may need to be relative (top-k gap) rather than absolute.

## Files touched (when implemented)

- `mcpbrain/prompt_recall.py` (new) — hook body + helpers
- `mcpbrain/cli.py` — `user-prompt-submit` subcommand
- `mcpbrain/control_api.py` — `POST /api/recall`
- `mcpbrain/daemon.py` — `search()` method
- `mcpbrain/config.py` — `prompt_recall_enabled()` accessor
- `plugin/hooks/hooks.json` — `UserPromptSubmit` entry
- `tests/test_prompt_recall.py` (new), `tests/test_control_api.py` (endpoint)
