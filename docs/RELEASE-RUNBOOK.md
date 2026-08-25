# mcpbrain — Release & Rollout Runbook

Concrete maintainer steps to publish a new mcpbrain version and put it on a
colleague's computer. Companion to `docs/DISTRIBUTION.md` (the *why*); this is the
*do*. See `docs/ARCHITECTURE.md` for the system overview. The current version is
whatever `mcpbrain/__init__.py` says — do not hard-code it in this doc (it goes stale).

## Distribution topology (all under the Centrepoint-Church org)

- **`Centrepoint-Church/mcpbrain`** — private source repo (this repo). The daemon
  source of truth.
- **`Centrepoint-Church/mcpbrain-dist`** — public PEP 503 wheel index served via
  GitHub Pages at `https://centrepoint-church.github.io/mcpbrain-dist/simple/`.
  Contains only `simple/` (the index + the current wheel). This is the URL the
  shipped `update.py` `DEFAULT_INDEX_URL` pulls from, so a published bump
  auto-updates installed daemons within ~a day.
- **`Centrepoint-Church/mcpbrain-plugin`** — public plugin assets (skills, hooks,
  commands, `.claude-plugin/{plugin,marketplace}.json`). Distributed to staff
  through the org **plugin marketplace**. Note: the plugin's `.mcp.json` bundles
  **no** MCP server — the `mcpbrain` connector is registered by `mcpbrain setup`
  at user scope (see `docs/ARCHITECTURE.md` for why). The plugin ships **no
  top-level `bin/`** — claude.ai-hosted plugins fail validation if they do
  (executables must be declared via hooks/commands/mcpServers), so the old
  `bin/mcpbrain-{mcp,monitor}` shims and the `monitors/` health monitor were
  removed in 0.7.96; `mcpbrain doctor` covers health on demand.

Local clones used for publishing live at `~/GitHub/mcpbrain-dist` and
`~/GitHub/mcpbrain-plugin`, both with `origin` = the Centrepoint-Church
repos. **Always confirm the remote is the org** before pushing
(`git -C <clone> remote get-url origin`) — older runbooks referenced a personal
`itsjoshuakemp` org that is no longer used.

## How a colleague installs (current flow)

There is **no `curl install.sh` one-liner and no `/mcpbrain-install` skill any
more.** Installation is a single Claude Code session driven by a copy-paste
prompt — the canonical copy lives in `plugin/INSTALL.md`:

1. The org admin makes `mcpbrain-plugin` available in Claude Team/Enterprise
   settings (see step 2 below) — ideally **required/default** so it auto-installs.
2. The colleague pastes the `plugin/INSTALL.md` prompt into a **Claude Code
   (Desktop)** session. It installs uv if missing, then:
   `uv tool install --python 3.12 --index "mcpbrain=https://centrepoint-church.github.io/mcpbrain-dist/simple/" "mcpbrain[daemon]" --force`,
   and runs `mcpbrain setup`.
3. `mcpbrain setup` registers the login agent (launchd/schtasks), **connects the
   brain to Claude Desktop** by writing `mcpbrain` into the Desktop MCP config
   (`claude_desktop_config.json`, with the absolute install path), and opens the
   browser wizard. Backup/recovery is automatic in the wizard — no manual
   `restore`/bootstrap step.
4. The colleague completes the wizard (Google sign-in + identity + timezone),
   creates the four **Local** scheduled tasks (Sonnet 4.6 + Auto permission mode)
   in the same session, and turns on **Claude → Settings → Desktop App → General →
   "Run on startup"** (so Claude launches at login and the Local tasks fire).

The `--python 3.12` pin is **required**: without it the install fails on any
machine whose default Python is < 3.12 (uv provisions 3.12 when pinned).

---

## 1. Cut a new release (each time) — THE CORE PROCEDURE

From the source repo (`~/GitHub/mcpbrain`), on `main`, with a clean tree:

### 1a. Bump the version in all FOUR sources of truth (keep them equal)

