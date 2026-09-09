# Forking mcpbrain for another organisation

mcpbrain is built for one organisation per build. Everything organisation-specific
lives in a **tenant profile**: `mcpbrain/tenant.json` (committed, no secrets) plus
`mcpbrain/google_oauth_client.json` (never committed — it lives in a private tenant
repo and is copied in before a build).

A build carrying no profile **disables** fleet sync, backup upload and auto-update.
It never falls back to the upstream organisation's infrastructure. That is
deliberate: it is what stops a fork silently writing its health beacons and
encrypted backups into someone else's Shared Drive.

`docs/DISTRIBUTION.md` explains why the distribution works this way;
`docs/RELEASE-RUNBOOK.md` is the release procedure. This document is the one-time
setup that comes before both.

## 1. Google Cloud

1. Create a Google Cloud project.
2. Enable the **Gmail API**, **Google Calendar API** and **Google Drive API**.
3. Configure the OAuth consent screen as **Internal**. Internal restricts consent to
   your own Google Workspace, which means no verification review, no 100-user cap,
   and no stranger can run a consent flow that renders under your organisation's
   name. Choose External only if you genuinely need accounts outside your Workspace,
   and understand that this is a phishing surface.
4. Add these scopes (they are `auth.CONSENT_SCOPES` in the code):
   `gmail.readonly`, `calendar.readonly`, `drive.readonly`, `drive.file`,
   `userinfo.email`, `userinfo.profile`.
5. Create an OAuth client of type **Desktop app** and download the JSON. It must be
   Desktop: a `web` client fails at consent with `redirect_uri_mismatch`.

## 2. Google Drive

Create a **Shared Drive** (not a My Drive folder — the `drive.file` scope cannot
write to My Drive), then two folders inside it:

- a fleet folder, for per-user health beacons and `org-config.json`
- an escrow folder, for per-user encrypted backup snapshots

Record both folder ids from their URLs. To run without fleet sync or backup upload,
leave the corresponding profile fields blank.

## 3. GitHub

- Fork or copy `mcpbrain` — your source repo.
- Create `<your-org>/mcpbrain-dist`, **public**, with GitHub Pages enabled on
  `main` / root. This serves your wheel index.
- Create `<your-org>/mcpbrain-plugin`, private, for the plugin mirror.
- Create `<your-org>/mcpbrain-tenant`, **private**, holding
  `google_oauth_client.json` (the client you downloaded in step 1) and a reference
  copy of your `tenant.json`.

## 4. Fill in the profile

Copy `mcpbrain/tenant.example.json` to `mcpbrain/tenant.json` and replace every
value. The template's placeholders are rejected by the checker, so a half-filled
profile fails loudly rather than half-working.

**Then re-point the five install surfaces to match.** These carry runnable
commands, so they name your marketplace and index directly rather than reading the
profile at runtime — but `bin/tenant.py check` requires them to *agree* with
`tenant.json`, and will tell you exactly which one is out of step:

| File | What to change |
|---|---|
| `plugin/.claude-plugin/marketplace.json` | `name` → your `marketplace_name` |
| `plugin/.claude-plugin/plugin.json` | `homepage` → `https://github.com/<owner>/<repo>` |
| `plugin/scripts/install.ps1` | `$INDEX` → your `index_url` |
| `plugin/commands/install.md` | the `--index` URL → your `index_url` |
| `plugin/INSTALL.md` | both `claude plugin …` commands → your owner/repo and name |

Leave `"mcpbrain[daemon]"` in every `uv tool install` command exactly as it is. It
is the one spelling that resolves against both old and new wheels, and dropping it
ships a brain with no embedder that the daily auto-update cannot repair.

## 4b. Gold eval set (optional)

`bin/tenant.py use` also copies `eval/golden_retrieval_set*.yaml` from your tenant
repo into `tests/eval/`, if you have any. These are hand-curated query→chunk cases
used by the retrieval quality gate, and they are **tenant data** — the chunk ids
point into your own store, so ours are useless to you and yours should not be
committed here. Without them the gate skips honestly rather than failing; build
your own once you have a corpus worth measuring.

## 5. Install and verify

```bash
python bin/tenant.py use ../mcpbrain-tenant
python bin/tenant.py check --online
```

Fix everything it reports before going further.

## 6. Release and install

Follow `docs/RELEASE-RUNBOOK.md` unchanged. `bin/release.py` refuses to build
without a valid profile and asserts that the wheel it produced actually carries it.

## Failure modes that are hard to diagnose from symptoms

| Symptom | Cause |
|---|---|
| Consent screen names the wrong organisation | You are still shipping the upstream OAuth client. `tenant check` catches this as a `project_id` disagreement. |
| `redirect_uri_mismatch` at consent | The OAuth client is type `web`, not Desktop. |
| "Unverified app" warning, or consent capped at 100 users | The consent screen is External, not Internal. |
| Backups appear to run but nothing lands in Drive | The escrow folder is on My Drive, not a Shared Drive — `drive.file` cannot write there. |
| Installs never auto-update | GitHub Pages is not enabled on the dist repo, or `index_url` is wrong. `tenant check --online` catches both. |
| Fleet and backup silently do nothing | No tenant profile in the build. `mcpbrain doctor` reports `Tenant NOT CONFIGURED`. |
| `tenant check --online` always reports the marketplace repo unreachable, even when everything else is correct | Expected. The plugin mirror repo is private by design (step 3 above), so an anonymous check cannot reach it. This is advisory, not a real failure — confirm the repo exists and is correctly named by hand. |
