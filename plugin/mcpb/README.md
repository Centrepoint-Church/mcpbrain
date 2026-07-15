# mcpbrain Desktop Extension (.mcpb)

This directory is the source for the one-click Claude Desktop Extension bundle
that connects the `brain_*` tools with no config edit and no quit/reopen.

## What it does

`manifest.json` describes a `server.type: "uv"` bundle whose `mcp_config`
runs:

```
uvx --from mcpbrain mcpbrain mcp-server
```

This is the **plain `mcpbrain`** package — no `[daemon]` extra, so `uvx`
never pulls in `fastembed`/`onnxruntime`. The bridge process is native-dep-free
(Task 4) and only talks to an **already-running mcpbrain daemon**, which it
locates via `control_port`/`control_token` written to the app dir. That means
this extension needs **no `user_config` secrets** — installing it is just
"double-click the `.mcpb` → Install".

The daemon itself (`mcpbrain` with the `[daemon]` extra, `mcpbrain setup`,
`launchctl`/scheduled-task registration, embeddings, etc.) is a **separate,
already-installed piece** — this extension is only the thin MCP bridge that
Claude Desktop launches per-session.

## Building the `.mcpb`

The packed `.mcpb` file is a **release artifact** — it is built at release
time (see `docs/RELEASE-RUNBOOK.md`), not committed to this repo.

```bash
# from the repo root, using the current @anthropic-ai/mcpb CLI:
npx @anthropic-ai/mcpb validate plugin/mcpb/manifest.json
npx @anthropic-ai/mcpb pack plugin/mcpb /path/to/output/mcpbrain.mcpb
```

`validate` checks `manifest.json` against the mcpb schema; `pack` produces
the installable bundle. Re-run both any time `manifest.json` changes.

## Keeping the version in step

`manifest.json`'s `version` field is a **fifth version-source file** (per
`docs/RELEASE-RUNBOOK.md`) and must be bumped alongside `pyproject.toml`,
`mcpbrain/__init__.py`, `plugin/.claude-plugin/plugin.json`, and
`plugin/.claude-plugin/marketplace.json` on every release.

## Schema note

This manifest targets **`manifest_version: "0.4"`** of the mcpb schema, not
the `"0.2"` used in older examples: schema `0.2`/`0.3` restrict
`server.type` to `python | node | binary` and reject `"uv"`. `"uv"` was only
added to the `server.type` enum in schema `0.4`
(`@anthropic-ai/mcpb@2.1.2`'s `schemas/mcpb-manifest-v0.4.schema.json`).
Bumping `manifest_version` to `"0.4"` is what makes
`npx @anthropic-ai/mcpb validate plugin/mcpb/manifest.json` pass with
`server.type: "uv"`.

## Manual install test (do this once per release, both OSes)

1. Install the `.mcpb` in Claude Desktop (double-click → Install).
2. With the mcpbrain daemon running, confirm `brain_search` returns results.
3. Stop the daemon and confirm the tools fail gracefully (empty/clear error,
   no crash) rather than hanging or crashing Claude Desktop.
