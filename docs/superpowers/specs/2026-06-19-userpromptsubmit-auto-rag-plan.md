# UserPromptSubmit auto-recall — implementation plan (one session)

**Date:** 2026-06-19
**Status:** implemented (2026-06-19) — shipped default-ON

> Implemented in one session. Deviation from the spec: the `prompt_recall` flag
> ships **default-ON** (a brain-connected session is grounded out of the box;
> set `prompt_recall: false` to disable). Because hybrid_search's score is
> intra-query (top hit ~1.0, not cross-query comparable), an absolute relevance
> floor can't suppress off-topic prompts — so the implemented safeguards are a
> *relative* tail-trim (`_REL_FLOOR`), tight caps, per-session de-dup, and the
> "ignore if off-topic" framing header. First follow-up: an absolute
> vector-distance gate for true off-topic suppression.
**Spec:** [2026-06-19-userpromptsubmit-auto-rag.md](./2026-06-19-userpromptsubmit-auto-rag.md)

One phase, one sitting. Ship the whole feature end-to-end — endpoint, hook, flag,
all guardrails, lightweight per-session de-dup, and tests — in a single session.
The `prompt_recall` flag stays as a permanent safety switch (default OFF), not a
rollout stage: the code is complete on day one; the flag just gates whether it
runs. No "tune later" deferrals — the threshold/caps/de-dup are all in this build.

Build order is bottom-up so each step is testable before the next depends on it.

---

## Step 1 — config flag (`mcpbrain/config.py`)

Add next to the other `owner_*` accessors:

```python
def prompt_recall_enabled(home) -> bool:
    """Whether the UserPromptSubmit hook injects brain recall (default off).
    A permanent safety switch — the hook returns instantly when this is false."""
    return bool(read_config(home).get("prompt_recall", False))
```

**Accept:** `prompt_recall_enabled` returns False on empty config, True when set.

## Step 2 — daemon search method (`mcpbrain/daemon.py`)

`hybrid_search` is already imported in mcp_server but not daemon — import it in
`daemon.py` (top, with the other `from mcpbrain.retrieval import …` or add one).
Add a read-only method on `Daemon` (near `status`/`config_profile`):

```python
def search(self, query: str, limit: int = 5) -> list[dict]:
    """Semantic recall for the UserPromptSubmit hook. Read-only; never raises
    into the control API (returns [] on any failure)."""
    try:
        return hybrid_search(self._store, self._embedder, query, limit)
    except Exception:  # noqa: BLE001 — recall must never break a prompt
        log.warning("recall search failed", exc_info=True)
        return []
```

**Accept:** `daemon.search("x")` returns a list; returns `[]` when the store raises.

## Step 3 — loopback endpoint (`mcpbrain/control_api.py`)

In the POST handler block (alongside `/api/sync-now`, `/api/session/ingest`),
add — Bearer-token-gated like its neighbours:

```python
if h.path == "/api/recall":
    q = (body.get("query") or "").strip()
    limit = min(int(body.get("limit") or 5), 10)
    return h_json(h, 200, {"results": d.search(q, limit) if q else []})
```

**Accept:** POST with token + `{"query":"…"}` returns `{"results":[…]}`; empty
query → `{"results":[]}`; missing/bad token → rejected (existing auth path).

## Step 4 — the hook (`mcpbrain/prompt_recall.py`, new)

Kept out of `session_hooks.py` so the per-prompt hot path imports the bare
minimum (`json`, `urllib`, `config`). Constants tuned conservatively; revisit
only from real data.

```python
_LIMIT       = 4       # max hits requested
_SNIPPET     = 200     # max chars per hit
_MAX_TOTAL   = 1200    # hard cap on injected chars
_TIMEOUT_S   = 1.2     # fail-open budget
_MIN_PROMPT  = 12      # skip trivially short prompts
_MIN_SCORE   = 0.0     # relevance floor — set from phase-1 scores before default-ON
_SEEN_TTL_S  = 86400   # prune per-session seen-files older than this
```

Functions:

- `_worth_recalling(prompt) -> bool` — False for empty, `len < _MIN_PROMPT`, or
  prompts starting with `/` (slash commands).
