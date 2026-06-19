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
import sys
import time
import urllib.request
from pathlib import Path

from mcpbrain import config

_LIMIT = 4              # max hits requested from the daemon
_KEEP = 3              # max hits actually injected
_SNIPPET = 200         # max chars per injected snippet
_MAX_TOTAL = 1200      # hard cap on total injected chars
_TIMEOUT_S = 1.2       # fail-open latency budget for the loopback call
_MIN_PROMPT = 12       # skip trivially short prompts
_REL_FLOOR = 0.55      # keep hits scoring >= this fraction of the top hit
_SEEN_TTL_S = 86400    # prune per-session seen-files older than a day

_HEADER = "## From your brain (possibly relevant — ignore if off-topic)"


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
    payload = json.dumps({"query": query, "limit": _LIMIT}).encode()
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


def _load_seen(home: str, session_id: str, *, now: float) -> tuple[set, Path]:
    """Return (doc_ids already injected this session, this session's seen-file).

    Prunes sibling seen-files older than _SEEN_TTL_S on the way through, so the
    directory is self-cleaning and needs no SessionEnd coupling.
    """
    d = Path(home) / "recall_seen"
    path = _seen_path(home, session_id)
    seen: set = set()
    try:
        files = list(d.glob("*.json"))
    except OSError:
        return seen, path
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
                seen = set(json.loads(f.read_text()) or [])
            except (OSError, ValueError):
                seen = set()
    return seen, path


def _save_seen(path: Path, ids) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sorted(i for i in ids if i)))
    except OSError:
        pass


def _format_context(results: list[dict], seen: set) -> tuple[str, list]:
    """Filter, de-dup and cap recall hits into an injectable block.

    Keeps hits within _REL_FLOOR of the top score (intra-query, so the top is
    ~1.0 — this trims the weak tail, it does NOT suppress off-topic prompts),
    drops doc_ids already injected this session, caps count/snippet/total chars.
    Returns ("", []) when nothing survives.
    """
    if not results:
        return "", []
    top = max((float(r.get("score") or 0.0) for r in results), default=0.0)
    floor = top * _REL_FLOOR
    lines: list[str] = []
    used_ids: list = []
    total = 0
    for r in results:
        if len(lines) >= _KEEP:
            break
        doc_id = r.get("doc_id")
        if doc_id in seen:
            continue
        if float(r.get("score") or 0.0) < floor:
            continue
        snippet = " ".join((r.get("text") or "").split())[:_SNIPPET].strip()
        if not snippet:
            continue
        if total + len(snippet) > _MAX_TOTAL:
            break
        lines.append(f"- {snippet}")
        used_ids.append(doc_id)
        total += len(snippet)
    if not lines:
        return "", []
    return _HEADER + "\n" + "\n".join(lines), used_ids


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
    results = _recall(home, prompt)
    if not results:
        return
    now = now if now is not None else time.time()
    seen, seen_path = _load_seen(home, hook.get("session_id") or "x", now=now)
    block, used_ids = _format_context(results, seen)
    if not block:
        return
    _save_seen(seen_path, seen | set(used_ids))
    out.write(json.dumps({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": block}}))


def user_prompt_submit_main(argv=None) -> int:
    user_prompt_submit(str(config.app_dir()))
    return 0
