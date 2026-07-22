"""UserPromptSubmit hook: inject brain recall as optional context per prompt.

Runs once per user prompt. Reads the prompt from the hook JSON on stdin, asks
the warm daemon for a few semantically-relevant snippets over the loopback
control API, and emits them as `additionalContext`. Default ON (config flag
`prompt_recall`), but fail-open in every direction: a missing flag, a slow or
down daemon, a trivial prompt, or no good hits all resolve to empty output and
exit 0 — a prompt is never blocked or noticeably delayed.

The embedder is far too slow to load per prompt, so this never opens its own
store/embedder; it always goes through the daemon, which holds both.

Relevance note: hybrid_search's score is intra-query (the top hit is ~1.0), so
the relative floor here trims the weak tail but cannot by itself suppress an
off-topic prompt — there is always a "top" hit. The optional-framing header is
the safeguard for that case (and the planned follow-up is an absolute
vector-distance gate). See the design + plan docs in docs/superpowers/specs/.
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.request
from pathlib import Path

from mcpbrain import config

_LIMIT = 4              # max hits requested from the daemon
_KEEP = 3              # max hits actually injected
_SNIPPET = 200         # max chars per injected snippet
_MAX_TOTAL = 1200      # hard cap on total injected chars
_EXPANDED_SNIPPET = 1500     # per-item cap when expansion is active (stitched context)
_EXPANDED_MAX_TOTAL = 4000   # total cap for the expanded injection block
_TIMEOUT_S = 1.2       # fail-open latency budget for the loopback call
_MIN_PROMPT = 12       # skip trivially short prompts
_REL_FLOOR = 0.55      # keep hits scoring >= this fraction of the top hit
_SEEN_TTL_S = 86400    # prune per-session seen-files older than a day

# Quote-back accept signal (S2/S4/S5): a recall counts as 'used' when the
# distinctive words of an injected snippet later reappear in the assistant's
# response — i.e. the recalled content actually flowed into the answer. This is
# a deterministic behavioural check on the transcript, not the model grading
# itself, and it only ever evaluates snippets we actually injected.
_QB_MIN_TOKENS = 5         # snippet needs this many distinctive tokens to score
_QB_THRESHOLD = 0.6        # fraction of snippet tokens that must reappear
_QB_ASSISTANT_TURNS = 2    # most-recent assistant turns to scan
_QB_MAX_CHARS = 8000       # cap on assistant text scanned per fire
_WORD_RE = re.compile(r"[a-z0-9]{4,}")

_HEADER = "## From your brain (possibly relevant — ignore if off-topic)"
_CORE_HEADER = "## Core context (always)"


def _worth_recalling(prompt: str) -> bool:
    """True for substantive prompts worth a recall. Slash commands and very
    short prompts (acks like 'yes', 'go on') never benefit and only add noise."""
    p = (prompt or "").strip()
    if len(p) < _MIN_PROMPT:
        return False
    if p.startswith("/"):
        return False
    return True


def _recall(home: str, query: str) -> list[dict]:
    """POST the prompt to the daemon's /api/recall; return hits or [] on any
    failure (daemon down, timeout, parse). Mirrors session_hooks._open_actions."""
    try:
        port = (Path(home) / "control_port").read_text().strip()
        token = (Path(home) / "control_token").read_text().strip()
    except OSError:
        return []
    expand = config.retrieval_expand_enabled(home)
    payload = json.dumps({"query": query, "limit": _LIMIT, "expand": expand}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/recall",
        data=payload, method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as r:  # noqa: S310 loopback only
            data = json.loads(r.read() or b"{}")
    except Exception:  # noqa: BLE001 — recall failure must be invisible to the prompt
        return []
    return (data or {}).get("results") or []


def _seen_path(home: str, session_id: str) -> Path:
    safe = "".join(c for c in (session_id or "x") if c.isalnum() or c in "-_")[:64] or "x"
    return Path(home) / "recall_seen" / f"{safe}.json"


def _load_seen(home: str, session_id: str, *, now: float) -> tuple[dict, Path]:
    """Return (session recall state, this session's seen-file).

    State = {"injected": {doc_id: snippet_text}, "used": [doc_ids credited]}.
    `injected` records what was actually shown (so quote-back only ever scores
    real injections) and doubles as the dedup set; `used` is the idempotency
    guard so a quoted doc is credited once. Legacy files (a bare list of ids)
    are upgraded in place. Prunes sibling seen-files older than _SEEN_TTL_S so
    the directory is self-cleaning and needs no SessionEnd coupling.
    """
    d = Path(home) / "recall_seen"
    path = _seen_path(home, session_id)
    state: dict = {"injected": {}, "used": []}
    try:
        files = list(d.glob("*.json"))
    except OSError:
        return state, path
    for f in files:
        try:
            stale = now - f.stat().st_mtime > _SEEN_TTL_S
        except OSError:
            continue
        if stale:
            try:
                f.unlink()
            except OSError:
                pass
            continue
        if f == path:
            try:
                raw = json.loads(f.read_text())
            except (OSError, ValueError):
                raw = None
            if isinstance(raw, dict):
                inj = raw.get("injected") or {}
                state["injected"] = inj if isinstance(inj, dict) else {}
                used = raw.get("used") or []
                state["used"] = list(used) if isinstance(used, list) else []
            elif isinstance(raw, list):  # legacy: bare id list, no snippet text
                state["injected"] = {i: "" for i in raw if i}
    return state, path


def _save_seen(path: Path, state: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "injected": {k: v for k, v in (state.get("injected") or {}).items() if k},
            "used": sorted(set(state.get("used") or [])),
        }))
    except OSError:
        pass


def _tokens(text: str) -> set:
    """Distinctive lowercase word tokens (len>=4) used for overlap scoring."""
    return set(_WORD_RE.findall((text or "").lower()))


def _overlap(snippet: str, response_tokens: set) -> float:
    """Containment of a snippet's distinctive tokens in the response token set.

    Returns 0.0 for snippets with too few distinctive tokens to judge reliably.
    """
    st = _tokens(snippet)
    if len(st) < _QB_MIN_TOKENS:
        return 0.0
    return len(st & response_tokens) / len(st)


def _recent_assistant_text(transcript_path: str) -> str:
    """Concatenate the last _QB_ASSISTANT_TURNS assistant text turns from the
    Claude Code transcript JSONL. Returns '' on any failure (fail-open)."""
    if not transcript_path:
        return ""
    try:
        lines = Path(transcript_path).read_text(errors="ignore").splitlines()
    except OSError:
        return ""
    texts: list[str] = []
    for line in reversed(lines):
        if len(texts) >= _QB_ASSISTANT_TURNS:
            break
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") != "assistant":
            continue
        content = (ev.get("message") or {}).get("content")
        if isinstance(content, list):
            parts = [b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text"]
            joined = " ".join(p for p in parts if p)
        elif isinstance(content, str):
            joined = content
        else:
            joined = ""
        if joined.strip():
            texts.append(joined)
    return " ".join(texts)[:_QB_MAX_CHARS]


def _detect_quoteback(home: str, transcript_path: str, state: dict,
                      session_id: str) -> list:
    """Credit injected docs whose content reappears in the assistant's recent
    response. Mutates state['used']; returns the doc_ids newly credited."""
    injected = state.get("injected") or {}
    already = set(state.get("used") or [])
    candidates = {d: t for d, t in injected.items() if d not in already and t}
    if not candidates:
        return []
    response_tokens = _tokens(_recent_assistant_text(transcript_path))
    if not response_tokens:
        return []
    newly = [d for d, text in candidates.items()
             if _overlap(text, response_tokens) >= _QB_THRESHOLD]
    if newly:
        state["used"] = sorted(already | set(newly))
    return newly


def _format_context(results: list[dict], seen: set, *, expanded: bool = False) -> tuple[str, dict]:
    """Filter, de-dup and cap recall hits into an injectable block.

    Keeps hits within _REL_FLOOR of the top score (intra-query, so the top is
    ~1.0 — this trims the weak tail, it does NOT suppress off-topic prompts),
    drops doc_ids already injected this session, caps count/snippet/total chars.
    Returns ("", {}) when nothing survives, else (block, {doc_id: snippet}).
    The snippet map is persisted so quote-back can later score what was shown.

    When `expanded` is True (the daemon stitched richer parent context), the
    per-item and total caps widen from _SNIPPET/_MAX_TOTAL to
    _EXPANDED_SNIPPET/_EXPANDED_MAX_TOTAL — everything else (rel floor, dedup,
    _KEEP, header) is unchanged.
    """
    if not results:
        return "", {}
    snip_cap = _EXPANDED_SNIPPET if expanded else _SNIPPET
    total_cap = _EXPANDED_MAX_TOTAL if expanded else _MAX_TOTAL
    top = max((float(r.get("score") or 0.0) for r in results), default=0.0)
    floor = top * _REL_FLOOR
    lines: list[str] = []
    injected: dict = {}
    total = 0
    for r in results:
        if len(lines) >= _KEEP:
            break
        doc_id = r.get("doc_id")
        if doc_id in seen:
            continue
        if float(r.get("score") or 0.0) < floor:
            continue
        snippet = " ".join((r.get("text") or "").split())[:snip_cap].strip()
        if not snippet:
            continue
        if total + len(snippet) > total_cap:
            break
        lines.append(f"- {snippet}")
        injected[doc_id] = snippet
        total += len(snippet)
    if not lines:
        return "", {}
    return _HEADER + "\n" + "\n".join(lines), injected


def _record_used(home: str, doc_ids: list, session_id: str) -> None:
    """Fire-and-forget POST of a 'used' accept signal to the daemon. Best-effort."""
    try:
        port = (Path(home) / "control_port").read_text().strip()
        token = (Path(home) / "control_token").read_text().strip()
        payload = json.dumps({"doc_ids": doc_ids, "event": "used",
                              "session_id": session_id}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/recall-feedback",
            data=payload, method="POST",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S):  # noqa: S310 loopback only
            pass
    except Exception:  # noqa: BLE001 — feedback must never disrupt a prompt
        pass


def _get_core_block(home: str) -> str:
    """Return the always-injected core block from the daemon via /api/core.

    Calls a dedicated lightweight endpoint that reads core-tier chunks from the
    store. Returns '' on any failure (daemon down, not yet configured, etc.).
    """
    try:
        from pathlib import Path as _Path
        import json as _json
        import urllib.request as _ur
        port = (_Path(home) / "control_port").read_text().strip()
        token = (_Path(home) / "control_token").read_text().strip()
        req = _ur.Request(
            f"http://127.0.0.1:{port}/api/core",
            method="GET",
            headers={"Authorization": f"Bearer {token}"},
        )
        with _ur.urlopen(req, timeout=0.5) as r:  # noqa: S310 loopback only
            data = _json.loads(r.read() or b"{}")
        return (data or {}).get("core_block") or ""
    except Exception:  # noqa: BLE001 — core block failure must be invisible
        return ""


def user_prompt_submit(home: str, stdin=None, out=None, *, now=None) -> None:
    stdin = stdin or sys.stdin
    out = out or sys.stdout
    if not config.prompt_recall_enabled(home):
        return  # flag off -> instant no-op: no stdin read, no I/O
    try:
        hook = json.loads(stdin.read() or "{}")
    except (ValueError, OSError):
        return
    prompt = (hook.get("prompt") or "").strip()
    if not _worth_recalling(prompt):
        return
    now = now if now is not None else time.time()
    session_id = hook.get("session_id") or "x"
    state, seen_path = _load_seen(home, session_id, now=now)

    # Accept signal (S2/S4/S5 keystone): credit any earlier-injected snippet whose
    # distinctive words have since reappeared in the assistant's response — the
    # recall actually flowed into the answer. A deterministic behavioural check on
    # the transcript (not the model grading itself); only scores docs we injected.
    # This is the real positive signal the bandit (S4) and lessons (S5) consume.
    if config.feedback_enabled(home):
        newly_used = _detect_quoteback(
            home, hook.get("transcript_path") or "", state, session_id)
        if newly_used:
            _record_used(home, newly_used, session_id)

    results = _recall(home, prompt)
    seen = set(state.get("injected") or {})

    # B2: prepend always-injected core block (tiered_memory flag guards internally)
    core_block = _get_core_block(home)

    expanded = config.retrieval_expand_enabled(home)
    block, injected = _format_context(results, seen, expanded=expanded)

    # Build final context: core block first (always fresh), then recall hits
    parts = [p for p in (core_block, block) if p]
    if not parts:
        # Still persist any quote-back credit recorded above.
        if injected or state.get("used"):
            _save_seen(seen_path, state)
        return

    state.setdefault("injected", {}).update(injected)
    _save_seen(seen_path, state)
    out.write(json.dumps({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": "\n\n".join(parts)}}))


def user_prompt_submit_main(argv=None) -> int:
    user_prompt_submit(str(config.app_dir()))
    return 0
