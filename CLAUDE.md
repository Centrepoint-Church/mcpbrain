# mcpbrain — project context for Claude Code

Rules that are true **today**. The full history behind them (releases, incidents,
measurements, SHAs) is in `docs/HISTORY.md`; search it for the *why*. When a rule
here stops being true, change it here, and record the story in HISTORY, not here.

## What this is, and what "shipped" means

mcpbrain ships to other users as a **Claude Code plugin + a pip-installable package**.

| Repo | What it is | How users get it |
|---|---|---|
| **mcpbrain** (this repo, public) | Python package `mcpbrain/`, plugin source `plugin/`, tests | source of truth |
| **`gh-pages` branch** (worktree `../mcpbrain-pages`) | PEP 503 wheel index at `centrepoint-church.github.io/mcpbrain/simple/` | installs auto-update daily from it |
| **mcpbrain-plugin** (`../mcpbrain-plugin`, **private**) | plugin mirrored from `plugin/` | org plugin marketplace (app catalogue) |
| **mcpbrain-tenant** (`../mcpbrain-tenant`, **private**) | OAuth client, `tenant.json`, gold eval sets, `tenant_people.json` | `python bin/tenant.py use ../mcpbrain-tenant` |

- Commit and push to `main` freely. **Releasing** (the index, the plugin repo) is an
  all-users action: only on an explicit instruction, following
  **`docs/RELEASE-RUNBOOK.md`** (the *do*; `docs/DISTRIBUTION.md` is the *why*).
- Installing locally affects this Mac only. Use **`python bin/dev_reinstall.py`**:
  it boots out daemon + tray, reinstalls, clears `__pycache__`, proves every
  installed file equals the tree and that `/api/status` reports this version.
  Do not hand-roll `launchctl stop`/`uv tool install --force`: KeepAlive, cached
  wheels and stale bytecode each made that silently run old code.
- Running MCP servers keep their start-time code until their client restarts.
- `python -c "import mcpbrain"` from inside the repo imports the WORKING TREE, not
  the install. Check `module.__file__` or run from a neutral cwd.

## Releasing — the parts that are easy to get wrong

- **Version lives in four files, keep them equal:** `pyproject.toml`,
  `mcpbrain/__init__.py`, `plugin/.claude-plugin/plugin.json`,
  `plugin/.claude-plugin/marketplace.json` (+ the `uv.lock` entry).
- Run the **full suite under the versions the fleet will resolve**, not only the
  lock (runbook: "resolve exactly as the fleet will"). Unbounded deps have broken
  the fleet twice (`mcp` 2.0, `nameparser` 3.0). Every runtime dependency that has
  bitten gets an upper bound and a pin test; `ruff` is pinned too.
- If extraction rules changed, `python bin/sync_agents.py` first.
- **No `.mcpb` Desktop Extension, ever** (it hijacked the connector name and failed
  to launch). `mcpbrain setup` is the only connector registration; the plugin
  bundles no server. Guarded by `tests/test_plugin_manifest.py`.
- Users install the plugin from the **app catalogue**, never
  `claude plugin marketplace add` (cannot work for a private marketplace).
- Do not move the index URL casually: each install updates from the URL baked into
  its own wheel, so a move must ship via the old index and keep it alive.
- **Windows hardware QA gate is OPEN** — do not onboard Windows users.

## Store safety (the 2026-09-10 corruption was two concurrent writers)

1. Before any attended store write: **`launchctl bootout gui/$(id -u)/com.mcpbrain`**,
   then confirm no daemon process exists (re-check during long phases). Afterwards
   `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.mcpbrain.plist`.
2. Run `PRAGMA integrity_check` after any attended store operation.
3. Take and keep snapshots (`Connection.backup` / `VACUUM INTO`) before writing.
4. A scheduled Claude session may bootstrap a booted-out daemon; re-confirm right
   before writing.
5. Every restore goes through `backup.swap_in_store`; `mcpbrain restore` refuses
   while the daemon runs. No path may overwrite a store that has content.
6. Attended scripts default to dry-run and need `--yes`. They build the store as
   `Store(config.store_path(), dim=...)` — `Store(str(home))` is wrong and has
   shipped three times because fakes hid it.

## Privacy (this repo is public)

- Never commit real people's names, name-derived entity ids, or org content.
  `tests/test_no_tenant_literals.py` scans `mcpbrain`, `plugin`, `tests` for org
  names and (via the gitignored `tenant_people.json`) person names. Use the
  fictional cast (Dana Okafor, Marcus Reyes, Priya Anand; Northgate Trust …).
- Gold eval sets are tenant data: they live in `mcpbrain-tenant/eval/`, never here.
- A history rewrite must remap every SHA cited in this file and HISTORY
  (`filter-repo` commit-map). GitHub keeps serving old SHAs until Support purges
  them. `git filter-repo` hangs on leftover `.git/filter-repo/` state.