- `_recall(home, query) -> list[dict]` — read `control_port`/`control_token`
  from `home`, POST `/api/recall` with `_TIMEOUT_S`, return `results` or `[]` on
  any error (daemon down, timeout, parse). Mirrors `session_hooks._open_actions`.
- `_seen(home, session_id) -> (set, path)` and `_remember(path, set, ids)` —
  a tiny JSON file `MCPBRAIN_HOME/recall_seen/<sid>.json`; prune sibling files
  older than `_SEEN_TTL_S` on read (no SessionEnd coupling — self-cleaning).
- `_format_context(results, seen) -> str` — drop hits below `_MIN_SCORE` and any
  doc_id in `seen`; cap to `_LIMIT`; truncate each snippet to `_SNIPPET`; stop at
  `_MAX_TOTAL`; return "" if nothing survives. Header frames it as optional:
  `## From your brain (possibly relevant — ignore if off-topic)`.
- `user_prompt_submit(home, stdin=None, out=None)` — orchestrates: flag check →
  parse → `_worth_recalling` → `_recall` → de-dup/format → emit
  `{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":block}}`.
  Always exit 0; never `decision:"block"`.
- `user_prompt_submit_main(argv=None) -> int`.

**Accept:** flag-OFF returns before any I/O; slash/short prompts skip; timeout and
connection-refused yield empty output; caps + threshold + de-dup enforced.

## Step 5 — CLI wiring (`mcpbrain/cli.py`)

Add `"user-prompt-submit"` to the subparser-name tuple and the dispatch dict:

```python
"user-prompt-submit": lambda: __import__(
    "mcpbrain.prompt_recall", fromlist=["user_prompt_submit_main"]
).user_prompt_submit_main(rest),
```

**Accept:** `echo '{"prompt":"hi"}' | mcpbrain user-prompt-submit` exits 0.

## Step 6 — plugin hook (`plugin/hooks/hooks.json`)

```json
"UserPromptSubmit": [
  { "hooks": [ { "type": "command", "command": "mcpbrain user-prompt-submit" } ] }
]
```

**Accept:** `test_plugin_manifest.py` still passes; valid JSON.

## Step 7 — tests

`tests/test_prompt_recall.py` (monkeypatch `_recall` to a fake; never hit the
network):
- flag OFF → no read of stdin, no output;
- `_worth_recalling`: rejects `""`, `"hi"`, `"/clear"`; accepts a real question;
- `_format_context`: applies threshold, count cap, snippet truncation, total cap,
  de-dups against `seen`, returns "" when nothing survives;
- `user_prompt_submit`: builds correct `additionalContext` JSON from fake hits;
- fail-open: `_recall` returning `[]` (simulated timeout/daemon-down) → empty out;
- de-dup: a doc_id shown once isn't re-injected on the next prompt in-session.

`tests/test_control_api.py` (extend): `/api/recall` returns daemon hits with the
token and is rejected without it.

**Accept:** `uv run pytest tests/test_prompt_recall.py tests/test_control_api.py
tests/test_cli.py tests/test_plugin_manifest.py -q` green.

## Step 8 — verify & enable for dogfooding

1. Full hook/CLI suite green (step 7 + `test_session_hooks.py`).
2. Manual loopback smoke against the live daemon:
   `printf '{"prompt":"who is on the launch team?","session_id":"s"}' | mcpbrain user-prompt-submit`
   → with `prompt_recall:true` in config, prints an `additionalContext` block;
   with it false/absent, prints nothing.
3. Set `prompt_recall: true` locally, use a session, sanity-check latency
   (should be imperceptible) and relevance. Set `_MIN_SCORE` from observed
   scores before considering a default-ON flip (separate change).

---

## Out of scope for this session

- Flipping the default to ON / wizard toggle — deliberate later change once
  `_MIN_SCORE` is calibrated.
- Mixing prompt-relevant open actions into the recall (possible follow-up).
- Subagent scoping — main session only for now.

## Definition of done

`prompt_recall` defaults OFF and is a true no-op when off; when on, every
substantive prompt gets ≤4 thresholded, capped, de-duped brain snippets injected
as optional context; any failure or slow daemon is invisible (empty output, exit
0); tests cover flag-gating, skip rules, formatting/caps, de-dup, and fail-open;
the loopback endpoint is token-gated. All in one commit.
```
