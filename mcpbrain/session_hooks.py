"""Cross-platform bodies for the mcpbrain session hooks.

session-start prints two kinds of priming context to stdout for Claude Code to
inject, gated differently:
  - a timeless policy block (how to use mcpbrain's tools/resources/write triggers)
    on EVERY SessionStart firing, including `compact` — that's exactly the moment
    early-context framing is most at risk of being dropped;
  - bounded state (recent hot.md + open actions) only at a genuine session
    boundary (startup/resume/clear) — after a `compact` the compaction summary
    already preserves the thread, so re-injecting stale continuity/actions here
    would just be noise.

session-end and pre-compact both read the hook JSON from stdin, parse the
transcript, and queue an ingest capture of the conversation (both sides, so the
enrich pipeline can summarise what was *decided/done*, not just what was asked):
  - session-end fires on a real end (it skips `resume`, which is a continuation);
  - pre-compact fires before compaction discards the full-fidelity thread.

session-end and pre-compact both read the hook JSON from stdin, parse the
transcript, and queue an ingest capture of the conversation (both sides, so the
enrich pipeline can summarise what was *decided/done*, not just what was asked):
  - session-end fires on a real end (it skips `resume`, which is a continuation);
  - pre-compact fires before compaction discards the full-fidelity thread.

All are best-effort and never hard-fail a session.
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

from mcpbrain import config, probes
from mcpbrain.capture import write_capture

_MAX_LINES = 8
_MIN_TURNS = 2          # at least this many user turns to count as "substantial"
_MIN_CHARS = 200        # ...or this much user text
# Upper bound on captured conversation text. Matches the enrich spool's per-shard
# char budget so a session lands as one shard when it fits; longer sessions keep
# the most recent tail (where decisions/outcomes concentrate) and the pipeline
# shards the rest. Far larger than the old 2000-char user-only clip on purpose.
_MAX_CONTENT = 24000

# In-context recovery: each needs_action probe maps to one copy-pasteable remedy.
# Strings are kept here (single source) so they stay consistent with monitor.py.
# NOTE: `mcpbrain doctor` and `/mcpbrain-fix` are named as text only — this module
# must never import or call them. `mcpbrain auth` already exists in cli.py.
_REMEDIES: dict[str, str] = {
    "google": "Google sign-in expired → run: mcpbrain auth",
    "claude": "Daemon/plugin not seen recently → run: mcpbrain doctor",
    "backup": "Backup overdue → run: mcpbrain doctor",
    "records": "Records repo problem → run: mcpbrain doctor",
    "enrichment": (
        "Enrichment stalled → open Claude so the hourly task can run, or run /mcpbrain-fix"
    ),
}

# Priority for the action-needed block: google, claude, then daemon/records, then the rest.
_REMEDY_PRIORITY: tuple[str, ...] = (
    "google",
    "claude",
    "records",
    "backup",
    "enrichment",
)

_MAX_ACTIONS = 3

# Open-actions selection. The injected block is capped at _MAX_LINES, and it used
# to be filled first-come across buckets in order — which meant a long `overdue`
# list made `due_today`/`upcoming` structurally unreachable. On the live store
# that was the normal case (20 overdue, all 2018-2023, vs 3 genuinely due today),
# so today's work was never injected at all. Each bucket now reserves slots, and
# long-dead items are dropped before the budget is spent on them.
_BUCKET_ORDER: tuple[str, ...] = ("due_today", "overdue", "upcoming")
_BUCKET_QUOTA: dict[str, int] = {"due_today": 3, "overdue": 3, "upcoming": 2}
# An overdue deadline older than this is almost certainly dead or superseded
# rather than a live task (the live store had items 3-8 years past due). This is
# a DISPLAY filter for the injected block only — nothing is mutated or deleted,
# and the dashboard still shows everything.
#
# Kept in step with store.archive_stale_actions' overdue_cutoff_days: that daily
# sweep is what actually retires this debris, so this is only a backstop for the
# window before it runs. A TIGHTER value here actively backfires — the dashboard
# hands us the 20 OLDEST overdue rows (dashboard.py sorts ascending, then caps),
# so a filter stricter than the sweep drops every row it is given and starves the
# overdue bucket to nothing while genuinely recent misses sit further down the
# uncapped list, never seen.
_STALE_AFTER_DAYS = 730

# The mcpbrain MCP server's connect-time `instructions` (rendered by
# render_project_instructions) already ask Claude to use these tools/resources,
# but that text is easy to lose among everything else in a long system prompt.
# This block re-states it at high salience, close to where the model actually
# reads system-reminders — because in practice these were only used when
# explicitly asked for, not proactively. It's timeless framing, not session
# state, so session_start() prints it on EVERY firing including `compact` (see
# module docstring) — unlike the resource mechanism (below), a resource is
# never force-injected, so this hook is the only reliable place to say so.
_TOOL_REMINDER = """\
## Use mcpbrain proactively
- Identity/voice/preferences/reference/decisions: read directly via the mcpbrain @-resources \
(context/voice.md, identity.md, preferences.md, reference/*, decisions.md) — no tool call \
needed. Apply voice to everything you produce for me: emails, documents, slides, any deliverable.
- Recall: these are deferred tools — load once via ToolSearch("select:mcp__mcpbrain__brain_search,\
mcp__mcpbrain__brain_context,mcp__mcpbrain__brain_actions,mcp__mcpbrain__brain_graph") then call \
directly for the rest of the session. Don't wait to be asked: check brain_search / brain_context \
whenever a question touches a person, org, project, decision, or anything that might already be \
in memory — even in passing.
- Keep the brain current as we work, unprompted: a decision that changes how things are done -> \
brain_decision; a "just decided / where we're up to" note -> brain_note; a durable learning, \
preference, or fact worth keeping -> brain_memory_write; a system/project materially changing -> \
propose an edit to the matching reference file. Never hand-edit records/context or records/state \
files directly — they're daemon-managed; always go through these write tools.
"""


def session_start(home: str, out=None, source: str = "startup") -> None:
    out = out or sys.stdout
    # Timeless policy framing — prints on every firing, including `compact`,
    # since compaction is exactly when early-context instructions risk being
    # dropped and a re-assertion matters most.
    print(_TOOL_REMINDER, file=out)
    # SessionStart also fires for startup | resume | clear | compact. The
    # continuity/actions state below belongs only at a genuine boundary
    # (startup/resume/clear): after a compaction the session is mid-flight, the
    # compaction summary preserves the thread, and pre-compact has separately
    # captured the full-fidelity history to the brain — so re-injecting stale
    # continuity + actions here would just be noise. Stay silent for those.
    if source == "compact":
        return
    print("## Recent continuity (hot.md)", file=out)
    try:
        hot = Path(config.records_dir(home)) / "state" / "hot.md"
        lines = [ln for ln in hot.read_text().splitlines()
                 if ln.startswith("- **20")][:_MAX_LINES]
        print("\n".join(lines) if lines else "(none)", file=out)
    except OSError:
        print("(none)", file=out)
    print("\n## Open actions", file=out)
    print(_open_actions(home), file=out)
    block = _action_needed(home)
    if block:
        print("\n" + block, file=out)


def _is_stale(deadline, *, today: date) -> bool:
    """True when a deadline is so far past that the item is almost certainly dead.

    Missing or unparseable deadlines are never stale — fail-open, because hiding
    a real action over a date-parse quirk is worse than showing an old one.
    """
    if not deadline:
        return False
    try:
        d = date.fromisoformat(str(deadline)[:10])
    except (ValueError, TypeError):
        return False
    return (today - d).days > _STALE_AFTER_DAYS


def _select_actions(actions: dict, *, today: date, limit: int = _MAX_LINES) -> list[str]:
    """Pick up to `limit` action lines, allocating slots fairly across buckets.

    Filters, in order: drop long-dead items (see _is_stale), de-duplicate on
    normalised text (the same action extracted from two emails otherwise burns
    two slots on one task), then take each bucket's reserved quota. Unused quota
    is redistributed round-robin so a short bucket doesn't waste the budget.
    Output stays grouped in _BUCKET_ORDER.
    """
    seen: set[str] = set()
    fresh: dict[str, list[str]] = {}
    for bucket in _BUCKET_ORDER:
        kept: list[str] = []
        for x in (actions.get(bucket) or []):
            if not isinstance(x, dict):
                continue
            text = " ".join((x.get("text") or "").split())
            if not text:
                continue
            key = text.casefold()
            if key in seen or _is_stale(x.get("deadline"), today=today):
                continue
            seen.add(key)
            kept.append(f"- [{bucket}] {text[:80]}")
        fresh[bucket] = kept

    counts = {b: min(len(fresh[b]), _BUCKET_QUOTA.get(b, 0)) for b in _BUCKET_ORDER}
    while sum(counts.values()) < limit:
        grew = False
        for b in _BUCKET_ORDER:
            if sum(counts.values()) >= limit:
                break
            if counts[b] < len(fresh[b]):
                counts[b] += 1
                grew = True
        if not grew:
            break  # every bucket exhausted -> budget can't be filled further

    out: list[str] = []
    for b in _BUCKET_ORDER:
        out.extend(fresh[b][:counts[b]])
    return out[:limit]


def _open_actions(home: str, *, today: date | None = None) -> str:
    today = today or datetime.now(timezone.utc).date()
    try:
        port = (Path(home) / "control_port").read_text().strip()
        token = (Path(home) / "control_token").read_text().strip()
    except OSError:
        return "(actions unavailable)"
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/dashboard/today",
        headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=3) as r:  # noqa: S310 loopback only
            data = json.loads(r.read() or b"{}")
    except Exception:  # noqa: BLE001 — daemon down / no dashboard
        return "(actions unavailable)"
    actions = (data or {}).get("actions", {}) or {}
    rows = _select_actions(actions, today=today)
    return "\n".join(rows) if rows else "(no open actions)"


def _action_needed(home: str) -> str:
    """Build the in-context recovery block: one remedy per needs_action probe.

    Returns the formatted block, or "" when nothing needs action. Never raises:
    if all_connections blows up, the caller still gets "" and the session is fine.
    not_started is deliberately ignored (mid-onboarding, not a regression).
    """
    try:
        conns = probes.all_connections(home, store=None) or {}
    except Exception:  # noqa: BLE001 — surfacing must never hard-fail the session
        return ""
    broken = [name for name, c in conns.items()
              if isinstance(c, dict) and c.get("state") == "needs_action"
              and name in _REMEDIES]

    def _rank(name: str) -> int:
        return _REMEDY_PRIORITY.index(name) if name in _REMEDY_PRIORITY else len(_REMEDY_PRIORITY)

    broken.sort(key=_rank)
    lines = [f"- {_REMEDIES[name]}" for name in broken[:_MAX_ACTIONS]]
    if not lines:
        return ""
    return "## ⚠️ Action needed\n" + "\n".join(lines)


def _read_transcript_turns(transcript_path: str) -> list[tuple[str, str]]:
    """Return [(role, text), ...] for user+assistant turns in a transcript JSONL.

    Empty list on any read/parse failure (missing file, bad JSON) so callers
    degrade to "nothing to capture" rather than raising into a hook.
    """
    try:
        raw = Path(transcript_path).read_text()
    except OSError:
        return []
    turns: list[tuple[str, str]] = []
    for line in raw.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        msg = ev.get("message") or {}
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        c = msg.get("content")
        if isinstance(c, str):
            text = c
        elif isinstance(c, list):
            text = " ".join(b.get("text", "") for b in c if isinstance(b, dict))
        else:
            text = ""
        text = text.strip()
        if text:
            turns.append((role, text))
    return turns


def _capture_session(home: str, transcript_path: str, *, session_id: str,
                     source: str, tags: str = "session") -> None:
    """Queue an ingest capture of a conversation for the enrich pipeline.

    Captures BOTH sides of the conversation: decisions and outcomes live in the
    assistant's replies, not just the user's prompts, so a user-only clip can't
    summarise what was actually done. "Substantial" is still gated on user
    engagement (turns/chars) so an assistant being verbose on a trivial prompt
    doesn't trip a capture. Best-effort: any failure is swallowed.
    """
    if not transcript_path:
        return
    turns = _read_transcript_turns(transcript_path)
    user_turns = [t for r, t in turns if r == "user"]
    joined_user = " ".join(user_turns)
    if len(user_turns) < _MIN_TURNS and len(joined_user) < _MIN_CHARS:
        return  # trivial / headless single-shot -> skip
    body = "\n\n".join(f"{role}: {text}" for role, text in turns).strip()
    if not body:
        return  # no usable text -> nothing to capture
    if len(body) > _MAX_CONTENT:
        body = body[-_MAX_CONTENT:]  # keep the most recent context
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    envelope = {
        "kind": "ingest",
        "source": source,
        "captured_at": stamp,
        "title": f"Session {session_id[:8]} {stamp[:10]}",
        "content": body,
        "tags": tags,
        "observation_type": "note",
    }
    try:
        write_capture(home, envelope)
    except (ValueError, OSError):
        return


def session_end(home: str, stdin=None) -> None:
    stdin = stdin or sys.stdin
    try:
        hook = json.loads(stdin.read() or "{}")
    except (ValueError, OSError):
        return
    # reason ∈ {clear, resume, logout, prompt_input_exit, bypass_permissions_disabled, other}.
    # `resume` is not a real end — the conversation is being handed off and will
    # end again later; capturing now would just double-capture. Every other
    # reason (and a missing one) is a genuine end worth capturing.
    if hook.get("reason") == "resume":
        return
    _capture_session(
        home, hook.get("transcript_path") or "",
        session_id=hook.get("session_id") or "unknown",
        source="session_end_hook")


def pre_compact(home: str, stdin=None) -> None:
    """Capture the full-fidelity thread BEFORE compaction summarises it away.

    Auto-compaction replaces the detailed transcript with a lossy summary. If
    the session later ends, session_end only sees that compacted transcript —
    so the decisions/facts from the pre-compaction thread would be lost. This
    snapshots them to the brain first. Tagged distinctly so the two captures of
    one long session are distinguishable downstream.
    """
    stdin = stdin or sys.stdin
    try:
        hook = json.loads(stdin.read() or "{}")
    except (ValueError, OSError):
        return
    _capture_session(
        home, hook.get("transcript_path") or "",
        session_id=hook.get("session_id") or "unknown",
        source="pre_compact_hook", tags="session,pre-compact")


def _read_hook_source(stdin) -> str:
    """SessionStart 'source' (startup|resume|clear|compact) from the hook JSON.

    Defaults to 'startup' when there's no hook JSON on stdin — e.g. a human
    running `mcpbrain session-start` in a terminal to preview the priming block,
    where reading stdin would otherwise block on the tty.
    """
    try:
        if stdin.isatty():
            return "startup"
        hook = json.loads(stdin.read() or "{}")
    except (ValueError, OSError, AttributeError):
        return "startup"
    return hook.get("source") or "startup"


def session_start_main(argv=None) -> int:
    session_start(str(config.app_dir()), source=_read_hook_source(sys.stdin))
    return 0


def session_end_main(argv=None) -> int:
    session_end(str(config.app_dir()))
    return 0


def pre_compact_main(argv=None) -> int:
    pre_compact(str(config.app_dir()))
    return 0