- The OAuth consent screen is **Internal**. If it is ever made External, rotate
  the client secret first.

## Design invariants — do not "fix" these

**Ingestion and enrichment**
- `source_class.py` is the one classification of a chunk's source. Derived chunks
  (sessions, briefs, drafts, consolidated notes, digests) are cold and **write
  nothing** in `graph_write.apply`. Relation strength = distinct evidence sources.
- Cold-marking is an enrichment-cost optimisation, never a retrieval filter.
- Supersede-on-silence was removed: silence is not completion.
- Duplicate carry is narrow on purpose: My Drive files and notes only, never Gmail
  or shared drives.
- A per-claim attempt counter was removed: do not reintroduce one without a
  separate counter (it would mark ~14 threads of unextracted text enriched).
- The model-echoed `part` is verified against the unit's real parts.
- `thread_enrich._chunk_key` checks `session_id` before `event_id`: a privacy
  boundary (meetings must never be labelled calendar and contribute to the org graph).
- anarlog ingestion is explicit opt-in; meetings never contribute to the org
  graph; `sessions.event_id` is anarlog's own key, not the Google event id.
- Org contribution fails closed when a relation's source chunk is gone.

**Chunks and embedding**
- Measure "too long" as the prefixed passage, `embed.passage_over_window`.
- `strip_html` is regex-only; a `<head>`/`<title>` ends at its close tag or the
  first `<body`. No bs4. Validate a cleaner change on real mail first.
- Heal with one-shot markers, never by bumping `CHUNKER_VERSION` /
  `SPLIT_VERSION` / `EXTRACTION_VERSIONS` (that re-fetches the corpus).
- Notes split with `chunking.split_lossless`. Never re-split a fixed-id note
  (`note-core-identity-seed`, `note-consolidated-*`): its writer addresses it by id.
- `chunks.metadata` is JSON TEXT; build every query/index expression with
  `store._meta_extract` (always `json_extract`). `patch_chunk_metadata` resets
  `fts_context_version`.

**Retrieval**
- Fusion depth is fixed (`retrieval.FUSION_DEPTH`), independent of `limit`;
  document collapse is a pure post-filter; summed/bonus aggregation measured worse.
- An unknown age scores 0.0 recency. Enriched digests stay undated on purpose.
  Three-axis defaults: recency 0.05, importance 0.15, decay 0.0.
- `brain_search` never expands; only prompt injection does (`retrieval_expand`).
- The cross-encoder reranker lost on this corpus and was removed.

**Graph**
- User corrections are sticky: automated writers skip locked fields and rejected
  triples; every automated `UPDATE entities SET` goes through the lock guard.
- Merges apply to name-identity types only (person/org/project), never on role
  addresses (`resolve.is_role_address`).
- `brain_graph` is bounded: hubs are returned but not traversed through.

**Backup**
- Prune trashes, never deletes. The Drive escrow key is canonical and never
  overwritten. A transiently skipped restore holds sync and backups ("hold, don't
  overwrite").

## Flags

Feature switches live in `config.py` (`*_enabled`). Fleet-flippable ones resolve
through `config.fleet_flag`: org overlay → local config → default, and a local
`false` is always the kill switch. Before adding a switch, decide what evidence
turns it on; delete it once decided. Switches whose investigation is recorded in
their docstring: read it before changing them.

## Testing

- Scope test runs to the edited files and their direct dependents; the owner runs
  the full suite.
- CI (Linux) must be green before a release: `gh run list --workflow ci -L 1`. It
  sat red from 2026-09-08 to 2026-11-13 (344 runs) and nobody noticed; macOS-only
  shell (`stat -f`) and tenant files absent from a public checkout were the causes.
  `docker` (OrbStack) reproduces a Linux-only failure locally.
- A test that only meets a fake proves little. Several real defects passed against
  fakes (`Store` constructor, the claim backstop); prefer a real `Store` on tmp.
- Seed dangling rows with a raw `sqlite3` connection (the store enables FKs at
  connect, and `PRAGMA foreign_keys=OFF` inside a transaction is a no-op).
- Gold gate: `tests/eval/run_eval.py --gold` on the production path. Live store
  2026-10-27: recall@10 0.900 / MRR 0.601. A change must not lose recall@10 and
  must not drop MRR by more than one rank step.

## Current state

- **Released: 0.7.149** (2026-11-10) — source `3f02aa85`, gh-pages `3844182d`,
  plugin `862da39`. Later commits on `main` are not released.
- **Open:** Windows hardware QA; the owner acceptance checklist for the console
  redesign; owner-only checks for graph corrections (pending-correction approval,
  `@` picker, elicitation accept path); unclosed `<style>`/`<title>` keeps text;
  11 reflow owners gave up (OCR-empty PDFs, refused attachments) and keep their old chunks.