- `pyproject.toml` → `[project] version`
- `mcpbrain/__init__.py` → `__version__`
- `plugin/.claude-plugin/plugin.json` → `version`
- `plugin/.claude-plugin/marketplace.json` → `plugins[0].version`

```bash
uv run pytest tests/test_version.py tests/test_plugin_manifest.py -q   # version semver + manifest sane
uv run pytest -q                                                       # full suite green
uv run ruff check mcpbrain/                                            # clean
git add -A && git commit -m "chore(release): bump to <version>" && git push origin main
```

### 1b. Build + publish the wheel to `mcpbrain-dist`

```bash
git -C ~/GitHub/mcpbrain-dist remote get-url origin   # MUST be Centrepoint-Church/mcpbrain-dist
git -C ~/GitHub/mcpbrain-dist pull --ff-only
uv run python bin/release.py --dist ~/GitHub/mcpbrain-dist
```

**⚠️ Stale-wheel gotcha:** `bin/release.py` copies every `mcpbrain-*.whl` it finds in
the source `dist/` build dir into the published index, and never deletes. So the
old version reappears unless you purge it from **both** places, then regenerate:

```bash
rm -f dist/mcpbrain-<OLD>-py3-none-any.whl                                  # source build dir
rm -f ~/GitHub/mcpbrain-dist/simple/mcpbrain/mcpbrain-<OLD>-py3-none-any.whl
uv run python bin/release.py --dist ~/GitHub/mcpbrain-dist        # regenerate index
ls ~/GitHub/mcpbrain-dist/simple/mcpbrain/                        # expect ONLY the new wheel
cd ~/GitHub/mcpbrain-dist && git add -A \
  && git commit -m "release: mcpbrain <version>" && git push origin main
```

`update.py` picks the highest PEP 440 version, so multiple wheels are *functionally*
fine — but keep the index to the current wheel for clarity.

### 1b.1 Publish install.ps1 to dist repo

The Windows installer script must also be published:

> **The `.mcpb` Desktop Extension was REMOVED (2026-08-24) and must not be reintroduced.**
> It registered a server under the same name (`mcpbrain`) as the connector `mcpbrain setup`
> writes into `claude_desktop_config.json`, won that collision, and then failed to launch —
> mcpb's `server.type: "uv"` drops the `--from mcpbrain mcpbrain` argv, so Desktop ran a bare
> `uv mcp-server` → `unrecognized subcommand`. It shadowed a working connector with a broken
> one. `mcpbrain setup` is the only supported way the connector is registered; the plugin
> deliberately bundles no server (`plugin/.mcp.json`, guarded by
> `tests/test_plugin_manifest.py`).

```bash
cp plugin/scripts/install.ps1 ~/GitHub/mcpbrain-dist/
cd ~/GitHub/mcpbrain-dist
git add install.ps1 \
  && git commit -m "release: mcpbrain <version> (install.ps1)" && git push origin main
```

`install.ps1` is often byte-identical between releases; `git add` then simply stages nothing for
it, which is fine.


### 1c. Sync the plugin assets to `mcpbrain-plugin`

Mirror the source `plugin/` tracked tree into the plugin repo. Use `git archive` so
**only tracked files** ship (this excludes macOS ` 2` conflict-copies and other
cruft by construction):

```bash
git -C ~/GitHub/mcpbrain-plugin remote get-url origin   # MUST be Centrepoint-Church/mcpbrain-plugin
git -C ~/GitHub/mcpbrain-plugin pull --ff-only
TMP=$(mktemp -d)
git archive HEAD:plugin | tar -x -C "$TMP"
rsync -a --delete --exclude='.git' --exclude='.DS_Store' "$TMP"/ ~/GitHub/mcpbrain-plugin/
rm -rf "$TMP"
cd ~/GitHub/mcpbrain-plugin
git status --short          # expect only intended changes; NO .DS_Store, NO ' 2' dirs
git add -A && git commit -m "release <version>: <one-line summary>" && git push origin main
```

The plugin repo carries a `.gitignore` with `.DS_Store`; if `git add -A` ever sweeps
in a `.DS_Store`, `git rm --cached` it before pushing.

