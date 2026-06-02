# mcpbrain — manual install (alpha)

This is the hand-run path for the technical alpha, before the packaged installer (Phase 6.3) exists. It gets mcpbrain syncing your Gmail/Drive into a local store and answering queries inside Claude Desktop on a **Mac**. Windows isn't ready yet (the single-writer lock has a Windows code path but it's unverified, and there's no binary).

Everything stays on your laptop. The only data that leaves the machine is the encrypted backup (if you turn it on) and whatever Claude Desktop sends to Anthropic when you query.

## Prerequisites

- macOS (Apple Silicon or Intel).
- Python 3.12 (`python3 --version`). If missing: `brew install python@3.12`.
- Claude Desktop installed.
- The `products/mcp-ops-brain/` folder copied to the Mac (e.g. `~/mcpbrain`).

## 0. Pick a data directory

```bash
export MCPBRAIN_HOME="$HOME/.mcpbrain"   # the daemon AND the registered MCP server must share this
```

Set this in the shell that runs the daemon. On macOS the default app dir would otherwise be `~/Library/Application Support/mcpbrain`, so the `~/.mcpbrain/` paths below are only accurate once `MCPBRAIN_HOME` is exported. The register step (step 4) writes the same `~/.mcpbrain` into Claude Desktop's config as `mcpbrain_home`, so both processes use the one store.

## 1. Install

```bash
cd ~/mcpbrain          # the folder containing pyproject.toml
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[tray]"
```

First run downloads the bge-small embedding model (~130 MB) into `~/.cache`.

## 2. Authorise Google (Gmail + Drive) — one click

mcpbrain ships a shared OAuth client, so you don't create a Google Cloud project — you just consent with your own Google account. Run-once:

```bash
python -m mcpbrain.auth    # opens a browser; grant Gmail + Drive (+ Calendar) read-only
```

Because the app isn't yet through Google's verification, the browser shows **"Google hasn't verified this app"**. Click **Advanced → Continue (go to mcpbrain)**, then grant access. This is expected during the alpha (see the verification note at the bottom). The token is written to `~/.mcpbrain/google_token.json`; the daemon picks it up automatically. A token without the `calendar.readonly` scope simply skips Calendar.

(Org/advanced: to use your own OAuth client instead of the bundled one, run `python -m mcpbrain.auth --client-secrets /path/to/client_secret.json`. An explicit `--client-secrets` overrides the bundled client; just dropping a file in `~/.mcpbrain/` does not.)

## 3. Sync + embed your recent mail/docs

The daemon syncs, embeds, and (with keys) enriches and backs up on a loop. For a first run, a bounded backfill is quickest:

```bash
# sync the last ~10 days into ~/.mcpbrain/brain.sqlite3
python -m mcpbrain.daemon --once          # one sync+embed cycle, or:
python -m mcpbrain.daemon                  # run the loop (Ctrl-C to stop)
```

(If `--once` isn't wired yet, run the daemon for a few minutes then stop it.) Watch `~/.mcpbrain/` fill: the store is `brain.sqlite3`.

## 4. Register mcpbrain with Claude Desktop

This writes the `mcpbrain` server entry into Claude Desktop's config, idempotently (it preserves any other MCP servers you have):

```bash
python - <<'PY'
import sys
from pathlib import Path
from mcpbrain.wizard.register import register_mcpbrain
register_mcpbrain(
    python=sys.executable,                 # your venv python
    mcpbrain_home=str(Path.home()/".mcpbrain"),
    embedder="bge-small",
    pythonpath=str(Path.cwd()),            # the dir containing the mcpbrain package
)
print("registered")
PY
```

Then fully quit and reopen Claude Desktop. It will spawn `python -m mcpbrain.mcp_server` over stdio.

## 5. Query the brain in Claude Desktop

In a Claude Desktop chat, the `mcpbrain` tools are now available:
- `brain_search` — semantic + keyword search over your synced mail/docs.
- `brain_read` — full text of a result.
- `brain_context` — a profile of a person/org/project: who they work with, owned actions.
- `brain_graph` — the relationship graph around an entity.

Try: "search my brain for the campus budget" or "what's the context on Joel Chelliah".

## 6. (Optional) Enrichment — the knowledge graph

Enrichment extracts entities/relationships/actions/decisions into a graph (powers `brain_context`/`brain_graph`). It needs a Gemini API key. Set it before running the daemon:

```bash
export GEMINI_API_KEY=...        # your key; never commit it
python -m mcpbrain.daemon        # the loop now also enriches new chunks (and resolves duplicates)
```

Without a key, enrichment is a no-op (search still works). The first enrichment pass over a backlog can be a few hundred Gemini calls — it's cheap on flash-lite but mind your quota.

## 7. (Optional) Encrypted backup to a Shared Drive

Backup is **off** unless configured. It snapshots the derived store, encrypts it with an org-held (escrow) key, and uploads to a per-user folder on a Shared Drive. Configure the daemon with a `BackupConfig` (escrow key + Drive service + shared-drive id + your user id) and a `backup_interval_s`. See `mcpbrain/daemon.py` `BackupConfig`. **Privacy disclosure:** the backup contains your synced mail/doc text, encrypted; only a holder of the escrow key can decrypt it. The org admin holds that key for recovery.

## Notes

- **Gatekeeper (future binary):** the eventual unsigned PyInstaller build will be blocked by Gatekeeper on first launch — right-click → Open, or `xattr -dr com.apple.quarantine <app>`. Not relevant to this source install.
- **One writer:** run only one daemon at a time (it takes a single-writer lock on the store). Claude Desktop's MCP server opens the store read-only, so it's safe alongside the daemon.
- **Reset:** to start clean, stop the daemon and delete `~/.mcpbrain/brain.sqlite3` (and re-sync).

## For the maintainer: provision the shared OAuth client (one-time)

This is done once by you, not by each user. It creates the one OAuth client the app embeds.

1. In the Google Cloud Console, create (or pick) a project.
2. **OAuth consent screen** → User type **External**. Add the app name, your support + developer email, and the scopes `gmail.readonly`, `calendar.readonly`, `drive.readonly`. Save. Leave it in **Testing** (add test users) or **Publish** to "In production" unverified.
3. **Credentials → Create credentials → OAuth client ID → Application type: Desktop app.** Download the JSON.
4. Put it at `products/mcp-ops-brain/mcpbrain/google_oauth_client.json` (the bundled path; it's gitignored so the real client isn't committed). For testing you can instead point `MCPBRAIN_GOOGLE_CLIENT` at the file. A desktop client's secret is non-confidential by Google's design (the flow uses PKCE), so bundling it with the app is the standard, accepted practice.

Until this file exists, `python -m mcpbrain.auth` raises a clear error (or falls back to a user-supplied `client_secret.json`).

## Going public: verification + cap

While the app is unverified:
- Capped at **~100 users**, and each sees the "unverified app → Advanced → Continue" warning.
- Zero cost, fine for the alpha.

To remove the warning and the cap (i.e. become like ClickUp/Zapier), submit the app for **Google OAuth verification**. Because Gmail/Drive read are **restricted scopes**, that also requires an annual third-party **CASA security assessment** (a real recurring cost). This is a deliberate "go public" business step, not a code change. Narrowing Drive to the non-restricted `drive.file` scope would dodge the Drive assessment but loses access to the user's existing Drive files, so it isn't suitable here.
