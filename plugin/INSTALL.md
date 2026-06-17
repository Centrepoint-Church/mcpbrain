# Installing mcpbrain

mcpbrain installs a background daemon on your **Mac** and is *used* in **Cowork**.
The install must run in **Claude Code** (it can run commands on your machine);
Cowork is sandboxed and can't. So it's two steps:

1. **Part 1 — Claude Code:** paste the prompt below into a Claude Code session.
2. **Part 2 — Cowork:** run the `mcpbrain-cowork-setup` skill.

---

## Part 1 — paste this into a Claude Code session

> Install mcpbrain on my Mac. Run these commands with the Bash tool:
>
> ```bash
> command -v uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | sh
> export PATH="$HOME/.local/bin:$PATH"
> uv tool install --python 3.12 --index "mcpbrain=https://centrepoint-church.github.io/mcpbrain-dist/simple/" mcpbrain --force
> mcpbrain setup
> ```
>
> `mcpbrain setup` registers the background agent (launchd) and opens a browser
> wizard — I'll complete Google sign-in, my identity, and timezone there. Leave the
> pre-filled Fleet / escrow folder IDs as-is.
>
> **If I've used mcpbrain before (reinstall or new machine):** after I've signed in,
> run `mcpbrain restore --check`; if it reports a backup, run
> `mcpbrain restore --auto` (add `--force` if it says the store already exists) to
> bring my whole brain back, and skip the bootstrap interview. Do **not** click
> "Enable backup" in the wizard when restoring — the restore brings my key and
> settings back.
>
> **If it's a fresh start (no backup found):** run the `mcpbrain-bootstrap` skill to
> seed my world-model.
>
> Then remind me to set Claude to open at login (System Settings → General → Login
> Items → add Claude).
>
> **Do NOT create any scheduled task or routine here in Claude Code** — that's Part 2,
> and it has to run in Cowork.

---

## Part 2 — in Cowork

When Part 1 is done, open a **Cowork** session and run the **`mcpbrain-cowork-setup`**
skill. It creates your "My Brain" project, the four recurring Cowork scheduled tasks
(enrich, meeting-packs, gardener, reference-gardener), and connects the brain — all in
the place where they actually work.