### 1d. Verify the release is live

```bash
curl -fsS https://centrepoint-church.github.io/mcpbrain-dist/simple/mcpbrain/ \
  | grep -o 'mcpbrain-[0-9.]*-py3-none-any.whl' | sort -u    # expect the new version only
curl -fsSI https://centrepoint-church.github.io/mcpbrain-dist/install.ps1   | head -1   # 200
```

GitHub Pages can lag ~1 min. Installed daemons auto-update on their next ~daily check.

**Verify the WHEEL CONTENTS, never the build output.** `uv build` reports success for a wheel
built from a stale tree just as happily as a fresh one. Open it and look for something unique to
this release — the same discipline as the local-install stale-wheel trap:

```bash
python - <<'PY'
import zipfile, glob
z = zipfile.ZipFile(glob.glob("dist/mcpbrain-*-py3-none-any.whl")[-1])
meta = z.read([n for n in z.namelist() if n.endswith("METADATA")][0]).decode()
print([l for l in meta.splitlines() if l.startswith(("Version:", "Requires-Dist: mcp"))])
# then assert a string only this release introduced, e.g.:
# assert "GRAPH_MAX_HOPS" in z.read("mcpbrain/mcp_server.py").decode()
PY
```

**Then resolve exactly as the fleet will.** This is the single best end-of-release gate: it proves
what a machine's auto-update actually gets, dependencies included. It is how you'd have caught the
0.7.112 exposure (an unbounded `mcp>=1.2` that let a daily update pull a breaking major):

```bash
printf 'mcpbrain[daemon]\n' > /tmp/fleet-req.in
uv pip compile /tmp/fleet-req.in \
  --index "mcpbrain=https://centrepoint-church.github.io/mcpbrain-dist/simple/" \
  --quiet -o /tmp/fleet-resolved.txt
grep -iE '^(mcpbrain|mcp|mcp-types|fastembed)==' /tmp/fleet-resolved.txt
```

Expect the new `mcpbrain==`, and every pinned dependency to match what you actually validated
locally. A transitive that resolves differently here than on your box is the release's real risk.

Finally, **install from the published index** (not `.`) and restart the client — the artifact users
get is the only one that counts:

```bash
launchctl bootout gui/$(id -u)/com.mcpbrain
uv tool install --python 3.12 \
  --index "mcpbrain=https://centrepoint-church.github.io/mcpbrain-dist/simple/" \
  "mcpbrain[daemon]" --upgrade --reinstall-package mcpbrain
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.mcpbrain.plist
mcpbrain doctor
```

### 1e. (0.7.99 one-shot) Relocate legacy in-drive ingest-cache folders

0.7.99 moved the shared-drive ingest cache out of every team drive's root and into
`<fleet folder>/ingest-cache/<source_drive_id>/.mcpbrain-cache/` (inside the MCPBrain
Backups drive). New installs write only to the central location; the old per-team-drive
`.mcpbrain-cache/` folders become dead clutter.

