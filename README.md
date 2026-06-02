# mcpbrain

mcpbrain is a local-first personal knowledge daemon. It syncs your Gmail and Drive (and Calendar, if you grant it) into a SQLite store on your own machine, embeds the text for search, and serves it to Claude Desktop over MCP. You then ask Claude things like "search my brain for the campus budget", "what's the context on this person", or "who's connected to this project", and it answers from your own mail and docs, including the relationships between the people and projects it finds.

Everything stays on your laptop. Nothing is sent anywhere unless you turn on the optional encrypted backup, or you query Claude Desktop (in which case Claude Desktop sends your query to Anthropic, same as any other Claude chat).

## Install

Three commands:

```bash
git clone <repo-url>
cd mcp-ops-brain
./install/setup.sh
```

On macOS, double-click `install/setup.command` instead of the last line. On Windows, run `install/setup.ps1` in PowerShell.

Each installer does the same things:

1. Installs `uv` if it isn't already on the machine.
2. Installs the `mcpbrain` CLI as a `uv` tool.
3. Warms the embedding model (the first sync downloads a small ONNX model into your cache).
4. Registers a login agent so the daemon starts when you log in (launchd on macOS, a systemd user service on Linux, a scheduled task on Windows).
5. Registers mcpbrain with Claude Desktop and opens a browser wizard to connect your Google account.

In the wizard you sign in with your own Google account and grant read-only access to Gmail, Drive, and (optionally) Calendar. You don't create a Google Cloud project; mcpbrain ships a shared OAuth client (see the maintainer notes below). Because the app isn't through Google's verification yet, the browser shows "Google hasn't verified this app". Click Advanced, then continue. The token is written to your local data directory and the daemon picks it up.

After the wizard, fully quit and reopen Claude Desktop. The `brain_search`, `brain_read`, `brain_context`, and `brain_graph` tools are then available in any chat.

## Updating

```bash
mcpbrain update
```

This pulls the latest commits (fast-forward only, so it aborts cleanly if you have local changes), reinstalls the CLI, and restarts the login agent so the new version takes effect. If the pull can't fast-forward it stops without touching your install and tells you how to resolve it.

## What runs at login

The login agent starts the mcpbrain daemon in the background. The daemon runs a loop: it syncs new mail and docs into the local store, embeds them for search, and (if you've set a Gemini key) extracts the entity/relationship graph that powers `brain_context` and `brain_graph`. Run only one daemon at a time; it holds a single-writer lock on the store. Claude Desktop opens the store read-only, so it's safe alongside the daemon.

A second, optional login agent runs a menu-bar tray (`mcpbrain tray`). It shows whether the daemon is running or paused and how many items are indexed, and gives you Pause/Resume, Open setup, and Quit. The tray is a status-and-control client that talks to the daemon over the loopback control API; it does not own the daemon, so quitting the icon closes the menu bar item only and leaves syncing running. It comes back at your next login, or run `mcpbrain tray` to relaunch it.

## What leaves your machine

Two things, and only these:

1. The encrypted backup, if you enable it. Backup is off by default. When configured, it snapshots the derived store, encrypts it with an org-held escrow key, and uploads it to a per-user folder on a Shared Drive. The backup contains your synced mail and doc text, encrypted; only a holder of the escrow key can decrypt it.
2. Whatever Claude Desktop sends to Anthropic when you query. That's the normal Claude Desktop data path, not something mcpbrain adds.

The sync, the store, the embeddings, and the knowledge graph all stay local.

## Trust model

This is an unsigned, clone-and-run tool shared from a private repo with invited collaborators. There's no notarised binary and no app-store review between you and the code. You were invited because someone trusts you with it; you read the repo, decide to trust it, and run it. If that isn't acceptable for your machine, don't run it.

## For the maintainer: provision the shared OAuth client

Done once by the maintainer, not by each user. It creates the one OAuth client the app embeds.

1. In the Google Cloud Console, create or pick a project.
2. OAuth consent screen: User type External. Add the app name, your support and developer email, and the scopes `gmail.readonly`, `calendar.readonly`, `drive.readonly`. Save. Leave it in Testing (with test users) or publish it to "In production" unverified.
3. Credentials → Create credentials → OAuth client ID → Application type: Desktop app. Download the JSON.
4. Put it at `mcpbrain/google_oauth_client.json` (the bundled path; it's gitignored so the real client isn't committed). A desktop client's secret is non-confidential by Google's design because the flow uses PKCE, so bundling it with the app is the standard, accepted practice.

Until this file exists, the auth step raises a clear error (or falls back to a user-supplied client secret).

## Verification and the user cap

The app is shared privately with invited collaborators, who sign in as Google "test users" on the unverified consent screen. That model needs no verification and no security assessment:

- An unverified app allows about 100 test users, and each one sees the "unverified app → Advanced → Continue" warning. A collaborator set sits well inside that.
- It costs nothing.

Verification only matters if you later open the app to the general public. Because Gmail and Drive read are restricted scopes, public verification also requires an annual third-party CASA security assessment, which is a real recurring cost, so it's a deliberate business decision rather than a code change. Narrowing Drive to the non-restricted `drive.file` scope would avoid the Drive assessment, but it loses access to your existing Drive files, so it isn't suitable here.

## Licence

See `LICENSE`. The licence is a placeholder (MIT) pending confirmation.
