# In-Context Failure Recovery

> Spec date: 2026-06-16. Baseline: mcpbrain 0.0.6. Roadmap item #2.
> Scope note: the daily proactive brief (#1) was dropped during brainstorming — this spec covers in-context recovery only.

---

## Problem

Sync and auth failures are passive. When Google auth expires or the daemon stops, a non-technical user never notices — the only signals today are a dashboard pill and a `mcpbrain monitor` line they never look at. The brain silently stops working.

---

## Goal

When a connection is broken, surface it **inside the user's Cowork/Claude session at the moment they start working**, with a one-step fix — using the channel that already reaches them every session: the `mcpbrain session-start` hook.

---

## Design

### Extend the existing session-start hook

`mcpbrain/session_hooks.py:session_start()` already runs on every session (wired via `plugin/hooks/hooks.json`) and prints continuity + open actions to stdout, which Claude Code injects into the session.

Add a third block, printed **after** continuity and actions:

1. Call `probes.all_connections(home, store=None)` (the same cheap, no-network probes the wizard and monitor use).
2. For each probe in state `needs_action`, emit one line: a plain-language problem + the single remedy.
3. **Ignore `not_started`** — that state means the connection was never configured (mid-onboarding), not a regression. Surfacing it would nag every new user about ClickUp/backup they deliberately skipped.
4. **Bound to the top 3** issues (priority order: google, claude, daemon/records, then the rest) so the block never floods the session.
5. If nothing is in `needs_action`, print nothing — no "all healthy" noise.

### Remedy map

Each `needs_action` probe maps to one concrete, copy-pasteable remedy:

| Probe | needs_action meaning | Remedy line |
|---|---|---|
| `google` | token expired / unreadable | `Google sign-in expired → run: mcpbrain auth` |
| `claude` | MCP not seen in 14d | `Daemon/plugin not seen recently → run: mcpbrain doctor` |
| `clickup` | key present but invalid | `ClickUp key invalid → re-enter it in the mcpbrain wizard` |
| `backup` | snapshot overdue | `Backup overdue → run: mcpbrain doctor` |
| `records` | repo missing/broken | `Records repo problem → run: mcpbrain doctor` |
| `enrichment` | no enrichment in 48h | `Enrichment stalled → open Claude so the hourly task can run, or run /mcpbrain-fix` |

The exact remedy strings live in a single `_REMEDIES` dict in `session_hooks.py` so they stay consistent with `monitor.py`'s messages. The referenced entry points are the **existing** `mcpbrain auth` subcommand (`cli.py` → `auth.main`) and `mcpbrain doctor` (from the doctor spec) — no new subcommand is invented.

### Example output appended to a session

```
## ⚠️ Action needed
- Google sign-in expired → run: mcpbrain auth
- Enrichment stalled → open Claude so the hourly task can run, or run /mcpbrain-fix
```

---

## Components

**Modified: `mcpbrain/session_hooks.py`**
- New `_action_needed(home) → str` helper: calls `probes.all_connections`, filters to `needs_action`, maps via `_REMEDIES`, caps at 3, returns the block (or `""`).
- `session_start()` prints the block after the actions block.
- New module-level `_REMEDIES: dict[str, str]`.

**No `cli.py` change needed** — the remedies reference the existing `mcpbrain auth` and `mcpbrain doctor` subcommands.

**No new daemon code, no new delivery channel, no new scheduled task.**

---

## Error handling

| Failure | Behaviour |
|---|---|
| `all_connections` raises | Caught; the action-needed block is skipped. The hook never hard-fails a session (existing contract). |
| Daemon down → control API unreachable | `probe_claude`/`probe_records` already return `needs_action`/`not_started` from local files; no network dependency. |
| More than 3 issues | Show top 3 by priority; the rest are visible via `mcpbrain doctor`. |

---

## Testing

`tests/test_session_hooks.py` (extend):
- `needs_action` on google → output contains the connect-google remedy
- `not_started` on clickup/backup → those produce **no** line
- `ok` everywhere → action-needed block is empty
- >3 `needs_action` → exactly 3 lines, in priority order
- `all_connections` raising → `session_start` still completes and prints continuity/actions

---

## Out of scope

- Proactive daily brief (#1) — dropped.
- A push/notification channel beyond the session hook.
- Auto-repair — that's `mcpbrain doctor` (separate spec); here we only *surface* + point at the remedy.