**Run once, and ONLY AFTER the whole fleet has auto-updated to ≥0.7.99** (give it ~a day
past the dist publish, and confirm each colleague's daemon has updated). An install still
on ≤0.7.98 will recreate the in-drive folder on its next sync, so premature cleanup churns.

```bash
python bin/relocate_ingest_cache.py                  # dry-run: report the legacy footprint
python bin/relocate_ingest_cache.py --delete-legacy  # actually remove them (after fleet updated)
```

Deletion is safe: the central location re-publishes any still-live doc on its next
cache-miss (regeneration is cheap; no copy step). Per-drive isolation — one drive's
failure never aborts the rest.

---

## 2. Org marketplace deployment (admin console — not scriptable)

Only a Claude **Team/Enterprise org owner** can do this, in claude.ai settings:

- Add/refresh the `Centrepoint-Church/mcpbrain-plugin` marketplace.
- Set the install preference to **`required`** (auto-installed, non-removable) or
  **`default`** (auto-installed, removable) — **not** merely `available` — so the
  current release reaches all staff without each person installing by hand. This is the onboarding
  (#9) + lifecycle (#6b) win from the platform-layer spec.

Until this is set, staff cannot install the plugin (and existing installs keep
running their current pinned version until they re-pull).

## 3. Authorise Google access (one-time per person)

The bundled OAuth client belongs to the **Centrepoint** Google Cloud project
(`mcpbrain/google_oauth_client.json`). Behaviour depends on its consent screen:

- **Internal consent screen (recommended, per the 0.0.6 OAuth gate):** any
  `@centrepoint.church` Workspace account can authorise with **no per-user step**,
  and there is no "unverified app" warning. Confirm the consent screen is set to
  *Internal* for the Centrepoint Workspace.
- **Testing mode (fallback, ≤100 users):** add each colleague's
  `@centrepoint.church` address under **APIs & Services → OAuth consent screen →
  Audience → Test users**. They will see "Google hasn't verified this app →
  Advanced → Continue" — the wizard explains it.

## 4. macOS clean-machine validation (do once before wider rollout)

On a Mac that is NOT your dev box, with a **non-author** `@centrepoint.church` account:

- Install the plugin (org marketplace) → paste the `plugin/INSTALL.md` prompt and
  run it end to end.
- Confirm: uv + wheel install; `mcpbrain --version` resolves in a fresh shell;
  daemon starts (menu-bar icon); **`mcpbrain setup` wrote the Claude Desktop MCP
  config** (`~/Library/Application Support/Claude/claude_desktop_config.json` has
  an `mcpbrain` entry with the absolute path) and Claude Desktop shows the
  `brain_*` tools after a restart; wizard completes with the *different* Google
  account; backup/recovery runs
  automatically; the four Local scheduled tasks are created; `brain_search`
  returns a result with a `score` field; the hourly enrich task drains
  `enrich_inbox`; `mcpbrain doctor` runs and its auto-fixes work; and
  `mcpbrain restore` round-trips a snapshot.

## 5. Windows QA (MANDATORY pre-ship gate) — Hardware & installer validation

**Do not ship the Windows path without passing this gate.** Test the `install.ps1` script on real hardware before wider Windows rollout. Do this once per release cycle with a **non-author** `@centrepoint.church` account.

**Architecture note:** Windows uses **x64 Python under emulation on ARM64** machines. Native ARM64 wheels are not available for sqlite-vec, cryptography, pymupdf, and leidenalg, so the installer probes the machine, detects ARM64, and provisions the x64 Python runtime + VC++ runtime. The daemon runs with emulation overhead but no translation via Rosetta. Confirm `mcpbrain doctor` reports the correct architecture (`ARM64` vs. `X64`).

- [ ] **ARM64 box — x64-under-emulation (clean install)**
  - Download `install.ps1` from `https://centrepoint-church.github.io/mcpbrain-dist/install.ps1`
  - Run it from a clean Windows install (no mcpbrain present):
    ```powershell
    irm https://centrepoint-church.github.io/mcpbrain-dist/install.ps1 -OutFile "$env:TEMP\mcpbrain-install.ps1"
    & "$env:TEMP\mcpbrain-install.ps1"
    ```
  - Confirm `install.ps1` (via uv) installs an **x64** Python + the **x64** VC++ redist — not native ARM64 (native-ARM64 isn't viable: several dependencies ship no ARM64 Windows wheels)
  - `mcpbrain doctor` reports OS=ARM64 / interpreter=win-amd64 → **"emulated — expected"** (a match, not a fault)
  - Embedder loads under Windows' transparent emulation — translation overhead is **expected**, not absent
  - Wizard launches and model-download step reaches "Ready"

- [ ] **x64 native box (clean install)**
  - Same as ARM64 box above, but the x64 Python/VC++ redist run natively (no emulation) and `mcpbrain doctor` reports `arch=X64` (matches native machine)

- [ ] **Policy-blocked box (Task Scheduler disabled)**
  - Simulate or test on a machine where Task Scheduler is blocked (Group Policy)
  - `install.ps1` detects the block and falls through to Startup-shortcut mechanism
  - Daemon runs at next user logon (check Task Manager → Startup tab or registry `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`)

**Record results below. Do not roll out Windows until all items pass.**

| Test | Result |
|------|--------|
| ARM64 arch match | |
| ARM64 embedder load | |
| ARM64 wizard ready | |
| x64 arch match | |
| x64 embedder load | |
| x64 wizard ready | |
| Policy-blocked fallback | |
| brain_search Windows | |
| brain_search macOS | |

## 6. Windows desktop validation (HARD GATE — must pass before Windows rollout)

The schtasks generators are unit-tested (`tests/test_agents_windows_xplat.py`) but
the live desktop flow has had **zero** real-machine testing. Run once on a clean
Windows box with a **non-author** `@centrepoint.church` Google account.

- [ ] **1. Install plugin → paste `plugin/INSTALL.md` prompt** on a clean Windows
  machine. (Note: `INSTALL.md` is currently macOS-worded — the Windows install
  commands/PATH still need their own pass; see the gaps note below.)
- [ ] **2. uv + wheel install; PATH correct** — the prompt runs the
  `uv tool install … --index "mcpbrain=https://centrepoint-church.github.io/mcpbrain-dist/simple/" …`
  step; `mcpbrain --version` resolves in a fresh shell (validates uv shim + PATH).
- [ ] **3. `mcpbrain setup` registers daemon + tray via schtasks** — confirm both:
  `schtasks /query /tn mcpbrain` and `schtasks /query /tn mcpbrain-tray` (or
  `schtasks /query | findstr mcpbrain`).
- [ ] **4. `mcpbrain setup` wrote the Claude Desktop MCP config** —
  `%APPDATA%\Claude\claude_desktop_config.json` has an `mcpbrain` entry whose
  `command` is the absolute `mcpbrain.exe` path (no `MCPBRAIN_HOME`), and Claude
  Desktop shows the `brain_*` tools after a restart. This is the cross-platform
  connector mechanism (a config write, not a plugin shim) and is the main thing
  this Windows gate exists to prove.
- [ ] **5. Wizard loads; non-author Google sign-in works** with a *different*
  Centrepoint account.
- [ ] **6. The four Local scheduled tasks can be created** (Sonnet 4.6 + Auto
  permission mode), per `INSTALL.md`. Do **not** use `/schedule` (that makes a
  cloud routine that can't reach the local daemon). The working folder doesn't
  matter — the tasks reach mcpbrain via its MCP tools.
- [ ] **7. `brain_search` returns** a result (with a `score` field).
- [ ] **8. Hourly enrich task drains `enrich_inbox`** — drop a pending batch and
  confirm it is consumed (now via `brain_enrich_pull`/`brain_enrich_push`).
- [ ] **9. `mcpbrain restore` round-trips a snapshot.**
- [ ] **10. `mcpbrain doctor` runs and its auto-fixes work on Windows** —
  restart/re-register via schtasks (`schtasks /end`+`/run`, `/create /f`).

**Likely gap candidates:** PATH / uv-shim differences, `mcpbrain home` resolution
(`%APPDATA%\mcpbrain`), and schtasks arg quoting for paths with spaces (covered by
`tests/test_agents_windows_xplat.py`). Fix any gap in `agents.py` / `setup.py` and
add a regression assertion. **Record results here. Do not roll out to Windows until
this gate passes.**

## 7. Store maintenance: `bin/optimise_store.py` (attended SQLite rebuild)

**This is an ATTENDED, backup-gated migration a human runs at the terminal. It is
NEVER wired into a daemon cadence, a cron, or any automatic trigger — same
posture as `bin/consolidate.py` (§ its own module docstring: "Attended,
backup-gated consolidation migrations (curator-run)"). `init()` uses `CREATE
TABLE IF NOT EXISTS`, so a schema change (page size, STRICT tables,
contentless FTS5, FK constraints) reaches a
**new** install for free but an **existing** store only picks it up by running
this tool. There is no silent migration path and there should never be one.**

**The same `IF NOT EXISTS` property also applies to `init()`'s five expression
indexes on `chunks.metadata`, and there it is a TRAP:** `CREATE INDEX IF NOT
EXISTS` keys on the index NAME, not its expression, so changing the SQL an index
is built over does **not** rebuild an existing store's copy — the persisted index
keeps the old expression while queries move to the new one, SQLite stops matching
them, and every affected query silently degrades to a full `SCAN chunks` (the
0.7.105 outage). That is why `store._meta_extract()` emits `json_extract`
unconditionally and must never become version-dependent; see its docstring and
`tests/test_metadata_jsonb.py::test_reinit_on_existing_store_*`. **If a metadata
index expression ever legitimately has to change, the index must be renamed (or
explicitly dropped) — never edited in place.** Relatedly: metadata is stored as
JSON **TEXT**, not JSONB. Task 7's STRICT `chunks` table declares `metadata TEXT`,
so a JSONB blob cannot be written to it at all; true JSONB storage would need
that column loosened to `ANY`/`BLOB` plus a dedicated rebuild.

Run this when: the live store is due for its (infrequent, one-off-per-schema-
generation) physical rebuild — not on a schedule. **Budget disk generously: on
the 2.62 GB live store, following the procedure below as written needs
~12-13 GB free, not merely ~2.4x the store size.** The tool's snapshot step
(Gate 2, inside `_verified_snapshot`) runs on **every**
non-`--swap`/non-`--rollback` invocation — including the report-only run in
step 2 below — writes a **timestamped** artifact
(`<store>.snapshot-<epoch>.enc`, 3.48 GB — Fernet base64 costs 4/3 over the
2.62 GB plaintext), and never deletes it. Each call to `_verified_snapshot`
also materialises a **transient 2.62 GB cleartext copy twice**: once inside
`backup.make_encrypted_snapshot` (a full plaintext snapshot, encrypted
in place) and again when `_verified_snapshot` decrypts its own output back
to a temp file to integrity-check it before trusting it as a rollback —
both removed immediately after, but present on disk while that call runs.
Doing step 2 and then step 3 as written therefore peaks (at step 3's own
Gate 2, just before its transient cleartext is cleaned up, and *before* the
rebuild file has even started writing) at: **two** 3.48 GB encrypted
snapshots left over from steps 2 and 3 (6.96 GB) + **one** 2.62 GB transient
snapshot-verify cleartext (step 3's own, still live at that instant) + the
still-present 2.62 GB old/live store ≈ **12.2 GB** — before the up-to-1.49 GB
rebuild file even starts growing. Delete the older
`<store>.snapshot-*.enc` (and its own `<that-snapshot>.key`, if you don't
need it retained — one key file per snapshot, never a shared name, so
deleting an older snapshot's key can never affect a newer one) once you've
moved past the step that produced it and confirmed the next step's own
snapshot succeeded.

### Procedure

1. **Stop the daemon.** `launchctl bootout gui/$UID/com.mcpbrain` (macOS) /
   the Windows scheduled-task equivalent. The tool's own Gate 1 refuses to run
   against the live store while the daemon holds `daemon.lock`, but stopping it
   first avoids a confusing refusal message and is the point at which "attended"
   starts — nothing below should run with the daemon up.
2. **Report only, no `--yes`.** `uv run python bin/optimise_store.py` — prints
   orphan-row counts (rows whose FK parent is missing), the schema preflight
   (unmanaged tables carried verbatim, dead columns to be dropped with their
   non-null counts), and takes a **verified encrypted snapshot** first (Gate 2:
   the snapshot is decrypted to a temp file and integrity-checked before the
   tool proceeds at all — an unverified snapshot is not a rollback). **This
   step does not touch the live store's own data or schema, but it is not a
   no-op**: it writes a multi-GB encrypted snapshot artifact next to the store
   (and an escrow-key file, if none is configured yet) every time it runs —
   see the disk-budget note above. Read the report.
3. **Rebuild, out of place.** `uv run python bin/optimise_store.py --yes` —
   produces `<store>.new` next to the live file. The live file is **never**
   touched by this step. Gates 5/6 (integrity_check + foreign_key_check, and a
   row-count reconciliation against the orphan report) must both report `ok`
   before the tool will say it is safe to promote; if either fails, `<store>.new`
   is retained for inspection and the live store is untouched — do not proceed
   to `--swap`.
4. **Verify independently before swapping.** Beyond the tool's own gates, spot
   check on `<store>.new` with `bin/measure_store.py --latency` (expect
   `has_stat1: true` now that the rebuild ran `ANALYZE`, and the four 0.7.105
   benchmark latencies at least as good as the live store's), and run the gold
   harness against a **copy** of `<store>.new` renamed to `brain.sqlite3` under
   a scratch `MCPBRAIN_HOME` (never against the live `MCPBRAIN_HOME` — that
   would silently exercise the live store, not the rebuild):
   ```bash
   cp <store>.new /tmp/gold-check/brain.sqlite3
   MCPBRAIN_HOME=/tmp/gold-check uv run python tests/eval/run_eval.py --gold --k 10
   ```
   Expected: recall@10 **≥ 0.780**, MRR **≥ 0.550** (the plan's non-negotiable
   floor — raised 2026-08-25 from the original 0.750/0.514, which was stale
   relative to every actual pre-rebuild measurement taken during this work,
   consistently recall@10=0.800 / MRR 0.565–0.591). Contentless FTS5 is ranking-neutral by
   construction — same tokens indexed, same BM25 ranking — so if the numbers
   differ from the live store's own current gold run, explain why before
   proceeding. A small **upward** move is possible here: the rebuild's
   `_rederive_fts` unconditionally regenerates every row's FTS text from that
   row's *current* metadata, which corrects any chunk whose indexed text had
   drifted stale relative to its metadata — a real, pre-existing gap
   (`Store.patch_chunk_metadata` updates `chunks.metadata` but never
   refreshes the `fts_chunks` mirror or resets `fts_context_version`, so a
   metadata-only write — e.g. a Drive re-sync backfilling `folder_path` onto
   an already-indexed chunk — can leave the FTS index silently behind
   metadata forever — see the "SQLite optimisation" entry under this repo's
   root `CLAUDE.md` § "Shipping caveats" for the full worked example,
   including the specific commit SHAs and the one gold-set chunk this was
   confirmed against directly, not just inferred from score deltas). Also
   spot-check the injection path (`daemon.search`/`prompt_recall`)
   directly — the `--gold` harness calls `hybrid_search` and does not
   exercise the `recall_max_distance` off-topic gate.
5. **Promote.** `uv run python bin/optimise_store.py --swap --yes` — checkpoints
   `<store>.new`'s WAL, re-verifies integrity, then swaps it over the live file.
   **The old store is retained, never deleted**, at
   `<store>.pre-rebuild-<timestamp>` (sidecars moved with it, so nothing is left
   for a later re-open to silently replay into the wrong file).
6. **Restart the daemon** and confirm it opens the promoted store normally
   (`mcpbrain doctor`, a live `brain_search` call).
7. **Keep the retained pre-rebuild file until the next successful scheduled
   backup run** confirms the new store is being backed up correctly — only
   then delete `<store>.pre-rebuild-<timestamp>`. Deleting it immediately after
   swap removes your only same-machine fallback before the new store has had a
   full cadence cycle to prove itself. **Also clean up the encrypted snapshot
   artifact(s)** from steps 2/3 (`<store>.snapshot-<epoch>.enc`, plus each
   one's own `<that-snapshot>.enc.key` escrow-key sidecar — one key file per
   snapshot, never a shared name, so removing an older snapshot never
   touches a newer one's key) — the tool never
   deletes these itself, they are 3.48 GB each on the live store, and a
   report-only run followed by a real `--yes` run leaves **two** of them on
   disk. Confirm you no longer need a given snapshot (i.e. you're past the
   step it protected) before removing it.

### Emergency recovery: `--rollback --yes`

If something is wrong post-swap and you need to go back **now**, on the same
machine, prefer this over restoring the encrypted snapshot by hand — it is
faster and, critically, it is **sidecar-aware** in a way a bare `mv` is not:

```bash
uv run python bin/optimise_store.py --src <store> --rollback --yes
```

This moves the *current* store aside (with its own `-wal`/`-shm`, so no
foreign WAL is left behind to be replayed into the restored file), restores
the newest `<store>.pre-rebuild-*` (or the one named by `--from`) together
with **its own** sidecars, checkpoints it, and re-verifies integrity before
declaring success. The tool's own docstring explains why the sidecar handling
matters: a bare `mv <kept> <store>` used to be the documented rollback and it
**silently corrupted the restored store** — SQLite replays whatever `-wal`
happens to be sitting next to the file on next open, no error, `integrity_check`
still reports `ok`, and the restored content and page size both come from the
wrong store.

**`--from` is validated in code, not left to operator care.** `_refuse_as_store`
rejects a candidate before anything is promoted if it (a) has a `-wal`/`-shm`
suffix, (b) opens as fewer than 16 pages (a fresh store is already 114, the live
one ~640k — so this catches a 0-byte or truncated file), or (c) has no `chunks`
table. This matters because `PRAGMA integrity_check` **cannot** catch any of
them: a 0-byte sidecar opens as a perfectly valid, structurally sound, *empty*
SQLite database, so every downstream gate passes and the tool would report
`restored store: integrity_check=['ok']` having installed an empty brain over a
real one. The retained generations (`.pre-rebuild-*`, `.rolled-back-*`, and both
of their sidecars) all share the store name as a prefix, so that was one
tab-completion away on the highest-stakes command here. Still prefer omitting
`--from` and letting `_find_retained` pick the newest retained main file.

### Cross-machine restore: SQLite version is a one-way door

**A rebuilt (or freshly `init()`'d) store requires the SAME OR NEWER SQLite
version on any machine it is later opened on for writes.** Restoring one onto a
machine with an OLDER SQLite will fail. The binding constraint is contentless
FTS5: `init()` creates `fts_chunks` with `contentless_delete=1` when SQLite is
≥ 3.43 (`store.fts5_supports_contentless`), and an older FTS5 module does not
recognise that option — it rejects the table rather than degrading. STRICT
tables (≥ 3.37) are a second, lower floor.
Practical consequences:

- A store rebuilt on a modern machine is **not** portable backwards. Note down
  the SQLite version it was built under (`uv run python -c "import sqlite3;
  print(sqlite3.sqlite_version)"`) alongside the backup.
- Restoring a backup onto a **new or reinstalled** machine: check that machine's
  SQLite version *before* restoring, not after. `uv tool install --python 3.12`
  (the pin § 1 already requires) is what keeps the fleet's interpreters —
  and therefore their bundled SQLite — consistent enough for this not to bite.
- The daemon does **not** currently detect this and refuse cleanly; an
  incompatible open surfaces as a raw `sqlite3.OperationalError`. Stamping a
  minimum-SQLite marker into the store's `meta` table with a startup check was
  considered and deliberately deferred — it touches daemon startup and
  `doctor.py`, so it is a separate change, not part of the rebuild work.
- Metadata storage is *not* a factor here: it is JSON TEXT read with
  `json_extract`, which every supported SQLite has.

## Environment note — repos live outside iCloud

All repos now live under `~/GitHub` (moved off the iCloud-synced `~/Documents`
tree). This removes the class of iCloud conflict-copy artifacts (`… 2.py` /
`… 2.md` / `… 2/`) and `.DS_Store` churn that previously polluted `git status` and
inflated the test count. If you ever see such stray files reappear, sweep the
source tree before committing:

```bash
find . -not -path './.git/*' \( -name '* 2' -o -name '* 2.*' -o -name '.DS_Store' \) -exec rm -rf {} +
```
